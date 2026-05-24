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

import pydantic
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
            "risorsa_id": a.risorsa_id,
            "ora": a.data_ora.strftime("%H:%M"),
            "nome_cliente": a.nome_cliente,
            "servizio": a.servizio,
            "durata_minuti": a.durata_minuti,
            "num_persone": getattr(a, "num_persone", 1),
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

# Buffer in-memory degli ultimi 20 eventi (per diagnosi senza accesso ai log)
_eventi_recenti: list[dict] = []
_MAX_EVENTI = 20


def _registra_evento(tipo: str, dati: dict):
    """Aggiunge un evento al buffer di diagnostica."""
    from datetime import datetime
    _eventi_recenti.append({
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "tipo": tipo,
        **dati,
    })
    if len(_eventi_recenti) > _MAX_EVENTI:
        _eventi_recenti.pop(0)


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


@app.get("/debug/status", dependencies=[Depends(verificar_admin)])
async def debug_status():
    """
    Mostra gli ultimi eventi di webhook ricevuti e lo stato GreenAPI.
    Utile per diagnosticare problemi senza accesso ai log del server.
    Richiede X-Admin-Key.
    """
    return {
        "eventi_recenti": list(reversed(_eventi_recenti)),
        "istruzioni": {
            "step1": "Fai inviare un messaggio WhatsApp al bot da un numero nuovo",
            "step2": "Attendi 10 secondi",
            "step3": "Ricarica questa pagina — dovresti vedere l'evento 'webhook_ricevuto'",
            "step4": "Se non vedi nulla → il webhook GreenAPI non arriva al server (controlla URL webhook in GreenAPI)",
            "step5": "Se vedi 'invio_fallito' → le credenziali GreenAPI non sono valide o il piano non lo permette",
        }
    }


