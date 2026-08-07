"""Подключение к БД. По умолчанию SQLite (aiosqlite); URL можно указать любой,
поддерживаемый SQLAlchemy async — в т.ч. PostgreSQL (asyncpg) при переезде (п.19 ТЗ)."""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


def create_db(db_url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_url, echo=False)
    if db_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            # WAL + busy_timeout: устойчивость к «database is locked» при
            # параллельных апдейтах.
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
            # Отключаем собственное управление транзакциями у драйвера pysqlite.
            # Без этого SAVEPOINT сломан: драйвер не открывает настоящую
            # транзакцию, RELEASE SAVEPOINT фактически фиксирует запись, и
            # последующий session.rollback() её уже не отменяет. Проверено:
            # ученик, созданный через begin_nested, переживал откат — то есть
            # «одна область запроса = одна транзакция» переставала быть правдой
            # ровно там, где транзакция нужнее всего. См. tests/test_unit_of_work.
            dbapi_conn.isolation_level = None

        @event.listens_for(engine.sync_engine, "begin")
        def _sqlite_begin(conn):  # noqa: ANN001
            # Раз драйвер больше не начинает транзакции сам, начинаем явно.
            conn.exec_driver_sql("BEGIN")

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessionmaker


async def init_models(engine: AsyncEngine) -> None:
    """Создаёт таблицы через метаданные.

    Бот этим больше НЕ пользуется: схему приводит к голове alembic при старте.
    Осталось только ради разового `migrate_from_serverless.py` (выполнен при
    переезде 2026-07-21). Не звать из нового кода — create_all рядом с
    миграциями и есть тот самый дрейф схемы, который потом ищут часами.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
