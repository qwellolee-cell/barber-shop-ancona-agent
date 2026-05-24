# agent/memory.py — ORM models + query layer multi-tenant con risorse
# Cleek — Fase 3

"""
Layer dati di Cleek. Tutte le query sono scoped per tenant_id.

Novità Fase 3:
- Modello Risorsa (sedia/tavolo/cabina/studio)
- Appuntamento arricchito: risorsa_id, num_persone, buffer_minuti
- slot_disponibili() usa conflict check per-risorsa
- prenota_appuntamento() auto-assegna la risorsa con capienza minima adeguata
- seed_risorse_da_settings() legge le risorse da tenants/{slug}/settings.yaml

Compatibilità backward:
- Tutte le funzioni hanno default tenant_id=1 e num_persone=1
- Se il tenant non ha risorse configurate → fallback a conflict check globale
- Appuntamenti legacy (risorsa_id NULL) bloccano tutte le risorse (comportamento sicuro)
"""

import os
import logging
import yaml
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
# Tenant
# ─────────────────────────────────────────────────────────────────────────────

class Tenant(Base):
    """Registro di un business cliente di Cleek."""
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    nome: Mapped[str] = mapped_column(String(100), nullable=False)
    business_type: Mapped[str] = mapped_column(String(50), default="barbiere")
    whatsapp_numero: Mapped[Optional[str]] = mapped_column(String(60), unique=True, nullable=True)
    config_path: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    attivo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# Risorsa — NUOVO Fase 3
# ─────────────────────────────────────────────────────────────────────────────

class Risorsa(Base):
    """
    Risorsa fisica prenotabile: sedia (barbiere), tavolo (ristorante),
    cabina (estetista), studio (dentista).

    Capienza: quante persone può accogliere contemporaneamente.
    Per barbieri/estetisti/dentisti = 1.
    Per ristoranti = N (es. tavolo da 4).
    """
    __tablename__ = "risorse"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    nome: Mapped[str] = mapped_column(String(100), nullable=False)
    tipo: Mapped[str] = mapped_column(String(50), default="generico")  # sedia/tavolo/cabina/studio
    capienza: Mapped[int] = mapped_column(Integer, default=1)
    attiva: Mapped[bool] = mapped_column(Boolean, default=True)
    ordine: Mapped[int] = mapped_column(Integer, default=0)


# ─────────────────────────────────────────────────────────────────────────────
# Conversazioni
# ─────────────────────────────────────────────────────────────────────────────

class Mensaje(Base):
    """Storico conversazioni WhatsApp, scoped per tenant."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# Prenotazioni — arricchito in Fase 3
# ─────────────────────────────────────────────────────────────────────────────

class Appuntamento(Base):
    """
    Prenotazione di un cliente, scoped per tenant.

    Fase 3: aggiunto risorsa_id (quale sedia/tavolo è occupato),
    num_persone (coperti per ristorante), buffer_minuti (dentisti).
    """
    __tablename__ = "appuntamenti"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # Fase 3: risorsa assegnata (nullable per compat backward)
    risorsa_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    nome_cliente: Mapped[str] = mapped_column(String(100))
    servizio: Mapped[str] = mapped_column(String(100))
    data_ora: Mapped[datetime] = mapped_column(DateTime, index=True)
    durata_minuti: Mapped[int] = mapped_column(Integer)
    # Fase 3: numero persone (default 1; per ristoranti indica i coperti)
    num_persone: Mapped[int] = mapped_column(Integer, default=1)
    # Fase 3: minuti di buffer post-appuntamento (es. 15 per dentisti)
    buffer_minuti: Mapped[int] = mapped_column(Integer, default=0)
    stato: Mapped[str] = mapped_column(String(20), default="confermato")
    reminder_inviato: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# Inizializzazione DB con migrazione automatica
# ─────────────────────────────────────────────────────────────────────────────

async def inicializar_db():
    """
    Crea tutte le tabelle (se non esistono) e migra quelle esistenti.
    Esegue il seed di tenant default e relative risorse.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Migrazione colonne Fase 2 (tenant_id) e Fase 3 (risorsa_id, num_persone, buffer)
    async with engine.begin() as conn:
        for sql in [
            "ALTER TABLE appuntamenti ADD COLUMN tenant_id INTEGER",
            "ALTER TABLE mensajes ADD COLUMN tenant_id INTEGER",
            "ALTER TABLE appuntamenti ADD COLUMN risorsa_id INTEGER",
            "ALTER TABLE appuntamenti ADD COLUMN num_persone INTEGER DEFAULT 1",
            "ALTER TABLE appuntamenti ADD COLUMN buffer_minuti INTEGER DEFAULT 0",
        ]:
            try:
                await conn.execute(text(sql))
                logger.info(f"Migrazione: {sql}")
            except Exception:
                pass  # Colonna già esistente

        # Backfill tenant_id su record orfani
        for sql in [
            "UPDATE appuntamenti SET tenant_id = 1 WHERE tenant_id IS NULL",
            "UPDATE mensajes SET tenant_id = 1 WHERE tenant_id IS NULL",
            "UPDATE appuntamenti SET num_persone = 1 WHERE num_persone IS NULL",
            "UPDATE appuntamenti SET buffer_minuti = 0 WHERE buffer_minuti IS NULL",
        ]:
            await conn.execute(text(sql))

    # Seed tenant default
    await _seed_tenant_default()
    # Seed risorse del tenant default
    await seed_risorse_da_settings("barber-shop-ancona", tenant_id=1)


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
    async with async_session() as session:
        result = await session.execute(
            select(Tenant).where(and_(Tenant.slug == slug, Tenant.attivo == True))  # noqa: E712
        )
        return result.scalars().first()


