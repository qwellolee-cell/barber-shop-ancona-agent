# agent/scheduler.py — Scheduler promemoria WhatsApp multi-tenant
# Cleek — Fase 2

"""
Job APScheduler che invia promemoria WhatsApp ai clienti.

In Fase 2 itera su tutti i tenant attivi nel DB (non solo quello default),
usando la config YAML di ognuno per formattare il messaggio corretto.

Ogni tenant ha:
- La propria finestra reminder (24h barbiere, 2h ristorante, 48h dentista)
- Il proprio template messaggio (con nome business, emoji, tono)
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from agent.memory import (
    get_appuntamenti_promemoria,
    segna_reminder_inviato,
    async_session,
    Tenant,
)
from agent.providers import obtener_proveedor
from sqlalchemy import select

logger = logging.getLogger("agentkit")


async def _get_tenant_attivi() -> list[Tenant]:
    """Recupera la lista di tutti i tenant attivi dal DB."""
    async with async_session() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.attivo == True)  # noqa: E712
        )
        return list(result.scalars().all())


async def invia_promemoria():
    """
    Job orario: per ogni tenant attivo, invia promemoria WhatsApp ai clienti
    con appuntamento nella finestra configurata e senza reminder già inviato.
    """
    proveedor = obtener_proveedor()

    tenants = await _get_tenant_attivi()
    if not tenants:
        logger.warning("Nessun tenant attivo trovato per i promemoria")
        return

    for db_tenant in tenants:
        await _processa_promemoria_tenant(db_tenant, proveedor)


async def _processa_promemoria_tenant(db_tenant: Tenant, proveedor) -> None:
    """Processa i promemoria per un singolo tenant."""
    slug = db_tenant.slug
    tenant_id = db_tenant.id

    # Carica la config YAML del tenant per orari e template messaggio
    try:
        from agent.tenant_loader import carica_tenant
        tenant_config = carica_tenant(slug)
    except Exception as e:
        logger.error(f"[{slug}] Config non caricata, skip promemoria: {e}")
        return

    # Usa la finestra più grande tra quelle configurate
    finestra_ore = max(tenant_config.reminder_finestre_ore) if tenant_config.reminder_finestre_ore else 24

    appuntamenti = await get_appuntamenti_promemoria(
        tenant_id=tenant_id,
        finestra_ore=finestra_ore,
    )

    if not appuntamenti:
        return

    logger.info(f"[{slug}] Promemoria: {len(appuntamenti)} appuntamenti da notificare")

    for apt in appuntamenti:
        ora = apt.data_ora.strftime("%H:%M")
        data = apt.data_ora.strftime("%d/%m/%Y")

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
            logger.info(f"[{slug}] Promemoria inviato → {apt.telefono} (#{apt.id})")
        else:
            logger.error(f"[{slug}] Errore invio promemoria → {apt.telefono} (#{apt.id})")


def crea_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(invia_promemoria, "interval", hours=1, id="promemoria_multi_tenant")
    return scheduler
