"""Схема из миграций обязана совпадать с моделями.

Без этого теста расхождение живёт незаметно: тесты гоняют одну схему, прод —
другую, и обе зелёные. Ровно этот класс ошибки D16 у соседнего бота описывает
как уже случившийся однажды.
"""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from bot.models import Base, Student  # импорт регистрирует все таблицы на Base.metadata
from tests.schema import PROJECT_ROOT, apply_migrations


def _diff_against_models(db_path) -> list:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            return compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()


def test_схема_из_миграций_совпадает_с_моделями(tmp_path):
    db_path = tmp_path / "schema.db"
    apply_migrations(f"sqlite:///{db_path}")

    assert _diff_against_models(db_path) == []


def test_базовая_миграция_не_трогает_существующую_базу(tmp_path):
    """Боевая база уже содержит все таблицы, но записи в alembic_version у неё
    нет. Базовая миграция обязана это распознать и ничего не создавать — иначе
    первая же выкатка упала бы на «table already exists», а данные преподавателя
    остались бы заложником отката."""
    db_path = tmp_path / "existing.db"

    # Изображаем боевую базу: схема есть, отметки о миграциях нет.
    # Запись кладём через ORM, а не сырым SQL: умолчания колонок заданы на
    # стороне Python, и перечислять их руками пришлось бы заново при каждой
    # правке модели.
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            s.add(Student(owner_id=1, name="Лера", name_lower="лера", price=160000))
            s.commit()
    finally:
        engine.dispose()

    apply_migrations(f"sqlite:///{db_path}")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            students = list(conn.exec_driver_sql("select name from students"))
            revision = list(conn.exec_driver_sql("select version_num from alembic_version"))
    finally:
        engine.dispose()

    assert [r[0] for r in students] == ["Лера"], "данные обязаны уцелеть"
    assert revision, "ревизия должна быть отмечена применённой"


def test_откат_до_нуля_и_обратно_чист(tmp_path):
    """Миграции применяются при старте бота, значит путь вниз — тоже боевой
    код, а не только аварийный выход."""
    db_path = tmp_path / "roundtrip.db"
    apply_migrations(f"sqlite:///{db_path}")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
            cfg.attributes["connection"] = connection
            command.downgrade(cfg, "base")
        with engine.connect() as connection:
            assert inspect(connection).get_table_names() == ["alembic_version"]
        with engine.begin() as connection:
            cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
    finally:
        engine.dispose()

    assert _diff_against_models(db_path) == []
