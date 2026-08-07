"""Хвостовой роутер: отвечает на нераспознанные колбэки.

Подключается последним. Без него кнопка с незнакомой callback_data оставляла бы
у пользователя вечно «крутящийся» индикатор: Telegram ждёт ответа на каждый
callback_query.
"""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery
from dishka import FromDishka

from ..ui import Responder

router = Router()
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


@router.callback_query()
async def on_unknown_callback(cb: CallbackQuery, ui: FromDishka[Responder]) -> None:
    ui.callback(cb)