@app.post("/debug/test-send", dependencies=[Depends(verificar_admin)])
async def debug_test_send(numero: str):
    """
    Testa l'invio di un messaggio WhatsApp a un numero specifico.
    Usa: POST /debug/test-send?numero=393331234567
    Richiede X-Admin-Key.
    """
    messaggio_test = "✅ Test Cleek: se ricevi questo messaggio, il bot funziona correttamente!"
    ok = await proveedor.enviar_mensaje(numero, messaggio_test)
    risultato = {
        "numero": numero,
        "inviato": ok,
        "provider": proveedor.__class__.__name__,
    }
    if not ok:
        risultato["errore"] = (
            "Invio fallito. Possibili cause:\n"
            "1. GREEN_API_INSTANCE_ID o GREEN_API_TOKEN non corretti\n"
            "2. L'istanza GreenAPI non è attiva (controlla il pannello GreenAPI)\n"
            "3. Il numero non ha WhatsApp\n"
            "4. Piano GreenAPI non supporta l'invio a nuovi contatti"
        )
    _registra_evento("test_invio", risultato)
    return risultato


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

    Restituisce sempre HTTP 200 così il provider non fa retry inutili.
    Gli errori sono loggati ma non ri-alzati come 500.
    """
    try:
        # Risolvi tenant — da slug URL o da env default
        tenant_config, tenant_id = await _resolve_tenant(tenant_slug)

        mensajes = await proveedor.parsear_webhook(request)

        if not mensajes:
            # Il provider ha scartato il webhook (tipo non gestito, messaggio vuoto, ecc.)
            _registra_evento("webhook_ignorato", {"tenant": tenant_config.slug, "motivo": "parsear_webhook ha restituito lista vuota"})
            return {"status": "ok", "tenant": tenant_config.slug, "processed": 0}

        processati = 0
        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"[{tenant_config.slug}] ← {msg.telefono}: {msg.texto[:100]!r}")
            _registra_evento("webhook_ricevuto", {
                "tenant": tenant_config.slug,
                "telefono": msg.telefono[-4:] + "****",  # oscura per privacy
                "testo_preview": msg.texto[:60],
            })

            # Storico conversazione scoped per tenant (vuoto per numeri nuovi — va bene)
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

            # Invia risposta via WhatsApp — logga esito
            inviato = await proveedor.enviar_mensaje(msg.telefono, respuesta)
            if inviato:
                logger.info(f"[{tenant_config.slug}] → {msg.telefono}: {respuesta[:80]!r}...")
                _registra_evento("risposta_inviata", {
                    "tenant": tenant_config.slug,
                    "telefono": msg.telefono[-4:] + "****",
                    "risposta_preview": respuesta[:80],
                })
            else:
                logger.error(
                    f"[{tenant_config.slug}] INVIO FALLITO a {msg.telefono}. "
                    f"Controlla le credenziali GREEN_API_INSTANCE_ID / GREEN_API_TOKEN nel .env."
                )
                _registra_evento("invio_fallito", {
                    "tenant": tenant_config.slug,
                    "telefono": msg.telefono[-4:] + "****",
                    "problema": "GreenAPI ha rifiutato l'invio — controlla le credenziali e il piano",
                })

            processati += 1

        return {"status": "ok", "tenant": tenant_config.slug, "processed": processati}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Errore webhook [{tenant_slug}]: {e}", exc_info=True)
        _registra_evento("errore_webhook", {"errore": str(e)[:200]})
        # Ritorna 200 comunque — così il provider non ritenta all'infinito
        return {"status": "error", "detail": str(e)}


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


# ─────────────────────────────────────────────────────────────────────────────
# Admin API — gestione risorse per tenant
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/admin/{tenant_slug}/risorse", dependencies=[Depends(verificar_admin)])
async def admin_tenant_risorse(tenant_slug: str):
    """Lista le risorse configurate per un tenant specifico."""
    from sqlalchemy import select as sa_select
    from agent.memory import async_session, Risorsa

    _, tenant_id = await _resolve_tenant(tenant_slug)
    async with async_session() as session:
        result = await session.execute(
            sa_select(Risorsa)
            .where(Risorsa.tenant_id == tenant_id)
            .order_by(Risorsa.ordine, Risorsa.id)
        )
        risorse = result.scalars().all()

    return {
        "tenant": tenant_slug,
        "tenant_id": tenant_id,
        "totale": len(risorse),
        "risorse": [
            {
                "id": r.id,
                "nome": r.nome,
                "tipo": r.tipo,
                "capienza": r.capienza,
                "attiva": r.attiva,
            }
            for r in risorse
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Admin API — creazione nuovo tenant (onboarding programmatico)
# ─────────────────────────────────────────────────────────────────────────────

class NuovoTenantRequest(pydantic.BaseModel):
    slug: str
    nome: str
    business_type: str = "barbiere"
    nome_agente: str = "Assistente"
    emoji_firma: str = "🤖"
    tono: str = "professionale e cordiale"
    whatsapp_numero: Optional[str] = None
    risorse: list[dict] = []  # [{"nome": "Sedia 1", "tipo": "sedia", "capienza": 1}]


@app.post("/admin/tenants", dependencies=[Depends(verificar_admin)])
async def admin_crea_tenant(body: NuovoTenantRequest):
    """
    Crea un nuovo tenant: registra nel DB, genera i file YAML di configurazione.
    Richiede X-Admin-Key.
    """
    import re

    # Valida slug: solo lettere minuscole, cifre e trattini
    if not re.match(r'^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$', body.slug):
        raise HTTPException(
            status_code=400,
            detail="Slug non valido. Usa solo lettere minuscole, cifre e trattini (es. mio-negozio)."
        )

    from agent.memory import (
        crea_o_aggiorna_tenant,
        seed_risorse_da_settings,
        get_tenant_by_slug,
        async_session,
        Risorsa,
    )
    from sqlalchemy import select as sa_select

    # Verifica che lo slug non esista già
    existing = await get_tenant_by_slug(body.slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"Tenant '{body.slug}' già esistente.")

    # Crea la directory del tenant
    tenant_dir = pathlib.Path(f"tenants/{body.slug}")
    tenant_dir.mkdir(parents=True, exist_ok=True)

    # Carica template business-type per i default
    bt_template = _carica_business_type_template(body.business_type)

    # Genera settings.yaml — legge correttamente dalla struttura nested del template
    scheduling = bt_template.get("scheduling", {})
    slot_gran = scheduling.get("slot_granularity_min", 15)

    reminder_section = bt_template.get("reminder", {})
    reminder_finestre = reminder_section.get("finestre_ore", [24])
    _reminder_raw = reminder_section.get(
        "messaggio_template",
        "Ciao {nome_cliente}! Ricorda il tuo appuntamento alle *{ora}* da {nome_business}. {emoji_firma}"
    ).strip()
    # Re-indent ogni riga di 4 spazi (YAML block scalar "|" richiede indentazione uniforme)
    reminder_msg = "\n    ".join(_reminder_raw.splitlines())

    orari_default = bt_template.get("orari_default", [])  # lista di {giorno, aperto, turni}

    # Genera sezione risorse nello YAML
    if body.risorse:
        risorse_yaml_lines = []
        for r in body.risorse:
            risorse_yaml_lines.append(
                f'  - nome: "{r.get("nome", "Risorsa 1")}"\n'
                f'    tipo: {r.get("tipo", "generico")}\n'
                f'    capienza: {r.get("capienza", 1)}\n'
                f'    attiva: true'
            )
        risorse_section = "risorse:\n" + "\n".join(risorse_yaml_lines)
    else:
        # Risorse di default dal template
        default_risorsa = bt_template.get("risorsa_default", {"nome": "Risorsa 1", "tipo": "generico", "capienza": 1})
        risorse_section = (
            f'risorse:\n'
            f'  - nome: "{default_risorsa.get("nome", "Risorsa 1")}"\n'
            f'    tipo: {default_risorsa.get("tipo", "generico")}\n'
            f'    capienza: {default_risorsa.get("capienza", 1)}\n'
            f'    attiva: true'
        )

    # Genera orari YAML (usa quelli del template oppure orari tipici)
    if orari_default:
        orari_yaml = _genera_orari_yaml_da_template(orari_default)
    else:
        orari_yaml = _genera_orari_yaml_default()

    settings_content = f"""# tenants/{body.slug}/settings.yaml
