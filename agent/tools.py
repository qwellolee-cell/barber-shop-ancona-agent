# agent/tools.py — Funzioni helper del negocio, configurabili per tenant
# Cleek — Fase 1

"""
Funzioni helper per operazioni del negocio che non richiedono il database.
In Fase 1 le funzioni leggono dalla config del tenant invece di avere
dati hardcoded.

Nota: obtener_servicios() è stata rimossa perché duplicava le informazioni
già presenti in config/prompts.yaml. Claude legge i servizi direttamente
dal system prompt — non serve una funzione separata.
La funzione è mantenuta qui come stub per backward compat con eventuali
import, ma delega a prompts.yaml.
"""

import os
import yaml
import logging

logger = logging.getLogger("agentkit")


def cargar_info_negocio() -> dict:
    """Carica le informazioni del negocio da config/business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("config/business.yaml non trovato, uso config tenant")
        # Fallback: leggi dal tenant config
        try:
            from agent.tenant_loader import carica_tenant_default
            config = carica_tenant_default()
            return {
                "negocio": {
                    "nombre": config.nome_business,
                    "horario": "Consulta orari sul sito",
                }
            }
        except Exception:
            return {}


def obtener_horario() -> dict:
    """
    Restituisce il riepilogo dell'orario di apertura.
    Legge dalla config del tenant (non da business.yaml hardcoded).
    """
    try:
        from agent.tenant_loader import carica_tenant_default
        config = carica_tenant_default()

        NOMI_IT = {0: "Lunedì", 1: "Martedì", 2: "Mercoledì",
                   3: "Giovedì", 4: "Venerdì", 5: "Sabato", 6: "Domenica"}

        righe = []
        for giorno_iso in range(7):
            nome = NOMI_IT[giorno_iso]
            if config.is_giorno_chiuso(giorno_iso):
                righe.append(f"{nome}: chiuso")
            else:
                turni = config.turni_apertura(giorno_iso)
                turni_str = " e ".join(
                    f"{t.apertura.strftime('%H:%M')}-{t.chiusura.strftime('%H:%M')}"
                    for t in turni
                )
                righe.append(f"{nome}: {turni_str}")

        return {
            "horario": "\n".join(righe),
            "esta_abierto": True,   # TODO: calcolare in base all'ora corrente
        }

    except Exception as e:
        logger.warning(f"Impossibile caricare orari dal tenant: {e}")
        info = cargar_info_negocio()
        return {
            "horario": info.get("negocio", {}).get("horario", "Non disponibile"),
            "esta_abierto": True,
        }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Cerca informazioni rilevanti nei file della cartella /knowledge.
    Ritorna il contenuto più rilevante trovato.
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "Nessun file di conoscenza disponibile."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "Non ho trovato informazioni specifiche su questo nei miei file."


def confirmar_cita(
    telefono: str,
    dia: str,
    hora: str,
    servicio: str,
    tenant_config=None,
) -> str:
    """
    Genera il messaggio di conferma appuntamento.
    Usa il nome del negocio dalla config del tenant.
    """
    if tenant_config is None:
        try:
            from agent.tenant_loader import carica_tenant_default
            tenant_config = carica_tenant_default()
            nome_business = tenant_config.nome_business
            emoji = tenant_config.emoji_firma
        except Exception:
            nome_business = "il negozio"
            emoji = ""
    else:
        nome_business = tenant_config.nome_business
        emoji = tenant_config.emoji_firma

    return (
        f"Perfetto! ✅ Ho fissato il tuo appuntamento:\n"
        f"📅 {dia} alle {hora}\n"
        f"💈 {servicio}\n"
        f"📍 {nome_business}\n"
        f"Ti aspettiamo! {emoji}"
    )


# ---------------------------------------------------------------------------
# STUB legacy — rimosso il contenuto hardcoded, ora Claude legge i servizi
# direttamente dal system prompt in config/prompts.yaml
# ---------------------------------------------------------------------------
def obtener_servicios() -> str:
    """
    DEPRECATA: i servizi sono nel system prompt (config/prompts.yaml).
    Claude li conosce già — non serve duplicarli qui.
    Questa funzione è mantenuta solo per compatibilità backward.
    """
    logger.warning(
        "obtener_servicios() è deprecata. "
        "I servizi sono nel system prompt di Claude (config/prompts.yaml)."
    )
    return "Per la lista completa dei servizi consulta il nostro menù."
