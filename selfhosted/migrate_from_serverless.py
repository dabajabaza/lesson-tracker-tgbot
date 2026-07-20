"""Разовый перенос данных из serverless-БД (Node/SQLite) в selfhosted (SQLAlchemy).

Схемы совместимы по именам колонок; отличия, которые учитываем:
- snapshot_before в serverless хранит ключи в camelCase → приводим к snake_case,
  иначе отмена перенесённых операций не восстановит поля (restore() по атрибутам);
- undone 0/1 → bool.

Запуск (из каталога selfhosted/, с активированным .venv):
    python migrate_from_serverless.py
Идемпотентность: если целевая БД уже содержит учеников — миграция отменяется.
"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import func, select

from bot.db import create_db, init_models
from bot.models import Operation, Student

ROOT = Path(__file__).resolve().parent
OLD_DB = ROOT.parent / "serverless" / "local" / "data.sqlite"
NEW_URL = f"sqlite+aiosqlite:///{ROOT / 'data.sqlite'}"

# camelCase (serverless) → snake_case (атрибуты модели selfhosted)
KEY_MAP = {
    "ownerId": "owner_id",
    "nameLower": "name_lower",
    "lastPaymentAt": "last_payment_at",
    "lastPaymentAmount": "last_payment_amount",
    "lastPaymentLessons": "last_payment_lessons",
    "createdAt": "created_at",
}


def _norm_snapshot(d: dict) -> dict:
    return {KEY_MAP.get(k, k): v for k, v in d.items()}


async def main() -> None:
    if not OLD_DB.exists():
        sys.exit(f"Не найдена исходная БД: {OLD_DB}")

    old = sqlite3.connect(f"file:{OLD_DB}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    students = old.execute("SELECT * FROM students").fetchall()
    operations = old.execute("SELECT * FROM operations ORDER BY id").fetchall()
    old.close()

    engine, sessionmaker = create_db(NEW_URL)
    await init_models(engine)
    async with sessionmaker() as session:
        if await session.scalar(select(func.count()).select_from(Student)):
            await engine.dispose()
            sys.exit("Целевая БД уже содержит учеников — миграция отменена.")

        for r in students:
            session.add(Student(
                id=r["id"], owner_id=r["owner_id"], name=r["name"],
                name_lower=r["name_lower"], price=r["price"], balance=r["balance"],
                remainder=r["remainder"], last_payment_at=r["last_payment_at"],
                last_payment_amount=r["last_payment_amount"],
                last_payment_lessons=r["last_payment_lessons"], created_at=r["created_at"],
            ))
        for r in operations:
            snap = json.loads(r["snapshot_before"]) if r["snapshot_before"] else {}
            session.add(Operation(
                id=r["id"], owner_id=r["owner_id"], student_id=r["student_id"],
                type=r["type"], amount=r["amount"], lessons_delta=r["lessons_delta"],
                balance_after=r["balance_after"], remainder_after=r["remainder_after"],
                new_price=r["new_price"], snapshot_before=_norm_snapshot(snap),
                undone=bool(r["undone"]), created_at=r["created_at"],
            ))
        await session.commit()

        n_students = await session.scalar(select(func.count()).select_from(Student))
        n_ops = await session.scalar(select(func.count()).select_from(Operation))
    await engine.dispose()
    print(f"Перенесено: учеников={n_students}, операций={n_ops}")


if __name__ == "__main__":
    asyncio.run(main())
