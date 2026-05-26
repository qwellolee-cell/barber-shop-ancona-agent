"""
Configurazione globale dei test Cleek.

Le env var vengono settate a livello di modulo, prima di qualsiasi
import di agent/, così i moduli vengono inizializzati con i valori di test.

Il DB in-memory viene condiviso tramite StaticPool per evitare che ogni
connessione SQLite crei un database separato.
"""
import os
import pathlib

# ── Cambia CWD alla root del progetto ──────────────────────────────────────
# Necessario per i path relativi usati in memory.py (seed_risorse_da_settings)
# e in brain.py (_carica_prompt_tenant).
_PROJECT_ROOT = pathlib.Path(__file__).parent.parent
os.chdir(_PROJECT_ROOT)

# ── Env var di test — devono precedere ogni import di agent/ ───────────────
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key-not-real")
os.environ.setdefault("TENANT_SLUG", "barber-shop-ancona")
os.environ.setdefault("WHATSAPP_PROVIDER", "greenapi")
os.environ.setdefault("ADMIN_KEY", "test-admin-key-123")
os.environ.setdefault("GREEN_API_INSTANCE_ID", "test-instance")
os.environ.setdefault("GREEN_API_TOKEN", "test-token")

import pytest
import pytest_asyncio  # noqa: F401  (importato per autouse nei moduli di test)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool

import agent.memory as memory

# ── Override engine → SQLite :memory: condiviso tra tutte le connessioni ───
_TEST_ENGINE = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TEST_SESSION = async_sessionmaker(
    _TEST_ENGINE, class_=AsyncSession, expire_on_commit=False
)

# Sostituisce le variabili globali nel modulo memory usate da tutte le query
memory.engine = _TEST_ENGINE
memory.async_session = _TEST_SESSION


@pytest_asyncio.fixture(autouse=True)
async def reset_db():
    """
    Crea lo schema prima di ogni test asincrono e lo abbatte dopo.
    Garantisce isolamento completo tra i test.
    """
    async with _TEST_ENGINE.begin() as conn:
        await conn.run_sync(memory.Base.metadata.create_all)
    yield
    async with _TEST_ENGINE.begin() as conn:
        await conn.run_sync(memory.Base.metadata.drop_all)


@pytest.fixture
def tenant_config():
    """Carica (e invalida la cache di) TenantConfig per barber-shop-ancona."""
    from agent.tenant_loader import carica_tenant
    carica_tenant.cache_clear()
    return carica_tenant("barber-shop-ancona")
