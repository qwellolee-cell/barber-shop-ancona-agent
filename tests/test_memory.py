"""
Test asincroni per agent/memory.py.

Ogni test parte da un DB vuoto (reset_db in conftest.py è autouse).
Il tenant_id di default usato nei test è 1.
"""
from datetime import datetime, date, timedelta, UTC
from unittest.mock import MagicMock

from agent.memory import (
    prenota_appuntamento,
    cancella_appuntamento,
    get_appuntamenti_giorno,
    get_appuntamento_cliente,
    guardar_mensaje,
    obtener_historial,
    limpiar_historial,
    crea_o_aggiorna_tenant,
    get_tenant_by_slug,
    _slot_ha_risorsa_libera,
)

TENANT_ID = 1
TELEFONO = "391234567890"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mk_risorsa(id_: int, capienza: int = 1):
    r = MagicMock()
    r.id = id_
    r.capienza = capienza
    return r


def _mk_apt(risorsa_id, data_ora: datetime, durata: int, buffer: int = 0):
    a = MagicMock()
    a.risorsa_id = risorsa_id
    a.data_ora = data_ora
    a.durata_minuti = durata
    a.buffer_minuti = buffer
    return a


# ── Tenant CRUD ───────────────────────────────────────────────────────────────

async def test_crea_tenant():
    t = await crea_o_aggiorna_tenant("biz-test", "Negozio Test", "barbiere")
    assert t.slug == "biz-test"
    assert t.nome == "Negozio Test"
    assert t.id is not None


async def test_get_tenant_by_slug_trovato():
    await crea_o_aggiorna_tenant("biz-test", "Negozio Test", "barbiere")
    t = await get_tenant_by_slug("biz-test")
    assert t is not None
    assert t.slug == "biz-test"


async def test_get_tenant_by_slug_non_trovato():
    t = await get_tenant_by_slug("non-esiste-xyz")
    assert t is None


async def test_aggiorna_tenant_esistente():
    await crea_o_aggiorna_tenant("biz-test", "Nome Vecchio", "barbiere")
    t2 = await crea_o_aggiorna_tenant("biz-test", "Nome Nuovo", "ristorante")
    assert t2.nome == "Nome Nuovo"
    assert t2.business_type == "ristorante"


# ── Prenotazioni ──────────────────────────────────────────────────────────────

async def test_prenota_appuntamento_success():
    data_ora = datetime(2026, 6, 10, 10, 0)  # mercoledì
    apt = await prenota_appuntamento(
        telefono=TELEFONO,
        nome="Mario Rossi",
        servizio="Taglio",
        data_ora=data_ora,
        durata_minuti=30,
        tenant_id=TENANT_ID,
    )
    assert apt is not None
    assert apt.nome_cliente == "Mario Rossi"
    assert apt.servizio == "Taglio"
    assert apt.stato == "confermato"
    assert apt.tenant_id == TENANT_ID
    assert apt.id is not None


async def test_prenota_appuntamento_conflitto():
    """Stessa fascia oraria per lo stesso tenant → il secondo fallisce."""
    data_ora = datetime(2026, 6, 10, 10, 0)
    apt1 = await prenota_appuntamento(
        telefono=TELEFONO, nome="Mario", servizio="Taglio",
        data_ora=data_ora, durata_minuti=30, tenant_id=TENANT_ID,
    )
    assert apt1 is not None
    apt2 = await prenota_appuntamento(
        telefono="399999999999", nome="Luigi", servizio="Barba",
        data_ora=data_ora, durata_minuti=30, tenant_id=TENANT_ID,
    )
    assert apt2 is None


async def test_prenota_appuntamento_non_sovrapposto():
    """Slot contigui (non sovrapposti) devono entrambi riuscire."""
    apt1 = await prenota_appuntamento(
        telefono=TELEFONO, nome="A", servizio="Taglio",
        data_ora=datetime(2026, 6, 10, 10, 0), durata_minuti=30,
        tenant_id=TENANT_ID,
    )
    apt2 = await prenota_appuntamento(
        telefono=TELEFONO, nome="B", servizio="Barba",
        data_ora=datetime(2026, 6, 10, 10, 30), durata_minuti=30,
        tenant_id=TENANT_ID,
    )
    assert apt1 is not None
    assert apt2 is not None


