"""
Test degli endpoint HTTP FastAPI.

Usa httpx.AsyncClient con ASGITransport per chiamate in-process
(senza avviare uvicorn, senza trigger del lifespan).
Le tabelle DB sono già create dal fixture reset_db (autouse in conftest.py).
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

# Importa l'app dopo che conftest.py ha già settato le env var
from agent.main import app

ADMIN_KEY = "test-admin-key-123"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest_asyncio.fixture
async def client():
    """Client HTTP asincrono che chiama l'app FastAPI in-process."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── Health check ──────────────────────────────────────────────────────────────

async def test_health_check(client):
    r = await client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "proveedor" in data


# ── Debug webhook ─────────────────────────────────────────────────────────────

async def test_debug_webhook_echo(client):
    """POST /debug/webhook deve fare eco del payload ricevuto."""
    payload = {"test": "valore", "numero": 42}
    r = await client.post("/debug/webhook", json=payload)
    assert r.status_code == 200
    assert r.json()["recibido"] == payload


# ── Admin — autenticazione ────────────────────────────────────────────────────

async def test_admin_oggi_senza_chiave(client):
    r = await client.get("/admin/appuntamenti/oggi")
    assert r.status_code == 401


async def test_admin_oggi_chiave_errata(client):
    r = await client.get(
        "/admin/appuntamenti/oggi",
        headers={"X-Admin-Key": "chiave-sbagliata"},
    )
    assert r.status_code == 401


async def test_admin_oggi_chiave_corretta(client):
    r = await client.get("/admin/appuntamenti/oggi", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert "totale" in data
    assert "appuntamenti" in data
    assert isinstance(data["appuntamenti"], list)


async def test_admin_appuntamenti_per_data(client):
    r = await client.get(
        "/admin/appuntamenti",
        params={"data": "2026-06-10"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["data"] == "2026-06-10"


async def test_admin_appuntamenti_data_non_valida(client):
    r = await client.get(
        "/admin/appuntamenti",
        params={"data": "non-una-data"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 400


async def test_admin_cancella_non_trovato(client):
    r = await client.delete("/admin/appuntamenti/99999", headers=ADMIN_HEADERS)
    assert r.status_code == 404


async def test_admin_tenants_senza_chiave(client):
    r = await client.get("/tenants")
    assert r.status_code == 401


async def test_admin_tenants_con_chiave(client):
    r = await client.get("/tenants", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert "totale" in data
    assert "tenants" in data


# ── Webhook WhatsApp ──────────────────────────────────────────────────────────

async def test_webhook_get_ok(client):
    """GET /webhook deve rispondere 200 (verifica Meta, no-op per Whapi)."""
    r = await client.get("/webhook")
    assert r.status_code == 200


async def test_webhook_post_payload_vuoto(client):
    """Un payload senza messaggi deve rispondere 200 con processed=0."""
    r = await client.post("/webhook", json={})
    assert r.status_code == 200
    assert r.json()["processed"] == 0


async def test_webhook_post_messaggio_greenapi(client):
    """Un messaggio GreenAPI valido deve essere processato (generar_respuesta mockato)."""
    payload = {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "msg_001",
        "senderData": {"chatId": "391234567890@c.us"},
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": "Ciao, vorrei prenotare"},
        },
    }
    with (
        patch("agent.main.generar_respuesta", new=AsyncMock(return_value="Certo! Quando vuoi?")),
        patch("agent.main.proveedor.enviar_mensaje", new=AsyncMock(return_value=True)),
    ):
        r = await client.post("/webhook", json=payload)

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["processed"] == 1


async def test_webhook_post_webhook_non_incoming_ignorato(client):
    """I webhook di tipo outgoing devono essere ignorati (processed=0)."""
    payload = {
        "typeWebhook": "outgoingMessageReceived",
        "idMessage": "msg_002",
        "senderData": {"chatId": "391234567890@c.us"},
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": "Risposta del bot"},
        },
    }
    r = await client.post("/webhook", json=payload)
    assert r.status_code == 200
    assert r.json()["processed"] == 0


# ── Onboarding tenant ─────────────────────────────────────────────────────────

async def test_admin_tenants_post_slug_non_valido(client):
    """Uno slug con caratteri non validi deve restituire 422 (Pydantic) o 400."""
    payload = {
        "slug": "Slug Con Maiuscole!",
        "nome": "Test",
        "business_type": "barbiere",
        "nome_agente": "Bot",
        "tono": "amichevole",
        "emoji_firma": "✂️",
    }
    r = await client.post("/admin/tenants", json=payload, headers=ADMIN_HEADERS)
    assert r.status_code in (400, 422)
