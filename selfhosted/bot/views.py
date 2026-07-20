"""Готовые экраны: (текст, клавиатура). Используются обработчиками."""

import math

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import repo
from .keyboards import (
    HISTORY_PAGE_SIZE,
    PAGE_SIZE,
    card_kb,
    history_kb,
    home_kb,
    main_menu_kb,
)
from .render import list_button_label, render_card, render_operation

SORT_LABELS = {"name": "по имени", "bal": "по остатку занятий", "due": "скоро оплата"}
MAX_SEARCH_RESULTS = 20


async def main_menu_view(session, owner_id, sort="name", page=0):
    students = await repo.list_students(session, owner_id, sort)
    if not students:
        return (
            "Учеников пока нет.\n\nНажмите «➕ Добавить», чтобы создать первую карточку.",
            main_menu_kb([], sort, 0, 1),
        )
    total_pages = max(1, math.ceil(len(students) / PAGE_SIZE))
    page = min(max(page, 0), total_pages - 1)
    chunk = students[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    text = f"👩‍🏫 Ученики: {len(students)}\nСортировка: {SORT_LABELS.get(sort, SORT_LABELS['name'])}"
    return text, main_menu_kb(chunk, sort, page, total_pages)


async def card_view(session, owner_id, sid):
    s = await repo.get_student(session, owner_id, sid)
    if not s:
        return "Ученик не найден.", home_kb()
    return render_card(s), card_kb(s.id)


async def history_view(session, owner_id, sid, page=0):
    s = await repo.get_student(session, owner_id, sid)
    if not s:
        return "Ученик не найден.", home_kb()
    total = await repo.count_history(session, owner_id, sid)
    if not total:
        return f"📜 {s.name}: операций ещё не было.", history_kb(sid, 0, 1)
    total_pages = max(1, math.ceil(total / HISTORY_PAGE_SIZE))
    page = min(max(page, 0), total_pages - 1)
    ops = await repo.get_history(session, owner_id, sid, page, HISTORY_PAGE_SIZE)
    body = "\n\n".join(render_operation(op) for op in ops)
    return f"📜 История: {s.name}\n\n{body}", history_kb(sid, page, total_pages)


def search_results_view(found, query):
    shown = found[:MAX_SEARCH_RESULTS]
    rows = [[InlineKeyboardButton(text=list_button_label(s), callback_data=f"card:{s.id}")] for s in shown]
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="home")])
    if not found:
        text = f"🔍 По запросу «{query}» никого не нашлось."
    elif len(found) > MAX_SEARCH_RESULTS:
        text = (
            f"🔍 Найдено по «{query}»: {len(found)}, показаны первые "
            f"{MAX_SEARCH_RESULTS}. Уточните запрос."
        )
    else:
        text = f"🔍 Найдено по «{query}»: {len(found)}"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)
