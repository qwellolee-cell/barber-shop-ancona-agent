import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorGreenAPI(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Green API."""

    def __init__(self):
        self.instance_id = os.getenv("GREEN_API_INSTANCE_ID")
        self.token = os.getenv("GREEN_API_TOKEN")
        self.base_url = f"https://api.green-api.com/waInstance{self.instance_id}"

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload de Green API."""
        body = await request.json()

        # Solo procesamos mensajes entrantes de texto
        if body.get("typeWebhook") != "incomingMessageReceived":
            return []

        message_data = body.get("messageData", {})
        tipo = message_data.get("typeMessage", "")

        if tipo == "textMessage":
            texto = message_data.get("textMessageData", {}).get("textMessage", "")
        elif tipo == "extendedTextMessage":
            texto = message_data.get("extendedTextMessageData", {}).get("text", "")
        else:
            return []

        sender_data = body.get("senderData", {})
        telefono = sender_data.get("chatId", "").replace("@c.us", "")
        mensaje_id = body.get("idMessage", "")

        if not texto or not telefono:
            return []

        return [MensajeEntrante(
            telefono=telefono,
            texto=texto,
            mensaje_id=mensaje_id,
            es_propio=False,
        )]

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía mensaje via Green API."""
        if not self.instance_id or not self.token:
            logger.warning("GREEN_API_INSTANCE_ID o GREEN_API_TOKEN no configurados")
            return False

        # Green API espera el número con @c.us
        chat_id = f"{telefono}@c.us" if "@" not in telefono else telefono

        url = f"{self.base_url}/sendMessage/{self.token}"
        async with httpx.AsyncClient() as client:
            r = await client.post(
                url,
                json={"chatId": chat_id, "message": mensaje},
            )
            if r.status_code != 200:
                logger.error(f"Error Green API: {r.status_code} — {r.text}")
            return r.status_code == 200
