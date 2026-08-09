"""Подмена FSM-контекста на хранилище общей сессии запроса."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import TelegramObject
from dishka import AsyncContainer


class FsmSessionMiddleware(BaseMiddleware):
    """Подменяет FSM-контекст на хранилище общей сессии запроса.

    Встроенный FSMContextMiddleware aiogram собирает data["state"] поверх
    хранилища диспетчера — у нас это read-only-обёртка (см. bot/storage.py).
    Здесь контекст пересобирается с тем же ключом, но поверх request-скоупного
    SqlAlchemyStorage: записи состояния ложатся в ту же транзакцию, что и
    бизнес-изменения. FSMContext — это только пара (storage, key), подмена
    законна и дешева.

    Регистрировать ПОСЛЕ DbSessionMiddleware — не по зависимости данных, а
    чтобы запрошенное здесь хранилище получило ту же сессию из кэша скоупа.

    Здесь же обновляется data["raw_state"] — снимок, по которому aiogram
    сопоставляет StateFilter. Его снимает FSMContextMiddleware до входа в
    замок, поэтому при двух быстрых сообщениях подряд второе матчилось по
    состоянию, которое первое уже успело сбросить: обработчик Flow.new_price
    выигрывал фильтр, не находил имени в данных и уводил диалог обратно на
    «Введите имя ученика» — сразу после того, как ученик успешно создан.
    Перечитываем внутри замка, тем же хранилищем, что увидит обработчик.
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
            data["raw_state"] = await storage.get_state(state.key)
        return await handler(event, data)
