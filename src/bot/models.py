"""Модели БД. Деньги — в копейках (int), время — unix-секунды.

Мультитенантность: owner_id = Telegram user id. Каждый пользователь бота видит
и меняет только свои строки — все запросы репозитория фильтруются по owner_id.
FK намеренно не объявляем (как и в serverless-версии): целостность — в коде.
"""

import time

from sqlalchemy import JSON, BigInteger, Boolean, Index, Integer, String, UniqueConstraint
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

    __table_args__ = (UniqueConstraint("owner_id", "name_lower", name="uq_students_owner_name"),)

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


class ProcessedUpdate(Base):
    """Отметка «этот апдейт уже применён» — защита от повторной обработки.

    Telegram считает апдейт доставленным только после подтверждения offset, а
    подтверждение уходит со СЛЕДУЮЩИМ вызовом getUpdates. Процесс, умерший
    между коммитом и этим вызовом (а деплой убивает бота намеренно), получит
    тот же update_id заново — и оплата применится дважды. Для учёта денег дубль
    хуже потери: пропавшее сообщение пользователь повторит сам, а лишнее
    списание тихо испортит баланс.

    Строка ложится в ту же транзакцию, что и бизнес-изменение: её кладёт в
    сессию middleware, а фиксирует общий коммит единицы работы. Промежуточного
    состояния не существует — либо записано и изменение, и отметка, либо ничего.

    Первая попытка (07.08.2026) была откачена: тогда FSM-хранилище писало в
    отдельной сессии, и незакоммиченная отметка забирала блокировку записи
    SQLite на всю обработку — бот падал с «database is locked». Теперь писатель
    один, и приём безопасен.
    """

    __tablename__ = "processed_updates"

    # autoincrement=False: id назначает Telegram, а не база.
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_ts)


class OutboxMessage(Base):
    """Обещание доставить сообщение, данное в той же транзакции, что и операция.

    Порядок «обработчик → коммит → отправка» (см. middlewares.py) снял потолок
    пропускной способности, но взамен разорвал связь между «операция применена»
    и «пользователь об этом узнал»: сбой сети после коммита оставлял человека в
    неведении. Для учёта денег это опасно ровно так же, как дубль, — не увидев
    подтверждения, преподаватель вводит сумму заново.

    Строка пишется вместе с бизнес-изменением, отправка удаляет её. Не удалось
    отправить — строка осталась, и её дожмёт фоновый отправщик (outbox.py).
    Гарантия получается «хотя бы один раз»: дубль ответа безобиден, потеря —
    нет.

    Кладётся сюда не всё, и делит не тип вызова, а смысл. Персистентно то, что
    несёт РЕЗУЛЬТАТ зафиксированной операции: подтверждение оплаты, карточка
    после списания. Не персистентно то, что рисует ЭКРАН: подсказка диалога,
    меню, список, замечание о вводе — доставленные через минуту, они приходят
    туда, откуда пользователь давно ушёл.

    В самой таблице при этом всегда лежит отправка сообщения, даже когда
    операцию подтверждала правка: воспроизвести позже можно только то, что
    ничего не затирает. У правки для этого есть запасное сообщение с тем же
    содержимым (см. ui.Responder.edit), и в очередь идёт именно оно.

    Прочее не попадает сюда по другим причинам: ответ на нажатие кнопки
    Telegram принимает считаные секунды, удаление подсказки косметическое, а
    выгрузка документа — мегабайты, которым нечего делать в базе, и повторить
    её пользователь может сам.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Имя класса метода aiogram («SendMessage») и его же payload в JSON.
    # Пара «имя + JSON», а не pickle: содержимое строки должно оставаться
    # читаемым глазами при разборе инцидента и переживать обновление кода.
    method: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_ts)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Момент, раньше которого повторять не стоит: backoff после неудачи.
    next_attempt_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=now_ts)

    __table_args__ = (Index("ix_outbox_next_attempt", "next_attempt_at"),)
