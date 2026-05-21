import os
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


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


async def generar_respuesta(mensaje: str, historial: list[dict]) -> str:
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    mensajes = []
    for msg in historial:
        mensajes.append({"role": msg["role"], "content": msg["content"]})

    mensajes.append({"role": "user", "content": mensaje})

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes
        )
        respuesta = response.content[0].text
        logger.info(f"Tokens: {response.usage.input_tokens} in / {response.usage.output_tokens} out")
        return respuesta

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
