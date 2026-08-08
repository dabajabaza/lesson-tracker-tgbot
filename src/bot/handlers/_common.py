"""Общее для обработчиков: разбор callback_data, владелец, меню.

Формат callback_data намеренно оставлен строковым («card:5», «list:due:0»), а не
переведён на CallbackData-фабрики: в чатах живут сообщения со старыми кнопками,
и смена формата превратила бы их все в «Кнопка устарела».
"""

from aiogram.types import CallbackQuery, Message

from ..models import fits_in_db
from ..money import MAX_MONEY, format_money
from ..services import StudentService, ViewPrefService
from ..views import main_menu_view


def as_int(value) -> int | None:
    """Безопасный разбор аргумента callback_data (его может подделать клиент).

    Проверяется и величина, а не только форма. Python-числа безразмерны, и
    `card:99999999999999999999999` (23 цифры — легко влезает в лимит Telegram)
    проходил разбор, доезжал до SQLite и падал там на привязке параметра:
    вместо «Кнопка устарела» пользователь получал «Не получилось выполнить
    действие» и трейсбек в журнале.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if fits_in_db(parsed) else None


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


def target_of(cb: CallbackQuery, ui) -> tuple[Message, int] | None:
    """Сообщение под кнопкой и id из callback_data — или None с внятным отказом.

    Четыре строки этой проверки были скопированы в восьми обработчиках; именно
    она защищает от подделанного клиентом callback_data, то есть место, где
    пропущенная копия обходится дороже всего. Побочно здесь же гасятся
    «часики»: без ответа кнопка крутилась бы вечно.
    """
    msg = message_of(cb)
    _cmd, a1, _a2 = parts_of(cb)
    sid = as_int(a1)
    if msg is None or sid is None:
        ui.callback(cb, "Кнопка устарела", show_alert=True)
        return None
    return msg, sid


def screen_of(cb: CallbackQuery, ui) -> Message | None:
    """Сообщение под кнопкой без аргументов в callback_data — или None.

    Тот же гард, что target_of, но для кнопок без id. Трёхстрочная копия жила
    в одиннадцати обработчиках; менять поведение для недоступного сообщения
    (скажем, отвечать «Сообщение слишком старое» вместо молчаливого гашения)
    значило бы править одиннадцать мест и промахнуться в одном.
    """
    msg = message_of(cb)
    if msg is None:
        ui.callback(cb)
        return None
    return msg


async def show_menu(cb: CallbackQuery, ui, students, prefs, toast: str | None = None) -> None:
    """Перерисовать главное меню под кнопкой и погасить «часики».

    Пять обработчиков (домой, отмена, обе пустые ветки отмены операции, отказ
    от отмены) отличались только текстом тоста; шаг, добавленный в один из
    них, молча не доезжал бы до остальных.
    """
    msg = screen_of(cb, ui)
    if msg is None:
        return
    ui.edit(msg, *await menu(students, prefs, cb.from_user.id))
    ui.callback(cb, toast)


def message_of(cb: CallbackQuery) -> Message | None:
    """Сообщение под кнопкой, если с ним ещё можно работать.

    isinstance, а не проверка на None: помимо отсутствующего сообщения бывает
    InaccessibleMessage (старше 48 ч и т.п.) — у него нет ни edit_text, ни answer.
    """
    return cb.message if isinstance(cb.message, Message) else None


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
