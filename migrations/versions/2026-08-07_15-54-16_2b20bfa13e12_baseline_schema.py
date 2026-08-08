"""baseline schema

Базовая миграция: повторяет схему, которая до сих пор создавалась через
Base.metadata.create_all. Существующие данные не трогает.

Ключевая деталь — проверка инспектором перед КАЖДОЙ таблицей. Боевая база уже
содержит эти таблицы (10 учеников, 109 операций), но записи в alembic_version у
неё нет. Без проверки первый же запуск попытался бы создать существующие
таблицы и упал бы прямо на выкатке. С ней база «штампуется» сама: alembic
отметит ревизию применённой, ничего не изменив. На пустой базе (тесты, новая
установка) миграция работает как обычно и создаёт схему.

Проверка именно по каждой таблице, а не одна на всю ревизию. Прежняя версия
спрашивала про `students` и при её наличии выходила целиком — то есть считала,
что раз есть одна таблица, есть и остальные шесть. База с ЧАСТЬЮ схемы
штамповалась на head с навсегда отсутствующими таблицами, и починить её
`upgrade head` уже не мог: ревизия числится применённой. Это не гипотеза —
ровно в таком состоянии оказалась dev-база репозитория: `students` и
`operations` на месте, `allowed_users` и `invites` нет, и каждый апдейт умирал
в AccessMiddleware на «no such table: allowed_users».

Revision ID: 2b20bfa13e12
Revises:
Create Date: 2026-08-07 15:54:16.596201

"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2b20bfa13e12"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_missing(name: str, *args: object) -> None:
    """Создать таблицу, если её ещё нет — и сказать об этом в лог.

    Лог не для красоты: при разборе инцидента с частичной схемой первым же
    вопросом было «а что именно миграция создала на боевом файле» — и ответа
    нигде не осталось.
    """
    if sa.inspect(op.get_bind()).has_table(name):
        return
    op.create_table(name, *args)
    logging.getLogger("alembic.runtime.migration").info("Создана таблица %s", name)


def upgrade() -> None:
    """Upgrade schema."""
    _create_missing(
        "allowed_users",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("invited_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
    )
    _create_missing(
        "fsm",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    _create_missing(
        "invites",
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("used_by", sa.BigInteger(), nullable=True),
        sa.Column("used_at", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("code"),
    )
    _create_missing(
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
    # Индексы тоже поимённо: таблица могла существовать без них.
    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("operations")}
    with op.batch_alter_table("operations", schema=None) as batch_op:
        if "idx_operations_owner_undone" not in existing:
            batch_op.create_index(
                "idx_operations_owner_undone", ["owner_id", "undone"], unique=False
            )
        if "idx_operations_student" not in existing:
            batch_op.create_index("idx_operations_student", ["student_id"], unique=False)

    _create_missing(
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
    _create_missing(
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