async def get_tenant_by_whatsapp(numero: str) -> Optional[Tenant]:
    async with async_session() as session:
        result = await session.execute(
            select(Tenant).where(
                and_(Tenant.whatsapp_numero == numero, Tenant.attivo == True)  # noqa: E712
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
    """Crea o aggiorna un tenant (upsert per slug). Poi fa il seed delle risorse."""
    async with async_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.slug == slug))
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

    # Seed risorse dal settings.yaml se disponibile
    await seed_risorse_da_settings(slug, tenant_id=tenant.id)
    return tenant


# ─────────────────────────────────────────────────────────────────────────────
# Gestione Risorse — Fase 3
# ─────────────────────────────────────────────────────────────────────────────

async def seed_risorse_da_settings(slug: str, tenant_id: int) -> None:
    """
    Legge la sezione 'risorse' da tenants/{slug}/settings.yaml e crea
    i record Risorsa nel DB se non esistono già per questo tenant.
    Operazione idempotente: non duplica risorse già presenti.
    """
    settings_path = f"tenants/{slug}/settings.yaml"
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return  # nessun settings.yaml, skip silenzioso

    risorse_yaml = data.get("risorse", [])
    if not risorse_yaml:
        return

    async with async_session() as session:
        # Controlla quante risorse esistono già per questo tenant
        result = await session.execute(
            select(Risorsa).where(Risorsa.tenant_id == tenant_id)
        )
        existing = result.scalars().all()
        nomi_esistenti = {r.nome for r in existing}

        nuove = 0
        for i, r_data in enumerate(risorse_yaml):
            nome = r_data.get("nome", f"Risorsa {i+1}")
            if nome in nomi_esistenti:
                continue
            session.add(Risorsa(
                tenant_id=tenant_id,
                nome=nome,
                tipo=r_data.get("tipo", "generico"),
                capienza=r_data.get("capienza", 1),
                attiva=r_data.get("attivo", True),
                ordine=i,
            ))
            nuove += 1

        if nuove:
            await session.commit()
            logger.info(f"Seed risorse [{slug}]: {nuove} risorse create")


