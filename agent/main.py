import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="AgentKit — Barber Shop Ancona",
    version="1.0.0",
    lifespan=lifespan
)


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
            respuesta = await generar_respuesta(msg.texto, historial)

            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            await proveedor.enviar_mensaje(msg.telefono, respuesta)
            logger.info(f"Respuesta a {msg.telefono}: {respuesta}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