# Configurazione tenant — generata da Cleek Onboarding
# Business type: {body.business_type}

tenant:
  slug: {body.slug}
  nome: "{body.nome}"
  business_type: {body.business_type}
  lingua: it

agente:
  nome: "{body.nome_agente}"
  tono: "{body.tono}"
  emoji_firma: "{body.emoji_firma}"

{orari_yaml}

scheduling:
  slot_granularity_min: {slot_gran}

reminder:
  finestre_ore: {reminder_finestre}
  messaggio_template: |
    {reminder_msg}

{risorse_section}
"""
    (tenant_dir / "settings.yaml").write_text(settings_content, encoding="utf-8")

    # Genera prompts.yaml minimale
    prompts_content = f"""# tenants/{body.slug}/prompts.yaml
# System prompt — personalizza questo file per adattare il comportamento dell'agente

system_prompt: |
  Sei {body.nome_agente}, l'assistente virtuale di {body.nome}.
  Il tuo tono è {body.tono}.
  Aiuta i clienti a prenotare appuntamenti, rispondere a domande e gestire le loro prenotazioni.
  Rispondi sempre in italiano.
  Firma i messaggi con {body.emoji_firma}

fallback_message: "Scusa, non ho capito bene. Puoi riscrivere? Sono qui per aiutarti {body.emoji_firma}"
error_message: "Mi dispiace, sto avendo un problema tecnico. Riprova tra qualche minuto!"
"""
    (tenant_dir / "prompts.yaml").write_text(prompts_content, encoding="utf-8")

    # Crea il tenant nel DB
    db_tenant = await crea_o_aggiorna_tenant(
        slug=body.slug,
        nome=body.nome,
        business_type=body.business_type,
        whatsapp_numero=body.whatsapp_numero,
    )

    # Seed risorse nel DB
    await seed_risorse_da_settings(body.slug, tenant_id=db_tenant.id)

    # Invalida cache tenant_loader
    try:
        from agent.tenant_loader import carica_tenant
        carica_tenant.cache_clear()
    except Exception:
        pass

    logger.info(f"Nuovo tenant creato: {body.slug} (id={db_tenant.id})")
    return {
        "successo": True,
        "tenant": {
            "id": db_tenant.id,
            "slug": db_tenant.slug,
            "nome": db_tenant.nome,
            "business_type": db_tenant.business_type,
        },
        "files": [
            f"tenants/{body.slug}/settings.yaml",
            f"tenants/{body.slug}/prompts.yaml",
        ],
        "messaggio": f"Tenant '{body.slug}' creato con successo. Personalizza i file YAML generati.",
    }


def _carica_business_type_template(business_type: str) -> dict:
    """Carica il template business-type da business-types/{tipo}.yaml."""
    try:
        import yaml
        with open(f"business-types/{business_type}.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _genera_orari_yaml_da_template(orari_default: list) -> str:
    """
    Genera la sezione orari YAML dal campo orari_default del template business-type.

    Il template usa il formato:
      - giorno: 0        # ISO: 0=lunedì … 6=domenica
        aperto: false
      - giorno: 1
        aperto: true
        turni: ["09:00-13:00", "15:00-19:00"]   # stringhe "HH:MM-HH:MM"
    """
    GIORNI_NOMI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]

    # Indicizza i giorni della lista template per numero ISO
    template_per_giorno: dict = {}
    for entry in orari_default:
        g = entry.get("giorno")
        if g is not None:
            template_per_giorno[int(g)] = entry

    linee = ["orari:"]
    for idx, nome in enumerate(GIORNI_NOMI):
        entry = template_per_giorno.get(idx, {})
        aperto = entry.get("aperto", True)
        linee.append(f"  - giorno: {idx}      # {nome}")
        if not aperto:
            linee.append("    aperto: false")
        else:
            linee.append("    aperto: true")
            turni_raw = entry.get("turni", ["09:00-18:00"])
            if turni_raw:
                linee.append("    turni:")
                for t in turni_raw:
                    if isinstance(t, str) and "-" in t:
                        ap, ch = t.split("-", 1)
                    elif isinstance(t, dict):
                        ap = t.get("apertura", "09:00")
                        ch = t.get("chiusura", "18:00")
                    else:
                        ap, ch = "09:00", "18:00"
                    linee.append(f'      - apertura: "{ap.strip()}"')
                    linee.append(f'        chiusura: "{ch.strip()}"')
    return "\n".join(linee)


def _genera_orari_yaml_default() -> str:
    """Genera orari di default (lun-ven 9-18) quando il template non li specifica."""
    GIORNI_NOMI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    linee = ["orari:"]
    for idx, nome in enumerate(GIORNI_NOMI):
        chiuso = idx in (5, 6)  # sabato e domenica
        linee.append(f"  - giorno: {idx}      # {nome}")
        if chiuso:
            linee.append("    aperto: false")
        else:
            linee.append("    aperto: true")
            linee.append("    turni:")
            linee.append('      - apertura: "09:00"')
            linee.append('        chiusura: "18:00"')
    return "\n".join(linee)