async def test_prenota_tenant_diversi_stesso_slot():
    """Tenant diversi non si bloccano a vicenda."""
    data_ora = datetime(2026, 6, 10, 10, 0)
    apt1 = await prenota_appuntamento(
        telefono=TELEFONO, nome="A", servizio="Taglio",
        data_ora=data_ora, durata_minuti=30, tenant_id=1,
    )
    apt2 = await prenota_appuntamento(
        telefono=TELEFONO, nome="B", servizio="Taglio",
        data_ora=data_ora, durata_minuti=30, tenant_id=2,
    )
    assert apt1 is not None
    assert apt2 is not None


async def test_cancella_appuntamento():
    data_ora = datetime(2026, 6, 10, 11, 0)
    apt = await prenota_appuntamento(
        telefono=TELEFONO, nome="Mario", servizio="Taglio",
        data_ora=data_ora, durata_minuti=30, tenant_id=TENANT_ID,
    )
    assert apt is not None
    ok = await cancella_appuntamento(apt.id, tenant_id=TENANT_ID)
    assert ok is True
    # Lo slot è ora libero
    apt2 = await prenota_appuntamento(
        telefono=TELEFONO, nome="Mario", servizio="Taglio",
        data_ora=data_ora, durata_minuti=30, tenant_id=TENANT_ID,
    )
    assert apt2 is not None


async def test_cancella_appuntamento_non_trovato():
    ok = await cancella_appuntamento(99999, tenant_id=TENANT_ID)
    assert ok is False


async def test_cancella_appuntamento_tenant_errato():
    """Un tenant non deve poter cancellare gli appuntamenti di un altro."""
    apt = await prenota_appuntamento(
        telefono=TELEFONO, nome="Mario", servizio="Taglio",
        data_ora=datetime(2026, 6, 10, 11, 0), durata_minuti=30,
        tenant_id=TENANT_ID,
    )
    assert apt is not None
    ok = await cancella_appuntamento(apt.id, tenant_id=999)
    assert ok is False


async def test_get_appuntamenti_giorno():
    giorno = date(2026, 6, 10)
    await prenota_appuntamento(
        telefono=TELEFONO, nome="A", servizio="Taglio",
        data_ora=datetime(2026, 6, 10, 10, 0), durata_minuti=30, tenant_id=TENANT_ID,
    )
    await prenota_appuntamento(
        telefono=TELEFONO, nome="B", servizio="Barba",
        data_ora=datetime(2026, 6, 10, 11, 0), durata_minuti=30, tenant_id=TENANT_ID,
    )
    risultati = await get_appuntamenti_giorno(giorno, tenant_id=TENANT_ID)
    assert len(risultati) == 2
    # Ordine cronologico
    assert risultati[0].data_ora < risultati[1].data_ora


async def test_get_appuntamenti_giorno_vuoto():
    risultati = await get_appuntamenti_giorno(date(2026, 6, 10), tenant_id=TENANT_ID)
    assert risultati == []


async def test_get_appuntamento_cliente_futuro():
    futuro = datetime.now(UTC) + timedelta(hours=2)
    apt = await prenota_appuntamento(
        telefono=TELEFONO, nome="Mario", servizio="Taglio",
        data_ora=futuro, durata_minuti=30, tenant_id=TENANT_ID,
    )
    assert apt is not None
    trovato = await get_appuntamento_cliente(TELEFONO, tenant_id=TENANT_ID)
    assert trovato is not None
    assert trovato.id == apt.id


async def test_get_appuntamento_cliente_non_trovato():
    trovato = await get_appuntamento_cliente("000000000000", tenant_id=TENANT_ID)
    assert trovato is None


# ── Messaggi / Storico ────────────────────────────────────────────────────────

