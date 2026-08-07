"""Middleware: единица работы + сериализация пишущих апдейтов.

Одна область запроса = одна транзакция. Сессию отдаёт dishka (см. di.py),
функции repo НЕ коммитят — фиксирует DbSessionMiddleware, причём внутри
замка. Замок обязан накрывать коммит: уехав в закрытие скоупа, коммит оказался
бы за его пределами, и следующий апдейт читал бы старый баланс до фиксации
предыдущего.

Замок один на приложение, а не по пользователю. Пер-пользовательский замок
сериализовал только апдейты одного владельца, а SQLite допускает одного писателя
на всю базу: два владельца, нажавшие кнопку одновременно, открывали DEFERRED-
транзакции поверх общего снимка, и второй получал мгновенный «database is
locked» на повышении блокировки — операция терялась молча. Глобальный замок
делает такую гонку невозможной внутри процесса; `BEGIN IMMEDIATE` (см. db.py)
остаётся страховкой на внешнего писателя — миграцию, ручной скрипт.

Ответ пользователю уходит ДО коммита — сознательный разворот старого принципа
«коммит до отправки». Старый порядок при сбое отправки давал дубль оплаты:
запись уже есть, пользователь видит ошибку и вводит сумму снова. Новый при
сбое отправки откатывает всё разом (повтор безопасен), а теряет данные только
при отказе самого коммита — на локальном SQLite это умерший диск, не рабочий
режим. Для денег дубль хуже потери.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import Message, TelegramObject
from dishka import AsyncContainer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import access
from .models import ProcessedUpdate, now_ts

log = logging.getLogger(__name__)

# Telegram держит неподтверждённые апдейты сутки, так что недели отметок с
# запасом хватает, а таблица не растёт бесконечно.
_MARK_TTL = 7 * 24 * 3600
_PRUNE_EVERY = 3600


class DbSessionMiddleware(BaseMiddleware):
    """Сессия из REQUEST-скоупа dishka + замок + коммит + идемпотентность.

    Регистрировать ПОСЛЕ ContainerMiddleware (нужен data["dishka_container"]).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pruned_at = 0.0

    async def _prune(self, session: AsyncSession) -> None:
        """Чистка старых отметок. Раз в час и ПОСЛЕ обработчика.

        Порядок важен: DELETE в начале забрал бы блокировку записи SQLite на всю
        обработку. Здесь он ложится вплотную к штатному коммиту.
        """
        if time.monotonic() - self._pruned_at < _PRUNE_EVERY:
            return
        self._pruned_at = time.monotonic()
        await session.execute(
            delete(ProcessedUpdate).where(ProcessedUpdate.created_at < now_ts() - _MARK_TTL)
        )

    async def _run(self, handler, event, data):
        container: AsyncContainer = data["dishka_container"]
        session = await container.get(AsyncSession)
        data["session"] = session

        # Middleware висит на dp.update, поэтому event — это сам Update.
        # getattr, а не прямое обращение: в тестах сюда прилетают и голые
        # объекты сообщений, у которых update_id нет.
        update_id = getattr(event, "update_id", None)
        if update_id is not None:
            seen = await session.scalar(
                select(ProcessedUpdate.update_id).where(ProcessedUpdate.update_id == update_id)
            )
            if seen is not None:
                log.warning("Апдейт %s уже применён — повтор отброшен", update_id)
                return None
            # Только add: отдельный коммит отметил бы апдейт обработанным ДО
            # того, как случилось изменение, и смерть процесса в этот промежуток
            # превратила бы риск дубля в риск потери. Строку зафиксирует общий
            # коммит ниже — вместе с бизнес-изменением, одной транзакцией.
            session.add(ProcessedUpdate(update_id=update_id))

        try:
            result = await handler(event, data)
        except Exception:
            # Откат здесь, а не только в провайдере: обработчик ошибок aiogram
            # живёт снаружи скоупа и успел бы отправить «попробуйте ещё раз»
            # раньше, чем провайдер откатил бы хвост.
            await session.rollback()
            raise
        await self._prune(session)
        await session.commit()
        return result

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # aiogram обрабатывает апдейты конкурентно, а писатель в SQLite один.
        # Замок берётся на любой апдейт без исключений: отметка идемпотентности
        # ставится даже там, где нет event_from_user, то есть пишут все.
        async with self._lock:
            return await self._run(handler, event, data)


class FsmSessionMiddleware(BaseMiddleware):
    """Подменяет FSM-контекст на хранилище общей сессии запроса.

    Встроенный FSMContextMiddleware aiogram собирает data["state"] поверх
    хранилища диспетчера — у нас это read-only-обёртка (см. storage.py).
    Здесь контекст пересобирается с тем же ключом, но поверх request-скоупного
    SqlAlchemyStorage: записи состояния ложатся в ту же транзакцию, что и
    бизнес-изменения. FSMContext — это только пара (storage, key), подмена
    законна и дешева.

    Регистрировать ПОСЛЕ DbSessionMiddleware — не по зависимости данных, а
    чтобы запрошенное здесь хранилище получило ту же сессию из кэша скоупа.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if state is not None:
            container: AsyncContainer = data["dishka_container"]
            storage = await container.get(BaseStorage)
            data["state"] = FSMContext(storage=storage, key=state.key)
        return await handler(event, data)


_MAX_TRACKED_IDS = 256


class _DenialLog:
    """Первый отказ для каждого id пишем как WARNING, последующие — как DEBUG.

    Иначе спам-бот забил бы лог. Множество ограничено сверху: при переполнении
    очищаем целиком — снова получим всплеск WARNING'ов, что для личного бота
    приемлемее, чем неограниченный рост памяти.
    """

    def __init__(self) -> None:
        self._seen: set[int] = set()

    def log(self, key: int, message: str, *args: object) -> None:
        if len(self._seen) >= _MAX_TRACKED_IDS:
            self._seen.clear()
        if key in self._seen:
            log.debug(message, *args)
        else:
            self._seen.add(key)
            log.warning(message, *args)


_denials = _DenialLog()


def _invite_code_from_start(event: TelegramObject) -> str | None:
    """Код из deep-link `/start <код>`, если он там есть."""
    if not isinstance(event, Message) or not event.text:
        return None
    if not event.text.startswith("/start"):
        return None
    parts = event.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


class AccessMiddleware(BaseMiddleware):
    """Молча отбрасывает апдейты от тех, кому нельзя.

    Единственный вход для постороннего — действующая одноразовая ссылка
    `/start <код>`. Всем остальным бот не отвечает ничего: сообщение вида
    «доступ запрещён» подтвердило бы, что бот жив, и приглашало бы долбиться
    дальше.

    Регистрировать ПОСЛЕ DbSessionMiddleware — берёт готовую сессию из data.
    Погашение инвайта не коммитится здесь: оно зафиксируется общим коммитом,
    только если апдейт обработан целиком. Упал обработчик — инвайт не сгорел,
    человек проходит по ссылке ещё раз.
    """

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        session: AsyncSession = data["session"]
        allowed = await access.is_allowed(session, self.admin_ids, user.id)

        invite_attempted = False
        if not allowed:
            code = _invite_code_from_start(event)
            invite_attempted = code is not None
            if code and await access.redeem_invite(session, code, user.id, user.username):
                allowed = True
                log.info("Инвайт погашен: user_id=%s username=%s", user.id, user.username)

        if not allowed:
            _denials.log(
                user.id,
                "Отказано в доступе: user_id=%s username=%s тип=%s инвайт=%s",
                user.id,
                user.username,
                type(event).__name__,
                invite_attempted,
            )
            return None

        return await handler(event, data)
