# agent/main.py — FastAPI entry point con routing multi-tenant
# Cleek — Fase 2

"""
Gestisce il routing dei webhook WhatsApp verso il tenant corretto.

Endpoint webhook:
  POST /webhook          — tenant default da TENANT_SLUG env (backward compat)
  POST /webhook/{slug}   — routing esplicito per slug tenant

Admin API:
  GET  /admin/appuntamenti/oggi           — tenant default
  GET  /admin/{slug}/appuntamenti/oggi    — tenant specifico
  GET  /admin/{slug}/appuntamenti?data=   — per data
  DELETE /admin/{slug}/appuntamenti/{id}  — cancella

Salute:
  GET /          — health check con info tenant
  GET /tenants   — lista tenant attivi (richiede ADMIN_KEY)
"""

import os
import pathlib
import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import (
    inicializar_db,
    guardar_mensaje,
    obtener_historial,
    get_appuntamenti_giorno,
    cancella_appuntamento,
    get_tenant_by_slug,
    get_tenant_by_whatsapp,
)
from agent.tenant_loader import carica_tenant, carica_tenant_default, TenantConfig
from agent.providers import obtener_proveedor
from agent.scheduler import crea_scheduler

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_tenant(slug: Optional[str] = None) -> tuple[TenantConfig, int]:
    """
    Risolve il tenant da usare per una richiesta.

    Precedenza:
    1. slug esplicito nell'URL → cerca nel DB
    2. TENANT_SLUG env var → carica da file YAML
    3. Fallback a 'barber-shop-ancona' (backward compat)

    Ritorna (TenantConfig, tenant_id_numerico).
    """
    if slug:
        # Cerca nel DB per ottenere l'ID numerico
        db_tenant = await get_tenant_by_slug(slug)
        if db_tenant is None:
            raise HTTPException(status_code=404, detail=f"Tenant '{slug}' non trovato o non attivo")
        try:
            config = carica_tenant(slug)
        except FileNotFoundError:
            raise HTTPException(status_code=503, detail=f"Config tenant '{slug}' non trovata")
        return config, db_tenant.id

    # Usa il tenant di default dall'env
    default_slug = os.getenv("TENANT_SLUG", "barber-shop-ancona")
    config = carica_tenant_default()
    db_tenant = await get_tenant_by_slug(default_slug)
    tenant_id = db_tenant.id if db_tenant else 1
    return config, tenant_id


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
            "tenant_id": a.tenant_id,
            "ora": a.data_ora.strftime("%H:%M"),
            "nome_cliente": a.nome_cliente,
            "servizio": a.servizio,
            "durata_minuti": a.durata_minuti,
            "telefono": a.telefono,
            "stato": a.stato,
        }
        for a in appuntamenti
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan — startup/shutdown
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    logger.info("Database inizializzato (con migrazione tenant_id)")

    # Carica tenant default per log di avvio
    try:
        config = carica_tenant_default()
        logger.info(f"Tenant attivo: {config.nome_business} ({config.slug})")
    except Exception as e:
        logger.warning(f"Config tenant non caricata: {e}")

    logger.info(f"Server Cleek in ascolto sulla porta {PORT}")
    logger.info(f"Provider WhatsApp: {proveedor.__class__.__name__}")

    scheduler = crea_scheduler()
    scheduler.start()
    logger.info("Scheduler promemoria avviato (ogni ora)")
    yield
    scheduler.shutdown()
    logger.info("Scheduler promemoria fermato")


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Cleek — WhatsApp Booking Agent",
    version="2.0.0",
    lifespan=lifespan,
)

DASHBOARD_DIR = pathlib.Path(__file__).parent.parent / "dashboard"
if DASHBOARD_DIR.exists():
    app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")


