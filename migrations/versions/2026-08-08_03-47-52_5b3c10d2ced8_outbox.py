"""outbox

Revision ID: 5b3c10d2ced8
Revises: 55fdba0670fc
Create Date: 2026-08-08 03:47:52.200404

"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b3c10d2ced8"
down_revision: str | Sequence[str] | None = "55fdba0670fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Колонки, без которых код с этой ревизией не работает. Гард обязан сверять
# ИМЕННО их, а не сам факт существования таблицы: остаток от откаченной
# попытки может нести другую схему, и «has_table → return» штамповал бы
# ревизию применённой поверх него. После этого каждый INSERT в outbox падал
# бы на «no column named attempts» — то есть каждая денежная операция
# откатывалась бы целиком, а `upgrade head` уже ничего не чинил: ревизия
# числится применённой. Ровно тот сценарий «частичная схема штампуется на
# head», от которого baseline-миграция защищается по каждой таблице.
_EXPECTED_COLUMNS = {"id", "method", "payload", "created_at", "attempts", "next_attempt_at"}


def upgrade() -> None:
    """Upgrade schema."""
    log = logging.getLogger("alembic.runtime.migration")
    inspector = sa.inspect(op.get_bind())

    if inspector.has_table("outbox"):
        existing = {col["name"] for col in inspector.get_columns("outbox")}
        if existing == _EXPECTED_COLUMNS:
            # Полный остаток (откаченная попытка, копия боевой базы) — можно
            # оставить: недоставленные строки в нём только к лучшему, их
            # дожмёт отправщик. Индекс проверяем отдельно, как baseline.
            indexes = {ix["name"] for ix in inspector.get_indexes("outbox")}
            if "ix_outbox_next_attempt" not in indexes:
                with op.batch_alter_table("outbox", schema=None) as batch_op:
                    batch_op.create_index(
                        "ix_outbox_next_attempt", ["next_attempt_at"], unique=False
                    )
            return
        # Схема разошлась — таблица от несовместимой версии. Её строки всё
        # равно недоставимы (другой формат), а оставить её значило бы ронять
        # каждую операцию. Пересоздаём, потеря содержимого здесь меньшее зло
        # и громко объявлена.
        log.warning(
            "Таблица outbox существует с несовместимыми колонками %s — пересоздаю",
            sorted(existing),
        )
        op.drop_table("outbox")

    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("outbox", schema=None) as batch_op:
        batch_op.create_index("ix_outbox_next_attempt", ["next_attempt_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("outbox", schema=None) as batch_op:
        batch_op.drop_index("ix_outbox_next_attempt")

    op.drop_table("outbox")
