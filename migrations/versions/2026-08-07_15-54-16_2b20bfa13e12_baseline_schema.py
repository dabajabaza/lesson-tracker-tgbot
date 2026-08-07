"""baseline schema

Базовая миграция: повторяет схему, которая до сих пор создавалась через
Base.metadata.create_all. Существующие данные не трогает.

Ключевая деталь — проверка инспектором в начале upgrade(). Боевая база уже
содержит все эти таблицы (10 учеников, 109 операций), но записи в
alembic_version у неё нет. Без проверки первый же запуск попытался бы создать
существующие таблицы и упал бы прямо на выкатке. С ней база «штампуется» сама:
alembic отметит ревизию применённой, ничего не изменив. На пустой базе (тесты,
новая установка) миграция работает как обычно и создаёт схему.

Revision ID: 2b20bfa13e12
Revises:
Create Date: 2026-08-07 15:54:16.596201

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2b20bfa13e12"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    if sa.inspect(op.get_bind()).has_table("students"):
        # Схема уже на месте — это существующая боевая база. Только отмечаем
        # ревизию применённой (это делает сам alembic после выхода отсюда).
        return

    op.create_table(
        "allowed_users",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("invited_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "fsm",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "invites",
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("used_by", sa.BigInteger(), nullable=True),
        sa.Column("used_at", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=True),
        sa.Column("lessons_delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("remainder_after", sa.BigInteger(), nullable=False),
        sa.Column("new_price", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_before", sa.JSON(), nullable=False),
        sa.Column("undone", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.create_index("idx_operations_owner_undone", ["owner_id", "undone"], unique=False)
        batch_op.create_index("idx_operations_student", ["student_id"], unique=False)

    op.create_table(
        "students",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_lower", sa.String(), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False),
        sa.Column("remainder", sa.BigInteger(), nullable=False),
        sa.Column("last_payment_at", sa.BigInteger(), nullable=True),
        sa.Column("last_payment_amount", sa.BigInteger(), nullable=True),
        sa.Column("last_payment_lessons", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name_lower", name="uq_students_owner_name"),
    )
    op.create_table(
        "ui_prefs",
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("sort", sa.String(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("ui_prefs")
    op.drop_table("students")
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.drop_index("idx_operations_student")
        batch_op.drop_index("idx_operations_owner_undone")

    op.drop_table("operations")
    op.drop_table("invites")
    op.drop_table("fsm")
    op.drop_table("allowed_users")
