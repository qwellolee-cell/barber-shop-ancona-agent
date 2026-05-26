import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")

# Tipi di messaggio GreenAPI che contengono testo diretto
_TIPI_TESTO = ("textMessage", "extendedTextMessage")

# Tipi media che possono avere una caption testuale
_TIPI_MEDIA_CON_CAPTION = ("imageMessage", "videoMessage", "documentMessage")

# Tipi che non hanno testo ma devono ricevere una risposta di cortesia
_TIPI_NON_TESTO = (
    "audioMessage", "voiceMessage", "stickerMessage",
    "locationMessage", "contactMessage", "reactionMessage",
    "pollMessage", "pollUpdateMessage",
)

# Messaggio interno che il bot riceve quando il tipo non è testo
_TESTO_MEDIA_NON_SUPPORTATO = (
    "[Il cliente ha inviato un messaggio non testuale (audio/immagine/video/sticker). "
    "Rispondi gentilmente chiedendo di scrivere la richiesta in testo.]"
)


class ProveedorGreenAPI(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Green API."""

    def __init__(self):
        self.instance_id = os.getenv("GREEN_API_INSTANCE_ID")
        self.token = os.getenv("GREEN_API_TOKEN")
        self.base_url = f"https://api.green-api.com/waInstance{self.instance_id}"

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """
        Parsea el payload de Green API.

        Gestisce:
        - textMessage / extendedTextMessage → testo diretto
        - imageMessage / videoMessage / documentMessage con caption → usa la caption
        - audio, sticker, reazione, location, ecc. → risponde con messaggio di cortesia
        - Qualsiasi altro typeWebhook (outgoing, status, ecc.) → ignorato
        """
        try:
            body = await request.json()
        except Exception as e:
            logger.error(f"GreenAPI: impossibile parsare il body JSON: {e}")
            return []

        tipo_webhook = body.get("typeWebhook", "")

        # Logga tutti i webhook ricevuti (livello DEBUG) per facilitare il debug
        logger.debug(f"GreenAPI webhook ricevuto: typeWebhook={tipo_webhook}")

        # Ignora tutto tranne i messaggi in arrivo
        if tipo_webhook != "incomingMessageReceived":
            logger.debug(f"GreenAPI: webhook ignorato (typeWebhook={tipo_webhook!r})")
            return []

        sender_data = body.get("senderData", {})
        chat_id_raw = sender_data.get("chatId", "")

        # Ignora i messaggi di gruppo (@g.us) — il bot serve solo chat individuali
        if "@g.us" in chat_id_raw:
            logger.debug(f"GreenAPI: messaggio di gruppo ignorato (chatId={chat_id_raw!r})")
            return []

        telefono = chat_id_raw.replace("@c.us", "").strip()
        if not telefono:
            logger.warning(f"GreenAPI: chatId mancante nel webhook — {body}")
            return []

        mensaje_id = body.get("idMessage", "")
        message_data = body.get("messageData", {})
        tipo_msg = message_data.get("typeMessage", "")

        # ── Testo puro ──────────────────────────────────────────────
        if tipo_msg == "textMessage":
            texto = message_data.get("textMessageData", {}).get("textMessage", "").strip()

        elif tipo_msg == "extendedTextMessage":
            texto = message_data.get("extendedTextMessageData", {}).get("text", "").strip()

        # ── Media con caption ───────────────────────────────────────
        elif tipo_msg in _TIPI_MEDIA_CON_CAPTION:
            data_key = tipo_msg + "Data"       # es. "imageMessageData"
            caption = message_data.get(data_key, {}).get("caption", "").strip()
            if caption:
                texto = caption
            else:
                # Media senza caption → tratta come non-testo
                texto = _TESTO_MEDIA_NON_SUPPORTATO

        # ── Media non-testo (audio, sticker, ecc.) ──────────────────
        elif tipo_msg in _TIPI_NON_TESTO:
            texto = _TESTO_MEDIA_NON_SUPPORTATO
            logger.info(f"GreenAPI: tipo media non testuale ({tipo_msg}) da {telefono} — invio cortesia")

        # ── Tipo sconosciuto — prova estrazione testo generica ─────
        else:
            # Cerca testo nei campi più comuni per non perdere messaggi
            # di nuovi contatti che usano formati inaspettati
            texto_generico = (
                message_data.get("textMessageData", {}).get("textMessage", "")
                or message_data.get("extendedTextMessageData", {}).get("text", "")
                or message_data.get("buttonsResponseMessage", {}).get("selectedButtonId", "")
                or message_data.get("listResponseMessage", {}).get("singleSelectReply", {}).get("selectedRowId", "")
                or ""
            ).strip()

            if texto_generico:
                logger.info(
                    f"GreenAPI: tipo non standard ({tipo_msg!r}) da {telefono} "
                    f"— testo estratto: {texto_generico[:60]!r}"
                )
                texto = texto_generico
            else:
                # Nessun testo estraibile → invia risposta di cortesia
                # così almeno il numero nuovo riceve qualcosa
                texto = _TESTO_MEDIA_NON_SUPPORTATO
                logger.warning(
                    f"GreenAPI: typeMessage sconosciuto ({tipo_msg!r}) da {telefono} "
                    f"— invio risposta di cortesia. Payload: {str(body)[:300]}"
                )

        if not texto:
            logger.debug(f"GreenAPI: messaggio vuoto da {telefono}, ignorato")
            return []

        logger.info(f"GreenAPI: messaggio in arrivo da {telefono} | tipo={tipo_msg} | testo={texto[:80]!r}")

        return [MensajeEntrante(
            telefono=telefono,
            texto=texto,
            mensaje_id=mensaje_id,
            es_propio=False,
        )]

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Invia un messaggio via Green API. Ritorna True se OK."""
        if not self.instance_id or not self.token:
            logger.error(
                "GreenAPI: GREEN_API_INSTANCE_ID o GREEN_API_TOKEN non configurati — "
                "impossibile inviare messaggi. Controlla il file .env."
            )
            return False

        # Green API vuole il numero con @c.us
        chat_id = f"{telefono}@c.us" if "@" not in telefono else telefono

        url = f"{self.base_url}/sendMessage/{self.token}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    url,
                    json={"chatId": chat_id, "message": mensaje},
                )
            if r.status_code == 200:
                logger.debug(f"GreenAPI: messaggio inviato a {telefono} ✓")
                return True
            else:
                logger.error(
                    f"GreenAPI: errore invio a {telefono} — "
                    f"HTTP {r.status_code}: {r.text[:200]}"
                )
                return False
        except httpx.TimeoutException:
            logger.error(f"GreenAPI: timeout nell'invio a {telefono}")
            return False
        except Exception as e:
            logger.error(f"GreenAPI: eccezione nell'invio a {telefono}: {e}")
            return False

    async def verificar_y_configurar_webhook(self) -> None:
        """
        Verifica che l'istanza GreenAPI abbia i webhook in entrata attivi.
        Se 'incomingWebhook' è disattivato, lo attiva automaticamente.
        Chiamare all'avvio del server (nel lifespan).
        """
        if not self.instance_id or not self.token:
            logger.warning("GreenAPI: credenziali mancanti — impossibile verificare le impostazioni")
            return

        url_get = f"{self.base_url}/getSettings/{self.token}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url_get)
            if r.status_code != 200:
                logger.warning(f"GreenAPI: getSettings fallito ({r.status_code}) — verifica manuale necessaria")
                return

            settings = r.json()
            incoming = settings.get("incomingWebhook", "no")

            if incoming != "yes":
                logger.warning(
                    "GreenAPI: 'incomingWebhook' è disattivato! "
                    "I messaggi in arrivo NON generano webhook — il bot non può rispondere. "
                    "Attivazione automatica in corso..."
                )
                url_set = f"{self.base_url}/setSettings/{self.token}"
                async with httpx.AsyncClient(timeout=10) as client:
                    r2 = await client.post(url_set, json={"incomingWebhook": "yes"})
                if r2.status_code == 200:
                    logger.info("GreenAPI: 'incomingWebhook' attivato con successo ✓")
                else:
                    logger.error(
                        f"GreenAPI: impossibile attivare 'incomingWebhook' automaticamente "
                        f"({r2.status_code}). Vai su app.green-api.com → Istanza → Impostazioni "
                        f"e abilita 'Webhook messaggi in arrivo'."
                    )
            else:
                logger.info("GreenAPI: 'incomingWebhook' attivo ✓ — il bot riceverà tutti i messaggi")

        except Exception as e:
            logger.warning(f"GreenAPI: impossibile verificare le impostazioni: {e}")
