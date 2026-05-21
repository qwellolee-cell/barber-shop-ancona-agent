import os
import pathlib
import logging
from contextlib import asynccontextmanager
from datetime import date
from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, get_appuntamenti_giorno, cancella_appuntamento
from agent.providers import obtener_proveedor
from agent.scheduler import crea_scheduler

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


async def verificar_admin(x_admin_key: str = Header(default=None)):
    """Dependency FastAPI: valida X-Admin-Key contro ADMIN_KEY in .env."""
    admin_key = os.getenv("ADMIN_KEY")
    if not admin_key:
        raise HTTPException(status_code=503, detail="ADMIN_KEY non configurata sul server")
    if x_admin_key != admin_key:
        raise HTTPException(status_code=401, detail="API key non valida o mancante")


def _serializza_appuntamenti(appuntamenti) -> list[dict]:
    return [
        {
            "id": a.id,
            "ora": a.data_ora.strftime("%H:%M"),
            "nome_cliente": a.nome_cliente,
            "servizio": a.servizio,
            "durata_minuti": a.durata_minuti,
            "telefono": a.telefono,
            "stato": a.stato,
        }
        for a in appuntamenti
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
    scheduler = crea_scheduler()
    scheduler.start()
    logger.info("Scheduler promemoria avviato (ogni ora)")
    yield
    scheduler.shutdown()
    logger.info("Scheduler promemoria fermato")


app = FastAPI(
    title="AgentKit — Barber Shop Ancona",
    version="1.0.0",
    lifespan=lifespan
)

DASHBOARD_DIR = pathlib.Path(__file__).parent.parent / "dashboard"
if DASHBOARD_DIR.exists():
    app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")


@app.get("/admin")
async def redirect_admin():
    return RedirectResponse(url="/dashboard")


@app.get("/")
async def health_check():
    return {
        "status": "ok",
        "agente": "Simone",
        "negocio": "Barber Shop Ancona",
        "proveedor": proveedor.__class__.__name__,
        "environment": os.getenv("ENVIRONMENT", "development"),
    }


@app.post("/debug/webhook")
async def debug_webhook(request: Request):
    """Endpoint de diagnóstico — muestra el payload raw que llega."""
    try:
        body = await request.json()
        logger.info(f"DEBUG payload: {body}")
        return {"recibido": body}
    except Exception as e:
        raw = await request.body()
        return {"error": str(e), "raw": raw.decode("utf-8", errors="replace")}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.get("/admin/appuntamenti/oggi", dependencies=[Depends(verificar_admin)])
async def admin_oggi():
    """Appuntamenti di oggi — shortcut senza parametri."""
    oggi = date.today()
    appuntamenti = await get_appuntamenti_giorno(oggi)
    return {
        "data": oggi.isoformat(),
        "totale": len(appuntamenti),
        "appuntamenti": _serializza_appuntamenti(appuntamenti),
    }


@app.get("/admin/appuntamenti", dependencies=[Depends(verificar_admin)])
async def admin_appuntamenti(data: str):
    """
    Appuntamenti per una data specifica.
    Query param: data=YYYY-MM-DD
    Header:      X-Admin-Key: <valore di ADMIN_KEY in .env>
    """
    try:
        giorno = date.fromisoformat(data)
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato data non valido. Usa YYYY-MM-DD")
    appuntamenti = await get_appuntamenti_giorno(giorno)
    return {
        "data": data,
        "totale": len(appuntamenti),
        "appuntamenti": _serializza_appuntamenti(appuntamenti),
    }


@app.delete("/admin/appuntamenti/{appuntamento_id}", dependencies=[Depends(verificar_admin)])
async def admin_cancella_appuntamento(appuntamento_id: int):
    """
    Cancella un appuntamento (soft delete — stato='cancellato').
    Header: X-Admin-Key
    """
    successo = await cancella_appuntamento(appuntamento_id)
    if not successo:
        raise HTTPException(status_code=404, detail="Appuntamento non trovato")
    return {"successo": True, "id": appuntamento_id}


@app.post("/webhook")
async def webhook_handler(request: Request):
    try:
        # Log del payload raw para diagnóstico (el body queda cacheado para el proveedor)
        body_bytes = await request.body()
        logger.info(f"Webhook recibido: {body_bytes.decode('utf-8', errors='replace')[:500]}")

        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

            historial = await obtener_historial(msg.telefono)
            respuesta = await generar_respuesta(msg.texto, historial, msg.telefono)

            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            await proveedor.enviar_mensaje(msg.telefono, respuesta)
            logger.info(f"Respuesta a {msg.telefono}: {respuesta}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
