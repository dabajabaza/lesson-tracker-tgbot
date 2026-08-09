from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def apply_migrations(sync_url: str, revision: str = "head") -> None:
    """Прогоняет `alembic upgrade` до ревизии по синхронному SQLite-адресу.

    revision по умолчанию — голова; тесты миграций данных задают конкретную,
    чтобы подготовить базу «как до выкатки» и прогнать догоняющий шаг.

    Живое синхронное соединение передаётся в env.py через
    `config.attributes["connection"]`. Так тесты обходят онлайн-путь env.py,
    который поднимает свой цикл событий (из работающего его не позвать), и
    заодно не требуют BOT_TOKEN — load_config без него падает.

    Главное же: тесты получают ту же схему, что и прод. Приближение через
    create_all умеет незаметно разойтись с миграциями, и тогда зелёные тесты
    ничего не значат.
    """
    engine = create_engine(sync_url)
    try:
        with engine.begin() as connection:
            cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, revision)
    finally:
        engine.dispose()
