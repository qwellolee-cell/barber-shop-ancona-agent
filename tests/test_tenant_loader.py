"""
Test unitari per agent/tenant_loader.py.

Test puri: nessun DB, nessuna chiamata I/O asincrona.
Si appoggia al file reale tenants/barber-shop-ancona/settings.yaml.
"""
import pytest
from datetime import time

from agent.tenant_loader import (
    TenantConfig,
    TurnoApertura,
    GiornoOrario,
    _parse_time,
    _parse_orari,
    carica_tenant,
)


# ── Caricamento YAML ──────────────────────────────────────────────────────────

def test_carica_tenant_barber_shop(tenant_config):
    """Verifica che il tenant barber-shop-ancona venga caricato correttamente."""
    assert isinstance(tenant_config, TenantConfig)
    assert tenant_config.slug == "barber-shop-ancona"
    assert tenant_config.nome_business == "Barber Shop Ancona"
    assert tenant_config.business_type == "barbiere"
    assert tenant_config.nome_agente == "Simone"
    assert tenant_config.emoji_firma == "✂️"
    assert tenant_config.lingua == "it"


def test_slot_granularity(tenant_config):
    assert tenant_config.slot_granularity_min == 15


def test_reminder_finestre_ore(tenant_config):
    assert tenant_config.reminder_finestre_ore == (24,)


def test_carica_tenant_non_esistente():
    """Un slug inesistente deve sollevare FileNotFoundError."""
    carica_tenant.cache_clear()
    with pytest.raises(FileNotFoundError):
        carica_tenant("slug-non-esiste-xyz")


# ── is_giorno_chiuso / turni_apertura ────────────────────────────────────────

def test_lunedi_chiuso(tenant_config):
    """Lunedì (0) è chiuso per il barber shop."""
    assert tenant_config.is_giorno_chiuso(0) is True


def test_domenica_chiusa(tenant_config):
    """Domenica (6) è chiusa."""
    assert tenant_config.is_giorno_chiuso(6) is True


def test_martedi_aperto(tenant_config):
    """Martedì (1) è aperto."""
    assert tenant_config.is_giorno_chiuso(1) is False


def test_sabato_aperto(tenant_config):
    """Sabato (5) è aperto."""
    assert tenant_config.is_giorno_chiuso(5) is False


def test_turni_martedi(tenant_config):
    """Martedì ha due turni: 09-13 e 15-19."""
    turni = tenant_config.turni_apertura(1)
    assert len(turni) == 2
    assert turni[0].apertura == time(9, 0)
    assert turni[0].chiusura == time(13, 0)
    assert turni[1].apertura == time(15, 0)
    assert turni[1].chiusura == time(19, 0)


def test_turni_lunedi_vuoti(tenant_config):
    """Lunedì chiuso → lista turni vuota."""
    assert tenant_config.turni_apertura(0) == []


def test_giorno_non_configurato_e_chiuso(tenant_config):
    """Un giorno non presente nello YAML deve risultare chiuso (default sicuro)."""
    giorno = tenant_config.orari_per_giorno(7)  # non esiste
    assert giorno.aperto is False


# ── formatta_messaggio_reminder ───────────────────────────────────────────────

def test_formatta_reminder_base(tenant_config):
    msg = tenant_config.formatta_messaggio_reminder(
        nome_cliente="Mario Rossi",
        ora="10:00",
        servizio="Taglio",
        data="26/05/2026",
        num_persone=1,
    )
    assert "Mario Rossi" in msg
    assert "10:00" in msg


def test_formatta_reminder_chiave_mancante(tenant_config):
    """Con una chiave mancante nel template ritorna il template grezzo (no crash)."""
    msg = tenant_config.formatta_messaggio_reminder(nome_cliente="Mario")
    assert isinstance(msg, str)
    assert len(msg) > 0


# ── Helper interni ────────────────────────────────────────────────────────────

def test_parse_time():
    assert _parse_time("09:30") == time(9, 30)
    assert _parse_time("00:00") == time(0, 0)
    assert _parse_time("18:45") == time(18, 45)


def test_parse_orari_formato_compatto():
    """Verifica il parsing del formato compatto 'HH:MM-HH:MM'."""
    orari_raw = [
        {"giorno": 1, "aperto": True, "turni": ["09:00-13:00", "15:00-19:00"]},
        {"giorno": 0, "aperto": False},
    ]
    result = _parse_orari(orari_raw)
    assert len(result) == 2
    aperto = next(g for g in result if g.giorno == 1)
    assert aperto.aperto is True
    assert len(aperto.turni) == 2
    assert aperto.turni[0].apertura == time(9, 0)
    assert aperto.turni[0].chiusura == time(13, 0)


def test_parse_orari_formato_dict():
    """Verifica il parsing del formato dict {apertura: ..., chiusura: ...}."""
    orari_raw = [
        {
            "giorno": 2,
            "aperto": True,
            "turni": [{"apertura": "10:00", "chiusura": "14:00"}],
        }
    ]
    result = _parse_orari(orari_raw)
    assert result[0].turni[0].apertura == time(10, 0)
    assert result[0].turni[0].chiusura == time(14, 0)
