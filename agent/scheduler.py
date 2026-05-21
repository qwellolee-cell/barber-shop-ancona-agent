import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from agent.memory import get_appuntamenti_promemoria, segna_reminder_inviato
from agent.providers import obtener_proveedor

logger = logging.getLogger("agentkit")


async def invia_promemoria():
    """
    Job orario: invia un messaggio WhatsApp ai clienti con appuntamento
    nelle prossime 24h che non hanno ancora ricevuto il promemoria.
    """
    appuntamenti = await get_appuntamenti_promemoria()
    if not appuntamenti:
        return

    logger.info(f"Promemoria: {len(appuntamenti)} appuntamenti da notificare")
    proveedor = obtener_proveedor()

    for apt in appuntamenti:
        ora = apt.data_ora.strftime("%H:%M")
        messaggio = (
            f"Ciao {apt.nome_cliente}! 👋\n"
            f"Ti ricordiamo il tuo appuntamento di domani alle *{ora}* "
            f"per *{apt.servizio}* da Barber Shop Ancona.\n\n"
            f"Scrivi *ANNULLA* entro stasera se non puoi venire.\n"
            f"A presto! ✂️"
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
