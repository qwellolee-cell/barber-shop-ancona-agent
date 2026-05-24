# agent/scheduler.py — Scheduler promemoria WhatsApp configurabile per tenant
# Cleek — Fase 1

"""
Job APScheduler che invia promemoria WhatsApp ai clienti con appuntamento
nella finestra di tempo configurata nel tenant.

In Fase 1 gestisce un singolo tenant (default).
In Fase 2 itererà su tutti i tenant attivi.
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from agent.memory import get_appuntamenti_promemoria, segna_reminder_inviato
from agent.providers import obtener_proveedor

logger = logging.getLogger("agentkit")


async def invia_promemoria():
    """
    Job orario: invia un messaggio WhatsApp ai clienti con appuntamento
    nella finestra configurata nel tenant e che non hanno ancora ricevuto
    il promemoria.

    Il messaggio viene formattato usando il template del tenant, così ogni
    business ha il proprio tono e firma senza hardcoding.
    """
    # Carica la config del tenant default (Fase 1: single-tenant)
    try:
        from agent.tenant_loader import carica_tenant_default
        tenant_config = carica_tenant_default()
    except Exception as e:
        logger.error(f"Impossibile caricare config tenant per promemoria: {e}")
        return

    # Usa la finestra più grande (es. dentista: max(48, 2) = 48)
    finestra_ore = max(tenant_config.reminder_finestre_ore) if tenant_config.reminder_finestre_ore else 24

    appuntamenti = await get_appuntamenti_promemoria(finestra_ore=finestra_ore)
    if not appuntamenti:
        return

    logger.info(f"Promemoria [{tenant_config.slug}]: {len(appuntamenti)} appuntamenti da notificare")
    proveedor = obtener_proveedor()

    for apt in appuntamenti:
        ora = apt.data_ora.strftime("%H:%M")
        data = apt.data_ora.strftime("%d/%m/%Y")

        # Formatta il messaggio con il template del tenant
        messaggio = tenant_config.formatta_messaggio_reminder(
            nome_cliente=apt.nome_cliente,
            ora=ora,
            data=data,
            servizio=apt.servizio,
            num_persone=getattr(apt, "num_persone", 1),
        )

        inviato = await proveedor.enviar_mensaje(apt.telefono, messaggio)
        if inviato:
            await segna_reminder_inviato(apt.id)
            logger.info(f"Promemoria inviato → {apt.telefono} (appuntamento #{apt.id})")
        else:
            logger.error(f"Errore invio promemoria → {apt.telefono} (appuntamento #{apt.id})")


def crea_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(invia_promemoria, "interval", hours=1, id="promemoria")
    return scheduler
