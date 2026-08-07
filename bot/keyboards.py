"""Inline-клавиатуры и схема callback data.

Схема:
  home | list:<sort>:<page> | card:<id> | pay:<id> | charge:<id> | refund:<id>
  price:<id> | hist:<id>:<page> | add | search | sortmenu | undo
  undo_yes:<opId> | undo_no | export | exp_xlsx | exp_csv | cancel | noop
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .render import list_button_label

PAGE_SIZE = 10
HISTORY_PAGE_SIZE = 10


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _pagination_row(prefix: str, page: int, total_pages: int) -> list[InlineKeyboardButton] | None:
    """Только реальные стрелки — Telegram отклоняет кнопки с пустым текстом."""
    if total_pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(_btn("⬅️", f"{prefix}:{page - 1}"))
    row.append(_btn(f"{page + 1}/{total_pages}", "noop"))
    if page < total_pages - 1:
        row.append(_btn("➡️", f"{prefix}:{page + 1}"))
    return row


def main_menu_kb(students, sort: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = [[_btn(list_button_label(s), f"card:{s.id}")] for s in students]
    pagination = _pagination_row(f"list:{sort}", page, total_pages)
    if pagination:
        rows.append(pagination)
    rows.append([_btn("➕ Добавить", "add"), _btn("🔍 Поиск", "search"), _btn("↕️ Сортировка", "sortmenu")])
    rows.append([_btn("↩️ Отменить действие", "undo"), _btn("📤 Экспорт", "export")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_kb(sid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("➕ Внести оплату", f"pay:{sid}")],
            [_btn("➖ Списать урок", f"charge:{sid}"), _btn("↩️ Вернуть урок", f"refund:{sid}")],
            [_btn("✏️ Изменить стоимость", f"price:{sid}"), _btn("📜 История", f"hist:{sid}:0")],
            [_btn("⬅️ К списку", "home")],
        ]
    )


def history_kb(sid: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    pagination = _pagination_row(f"hist:{sid}", page, total_pages)
    if pagination:
        rows.append(pagination)
    rows.append([_btn("⬅️ К карточке", f"card:{sid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sort_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🔤 По имени", "list:name:0")],
            [_btn("📚 По остатку занятий", "list:bal:0")],
            [_btn("⏳ Скоро потребуется оплата", "list:due:0")],
            [_btn("⬅️ Назад", "home")],
        ]
    )


def undo_confirm_kb(op_id: int) -> InlineKeyboardMarkup:
    # op_id в data — отменяем именно показанную операцию, а не «последнюю на момент клика».
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn("✅ Да, отменить", f"undo_yes:{op_id}"), _btn("❌ Нет", "undo_no")]]
    )


def export_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📊 Excel (.xlsx)", "exp_xlsx")],
            [_btn("📄 CSV (.csv)", "exp_csv")],
            [_btn("⬅️ Назад", "home")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("❌ Отмена", "cancel")]])


def home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ К списку", "home")]])
