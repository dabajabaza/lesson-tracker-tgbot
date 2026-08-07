"""Middleware: сессия БД на апдейт + сериализация апдейтов одного пользователя.

Коммит выполняют сами мутирующие функции repo (до отправки в Telegram) — здесь
только выдаём сессию и откатываем незакоммиченный «хвост» при ошибке.

Пер-пользовательский Lock: aiogram обрабатывает апдейты конкурентно, поэтому
двойной тап (например «Списать урок») мог бы дать потерянное обновление баланса
(read-modify-write без блокировки). Lock сериализует всё по владельцу."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import access

log = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _run(self, handler, event, data):
        async with self.sessionmaker() as session:
            data["session"] = session
            try:
                return await handler(event, data)
            except Exception:
                await session.rollback()  # откатить незакоммиченный хвост
                raise

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await self._run(handler, event, data)
        async with self._locks[user.id]:
            return await self._run(handler, event, data)


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
                await session.commit()
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
