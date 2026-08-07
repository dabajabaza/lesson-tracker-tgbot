"""Общие фикстуры.

Схема берётся из настоящих миграций, а не из create_all: приближение умеет
незаметно разойтись с тем, что выполняется в проде, и тогда зелёные тесты
перестают что-либо доказывать. Миграции гоняются один раз за сессию, каждому
тесту достаётся копия готового файла.
"""

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.__main__ import build_dispatcher
from bot.admin import router as admin_router
from bot.handlers import router as main_router
from tests.bot_harness import FAKE_BOT_TOKEN, BotHarness, RecordingSession
from tests.schema import apply_migrations

# Роутеры объявлены синглтонами на уровне модуля — для прода это верно, там
# диспетчер собирается ровно один раз. Но Router может быть привязан только к
# одному родителю за свою жизнь, а каждый тест собирает диспетчер заново, так
# что между тестами их надо отцеплять.
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
async def engine(db_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


@pytest_asyncio.fixture
async def harness(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[BotHarness]:
    """Полноценный диспетчер — собранный той же функцией, что и в проде, — и
    подделка сети вместо Telegram.

    Ради этой фикстуры и затевался PR: тесты, дёргающие middleware в вакууме,
    пропустили взаимную блокировку сессии запроса и FSM-хранилища.
    """
    session = RecordingSession()
    bot = Bot(token=FAKE_BOT_TOKEN, session=session)

    dp = build_dispatcher(sessionmaker, TEST_ADMIN_IDS)
    await dp.emit_startup()
    try:
        yield BotHarness(bot=bot, dp=dp, session=session)
    finally:
        await dp.emit_shutdown()
        for router in _SHARED_ROUTERS:
            router._parent_router = None  # noqa: SLF001
