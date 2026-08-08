"""outbox

Revision ID: 5b3c10d2ced8
Revises: 55fdba0670fc
Create Date: 2026-08-08 03:47:52.200404

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b3c10d2ced8"
down_revision: str | Sequence[str] | None = "55fdba0670fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Гард, как у миграции processed_updates. Таблица могла остаться от
    # прошлой, откаченной попытки или от копии боевой базы: без проверки
    # первая же выкатка падает на «table outbox already exists» ещё до старта
    # бота, а Restart=always превращает это в бесконечный цикл рестартов.
    if sa.inspect(op.get_bind()).has_table("outbox"):
        return

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
