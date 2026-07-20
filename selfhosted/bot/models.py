"""Модели БД. Деньги — в копейках (int), время — unix-секунды.

Мультитенантность: owner_id = Telegram user id. Каждый пользователь бота видит
и меняет только свои строки — все запросы репозитория фильтруются по owner_id.
FK намеренно не объявляем (как и в serverless-версии): целостность — в коде.
"""

import time

from sqlalchemy import BigInteger, Boolean, Integer, JSON, String, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_ts() -> int:
    return int(time.time())


class Base(DeclarativeBase):
    pass


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Имя в нижнем регистре — уникальность без учёта регистра (п.11 ТЗ), в рамках владельца.
    name_lower: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remainder: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_payment_at: Mapped[int | None] = mapped_column(BigInteger)
    last_payment_amount: Mapped[int | None] = mapped_column(BigInteger)
    last_payment_lessons: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_ts)

    __table_args__ = (
        UniqueConstraint("owner_id", "name_lower", name="uq_students_owner_name"),
    )

    def snapshot(self) -> dict:
        """Полная копия полей для отмены операции (п.16 ТЗ)."""
        return {
            "name": self.name,
            "name_lower": self.name_lower,
            "price": self.price,
            "balance": self.balance,
            "remainder": self.remainder,
            "last_payment_at": self.last_payment_at,
            "last_payment_amount": self.last_payment_amount,
            "last_payment_lessons": self.last_payment_lessons,
        }

    def restore(self, snap: dict) -> None:
        for k, v in snap.items():
            setattr(self, k, v)


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # payment|charge|refund|price_change
    amount: Mapped[int | None] = mapped_column(BigInteger)
    lessons_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    remainder_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_price: Mapped[int | None] = mapped_column(BigInteger)
    # Снимок состояния ученика ДО операции — основа отмены.
    snapshot_before: Mapped[dict] = mapped_column(JSON, nullable=False)
    undone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_ts)

    __table_args__ = (
        Index("idx_operations_student", "student_id"),
        Index("idx_operations_owner_undone", "owner_id", "undone"),
    )
