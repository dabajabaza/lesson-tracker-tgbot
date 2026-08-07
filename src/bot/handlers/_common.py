"""Общее для обработчиков: разбор callback_data, владелец, меню, правка сообщения.

Формат callback_data намеренно оставлен строковым («card:5», «list:due:0»), а не
переведён на CallbackData-фабрики: в чатах живут сообщения со старыми кнопками,
и смена формата превратила бы их все в «Кнопка устарела».
"""

import contextlib

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from ..money import MAX_MONEY, format_money
from ..services import StudentService, ViewPrefService
from ..views import main_menu_view


def as_int(value) -> int | None:
    """Безопасный разбор аргумента callback_data (его может подделать клиент)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parts_of(cb: CallbackQuery) -> tuple[str, str | None, str | None]:
    """Команда и до двух аргументов из callback_data."""
    chunks = (cb.data or "").split(":")
    return (
        chunks[0],
        chunks[1] if len(chunks) > 1 else None,
        chunks[2] if len(chunks) > 2 else None,
    )


def owner(message: Message) -> int:
    """Отправитель как владелец данных.

    В приватном чате from_user есть всегда (роутеры фильтруют по PRIVATE), но в
    общем случае aiogram допускает None — например, пост канала. Если такой
    апдейт всё же дойдёт сюда, честная ошибка со смыслом лучше, чем
    AttributeError из глубины обработчика.
    """
    user = message.from_user
    if user is None:
        raise RuntimeError("Сообщение без from_user прошло фильтр приватного чата")
    return user.id


def message_of(cb: CallbackQuery) -> Message | None:
    """Сообщение под кнопкой, если с ним ещё можно работать.

    isinstance, а не проверка на None: помимо отсутствующего сообщения бывает
    InaccessibleMessage (старше 48 ч и т.п.) — у него нет ни edit_text, ни answer.
    """
    return cb.message if isinstance(cb.message, Message) else None


async def edit(msg: Message, text: str, kb) -> None:
    """Правка сообщения, терпимая к повторному нажатию той же кнопки."""
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return  # ничего не поменялось — Telegram против, и он прав
        raise


async def menu(students: StudentService, prefs: ViewPrefService, owner_id: int):
    """Главное меню с учётом сохранённой сортировки/страницы пользователя."""
    sort, page = await prefs.get(owner_id)
    return await main_menu_view(students, owner_id, sort, page)


def money_error(kind: str, error: str, example: str) -> str:
    noun = "Стоимость" if kind == "price" else "Сумма"
    acc = "стоимость" if kind == "price" else "сумму"
    if error == "range":
        return (
            f"⚠️ Слишком большая {noun.lower()} — максимум {format_money(MAX_MONEY)}. "
            f"Введите {acc} поменьше:"
        )
    if error == "zero":
        return f"⚠️ {noun} должна быть больше нуля. Введите число, например: {example}"
    return f"⚠️ Это не похоже на {acc}. Введите число в рублях, например: {example}"


async def delete_quietly(bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    # Удаление косметическое: сообщение уже удалено / сеть — не критично.
    with contextlib.suppress(TelegramAPIError):
        await bot.delete_message(chat_id, message_id)
