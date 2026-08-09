"""Общие фикстуры.

Схема берётся из настоящих миграций, а не из create_all: приближение умеет
незаметно разойтись с тем, что выполняется в проде, и тогда зелёные тесты
перестают что-либо доказывать. Миграции гоняются один раз за сессию, каждому
тесту достаётся копия готового файла.
"""

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiogram import Bot
from dishka import AsyncContainer
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from lesson_tracker.__main__ import build_dispatcher
from lesson_tracker.bot.handlers import router as main_router
from lesson_tracker.bot.handlers.admin import router as admin_router
from lesson_tracker.config import Config
from lesson_tracker.db.engine import create_db
from lesson_tracker.di import build_container
from lesson_tracker.services import HistoryService, PaymentService, StudentService, ViewPrefService
from tests.bot_harness import FAKE_BOT_TOKEN, BotHarness, RecordingSession
from tests.schema import apply_migrations

# Роутеры объявлены синглтонами на уровне модуля — для прода это верно, там
# диспетчер собирается ровно один раз. Но Router может быть привязан только к
# одному родителю за свою жизнь, а каждый тест собирает диспетчер заново, так
# что между тестами их надо отцеплять. Достаточно двух верхних: дочерние
# привязаны к общему родителю из handlers/__init__, а он между тестами живёт.
_SHARED_ROUTERS = [admin_router, main_router]

# 1 — «наш» пользователь диалоговых тестов; 111 и 222 — два преподавателя для
# тестов межтенантной изоляции. Посторонние (например 999) не входят и молча
# отбрасываются AccessMiddleware.
TEST_ADMIN_IDS = frozenset({1, 111, 222})


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Настоящая цепочка миграций, один раз на всю сессию тестов."""
    path = tmp_path_factory.mktemp("schema") / "template.db"
    apply_migrations(f"sqlite:///{path}")
    return path


@pytest.fixture
def db_path(migrated_template: Path, tmp_path: Path) -> Path:
    """Личная копия готовой схемы на каждый тест — это просто копирование
    файла, зато состояние между тестами не протекает."""
    path = tmp_path / "test.db"
    shutil.copyfile(migrated_template, path)
    return path


@pytest_asyncio.fixture
async def _db(db_path: Path) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Движок И фабрика сессий — оба из create_db, как в проде.

    Не create_async_engine напрямую: в create_db живут PRAGMA и — важнее —
    отключение собственного управления транзакциями у драйвера SQLite, без
    которого SAVEPOINT не откатывается. И фабрика тоже оттуда, а не собранная
    руками копия: пересоборка «по образцу» повторяет только те kwargs, о
    которых вспомнили, — добавь create_db какую-нибудь session-настройку, и
    тесты молча остались бы на старой семантике. Собери тесты своим движком —
    и они будут проверять не ту семантику транзакций, которая работает в бою.
    """
    eng, sm = create_db(f"sqlite+aiosqlite:///{db_path}")
    yield eng, sm
    await eng.dispose()


@pytest_asyncio.fixture
async def engine(_db) -> AsyncEngine:
    return _db[0]


@pytest_asyncio.fixture
async def sessionmaker(_db) -> async_sessionmaker[AsyncSession]:
    return _db[1]


@pytest_asyncio.fixture
async def session(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


# Сервисы поверх той же сессии. В бою их собирает dishka; в тестах бизнес-логики
# строим руками — так виднее, что сервис это просто класс над сессией, и не
# приходится тащить контейнер туда, где он не нужен.
@pytest.fixture
def students(session: AsyncSession) -> StudentService:
    return StudentService(session)


@pytest.fixture
def payments(session: AsyncSession, students: StudentService) -> PaymentService:
    return PaymentService(session, students)


@pytest.fixture
def history(session: AsyncSession, students: StudentService) -> HistoryService:
    return HistoryService(session, students)


@pytest.fixture
def prefs(session: AsyncSession) -> ViewPrefService:
    return ViewPrefService(session)


def make_config(db_path: Path) -> Config:
    return Config(
        token=FAKE_BOT_TOKEN,
        db_url=f"sqlite+aiosqlite:///{db_path}",
        proxy=None,
        admin_ids=TEST_ADMIN_IDS,
    )


@pytest_asyncio.fixture
async def container(db_path: Path) -> AsyncIterator[AsyncContainer]:
    """Настоящий dishka-контейнер поверх той же per-test базы, что и фикстура
    session (соединения разные — SQLite с WAL это переживает)."""
    c = build_container(make_config(db_path))
    yield c
    await c.close()


@pytest_asyncio.fixture
async def harness(container: AsyncContainer) -> AsyncIterator[BotHarness]:
    """Полноценный диспетчер — собранный той же функцией, что и в проде, — и
    подделка сети вместо Telegram.

    Ради этой фикстуры и затевался PR: тесты, дёргающие middleware в вакууме,
    пропустили взаимную блокировку сессии запроса и FSM-хранилища.
    """
    session = RecordingSession()
    bot = Bot(token=FAKE_BOT_TOKEN, session=session)

    # Тот же замок, что и у прода, — и ОН ЖЕ должен использоваться тестами
    # фонового отправщика: свежий Lock() в тесте расщепил бы единственного
    # писателя ровно так, как это запрещает L4.
    write_lock = asyncio.Lock()
    dp = build_dispatcher(container, TEST_ADMIN_IDS, write_lock)
    await dp.emit_startup()
    harness = BotHarness(bot=bot, dp=dp, session=session, write_lock=write_lock)
    try:
        yield harness
    finally:
        # Именно harness.dp, а не замкнутая локальная dp: тест пересборки
        # диспетчера подменяет harness.dp, и завершать надо ЖИВОЙ экземпляр —
        # старый тест уже погасил сам. Иначе старый гасился дважды, а новый
        # (с его shutdown-хуками, включая aiogram'овский fsm.close) не гасился
        # вовсе.
        await harness.dp.emit_shutdown()
        for router in _SHARED_ROUTERS:
            router._parent_router = None
