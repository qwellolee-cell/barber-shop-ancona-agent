"""
Test per agent/brain.py.

Le chiamate alla Claude API vengono mockate per evitare costi e dipendenze di rete.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import agent.brain as brain
from agent.brain import (
    generar_respuesta,
    _carica_prompt_tenant,
    _carica_messaggio_errore,
    _carica_messaggio_fallback,
    _esegui_tool,
)


# ── Helper mock response ──────────────────────────────────────────────────────

def _claude_end_turn(text: str = "Risposta di test"):
    """Simula una risposta Claude con stop_reason='end_turn'."""
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.stop_reason = "end_turn"
    response.usage = MagicMock(input_tokens=50, output_tokens=20)
    response.content = [block]
    return response


def _claude_error():
    """Simula un'eccezione nell'API Claude."""
    raise Exception("Errore API simulato")


# ── Funzioni di caricamento prompt ────────────────────────────────────────────

def test_carica_prompt_tenant_nessun_tenant():
    """Senza tenant config deve restituire una stringa non vuota."""
    result = _carica_prompt_tenant(None)
    assert isinstance(result, str)
    assert len(result) > 0


def test_carica_prompt_tenant_con_config(tenant_config):
    """Con un tenant config deve restituire una stringa non vuota."""
    result = _carica_prompt_tenant(tenant_config)
    assert isinstance(result, str)
    assert len(result) > 0


def test_carica_messaggio_errore():
    msg = _carica_messaggio_errore(None)
    assert isinstance(msg, str)
    assert len(msg) > 5


def test_carica_messaggio_fallback():
    msg = _carica_messaggio_fallback(None)
    assert isinstance(msg, str)
    assert len(msg) > 5


# ── generar_respuesta — messaggi corti (nessuna chiamata Claude) ──────────────

async def test_messaggio_troppo_corto_ritorna_fallback():
    """Un singolo carattere non deve invocare Claude."""
    result = await generar_respuesta("a", [], "391234567890")
    assert isinstance(result, str)
    assert len(result) > 0


async def test_messaggio_vuoto_ritorna_fallback():
    result = await generar_respuesta("", [], "391234567890")
    assert isinstance(result, str)
    assert len(result) > 0


async def test_messaggio_solo_spazi_ritorna_fallback():
    result = await generar_respuesta("   ", [], "391234567890")
    assert isinstance(result, str)
    assert len(result) > 0


# ── generar_respuesta — con Claude mockato ────────────────────────────────────

async def test_generar_respuesta_end_turn():
    """Claude risponde con end_turn → il testo viene restituito."""
    with patch.object(
        brain.client.messages, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = _claude_end_turn("Benvenuto da Simone!")
        result = await generar_respuesta("Ciao", [], "391234567890")
    assert result == "Benvenuto da Simone!"
    mock_create.assert_called_once()


async def test_generar_respuesta_errore_api_ritorna_messaggio_errore():
    """Se Claude solleva un'eccezione, si ritorna il messaggio di errore configurato."""
    with patch.object(
        brain.client.messages, "create", new_callable=AsyncMock,
        side_effect=Exception("Timeout simulato"),
    ):
        result = await generar_respuesta("Ciao", [], "391234567890")
    assert isinstance(result, str)
    assert len(result) > 0


async def test_generar_respuesta_con_historial():
    """Lo storico viene passato correttamente a Claude."""
    historial = [
        {"role": "user", "content": "Prima domanda"},
        {"role": "assistant", "content": "Prima risposta"},
    ]
    with patch.object(
        brain.client.messages, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = _claude_end_turn("Seconda risposta")
        await generar_respuesta("Seconda domanda", historial, "391234567890")

    call_kwargs = mock_create.call_args.kwargs
    messages_sent = call_kwargs["messages"]
    # Deve contenere lo storico + il nuovo messaggio
    assert len(messages_sent) == 3
    assert messages_sent[0]["role"] == "user"
    assert messages_sent[0]["content"] == "Prima domanda"
    assert messages_sent[2]["content"] == "Seconda domanda"


async def test_generar_respuesta_inietta_data_nel_system_prompt():
    """Il system prompt deve contenere la data/ora corrente."""
    with patch.object(
        brain.client.messages, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = _claude_end_turn("OK")
        await generar_respuesta("Ciao", [], "391234567890")

    call_kwargs = mock_create.call_args.kwargs
    system = call_kwargs["system"]
    assert "2026" in system or "Data" in system or "data" in system


# ── _esegui_tool ──────────────────────────────────────────────────────────────

async def test_esegui_tool_sconosciuto():
    result = await _esegui_tool("tool_inesistente", {}, "391234567890")
    assert "errore" in result


async def test_esegui_tool_controlla_disponibilita(tenant_config):
    """controlla_disponibilita deve ritornare una lista di slot (può essere vuota)."""
    # 2026-06-10 è mercoledì (giorno aperto per barber-shop-ancona)
    result = await _esegui_tool(
        "controlla_disponibilita",
        {"data": "2026-06-10", "durata_minuti": 30},
        "391234567890",
        tenant_id=1,
        tenant_config=tenant_config,
    )
    assert "slots_disponibili" in result
    assert isinstance(result["slots_disponibili"], list)
    assert "totale" in result


async def test_esegui_tool_controlla_disponibilita_data_non_valida():
    result = await _esegui_tool(
        "controlla_disponibilita",
        {"data": "non-una-data", "durata_minuti": 30},
        "391234567890",
        tenant_id=1,
    )
    assert "errore" in result


async def test_esegui_tool_prenota_appuntamento(tenant_config):
    """prenota_appuntamento deve creare una prenotazione e restituire successo=True."""
    result = await _esegui_tool(
        "prenota_appuntamento",
        {
            "nome_cliente": "Mario Rossi",
            "servizio": "Taglio",
            "data": "2026-06-10",
            "ora": "10:00",
            "durata_minuti": 30,
        },
        "391234567890",
        tenant_id=1,
        tenant_config=tenant_config,
    )
    assert result["successo"] is True
    assert result["appuntamento_id"] is not None


async def test_esegui_tool_visualizza_appuntamento_non_trovato(tenant_config):
    result = await _esegui_tool(
        "visualizza_appuntamento",
        {},
        "391234567890",
        tenant_id=1,
        tenant_config=tenant_config,
    )
    assert result["trovato"] is False


async def test_esegui_tool_cancella_appuntamento_non_trovato(tenant_config):
    result = await _esegui_tool(
        "cancella_appuntamento",
        {"appuntamento_id": 99999},
        "391234567890",
        tenant_id=1,
        tenant_config=tenant_config,
    )
    assert result["successo"] is False
