"""Сброс незавершённого текстового ввода при нажатии кнопки."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, TelegramObject


class ClearStateOnCallbackMiddleware(BaseMiddleware):
    """Любое нажатие, кроме «noop», прерывает начатый диалог.

    Без этого «Введите сумму оплаты» для ученика A пережило бы переход к
    ученику B, и следующее число ушло бы не туда. Правило общее для всех
    кнопок, поэтому живёт в middleware, а не повторяется в каждом обработчике:
    забыть его в новом обработчике теперь невозможно.

    Сброс идёт ДО обработчика — те, кто начинает свой диалог (оплата, цена,
    добавление, поиск), выставляют состояние уже после и не затираются.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if isinstance(event, CallbackQuery) and event.data != "noop" and state is not None:
            await state.clear()
        return await handler(event, data)