async def test_guardar_y_obtener_historial():
    await guardar_mensaje(TELEFONO, "user", "Ciao!", tenant_id=TENANT_ID)
    await guardar_mensaje(TELEFONO, "assistant", "Ciao! Come posso aiutarti?", tenant_id=TENANT_ID)
    h = await obtener_historial(TELEFONO, tenant_id=TENANT_ID)
    assert len(h) == 2
    assert h[0]["role"] == "user"
    assert h[0]["content"] == "Ciao!"
    assert h[1]["role"] == "assistant"


async def test_historial_scoped_per_tenant():
    """Tenant diversi non devono condividere il medesimo storico."""
    await guardar_mensaje(TELEFONO, "user", "Tenant UNO", tenant_id=1)
    await guardar_mensaje(TELEFONO, "user", "Tenant DUE", tenant_id=2)
    h1 = await obtener_historial(TELEFONO, tenant_id=1)
    h2 = await obtener_historial(TELEFONO, tenant_id=2)
    assert len(h1) == 1 and h1[0]["content"] == "Tenant UNO"
    assert len(h2) == 1 and h2[0]["content"] == "Tenant DUE"


async def test_limpiar_historial():
    await guardar_mensaje(TELEFONO, "user", "msg 1", tenant_id=TENANT_ID)
    await guardar_mensaje(TELEFONO, "user", "msg 2", tenant_id=TENANT_ID)
    await limpiar_historial(TELEFONO, tenant_id=TENANT_ID)
    h = await obtener_historial(TELEFONO, tenant_id=TENANT_ID)
    assert h == []


async def test_historial_rispetta_il_limite():
    for i in range(25):
        await guardar_mensaje(TELEFONO, "user", f"msg {i}", tenant_id=TENANT_ID)
    h = await obtener_historial(TELEFONO, limite=10, tenant_id=TENANT_ID)
    assert len(h) == 10


# ── _slot_ha_risorsa_libera (funzione pura, senza DB) ────────────────────────

_S = datetime(2026, 6, 10, 10, 0)
_E = datetime(2026, 6, 10, 10, 30)


def test_slot_libero_nessun_appuntamento():
    assert _slot_ha_risorsa_libera(_S, _E, [], [_mk_risorsa(1)]) is True


def test_slot_occupato_stessa_risorsa():
    apt = _mk_apt(1, _S, 30)
    assert _slot_ha_risorsa_libera(_S, _E, [apt], [_mk_risorsa(1)]) is False


def test_slot_libero_seconda_risorsa_disponibile():
    """Se risorsa 1 è occupata ma risorsa 2 è libera → slot libero."""
    apt = _mk_apt(1, _S, 30)
    risorse = [_mk_risorsa(1), _mk_risorsa(2)]
    assert _slot_ha_risorsa_libera(_S, _E, [apt], risorse) is True


def test_slot_occupato_tutte_risorse():
    apt1 = _mk_apt(1, _S, 30)
    apt2 = _mk_apt(2, _S, 30)
    risorse = [_mk_risorsa(1), _mk_risorsa(2)]
    assert _slot_ha_risorsa_libera(_S, _E, [apt1, apt2], risorse) is False


def test_slot_legacy_blocca_tutto():
    """Un appuntamento senza risorsa_id (legacy) deve bloccare tutte le risorse."""
    apt = _mk_apt(None, _S, 30)  # risorsa_id=None → legacy
    risorse = [_mk_risorsa(1), _mk_risorsa(2)]
    assert _slot_ha_risorsa_libera(_S, _E, [apt], risorse) is False


def test_appuntamento_non_sovrapposto_non_conta():
    """Un appuntamento che finisce prima dell'inizio dello slot non crea conflitto."""
    apt = _mk_apt(1, datetime(2026, 6, 10, 9, 0), 30)  # 09:00-09:30, prima di 10:00
    assert _slot_ha_risorsa_libera(_S, _E, [apt], [_mk_risorsa(1)]) is True
