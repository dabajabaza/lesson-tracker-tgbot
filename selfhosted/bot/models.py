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


class UiPref(Base):
    """Настройки отображения списка (сортировка/страница) — чтобы «⬅️ К списку»
    не сбрасывал выбранную сортировку и позицию (п.15 ТЗ)."""

    __tablename__ = "ui_prefs"

    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sort: Mapped[str] = mapped_column(String, nullable=False, default="name")
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class FsmRecord(Base):
    """Персистентное состояние диалога (FSM). В памяти состояние терялось бы при
    рестарте сервиса (в т.ч. после сна ноутбука) — незавершённый ввод пропадал."""

    __tablename__ = "fsm"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str | None] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class AllowedUser(Base):
    """Белый список доступа. Бот отвечает только админам (из ADMIN_IDS) и тем,
    кто здесь, — иначе любой нашедший бота в поиске стал бы арендатором и
    сыпал спамом.

    Админов тут нет: их даёт конфиг, а не таблица. Строка появляется только у
    того, кого впустили явно — через /allow или погашенный инвайт."""

    __tablename__ = "allowed_users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_ts)
    # Кто впустил: id админа при /allow, автор инвайта при переходе по ссылке.
    invited_by: Mapped[int | None] = mapped_column(BigInteger)


class Invite(Base):
    """Одноразовый код-приглашение, живёт ограниченное время.

    Гасится переходом по deep-link `/start <код>`. Одноразовость держится не
    проверкой в коде, а UPDATE ... WHERE used_by IS NULL (см. access.py):
    два одновременных перехода иначе могли бы погасить один код дважды."""

    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_ts)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_by: Mapped[int | None] = mapped_column(BigInteger)
    used_at: Mapped[int | None] = mapped_column(BigInteger)
