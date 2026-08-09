import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from lesson_tracker.config import load_config
from lesson_tracker.models import Base  # импорт модуля регистрирует все модели на Base.metadata

# Значение-заглушка из alembic.ini: означает «URL не задан».
_ALEMBIC_INI_PLACEHOLDER = "driver://user:pass@localhost/dbname"

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite не умеет ALTER TABLE в объёме, нужном alembic: batch-режим
        # пересоздаёт таблицу вместо изменения на месте.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# Тесты передают живое синхронное соединение через config.attributes, минуя
# создание движка и чтение окружения. Благодаря этому alembic в тестах работает
# без BOT_TOKEN (load_config без него падает), а fileConfig не перебивает
# перехват логов pytest'ом.
injected_connection = config.attributes.get("connection")

if injected_connection is not None:
    do_run_migrations(injected_connection)
else:
    if config.config_file_name is not None:
        # disable_existing_loggers=False обязателен. По умолчанию fileConfig
        # НАВСЕГДА выключает все уже созданные логгеры — а миграции в проде
        # выполняются внутри процесса бота (см. __main__.main), после первого
        # logging.basicConfig. Без флага lesson_tracker.*, aiogram.* и watchdog
        # замолкают на весь срок жизни процесса, и второй basicConfig этого не
        # чинит: отказы доступа, «апдейт уже применён», сбои доставки и предупреждения
        # сторожа просто исчезают из журнала. В тестах ветка не выполняется
        # (там передаётся готовое соединение), поэтому дефект жил незамеченным.
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    # Только если вызывающий не задал URL сам. Безусловная перезапись делала
    # аргумент `_run_migrations(db_url)` мёртвым и разворачивала любой ручной
    # прогон alembic на боевую базу: оператор копировал файл, запускал
    # `alembic downgrade -1` над копией — и ронял таблицы в проде.
    if config.get_main_option("sqlalchemy.url") in (None, "", _ALEMBIC_INI_PLACEHOLDER):
        config.set_main_option("sqlalchemy.url", load_config().db_url)
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
