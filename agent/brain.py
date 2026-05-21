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

TOOLS = [
    {
        "name": "controlla_disponibilita",
        "description": (
            "Controlla gli slot liberi per una data specifica. "
            "Usalo prima di confermare una prenotazione per mostrare gli orari disponibili al cliente."
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
            },
            "required": ["data", "durata_minuti"],
        },
    },
    {
        "name": "prenota_appuntamento",
        "description": (
            "Prenota un appuntamento per il cliente. "
            "Chiama sempre controlla_disponibilita prima per verificare che lo slot sia libero. "
            "Assicurati di avere nome_cliente, servizio, data e ora prima di chiamare questo tool."
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
                    "description": "Nome del servizio (es. Sistemazione Barba, Taglio Uomo, Taglio + Barba Experience)",
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
                    "description": "Durata del servizio in minuti",
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


async def _esegui_tool(nome: str, params: dict, telefono: str) -> dict:
    """Esegue il tool richiesto da Claude e ritorna il risultato come dict."""
    import agent.memory as memory

    logger.info(f"Tool chiamato: {nome} — params: {params}")

    if nome == "controlla_disponibilita":
        try:
            data = date.fromisoformat(params["data"])
        except ValueError:
            return {"errore": f"Formato data non valido: {params['data']}. Usa YYYY-MM-DD."}
        slots = await memory.slot_disponibili(data, params["durata_minuti"])
        return {"slots_disponibili": slots, "totale": len(slots)}

    if nome == "prenota_appuntamento":
        try:
            data_ora = datetime.strptime(f"{params['data']} {params['ora']}", "%Y-%m-%d %H:%M")
        except ValueError:
            return {"successo": False, "messaggio": "Formato data o ora non valido.", "appuntamento_id": None}
        apt = await memory.prenota_appuntamento(
            telefono=telefono,
            nome=params["nome_cliente"],
            servizio=params["servizio"],
            data_ora=data_ora,
            durata_minuti=params["durata_minuti"],
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
        apt = await memory.get_appuntamento_cliente(telefono)
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
        successo = await memory.cancella_appuntamento(params["appuntamento_id"])
        return {"successo": successo}

    return {"errore": f"Tool sconosciuto: {nome}"}


def cargar_config_prompts() -> dict:
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    config = cargar_config_prompts()
    return config.get("system_prompt", "Sei un assistente utile. Rispondi in italiano.")


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get("error_message", "Mi dispiace, sto avendo un problema tecnico. Riprova tra qualche minuto!")


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get("fallback_message", "Scusa, non ho capito bene. Puoi riscrivere? Sono qui per aiutarti 😊")


async def generar_respuesta(mensaje: str, historial: list[dict], telefono: str) -> str:
    """
    Genera una risposta usando Claude con tool_use per le operazioni sul calendario.
    Continua il loop finché Claude non chiude con end_turn.
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    # Inietta data e ora correnti perché Claude possa risolvere
    # riferimenti relativi come "domani", "dopodomani", "venerdì".
    now = datetime.now()
    GIORNI_IT = ["lunedì","martedì","mercoledì","giovedì","venerdì","sabato","domenica"]
    MESI_IT   = ["gennaio","febbraio","marzo","aprile","maggio","giugno",
                 "luglio","agosto","settembre","ottobre","novembre","dicembre"]
    data_contesto = (
        f"Data e ora attuali: {GIORNI_IT[now.weekday()]} "
        f"{now.day} {MESI_IT[now.month - 1]} {now.year}, ore {now.strftime('%H:%M')}."
    )
    system_prompt = data_contesto + "\n\n" + system_prompt

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    try:
        for _ in range(10):  # max 10 iterazioni per evitare loop infiniti
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                tools=TOOLS,
                messages=mensajes,
            )
            logger.info(
                f"Tokens: {response.usage.input_tokens} in / {response.usage.output_tokens} out "
                f"— stop: {response.stop_reason}"
            )

            if response.stop_reason == "end_turn":
                testo = next(
                    (block.text for block in response.content if hasattr(block, "text")),
                    obtener_mensaje_fallback(),
                )
                return testo

            if response.stop_reason == "tool_use":
                # Aggiunge la risposta dell'assistente (con i tool_use block) alla conversazione
                mensajes.append({"role": "assistant", "content": response.content})

                # Esegue tutti i tool richiesti in questa risposta
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        risultato = await _esegui_tool(block.name, block.input, telefono)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(risultato, ensure_ascii=False, default=str),
                        })
                        logger.info(f"Tool {block.name} → {risultato}")

                mensajes.append({"role": "user", "content": tool_results})
                continue

            # stop_reason inatteso (es. max_tokens)
            logger.warning(f"Stop reason inatteso: {response.stop_reason}")
            testo = next(
                (block.text for block in response.content if hasattr(block, "text")),
                None,
            )
            return testo or obtener_mensaje_error()

        logger.error("Raggiunto il limite di iterazioni tool_use")
        return obtener_mensaje_error()

    except Exception as e:
        logger.error(f"Errore Claude API: {e}")
        return obtener_mensaje_error()
