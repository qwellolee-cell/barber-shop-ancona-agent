# agent/memory.py — ORM models + query layer multi-tenant
# Cleek — Fase 2

"""
Layer dati dell'applicazione. Tutte le query sono scoped per tenant_id,
garantendo isolamento completo tra tenant diversi.

Compatibilità backward: tutte le funzioni accettano tenant_id con default=1
(barber-shop-ancona), così il codice esistente continua a funzionare
senza modifiche immediate.

Migrazione automatica: inicializar_db() aggiunge tenant_id alle tabelle
esistenti se non presente (via ALTER TABLE con try/except).
"""

import os
import logging
from datetime import datetime, date, timedelta
from typing import Optional

from sqlalchemy import String, Text, DateTime, select, Integer, Boolean, and_, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./barber.db")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# NUOVO: Tabella Tenant — registro di tutti i business su Cleek
# ─────────────────────────────────────────────────────────────────────────────

class Tenant(Base):
    """Registro di un business cliente di Cleek."""
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    nome: Mapped[str] = mapped_column(String(100), nullable=False)
    business_type: Mapped[str] = mapped_column(String(50), default="barbiere")
    # Numero WhatsApp del business (es. "+39...@c.us") — usato per routing
    whatsapp_numero: Mapped[Optional[str]] = mapped_column(String(60), unique=True, nullable=True)
    # Path al file settings.yaml (es. "tenants/barber-shop-ancona/settings.yaml")
    config_path: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    attivo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# Tabelle principali — aggiunto tenant_id (nullable per compat backward)
# ─────────────────────────────────────────────────────────────────────────────

class Mensaje(Base):
    """Storico conversazioni WhatsApp, scoped per tenant."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Fase 2: tenant_id — nullable per compat backward (backfill avviene in inicializar_db)
    tenant_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Appuntamento(Base):
    """Prenotazione di un cliente, scoped per tenant."""
    __tablename__ = "appuntamenti"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Fase 2: tenant_id — nullable per compat backward
    tenant_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    nome_cliente: Mapped[str] = mapped_column(String(100))
    servizio: Mapped[str] = mapped_column(String(100))
    data_ora: Mapped[datetime] = mapped_column(DateTime, index=True)
    durata_minuti: Mapped[int] = mapped_column(Integer)
    stato: Mapped[str] = mapped_column(String(20), default="confermato")
    reminder_inviato: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# Inizializzazione DB con migrazione automatica
# ─────────────────────────────────────────────────────────────────────────────

async def inicializar_db():
    """
    Crea tutte le tabelle (nuove) e aggiunge tenant_id alle tabelle
    esistenti se non già presente.
    Seed: inserisce il record per barber-shop-ancona (id=1) se assente.
    """
    # Crea le tabelle che non esistono ancora (inclusa tenants)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Fase 2: aggiungi tenant_id alle tabelle esistenti create in Fase 1
    # ALTER TABLE è idempotente grazie al try/except
    async with engine.begin() as conn:
        for sql in [
            "ALTER TABLE appuntamenti ADD COLUMN tenant_id INTEGER",
            "ALTER TABLE mensajes ADD COLUMN tenant_id INTEGER",
        ]:
            try:
                await conn.execute(text(sql))
                logger.info(f"Migrazione: {sql}")
            except Exception:
                pass  # Colonna già esistente — OK

        # Backfill: assegna tutti i record privi di tenant_id al tenant 1
        for sql in [
            "UPDATE appuntamenti SET tenant_id = 1 WHERE tenant_id IS NULL",
            "UPDATE mensajes SET tenant_id = 1 WHERE tenant_id IS NULL",
        ]:
            await conn.execute(text(sql))

    # Seed: inserisce barber-shop-ancona come primo tenant se non esiste
    await _seed_tenant_default()


async def _seed_tenant_default():
    """Crea il record tenant per barber-shop-ancona (id=1) se non esiste."""
    async with async_session() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.slug == "barber-shop-ancona")
        )
        if result.scalars().first() is None:
            session.add(Tenant(
                id=1,
                slug="barber-shop-ancona",
                nome="Barber Shop Ancona",
                business_type="barbiere",
                config_path="tenants/barber-shop-ancona/settings.yaml",
                attivo=True,
                created_at=datetime.utcnow(),
            ))
            await session.commit()
            logger.info("Seed: tenant barber-shop-ancona (id=1) creato")


# ─────────────────────────────────────────────────────────────────────────────
# Gestione Tenant
# ─────────────────────────────────────────────────────────────────────────────

async def get_tenant_by_slug(slug: str) -> Optional[Tenant]:
    """Cerca un tenant per slug. Ritorna None se non trovato o non attivo."""
    async with async_session() as session:
        result = await session.execute(
            select(Tenant).where(
                and_(Tenant.slug == slug, Tenant.attivo == True)  # noqa: E712
            )
        )
        return result.scalars().first()


async def get_tenant_by_whatsapp(numero: str) -> Optional[Tenant]:
    """
    Cerca il tenant associato a un numero WhatsApp.
    Usato per il routing multi-tenant nel webhook.
    Ritorna None se nessun tenant ha quel numero.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Tenant).where(
                and_(
                    Tenant.whatsapp_numero == numero,
                    Tenant.attivo == True,  # noqa: E712
                )
            )
        )
        return result.scalars().first()


