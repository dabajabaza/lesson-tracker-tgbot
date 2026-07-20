"""Подключение к БД. По умолчанию SQLite (aiosqlite); URL можно указать любой,
поддерживаемый SQLAlchemy async — в т.ч. PostgreSQL (asyncpg) при переезде (п.19 ТЗ)."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base


def make_sessionmaker(db_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(db_url, echo=False)
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_models(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Создаёт таблицы, которых ещё нет (простая авто-миграция для SQLite)."""
    engine = sessionmaker.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
