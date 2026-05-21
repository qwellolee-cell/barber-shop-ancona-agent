import os
from datetime import datetime, date, timedelta
from typing import Optional
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select, Integer, Boolean, and_
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./barber.db")

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Appuntamento(Base):
    __tablename__ = "appuntamenti"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    nome_cliente: Mapped[str] = mapped_column(String(100))
    servizio: Mapped[str] = mapped_column(String(100))
    data_ora: Mapped[datetime] = mapped_column(DateTime, index=True)
    durata_minuti: Mapped[int] = mapped_column(Integer)
    stato: Mapped[str] = mapped_column(String(20), default="confermato")
    reminder_inviato: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def inicializar_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def prenota_appuntamento(
    telefono: str,
    nome: str,
    servizio: str,
    data_ora: datetime,
    durata_minuti: int,
) -> Optional[Appuntamento]:
    """
    Crea un appuntamento solo se lo slot è libero.
    Controlla che nessun appuntamento confermato si sovrapponga
    nell'intervallo [data_ora, data_ora + durata_minuti].
    Ritorna None se lo slot è occupato.
    """
    fine = data_ora + timedelta(minutes=durata_minuti)
    async with async_session() as session:
        # Cerca appuntamenti confermati che si sovrappongono
        overlap = await session.execute(
            select(Appuntamento).where(
                and_(
                    Appuntamento.stato == "confermato",
                    # L'appuntamento esistente inizia prima della fine del nuovo
                    Appuntamento.data_ora < fine,
                    # L'appuntamento esistente finisce dopo l'inizio del nuovo
                    # fine_esistente = data_ora + durata_minuti (calcolata via Python dopo fetch)
                )
            )
        )
        esistenti = overlap.scalars().all()
        for apt in esistenti:
            fine_esistente = apt.data_ora + timedelta(minutes=apt.durata_minuti)
            if apt.data_ora < fine and fine_esistente > data_ora:
                return None  # Slot occupato

        nuovo = Appuntamento(
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


async def get_appuntamenti_giorno(data: date) -> list[Appuntamento]:
    """Tutti gli appuntamenti confermati per una data, ordinati per orario."""
    inizio = datetime(data.year, data.month, data.day, 0, 0, 0)
    fine = datetime(data.year, data.month, data.day, 23, 59, 59)
    async with async_session() as session:
        result = await session.execute(
            select(Appuntamento)
            .where(
                and_(
                    Appuntamento.stato == "confermato",
                    Appuntamento.data_ora >= inizio,
                    Appuntamento.data_ora <= fine,
                )
            )
            .order_by(Appuntamento.data_ora)
        )
        return list(result.scalars().all())


async def get_appuntamento_cliente(telefono: str) -> Optional[Appuntamento]:
    """Il prossimo appuntamento futuro confermato del cliente."""
    ora = datetime.utcnow()
    async with async_session() as session:
        result = await session.execute(
            select(Appuntamento)
            .where(
                and_(
                    Appuntamento.telefono == telefono,
                    Appuntamento.stato == "confermato",
                    Appuntamento.data_ora >= ora,
                )
            )
            .order_by(Appuntamento.data_ora)
            .limit(1)
        )
        return result.scalars().first()


async def cancella_appuntamento(appuntamento_id: int) -> bool:
    """Soft delete: imposta stato='cancellato'. Ritorna False se l'id non esiste."""
    async with async_session() as session:
        result = await session.execute(
            select(Appuntamento).where(Appuntamento.id == appuntamento_id)
        )
        apt = result.scalars().first()
        if apt is None:
            return False
        apt.stato = "cancellato"
        await session.commit()
        return True


async def slot_disponibili(data: date, durata_minuti: int) -> list[str]:
    """
    Slot liberi in una data con granularità 15 min.
    Importa ORARIO_NEGOCIO e GIORNI_CHIUSI da agent/calendar.py
    per rispettare il calendario del negozio.
    Ritorna lista di stringhe "HH:MM".
    """
    # Import qui per evitare dipendenza circolare (calendar.py importa da memory.py)
    from agent.calendar import ORARIO_NEGOCIO, GIORNI_CHIUSI

    # Controlla giorno chiuso
    nome_giorno = data.strftime("%A").lower()  # monday, tuesday, ...
    nomi_it = {
        "monday": "lunedi", "tuesday": "martedi", "wednesday": "mercoledi",
        "thursday": "giovedi", "friday": "venerdi", "saturday": "sabato",
        "sunday": "domenica",
    }
    if nomi_it.get(nome_giorno) in GIORNI_CHIUSI:
        return []

    appuntamenti_giorno = await get_appuntamenti_giorno(data)

    liberi = []
    for apertura, chiusura in ORARIO_NEGOCIO:
        # Genera slot da apertura a (chiusura - durata)
        slot = datetime(data.year, data.month, data.day, apertura[0], apertura[1])
        limite = datetime(data.year, data.month, data.day, chiusura[0], chiusura[1])
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
            slot += timedelta(minutes=15)

    return liberi


async def get_appuntamenti_promemoria() -> list[Appuntamento]:
    """Appuntamenti confermati nelle prossime 24h con reminder non ancora inviato."""
    ora = datetime.utcnow()
    tra_24h = ora + timedelta(hours=24)
    async with async_session() as session:
        result = await session.execute(
            select(Appuntamento).where(
                and_(
                    Appuntamento.stato == "confermato",
                    Appuntamento.reminder_inviato == False,  # noqa: E712
                    Appuntamento.data_ora >= ora,
                    Appuntamento.data_ora <= tra_24h,
                )
            )
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


async def guardar_mensaje(telefono: str, role: str, content: str):
    async with async_session() as session:
        mensaje = Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow()
        )
        session.add(mensaje)
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()
        mensajes.reverse()
        return [{"role": msg.role, "content": msg.content} for msg in mensajes]


async def limpiar_historial(telefono: str):
    async with async_session() as session:
        query = select(Mensaje).where(Mensaje.telefono == telefono)
        result = await session.execute(query)
        mensajes = result.scalars().all()
        for msg in mensajes:
            await session.delete(msg)
        await session.commit()