async def crea_o_aggiorna_tenant(
    slug: str,
    nome: str,
    business_type: str = "barbiere",
    whatsapp_numero: Optional[str] = None,
    config_path: Optional[str] = None,
) -> Tenant:
    """Crea un nuovo tenant o aggiorna uno esistente (upsert per slug)."""
    async with async_session() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )
        tenant = result.scalars().first()
        if tenant is None:
            tenant = Tenant(
                slug=slug,
                nome=nome,
                business_type=business_type,
                whatsapp_numero=whatsapp_numero,
                config_path=config_path or f"tenants/{slug}/settings.yaml",
                attivo=True,
                created_at=datetime.utcnow(),
            )
            session.add(tenant)
        else:
            tenant.nome = nome
            tenant.business_type = business_type
            if whatsapp_numero is not None:
                tenant.whatsapp_numero = whatsapp_numero
            if config_path is not None:
                tenant.config_path = config_path
        await session.commit()
        await session.refresh(tenant)
        return tenant


# ─────────────────────────────────────────────────────────────────────────────
# Prenotazioni — tutte le funzioni filtrano per tenant_id
# ─────────────────────────────────────────────────────────────────────────────

async def prenota_appuntamento(
    telefono: str,
    nome: str,
    servizio: str,
    data_ora: datetime,
    durata_minuti: int,
    tenant_id: int = 1,
) -> Optional[Appuntamento]:
    """
    Crea un appuntamento solo se lo slot è libero per quel tenant.
    Il conflict check è scoped per tenant_id: due tenant diversi
    possono avere appuntamenti nello stesso slot senza interferenze.
    Ritorna None se lo slot è occupato.
    """
    fine = data_ora + timedelta(minutes=durata_minuti)
    async with async_session() as session:
        overlap = await session.execute(
            select(Appuntamento).where(
                and_(
                    Appuntamento.tenant_id == tenant_id,
                    Appuntamento.stato == "confermato",
                    Appuntamento.data_ora < fine,
                )
            )
        )
        esistenti = overlap.scalars().all()
        for apt in esistenti:
            fine_esistente = apt.data_ora + timedelta(minutes=apt.durata_minuti)
            if apt.data_ora < fine and fine_esistente > data_ora:
                return None  # Slot occupato per questo tenant

        nuovo = Appuntamento(
            tenant_id=tenant_id,
            telefono=telefono,
            nome_cliente=nome,
            servizio=servizio,
            data_ora=data_ora,
            durata_minuti=durata_minuti,
            stato="confermato",
            reminder_inviato=False,
            created_at=datetime.utcnow(),
        )
        session.add(nuovo)
        await session.commit()
        await session.refresh(nuovo)
        return nuovo


async def get_appuntamenti_giorno(
    data: date,
    tenant_id: int = 1,
) -> list[Appuntamento]:
    """Tutti gli appuntamenti confermati di un tenant per una data."""
    inizio = datetime(data.year, data.month, data.day, 0, 0, 0)
    fine = datetime(data.year, data.month, data.day, 23, 59, 59)
    async with async_session() as session:
        result = await session.execute(
            select(Appuntamento)
            .where(
                and_(
                    Appuntamento.tenant_id == tenant_id,
                    Appuntamento.stato == "confermato",
                    Appuntamento.data_ora >= inizio,
                    Appuntamento.data_ora <= fine,
                )
            )
            .order_by(Appuntamento.data_ora)
        )
        return list(result.scalars().all())


async def get_appuntamento_cliente(
    telefono: str,
    tenant_id: int = 1,
) -> Optional[Appuntamento]:
    """Il prossimo appuntamento futuro confermato del cliente, scoped per tenant."""
    ora = datetime.utcnow()
    async with async_session() as session:
        result = await session.execute(
            select(Appuntamento)
            .where(
                and_(
                    Appuntamento.tenant_id == tenant_id,
                    Appuntamento.telefono == telefono,
                    Appuntamento.stato == "confermato",
                    Appuntamento.data_ora >= ora,
                )
            )
            .order_by(Appuntamento.data_ora)
            .limit(1)
        )
        return result.scalars().first()


