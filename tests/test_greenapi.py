"""
Test del parsing webhook di ProveedorGreenAPI.

Crea Request starlette sintetiche per simulare i payload inviati
da Green API senza avviare un server HTTP reale.
"""
import json
import pytest
from starlette.requests import Request

from agent.providers.greenapi import ProveedorGreenAPI, _TESTO_MEDIA_NON_SUPPORTATO


# ── Fixture e helper ──────────────────────────────────────────────────────────

@pytest.fixture
def provider():
    return ProveedorGreenAPI()


def _make_request(body: dict) -> Request:
    """Costruisce una Request starlette con body JSON sintetico."""
    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhook",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    return Request(scope, receive)


def _incoming(tipo_msg: str, message_data: dict, chat_id: str = "391234567890@c.us") -> Request:
    """Shortcut per costruire un webhook incomingMessageReceived."""
    return _make_request({
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "msg_test_001",
        "senderData": {"chatId": chat_id},
        "messageData": {"typeMessage": tipo_msg, **message_data},
    })


# ── textMessage ───────────────────────────────────────────────────────────────

async def test_text_message(provider):
    req = _incoming("textMessage", {"textMessageData": {"textMessage": "Ciao!"}})
    msgs = await provider.parsear_webhook(req)
    assert len(msgs) == 1
    assert msgs[0].texto == "Ciao!"
    assert msgs[0].telefono == "391234567890"
    assert msgs[0].es_propio is False


async def test_text_message_preserva_messaggio_id(provider):
    req = _make_request({
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "ABC123",
        "senderData": {"chatId": "391111111111@c.us"},
        "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": "Test"}},
    })
    msgs = await provider.parsear_webhook(req)
    assert msgs[0].mensaje_id == "ABC123"


# ── extendedTextMessage ───────────────────────────────────────────────────────

async def test_extended_text_message(provider):
    req = _incoming(
        "extendedTextMessage",
        {"extendedTextMessageData": {"text": "Testo esteso con link"}},
    )
    msgs = await provider.parsear_webhook(req)
    assert len(msgs) == 1
    assert msgs[0].texto == "Testo esteso con link"


# ── imageMessage (con e senza caption) ───────────────────────────────────────

async def test_image_con_caption(provider):
    req = _incoming(
        "imageMessage",
        {"imageMessageData": {"caption": "Guarda questa foto!"}},
    )
    msgs = await provider.parsear_webhook(req)
    assert len(msgs) == 1
    assert msgs[0].texto == "Guarda questa foto!"


async def test_image_senza_caption_restituisce_cortesia(provider):
    req = _incoming("imageMessage", {"imageMessageData": {"caption": ""}})
    msgs = await provider.parsear_webhook(req)
    assert len(msgs) == 1
    assert msgs[0].texto == _TESTO_MEDIA_NON_SUPPORTATO


async def test_video_con_caption(provider):
    req = _incoming(
        "videoMessage",
        {"videoMessageData": {"caption": "Guarda il video"}},
    )
    msgs = await provider.parsear_webhook(req)
    assert msgs[0].texto == "Guarda il video"


# ── Tipi non-testo → risposta di cortesia ────────────────────────────────────

async def test_audio_message_cortesia(provider):
    req = _incoming("audioMessage", {})
    msgs = await provider.parsear_webhook(req)
    assert len(msgs) == 1
    assert msgs[0].texto == _TESTO_MEDIA_NON_SUPPORTATO


async def test_sticker_message_cortesia(provider):
    req = _incoming("stickerMessage", {})
    msgs = await provider.parsear_webhook(req)
    assert len(msgs) == 1
    assert msgs[0].texto == _TESTO_MEDIA_NON_SUPPORTATO


async def test_reaction_message_cortesia(provider):
    req = _incoming("reactionMessage", {})
    msgs = await provider.parsear_webhook(req)
    assert len(msgs) == 1
    assert msgs[0].texto == _TESTO_MEDIA_NON_SUPPORTATO


# ── Filtri — messaggi da ignorare ────────────────────────────────────────────

async def test_messaggio_di_gruppo_ignorato(provider):
    """I messaggi di gruppo (@g.us) devono essere scartati."""
    req = _incoming(
        "textMessage",
        {"textMessageData": {"textMessage": "Msg di gruppo"}},
        chat_id="123456789@g.us",
    )
    msgs = await provider.parsear_webhook(req)
    assert msgs == []


async def test_webhook_non_incoming_ignorato(provider):
    """Solo 'incomingMessageReceived' deve essere processato."""
    req = _make_request({
        "typeWebhook": "outgoingMessageReceived",
        "idMessage": "msg_out",
        "senderData": {"chatId": "391234567890@c.us"},
        "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": "Ciao"}},
    })
    msgs = await provider.parsear_webhook(req)
    assert msgs == []


async def test_chat_id_mancante_ignorato(provider):
    """Un webhook senza chatId non deve generare messaggi."""
    req = _make_request({
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "msg_001",
        "senderData": {},  # nessun chatId
        "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": "Ciao"}},
    })
    msgs = await provider.parsear_webhook(req)
    assert msgs == []


async def test_testo_vuoto_ignorato(provider):
    """Un textMessage con testo vuoto non deve generare messaggi."""
    req = _incoming("textMessage", {"textMessageData": {"textMessage": "   "}})
    msgs = await provider.parsear_webhook(req)
    assert msgs == []


async def test_tipo_sconosciuto_con_testo_estraibile(provider):
    """Un tipo non standard con testo nei campi comuni deve essere estratto."""
    req = _incoming(
        "buttonsResponseMessage",
        {"buttonsResponseMessage": {"selectedButtonId": "opzione_1"}},
    )
    msgs = await provider.parsear_webhook(req)
    assert len(msgs) == 1
    assert msgs[0].texto == "opzione_1"


async def test_json_non_valido_restituisce_lista_vuota(provider):
    """Un body non JSON non deve sollevare eccezioni."""
    async def receive():
        return {"type": "http.request", "body": b"non-json!!!"}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhook",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    req = Request(scope, receive)
    msgs = await provider.parsear_webhook(req)
    assert msgs == []
