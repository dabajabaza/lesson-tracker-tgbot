"""Middleware: единица работы + сериализация апдейтов одного пользователя.

Одна область запроса = одна транзакция. Сессию отдаёт dishka (см. di.py),
функции repo НЕ коммитят — фиксирует DbSessionMiddleware, причём внутри
пер-пользовательского замка. Замок обязан накрывать коммит: уехав в закрытие
скоупа, коммит оказался бы за пределами замка, и второй апдейт того же
пользователя (двойной тап) читал бы старый баланс до фиксации первого.

Ответ пользователю уходит ДО коммита — сознательный разворот старого принципа
«коммит до отправки». Старый порядок при сбое отправки давал дубль оплаты:
запись уже есть, пользователь видит ошибку и вводит сумму снова. Новый при
сбое отправки откатывает всё разом (повтор безопасен), а теряет данные только
при отказе самого коммита — на локальном SQLite это умерший диск, не рабочий
режим. Для денег дубль хуже потери.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import Message, TelegramObject
from dishka import AsyncContainer
from sqlalchemy.ext.asyncio import AsyncSession

from . import access

log = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    """Сессия из REQUEST-скоупа dishka + замок + коммит.

    Регистрировать ПОСЛЕ ContainerMiddleware (нужен data["dishka_container"]).
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _run(self, handler, event, data):
        container: AsyncContainer = data["dishka_container"]
        session = await container.get(AsyncSession)
        data["session"] = session
        try:
            result = await handler(event, data)
        except Exception:
            # Откат здесь, а не только в провайдере: обработчик ошибок aiogram
            # живёт снаружи скоупа и успел бы отправить «попробуйте ещё раз»
            # раньше, чем провайдер откатил бы хвост.
            await session.rollback()
            raise
        await session.commit()
        return result

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await self._run(handler, event, data)
        # aiogram обрабатывает апдейты конкурентно; замок сериализует
        # read-modify-write одного пользователя (двойной тап по «Списать урок»).
        async with self._locks[user.id]:
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