async def cancella_appuntamento(
    appuntamento_id: int,
    tenant_id: int = 1,
) -> bool:
    """
    Soft delete di un appuntamento.
    Il filtro su tenant_id previene cancellazioni cross-tenant.
    Ritorna False se l'appuntamento non esiste o non appartiene al tenant.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Appuntamento).where(
                and_(
                    Appuntamento.id == appuntamento_id,
                    Appuntamento.tenant_id == tenant_id,
                )
            )
        )
        apt = result.scalars().first()
        if apt is None:
            return False
        apt.stato = "cancellato"
        await session.commit()
        return True


async def slot_disponibili(
    data: date,
    durata_minuti: int,
    tenant_config=None,
    tenant_id: int = 1,
) -> list[str]:
    """
    Slot liberi in una data con granularità configurabile per tenant.
    Filtra gli appuntamenti esistenti per tenant_id per evitare
    che un tenant veda la disponibilità influenzata da un altro.

    Args:
        data: data per cui calcolare gli slot
        durata_minuti: durata del servizio richiesto
        tenant_config: TenantConfig (opzionale, usa default se None)
        tenant_id: id numerico del tenant (default=1 per backward compat)
    """
    if tenant_config is None:
        from agent.tenant_loader import carica_tenant_default
        tenant_config = carica_tenant_default()

    giorno_iso = data.weekday()  # 0=lunedì … 6=domenica

    if tenant_config.is_giorno_chiuso(giorno_iso):
        return []

    # Recupera solo gli appuntamenti di QUESTO tenant
    appuntamenti_giorno = await get_appuntamenti_giorno(data, tenant_id=tenant_id)

    granularita = tenant_config.slot_granularity_min
    liberi = []

    for turno in tenant_config.turni_apertura(giorno_iso):
        slot = datetime(data.year, data.month, data.day,
                        turno.apertura.hour, turno.apertura.minute)
        limite = datetime(data.year, data.month, data.day,
                          turno.chiusura.hour, turno.chiusura.minute)
        limite -= timedelta(minutes=durata_minuti)

        while slot <= limite:
            fine_slot = slot + timedelta(minutes=durata_minuti)
            occupato = any(
                apt.data_ora < fine_slot
                and apt.data_ora + timedelta(minutes=apt.durata_minuti) > slot
                for apt in appuntamenti_giorno
            )
            if not occupato:
                liberi.append(slot.strftime("%H:%M"))
            slot += timedelta(minutes=granularita)

    return liberi


async def get_appuntamenti_promemoria(
    tenant_id: int = None,
    finestra_ore: int = None,
) -> list[Appuntamento]:
    """
    Appuntamenti confermati nella finestra di tempo indicata
    con reminder non ancora inviato, filtrati per tenant.

    Args:
        tenant_id: id del tenant (None = tutti i tenant attivi)
        finestra_ore: ore da ora entro cui cercare. Se None, usa config tenant.
    """
    if finestra_ore is None:
        try:
            from agent.tenant_loader import carica_tenant_default
            config = carica_tenant_default()
            finestra_ore = max(config.reminder_finestre_ore) if config.reminder_finestre_ore else 24
        except Exception:
            finestra_ore = 24

    ora = datetime.utcnow()
    limite = ora + timedelta(hours=finestra_ore)

    async with async_session() as session:
        filtri = [
            Appuntamento.stato == "confermato",
            Appuntamento.reminder_inviato == False,  # noqa: E712
            Appuntamento.data_ora >= ora,
            Appuntamento.data_ora <= limite,
        ]
        if tenant_id is not None:
            filtri.append(Appuntamento.tenant_id == tenant_id)

        result = await session.execute(
            select(Appuntamento).where(and_(*filtri))
        )
        return list(result.scalars().all())


async def segna_reminder_inviato(appuntamento_id: int) -> None:
    """Imposta reminder_inviato=True per quell'appuntamento."""
    async with async_session() as session:
        result = await session.execute(
            select(Appuntamento).where(Appuntamento.id == appuntamento_id)
        )
        apt = result.scalars().first()
        if apt:
            apt.reminder_inviato = True
            await session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Messaggi / Conversazioni — scoped per tenant_id
# ─────────────────────────────────────────────────────────────────────────────

async def guardar_mensaje(
    telefono: str,
    role: str,
    content: str,
    tenant_id: int = 1,
):
    """Salva un messaggio nella cronologia della conversazione."""
    async with async_session() as session:
        session.add(Mensaje(
            tenant_id=tenant_id,
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow(),
        ))
        await session.commit()


async def obtener_historial(
    telefono: str,
    limite: int = 20,
    tenant_id: int = 1,
) -> list[dict]:
    """
    Recupera gli ultimi N messaggi della conversazione di un cliente,
    scoped per tenant. Un cliente dello stesso numero su due tenant
    diversi ha storie separate.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Mensaje)
            .where(
                and_(
                    Mensaje.tenant_id == tenant_id,
                    Mensaje.telefono == telefono,
                )
            )
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        msgs = list(result.scalars().all())
        msgs.reverse()
        return [{"role": m.role, "content": m.content} for m in msgs]


async def limpiar_historial(
    telefono: str,
    tenant_id: int = 1,
):
    """Cancella tutto lo storico conversazione di un cliente per il tenant."""
    async with async_session() as session:
        result = await session.execute(
            select(Mensaje).where(
                and_(
                    Mensaje.tenant_id == tenant_id,
                    Mensaje.telefono == telefono,
                )
            )
        )
        for msg in result.scalars().all():
            await session.delete(msg)
        await session.commit()
