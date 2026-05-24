# agent/brain.py — Cervello del agente: Claude API + tool use multi-tenant
# Cleek — Fase 3

"""
Integrazione con l'API di Anthropic Claude.
generar_respuesta() riceve tenant_config e tenant_id per:
  - usare il system prompt specifico del tenant
  - passare tenant_id a tutte le operazioni sul DB
  - iniettare il contesto data/ora corretto
"""

import os
import json
import yaml
import logging
from datetime import datetime, date
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions — Claude sceglie quale chiamare
# Fase 3: aggiunto num_persone per ristoranti e business con group booking
# ─────────────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "controlla_disponibilita",
        "description": (
            "Controlla gli slot liberi per una data specifica. "
            "Usalo prima di confermare una prenotazione. "
            "Per ristoranti, specifica anche num_persone per trovare tavoli con capienza sufficiente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "string",
                    "description": "Data in formato YYYY-MM-DD (es. 2026-05-22)",
                },
                "durata_minuti": {
                    "type": "integer",
                    "description": "Durata del servizio in minuti",
                },
                "num_persone": {
                    "type": "integer",
                    "description": (
                        "Numero di persone per la prenotazione (default: 1). "
                        "Per ristoranti indica i coperti richiesti."
                    ),
                },
            },
            "required": ["data", "durata_minuti"],
        },
    },
    {
        "name": "prenota_appuntamento",
        "description": (
            "Prenota un appuntamento per il cliente. "
            "Chiama sempre controlla_disponibilita prima per verificare che lo slot sia libero. "
            "Assicurati di avere nome_cliente, servizio, data e ora prima di chiamare questo tool. "
            "Per ristoranti includi num_persone (coperti richiesti)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_cliente": {
                    "type": "string",
                    "description": "Nome e cognome del cliente",
                },
                "servizio": {
                    "type": "string",
                    "description": "Nome del servizio o descrizione della prenotazione",
                },
                "data": {
                    "type": "string",
                    "description": "Data in formato YYYY-MM-DD",
                },
                "ora": {
                    "type": "string",
                    "description": "Orario in formato HH:MM (es. 15:30)",
                },
                "durata_minuti": {
                    "type": "integer",
                    "description": "Durata in minuti",
                },
                "num_persone": {
                    "type": "integer",
                    "description": (
                        "Numero di persone (default: 1). "
                        "Per ristoranti indica i coperti da riservare."
                    ),
                },
            },
            "required": ["nome_cliente", "servizio", "data", "ora", "durata_minuti"],
        },
    },
    {
        "name": "visualizza_appuntamento",
        "description": "Mostra il prossimo appuntamento confermato del cliente che sta scrivendo.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cancella_appuntamento",
        "description": (
            "Cancella un appuntamento esistente. "
            "Usa visualizza_appuntamento per ottenere l'appuntamento_id prima di cancellare."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "appuntamento_id": {
                    "type": "integer",
                    "description": "ID dell'appuntamento da cancellare",
                },
            },
            "required": ["appuntamento_id"],
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Esecuzione tool — scoped per tenant
# ─────────────────────────────────────────────────────────────────────────────

async def _esegui_tool(
    nome: str,
    params: dict,
    telefono: str,
    tenant_id: int = 1,
    tenant_config=None,
) -> dict:
    """
    Esegue il tool richiesto da Claude e ritorna il risultato come dict.
    Tutte le operazioni sul DB sono filtrate per tenant_id.
    """
    import agent.memory as memory

    logger.info(f"[tenant={tenant_id}] Tool chiamato: {nome} — params: {params}")

    if nome == "controlla_disponibilita":
        try:
            data = date.fromisoformat(params["data"])
        except ValueError:
            return {"errore": f"Formato data non valido: {params['data']}. Usa YYYY-MM-DD."}
        num_persone = int(params.get("num_persone", 1))
        slots = await memory.slot_disponibili(
            data,
            params["durata_minuti"],
            tenant_config=tenant_config,
            tenant_id=tenant_id,
            num_persone=num_persone,
        )
        return {"slots_disponibili": slots, "totale": len(slots)}

    if nome == "prenota_appuntamento":
        try:
            data_ora = datetime.strptime(f"{params['data']} {params['ora']}", "%Y-%m-%d %H:%M")
        except ValueError:
            return {"successo": False, "messaggio": "Formato data o ora non valido.", "appuntamento_id": None}
        num_persone = int(params.get("num_persone", 1))
        apt = await memory.prenota_appuntamento(
            telefono=telefono,
            nome=params["nome_cliente"],
            servizio=params["servizio"],
            data_ora=data_ora,
            durata_minuti=params["durata_minuti"],
            tenant_id=tenant_id,
            num_persone=num_persone,
        )
        if apt is None:
            return {
                "successo": False,
                "messaggio": "Slot occupato. Scegli un altro orario.",
                "appuntamento_id": None,
            }
        return {
            "successo": True,
            "messaggio": f"Prenotazione creata per {params['data']} alle {params['ora']}.",
            "appuntamento_id": apt.id,
        }

    if nome == "visualizza_appuntamento":
        apt = await memory.get_appuntamento_cliente(telefono, tenant_id=tenant_id)
        if apt is None:
            return {"trovato": False}
        return {
            "trovato": True,
            "appuntamento_id": apt.id,
            "nome_cliente": apt.nome_cliente,
            "servizio": apt.servizio,
            "data_ora": apt.data_ora.strftime("%Y-%m-%d %H:%M"),
            "durata_minuti": apt.durata_minuti,
            "stato": apt.stato,
        }

    if nome == "cancella_appuntamento":
        successo = await memory.cancella_appuntamento(
            params["appuntamento_id"],
            tenant_id=tenant_id,
        )
        return {"successo": successo}

    return {"errore": f"Tool sconosciuto: {nome}"}


# ─────────────────────────────────────────────────────────────────────────────
# Caricamento prompt — da config del tenant o da prompts.yaml legacy
# ─────────────────────────────────────────────────────────────────────────────

def _carica_prompt_tenant(tenant_config=None) -> str:
    """
    Carica il system prompt del tenant.
    Priorità: tenants/{slug}/prompts.yaml → config/prompts.yaml → fallback.
    """
    # 1. Prova il file prompts.yaml specifico del tenant
    if tenant_config is not None:
        tenant_prompts = f"tenants/{tenant_config.slug}/prompts.yaml"
        try:
            with open(tenant_prompts, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if "system_prompt" in data:
                    return data["system_prompt"]
        except FileNotFoundError:
            pass

    # 2. Fallback a config/prompts.yaml (file originale del progetto)
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data.get("system_prompt", "Sei un assistente utile. Rispondi in italiano.")
    except FileNotFoundError:
        pass

    return "Sei un assistente utile. Rispondi in italiano."


def _carica_messaggio_errore(tenant_config=None) -> str:
    """Carica il messaggio di errore dal prompt del tenant."""
    if tenant_config is not None:
        tenant_prompts = f"tenants/{tenant_config.slug}/prompts.yaml"
        try:
            with open(tenant_prompts, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if "error_message" in data:
                    return data["error_message"]
        except FileNotFoundError:
            pass
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data.get(
                "error_message",
                "Mi dispiace, sto avendo un problema tecnico. Riprova tra qualche minuto!"
            )
    except FileNotFoundError:
        return "Mi dispiace, sto avendo un problema tecnico. Riprova tra qualche minuto!"


def _carica_messaggio_fallback(tenant_config=None) -> str:
    """Carica il messaggio di fallback dal prompt del tenant."""
    if tenant_config is not None:
        tenant_prompts = f"tenants/{tenant_config.slug}/prompts.yaml"
        try:
            with open(tenant_prompts, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if "fallback_message" in data:
                    return data["fallback_message"]
        except FileNotFoundError:
            pass
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data.get(
                "fallback_message",
                "Scusa, non ho capito bene. Puoi riscrivere? Sono qui per aiutarti 😊"
            )
    except FileNotFoundError:
        return "Scusa, non ho capito bene. Puoi riscrivere?"


# ─────────────────────────────────────────────────────────────────────────────
# Funzione principale — genera risposta con Claude
# ─────────────────────────────────────────────────────────────────────────────

async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    telefono: str,
    tenant_config=None,
    tenant_id: int = 1,
) -> str:
    """
    Genera una risposta usando Claude con tool_use per le operazioni sul calendario.
    Continua il loop finché Claude non chiude con end_turn.

    Args:
        mensaje: testo del messaggio dell'utente
        historial: storia della conversazione
        telefono: numero del cliente (per lookup appuntamenti)
        tenant_config: TenantConfig del tenant corrente
        tenant_id: id numerico del tenant per le query DB
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return _carica_messaggio_fallback(tenant_config)

    system_prompt = _carica_prompt_tenant(tenant_config)

    # Inietta data e ora correnti per risolvere "domani", "dopodomani", ecc.
    now = datetime.now()
    GIORNI_IT = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    MESI_IT = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
               "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    data_contesto = (
        f"Data e ora attuali: {GIORNI_IT[now.weekday()]} "
        f"{now.day} {MESI_IT[now.month - 1]} {now.year}, ore {now.strftime('%H:%M')}."
    )
    system_prompt = data_contesto + "\n\n" + system_prompt

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    try:
        for _ in range(10):  # max 10 iterazioni tool_use
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                tools=TOOLS,
                messages=mensajes,
            )
            logger.info(
                f"[tenant={tenant_id}] Tokens: "
                f"{response.usage.input_tokens} in / {response.usage.output_tokens} out "
                f"— stop: {response.stop_reason}"
            )

            if response.stop_reason == "end_turn":
                testo = next(
                    (block.text for block in response.content if hasattr(block, "text")),
                    _carica_messaggio_fallback(tenant_config),
                )
                return testo

            if response.stop_reason == "tool_use":
                mensajes.append({"role": "assistant", "content": response.content})

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        risultato = await _esegui_tool(
                            block.name,
                            block.input,
                            telefono,
                            tenant_id=tenant_id,
                            tenant_config=tenant_config,
                        )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(risultato, ensure_ascii=False, default=str),
                        })
                        logger.info(f"[tenant={tenant_id}] Tool {block.name} → {risultato}")

                mensajes.append({"role": "user", "content": tool_results})
                continue

            logger.warning(f"Stop reason inatteso: {response.stop_reason}")
            testo = next(
                (block.text for block in response.content if hasattr(block, "text")),
                None,
            )
            return testo or _carica_messaggio_errore(tenant_config)

        logger.error(f"[tenant={tenant_id}] Raggiunto limite iterazioni tool_use")
        return _carica_messaggio_errore(tenant_config)

    except Exception as e:
        logger.error(f"[tenant={tenant_id}] Errore Claude API: {e}")
        return _carica_messaggio_errore(tenant_config)
