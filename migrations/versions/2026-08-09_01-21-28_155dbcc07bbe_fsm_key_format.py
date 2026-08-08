"""fsm key format

Ключ строки FSM получил шестое поле — business_connection_id (см.
storage._key: без него ключ не однозначен). Формат сменился с
`bot:chat:user:thread:destiny` на `bot:chat:user:thread:business:destiny`,
и существующие строки остались бы под старым ключом навсегда: код их больше
не находит.

Это не косметика, а нарушение L3 — «незавершённый ввод переживает деплой».
Преподаватель, у которого на момент выкатки открыт диалог «Введите сумму
оплаты», получил бы вместо записанной оплаты главное меню: raw_state по
новому ключу пуст, StateFilter(None) выигрывает. Плюс осиротевшая строка
лежала бы в таблице вечно — TTL-уборки у fsm нет.

Переписываем ключи на месте. Только строки с ровно четырьмя двоеточиями
(старый формат): вставляем `None:` перед последним сегментом (destiny).
У бота business-апдейтов никогда не было (allowed_updates = message,
callback_query), так что None — не догадка, а знание.

Revision ID: 155dbcc07bbe
Revises: 5b3c10d2ced8
Create Date: 2026-08-09 01:21:28.951389

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "155dbcc07bbe"
down_revision: str | Sequence[str] | None = "5b3c10d2ced8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_PARTS = 5  # bot:chat:user:thread:destiny


def _upgraded(key: str) -> str:
    head, destiny = key.rsplit(":", 1)
    return f"{head}:None:{destiny}"


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT key FROM fsm")).fetchall()
    for (key,) in rows:
        if key.count(":") != _OLD_PARTS - 1:
            continue  # уже новый формат (или чужой — не трогаем)
        conn.execute(
            sa.text("UPDATE fsm SET key = :new WHERE key = :old"),
            {"new": _upgraded(key), "old": key},
        )


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT key FROM fsm")).fetchall()
    for (key,) in rows:
        parts = key.split(":")
        if len(parts) != _OLD_PARTS + 1 or parts[-2] != "None":
            continue
        old = ":".join(parts[:-2] + parts[-1:])
        conn.execute(
            sa.text("UPDATE fsm SET key = :new WHERE key = :old"),
            {"new": old, "old": key},
        )
