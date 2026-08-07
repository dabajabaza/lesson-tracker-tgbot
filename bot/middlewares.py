"""Middleware: сессия БД на апдейт + сериализация апдейтов одного пользователя.

Коммит выполняют сами мутирующие функции repo (до отправки в Telegram) — здесь
только выдаём сессию и откатываем незакоммиченный «хвост» при ошибке.

Пер-пользовательский Lock: aiogram обрабатывает апдейты конкурентно, поэтому
двойной тап (например «Списать урок») мог бы дать потерянное обновление баланса
(read-modify-write без блокировки). Lock сериализует всё по владельцу.

Идемпотентность: Telegram повторяет апдейт, если процесс умер до подтверждения
offset (подтверждение уходит только со следующим getUpdates). Защита — отметка
update_id, которая ложится в ту же транзакцию, что и бизнес-изменение."""

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import access
from .models import ProcessedUpdate

log = logging.getLogger(__name__)

# Telegram держит неподтверждённые апдейты сутки, так что недели отметок с запасом
# хватает, а таблица не растёт бесконечно.
_MARK_TTL = 7 * 24 * 3600
_PRUNE_EVERY = 3600


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pruned_at = 0.0

    async def _prune(self) -> None:
        """Чистка старых отметок. Отдельной сессией — чтобы DELETE ни при каком
        исходе не попал в транзакцию с бизнес-изменением и не смешался с откатом."""
        if time.monotonic() - self._pruned_at < _PRUNE_EVERY:
            return
        self._pruned_at = time.monotonic()
        async with self.sessionmaker() as session:
            await session.execute(
                delete(ProcessedUpdate).where(ProcessedUpdate.created_at < int(time.time()) - _MARK_TTL)
            )
            await session.commit()

    async def _run(self, handler, event, data):
        async with self.sessionmaker() as session:
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
                # Только add: коммитить здесь НЕЛЬЗЯ. Отдельный коммит отметил бы
                # апдейт обработанным до того, как случилось само изменение, и
                # смерть процесса в этот промежуток превратила бы риск дубля в
                # риск потери. Строку зафиксирует общий commit из repo — вместе
                # с бизнес-изменением, одной транзакцией.
                session.add(ProcessedUpdate(update_id=update_id))

            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()  # откатить незакоммиченный хвост
                raise
        await self._prune()
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
