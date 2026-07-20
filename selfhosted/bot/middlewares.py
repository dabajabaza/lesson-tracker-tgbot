"""Middleware: сессия БД на апдейт + сериализация апдейтов одного пользователя.

Коммит выполняют сами мутирующие функции repo (до отправки в Telegram) — здесь
только выдаём сессию и откатываем незакоммиченный «хвост» при ошибке.

Пер-пользовательский Lock: aiogram обрабатывает апдейты конкурентно, поэтому
двойной тап (например «Списать урок») мог бы дать потерянное обновление баланса
(read-modify-write без блокировки). Lock сериализует всё по владельцу."""

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