@app.get("/admin")
async def redirect_admin():
    return RedirectResponse(url="/dashboard")


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def health_check():
    """Health check con informazioni sul tenant di default."""
    try:
        config = carica_tenant_default()
        return {
            "status": "ok",
            "tenant": config.slug,
            "agente": config.nome_agente,
            "negocio": config.nome_business,
            "business_type": config.business_type,
            "proveedor": proveedor.__class__.__name__,
            "environment": ENVIRONMENT,
            "version": "2.0.0",
        }
    except Exception:
        return {
            "status": "ok",
            "proveedor": proveedor.__class__.__name__,
            "environment": ENVIRONMENT,
            "version": "2.0.0",
        }


@app.get("/tenants", dependencies=[Depends(verificar_admin)])
async def lista_tenants():
    """Lista tutti i tenant attivi. Richiede X-Admin-Key."""
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import select
    from agent.memory import Tenant, async_session

    async with async_session() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.attivo == True)  # noqa: E712
        )
        tenants = result.scalars().all()

    return {
        "totale": len(tenants),
        "tenants": [
            {
                "id": t.id,
                "slug": t.slug,
                "nome": t.nome,
                "business_type": t.business_type,
                "created_at": t.created_at.isoformat(),
            }
            for t in tenants
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Debug
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/debug/webhook")
async def debug_webhook(request: Request):
    """Endpoint di diagnostica — mostra il payload raw ricevuto."""
    try:
        body = await request.json()
        logger.info(f"DEBUG payload: {body}")
        return {"recibido": body}
    except Exception as e:
        raw = await request.body()
        return {"error": str(e), "raw": raw.decode("utf-8", errors="replace")}


# ─────────────────────────────────────────────────────────────────────────────
# Webhook WhatsApp — tenant default (backward compat)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verifica GET del webhook (richiesta da Meta Cloud API, no-op per altri)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Webhook per il tenant di default (TENANT_SLUG in .env).
    Mantiene la compatibilità backward con le installazioni esistenti.
    """
    return await _processa_webhook(request, tenant_slug=None)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook WhatsApp — routing esplicito per slug tenant
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/webhook/{tenant_slug}")
async def webhook_tenant_verificacion(tenant_slug: str, request: Request):
    """Verifica GET del webhook per un tenant specifico."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok", "tenant": tenant_slug}


@app.post("/webhook/{tenant_slug}")
async def webhook_tenant_handler(tenant_slug: str, request: Request):
    """
    Webhook con routing esplicito per tenant.
    Ogni business ha il suo endpoint: POST /webhook/barber-shop-ancona
    """
    return await _processa_webhook(request, tenant_slug=tenant_slug)


async def _processa_webhook(request: Request, tenant_slug: Optional[str]) -> dict:
    """
    Logica comune per tutti i webhook.
    Risolve il tenant, processa i messaggi, genera risposta con Claude.
    """
    try:
        body_bytes = await request.body()
        logger.debug(f"Webhook: {body_bytes.decode('utf-8', errors='replace')[:300]}")

        # Risolvi tenant — da slug URL o da env default
        tenant_config, tenant_id = await _resolve_tenant(tenant_slug)

        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"[{tenant_config.slug}] Msg da {msg.telefono}: {msg.texto}")

            # Storico conversazione scoped per tenant
            historial = await obtener_historial(msg.telefono, tenant_id=tenant_id)

            # Genera risposta con Claude — passando tenant_config e tenant_id
            respuesta = await generar_respuesta(
                msg.texto,
                historial,
                msg.telefono,
                tenant_config=tenant_config,
                tenant_id=tenant_id,
            )

            # Salva nella storia scoped per tenant
            await guardar_mensaje(msg.telefono, "user", msg.texto, tenant_id=tenant_id)
            await guardar_mensaje(msg.telefono, "assistant", respuesta, tenant_id=tenant_id)

            # Invia risposta via WhatsApp
            await proveedor.enviar_mensaje(msg.telefono, respuesta)
            logger.info(f"[{tenant_config.slug}] Risposta a {msg.telefono}: {respuesta[:80]}...")

        return {"status": "ok", "tenant": tenant_config.slug}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Errore webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Admin API — tenant default (backward compat)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/admin/appuntamenti/oggi", dependencies=[Depends(verificar_admin)])
