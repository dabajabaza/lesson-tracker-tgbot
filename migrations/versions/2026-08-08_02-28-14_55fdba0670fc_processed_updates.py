"""processed updates

Отметки об уже применённых апдейтах Telegram: защита от повторной обработки при
повторной доставке (см. bot.models.ProcessedUpdate).

Revision ID: 55fdba0670fc
Revises: 2b20bfa13e12
Create Date: 2026-08-08 02:28:14.283657

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "55fdba0670fc"
down_revision: str | Sequence[str] | None = "2b20bfa13e12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # На боевой базе эта таблица уже есть — её создал через create_all релиз
    # v0.2.0 с первой, откаченной версией идемпотентности. Откат вернул код, но
    # таблицу, разумеется, не удалил, а базовая миграция её не описывала: она
    # снимала схему более раннего состояния. Без этой проверки первая же
    # выкатка упала бы на «table already exists» прямо на рестарте бота.
    #
    # Пересоздавать нечего: схема остатка совпадает с ожидаемой до буквы
    # (проверено на копии боевой базы), а лежащие там отметки вчерашних
    # апдейтов только к лучшему — их повтор и должен отбрасываться.
    if sa.inspect(op.get_bind()).has_table("processed_updates"):
        return

    op.create_table(
        "processed_updates",
        sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("update_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("processed_updates")