async def get_risorse_attive(
    tenant_id: int,
    min_capienza: int = 1,
) -> list[Risorsa]:
    """
    Restituisce le risorse attive del tenant con capienza >= min_capienza,
    ordinate per capienza crescente (minima capienza sufficiente prima).
    Questo approccio massimizza la disponibilità delle risorse grandi.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Risorsa)
            .where(
                and_(
                    Risorsa.tenant_id == tenant_id,
                    Risorsa.attiva == True,  # noqa: E712
                    Risorsa.capienza >= min_capienza,
                )
            )
            .order_by(Risorsa.capienza, Risorsa.ordine)
        )
        return list(result.scalars().all())


async def _trova_risorsa_libera(
    session: AsyncSession,
    tenant_id: int,
    data_ora: datetime,
    fine: datetime,
    risorse: list[Risorsa],
    buffer_minuti: int = 0,
) -> Optional[Risorsa]:
    """
    Cerca la prima risorsa libera nel periodo [data_ora, fine + buffer].
    Priorità: risorsa con capienza minima adeguata (efficienza massima).

    Regole di conflitto:
    - Appuntamento con risorsa_id → conflitto solo se stessa risorsa
    - Appuntamento senza risorsa_id (legacy) → blocca TUTTE le risorse (sicuro)
    """
    fine_con_buffer = fine + timedelta(minutes=buffer_minuti)

    # Carica tutti gli appuntamenti confermati che si sovrappongono al periodo
    result = await session.execute(
        select(Appuntamento).where(
            and_(
                Appuntamento.tenant_id == tenant_id,
                Appuntamento.stato == "confermato",
                Appuntamento.data_ora < fine_con_buffer,
            )
        )
    )
    candidati = result.scalars().all()

    # Filtra quelli che si sovrappongono davvero
    conflitti = []
    for apt in candidati:
        apt_fine = apt.data_ora + timedelta(minutes=apt.durata_minuti + (apt.buffer_minuti or 0))
        if apt.data_ora < fine_con_buffer and apt_fine > data_ora:
            conflitti.append(apt)

    # Appuntamenti senza risorsa_id bloccano tutte le risorse
    ha_conflitti_globali = any(apt.risorsa_id is None for apt in conflitti)
    if ha_conflitti_globali:
        return None

    # Per ogni risorsa: verifica che nessun conflitto la blocchi
    risorsa_id_occupati = {apt.risorsa_id for apt in conflitti if apt.risorsa_id is not None}

    for risorsa in risorse:
        if risorsa.id not in risorsa_id_occupati:
            return risorsa

    return None  # tutte le risorse occupate


# ─────────────────────────────────────────────────────────────────────────────
# Prenotazioni
# ─────────────────────────────────────────────────────────────────────────────

async def prenota_appuntamento(
    telefono: str,
    nome: str,
    servizio: str,
    data_ora: datetime,
    durata_minuti: int,
    tenant_id: int = 1,
    num_persone: int = 1,
    buffer_minuti: int = 0,
) -> Optional[Appuntamento]:
    """
    Crea un appuntamento se esiste almeno una risorsa libera.

    Logica:
    1. Cerca risorse attive con capienza >= num_persone
    2. Se trovate: assegna la prima libera nel periodo richiesto
    3. Se nessuna risorsa configurata: fallback a conflict check globale
    4. Ritorna None se slot occupato (nessuna risorsa libera)

    Args:
        num_persone: coperti richiesti (default 1; per ristoranti)
        buffer_minuti: minuti di sanificazione post-appuntamento (default 0; per dentisti)
    """
    fine = data_ora + timedelta(minutes=durata_minuti)

    async with async_session() as session:
        risorse = await get_risorse_attive(tenant_id, min_capienza=num_persone)

        risorsa_id: Optional[int] = None

        if risorse:
            # Modalità multi-risorsa: trova la prima risorsa libera
            risorsa_scelta = await _trova_risorsa_libera(
                session, tenant_id, data_ora, fine, risorse, buffer_minuti
            )
            if risorsa_scelta is None:
                logger.info(f"[tenant={tenant_id}] Nessuna risorsa libera in {data_ora}")
                return None
            risorsa_id = risorsa_scelta.id
            logger.info(f"[tenant={tenant_id}] Risorsa assegnata: {risorsa_scelta.nome} (cap={risorsa_scelta.capienza})")

        else:
            # Fallback globale (tenant senza risorse configurate)
            result = await session.execute(
                select(Appuntamento).where(
                    and_(
                        Appuntamento.tenant_id == tenant_id,
                        Appuntamento.stato == "confermato",
                        Appuntamento.data_ora < fine,
                    )
                )
            )
            for apt in result.scalars().all():
                apt_fine = apt.data_ora + timedelta(minutes=apt.durata_minuti)
                if apt.data_ora < fine and apt_fine > data_ora:
                    return None

        nuovo = Appuntamento(
            tenant_id=tenant_id,
            risorsa_id=risorsa_id,
            telefono=telefono,
            nome_cliente=nome,
            servizio=servizio,
            data_ora=data_ora,
            durata_minuti=durata_minuti,
            num_persone=num_persone,
            buffer_minuti=buffer_minuti,
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
    Soft delete scoped per tenant.
    Il filtro su tenant_id previene cancellazioni cross-tenant.
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
    num_persone: int = 1,
) -> list[str]:
    """
    Slot liberi in una data per un tenant.

    Fase 3 — logica multi-risorsa:
    - Se il tenant ha risorse configurate: uno slot è libero se almeno una
      risorsa con capienza >= num_persone è disponibile in quel periodo.
    - Se nessuna risorsa: fallback a conflict check globale (backward compat).

    Args:
        num_persone: persone da ospitare (filtra risorse per capienza sufficiente)
    """
    if tenant_config is None:
        from agent.tenant_loader import carica_tenant_default
        tenant_config = carica_tenant_default()

    giorno_iso = data.weekday()

    if tenant_config.is_giorno_chiuso(giorno_iso):
        return []

    # Carica risorse attive con capienza adeguata
    risorse = await get_risorse_attive(tenant_id, min_capienza=num_persone)
    # Carica tutti gli appuntamenti del giorno (per entrambe le logiche)
    appuntamenti = await get_appuntamenti_giorno(data, tenant_id=tenant_id)

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

            if risorse:
                slot_libero = _slot_ha_risorsa_libera(
                    slot, fine_slot, appuntamenti, risorse
                )
            else:
                # Fallback globale
                slot_libero = not any(
                    apt.data_ora < fine_slot
                    and apt.data_ora + timedelta(minutes=apt.durata_minuti) > slot
                    for apt in appuntamenti
                )

            if slot_libero:
                liberi.append(slot.strftime("%H:%M"))
            slot += timedelta(minutes=granularita)

    return liberi


def _slot_ha_risorsa_libera(
    slot_inizio: datetime,
    slot_fine: datetime,
    appuntamenti: list[Appuntamento],
    risorse: list[Risorsa],
) -> bool:
    """
    True se almeno una risorsa è libera nel periodo [slot_inizio, slot_fine].

    Regole:
    - Appuntamento con risorsa_id → conflitto solo su quella risorsa
    - Appuntamento senza risorsa_id (legacy) → blocca tutte le risorse
    """
    # Calcola i conflitti che si sovrappongono allo slot
    conflitti_nel_slot = []
    for apt in appuntamenti:
        apt_fine = apt.data_ora + timedelta(minutes=apt.durata_minuti + (apt.buffer_minuti or 0))
        if apt.data_ora < slot_fine and apt_fine > slot_inizio:
            conflitti_nel_slot.append(apt)

    # Se c'è un appuntamento legacy (senza risorsa), blocca tutto
    if any(apt.risorsa_id is None for apt in conflitti_nel_slot):
        return False

    # Set di risorsa_id occupati in questo slot
    occupati = {apt.risorsa_id for apt in conflitti_nel_slot}

    # Basta che una risorsa non sia occupata
    return any(r.id not in occupati for r in risorse)


async def get_appuntamenti_promemoria(
    tenant_id: Optional[int] = None,
    finestra_ore: Optional[int] = None,
) -> list[Appuntamento]:
    """
    Appuntamenti confermati nella finestra di tempo indicata
    con reminder non ancora inviato.
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
    async with async_session() as session:
        result = await session.execute(
            select(Appuntamento).where(Appuntamento.id == appuntamento_id)
        )
        apt = result.scalars().first()
        if apt:
            apt.reminder_inviato = True
            await session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Messaggi / Conversazioni
# ─────────────────────────────────────────────────────────────────────────────

async def guardar_mensaje(
    telefono: str,
    role: str,
    content: str,
    tenant_id: int = 1,
):
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
    async with async_session() as session:
        result = await session.execute(
            select(Mensaje)
            .where(and_(Mensaje.tenant_id == tenant_id, Mensaje.telefono == telefono))
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
    async with async_session() as session:
        result = await session.execute(
            select(Mensaje).where(
                and_(Mensaje.tenant_id == tenant_id, Mensaje.telefono == telefono)
            )
        )
        for msg in result.scalars().all():
            await session.delete(msg)
        await session.commit()