async def admin_oggi():
    """Appuntamenti di oggi per il tenant default."""
    _, tenant_id = await _resolve_tenant(None)
    oggi = date.today()
    appuntamenti = await get_appuntamenti_giorno(oggi, tenant_id=tenant_id)
    return {
        "data": oggi.isoformat(),
        "tenant_id": tenant_id,
        "totale": len(appuntamenti),
        "appuntamenti": _serializza_appuntamenti(appuntamenti),
    }


@app.get("/admin/appuntamenti", dependencies=[Depends(verificar_admin)])
async def admin_appuntamenti(data: str):
    """Appuntamenti per data per il tenant default."""
    _, tenant_id = await _resolve_tenant(None)
    try:
        giorno = date.fromisoformat(data)
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato data non valido. Usa YYYY-MM-DD")
    appuntamenti = await get_appuntamenti_giorno(giorno, tenant_id=tenant_id)
    return {
        "data": data,
        "tenant_id": tenant_id,
        "totale": len(appuntamenti),
        "appuntamenti": _serializza_appuntamenti(appuntamenti),
    }


@app.delete("/admin/appuntamenti/{appuntamento_id}", dependencies=[Depends(verificar_admin)])
async def admin_cancella_appuntamento(appuntamento_id: int):
    """Cancella appuntamento per il tenant default."""
    _, tenant_id = await _resolve_tenant(None)
    successo = await cancella_appuntamento(appuntamento_id, tenant_id=tenant_id)
    if not successo:
        raise HTTPException(status_code=404, detail="Appuntamento non trovato")
    return {"successo": True, "id": appuntamento_id}


# ─────────────────────────────────────────────────────────────────────────────
# Admin API — scoped per tenant slug
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/admin/{tenant_slug}/appuntamenti/oggi", dependencies=[Depends(verificar_admin)])
async def admin_tenant_oggi(tenant_slug: str):
    """Appuntamenti di oggi per un tenant specifico."""
    _, tenant_id = await _resolve_tenant(tenant_slug)
    oggi = date.today()
    appuntamenti = await get_appuntamenti_giorno(oggi, tenant_id=tenant_id)
    return {
        "data": oggi.isoformat(),
        "tenant": tenant_slug,
        "tenant_id": tenant_id,
        "totale": len(appuntamenti),
        "appuntamenti": _serializza_appuntamenti(appuntamenti),
    }


@app.get("/admin/{tenant_slug}/appuntamenti", dependencies=[Depends(verificar_admin)])
async def admin_tenant_appuntamenti(tenant_slug: str, data: str):
    """Appuntamenti per data per un tenant specifico."""
    _, tenant_id = await _resolve_tenant(tenant_slug)
    try:
        giorno = date.fromisoformat(data)
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato data non valido. Usa YYYY-MM-DD")
    appuntamenti = await get_appuntamenti_giorno(giorno, tenant_id=tenant_id)
    return {
        "data": data,
        "tenant": tenant_slug,
        "tenant_id": tenant_id,
        "totale": len(appuntamenti),
        "appuntamenti": _serializza_appuntamenti(appuntamenti),
    }


@app.delete("/admin/{tenant_slug}/appuntamenti/{appuntamento_id}", dependencies=[Depends(verificar_admin)])
async def admin_tenant_cancella_appuntamento(tenant_slug: str, appuntamento_id: int):
    """Cancella appuntamento per un tenant specifico (cross-tenant safe)."""
    _, tenant_id = await _resolve_tenant(tenant_slug)
    successo = await cancella_appuntamento(appuntamento_id, tenant_id=tenant_id)
    if not successo:
        raise HTTPException(status_code=404, detail="Appuntamento non trovato per questo tenant")
    return {"successo": True, "id": appuntamento_id, "tenant": tenant_slug}
