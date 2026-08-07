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


BASELINE_REVISION = "2b20bfa13e12"


def test_существующая_база_штампуется_и_догоняет_голову(tmp_path):
    """Боевая база на момент перехода: схема базовой ревизии есть, а записи в
    alembic_version нет. Базовая миграция обязана распознать это и ничего не
    создавать (иначе выкатка упала бы на «table already exists»), а следующие —
    нормально накатиться поверх, не тронув данные преподавателя.

    Состояние собирается самой базовой миграцией, а не create_all по текущим
    моделям: те уже включают таблицы более поздних ревизий, и получилась бы
    невозможная база — схема на голове, но без штампа.
    """
    db_path = tmp_path / "existing.db"

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, BASELINE_REVISION)
        # Убираем штамп — так выглядела боевая база до появления alembic.
        with engine.begin() as conn:
            conn.exec_driver_sql("drop table alembic_version")
        # Запись кладём через ORM: умолчания колонок заданы на стороне Python,
        # и перечислять их сырым SQL пришлось бы заново при каждой правке модели.
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
            tables = inspect(conn).get_table_names()
    finally:
        engine.dispose()

    assert [r[0] for r in students] == ["Лера"], "данные обязаны уцелеть"
    assert "processed_updates" in tables, "поздние миграции обязаны накатиться поверх"
    assert _diff_against_models(db_path) == [], "и схема должна сойтись с моделями"


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


def test_миграция_переживает_остаток_от_откаченного_релиза(tmp_path):
    """На боевой базе processed_updates уже существует: её создал через
    create_all релиз v0.2.0 с первой, откаченной версией идемпотентности. Откат
    вернул код, но таблицу не удалил, а базовая миграция её не описывала.

    Без распознавания этой ситуации выкатка падала бы на «table already exists»
    прямо на рестарте бота — проверено на копии боевой базы.
    """
    db_path = tmp_path / "leftover.db"

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, BASELINE_REVISION)
        # Остаток от откаченного релиза: таблица есть, ревизия про неё не знает.
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "create table processed_updates ("
                " update_id BIGINT NOT NULL, created_at BIGINT NOT NULL,"
                " PRIMARY KEY (update_id))"
            )
            conn.exec_driver_sql("insert into processed_updates values (42, 1)")
    finally:
        engine.dispose()

    apply_migrations(f"sqlite:///{db_path}")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            marks = list(conn.exec_driver_sql("select update_id from processed_updates"))
            revision = list(conn.exec_driver_sql("select version_num from alembic_version"))
    finally:
        engine.dispose()

    assert [r[0] for r in marks] == [42], "прежние отметки терять незачем"
    assert revision[0][0] != BASELINE_REVISION, "ревизия обязана догнать голову"
