"""Карточка ученика, добавление, изменение стоимости, поиск."""

import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from dishka import FromDishka

from ..keyboards import cancel_kb, card_kb
from ..money import format_money, parse_money_strict
from ..render import render_card
from ..services import StudentService, ViewPrefService
from ..states import Flow
from ..ui import Responder
from ..views import card_view, search_results_view
from ._common import as_int, menu, message_of, money_error, owner, target_of

MAX_NAME_LEN = 80

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


# ---------- карточка ----------


@router.callback_query(F.data.startswith("card:"))
async def on_card(
    cb: CallbackQuery, students: FromDishka[StudentService], ui: FromDishka[Responder]
) -> None:
    target = target_of(cb, ui)
    if target is None:
        return
    msg, sid = target
    ui.edit(msg, *await card_view(students, cb.from_user.id, sid))
    ui.callback(cb)


# ---------- добавление ученика ----------


@router.callback_query(F.data == "add")
async def on_add(cb: CallbackQuery, state: FSMContext, ui: FromDishka[Responder]) -> None:
    msg = message_of(cb)
    if msg is None:
        ui.callback(cb)
        return
    await state.set_state(Flow.new_name)
    await state.update_data(prompt_id=msg.message_id)
    ui.edit(msg, "➕ Введите имя нового ученика:", cancel_kb())
    ui.callback(cb)


@router.message(Flow.new_name)
async def on_new_name(
    message: Message,
    state: FSMContext,
    students: FromDishka[StudentService],
    ui: FromDishka[Responder],
) -> None:
    text = (message.text or "").strip()
    if not text:
        ui.reply(message, "Введите имя ученика:", reply_markup=cancel_kb())
        return
    name = re.sub(r"\s+", " ", text).strip()
    if len(name) > MAX_NAME_LEN:
        ui.reply(message, "⚠️ Слишком длинное имя. Введите короче:", reply_markup=cancel_kb())
        return
    if await students.find_by_name(owner(message), name):
        ui.reply(
            message,
            f"⚠️ Ученик с именем «{name}» уже существует. Введите другое имя:",
            reply_markup=cancel_kb(),
        )
        return
    data = await state.get_data()
    ui.delete(message.chat.id, data.get("prompt_id"))
    await state.set_state(Flow.new_price)
    await state.update_data(name=name)
    # Единственное место, где нужен id ЕЩЁ НЕ отправленного сообщения: этот
    # ответ сам становится подсказкой, которую следующий шаг обязан убрать.
    # Responder допишет prompt_id в состояние сразу после отправки.
    ui.reply(
        message,
        f"Имя: {name}\n\nТеперь введите стоимость одного занятия, например: 1600",
        reply_markup=cancel_kb(),
        prompt_for=state.key,
    )


@router.message(Flow.new_price)
async def on_new_price(
    message: Message,
    state: FSMContext,
    students: FromDishka[StudentService],
    ui: FromDishka[Responder],
) -> None:
    text = (message.text or "").strip()
    if not text:
        ui.reply(message, "Введите стоимость занятия:", reply_markup=cancel_kb())
        return
    parsed = parse_money_strict(text)
    if parsed.error or parsed.value is None:
        ui.reply(
            message,
            money_error("price", parsed.error or "format", "1600"),
            reply_markup=cancel_kb(),
        )
        return
    data = await state.get_data()
    name = data.get("name")
    if not isinstance(name, str) or not name:
        # Состояние потерялось между шагами — просим имя заново, а не роняем
        # обработчик на name=None внутри сервиса.
        await state.set_state(Flow.new_name)
        ui.reply(message, "Введите имя ученика:", reply_markup=cancel_kb())
        return
    student = await students.create(owner(message), name, parsed.value)
    if not student:
        await state.set_state(Flow.new_name)
        ui.reply(
            message, f"⚠️ Имя «{name}» уже занято. Введите другое имя:", reply_markup=cancel_kb()
        )
        return
    await state.clear()
    ui.delete(message.chat.id, data.get("prompt_id"))
    ui.confirm(
        message,
        f"✅ Ученик добавлен.\n\n{render_card(student)}",
        reply_markup=card_kb(student.id),
    )


# ---------- изменение стоимости ----------


@router.callback_query(F.data.startswith("price:"))
async def on_price(
    cb: CallbackQuery,
    state: FSMContext,
    students: FromDishka[StudentService],
    ui: FromDishka[Responder],
) -> None:
    target = target_of(cb, ui)
    if target is None:
        return
    msg, sid = target
    s = await students.get(cb.from_user.id, sid)
    if not s:
        ui.callback(cb, "Ученик не найден", show_alert=True)
        return
    await state.set_state(Flow.price_change)
    await state.update_data(student_id=s.id, prompt_id=msg.message_id)
    ui.edit(
        msg,
        f"✏️ {s.name}\nТекущая стоимость: {format_money(s.price)}.\n\n"
        "Введите новую стоимость занятия:",
        cancel_kb(),
    )
    ui.callback(cb)


@router.message(Flow.price_change)
async def on_price_change(
    message: Message,
    state: FSMContext,
    students: FromDishka[StudentService],
    prefs: FromDishka[ViewPrefService],
    ui: FromDishka[Responder],
) -> None:
    text = (message.text or "").strip()
    if not text:
        ui.reply(message, "Введите новую стоимость занятия:", reply_markup=cancel_kb())
        return
    parsed = parse_money_strict(text)
    if parsed.error or parsed.value is None:
        ui.reply(
            message,
            money_error("price", parsed.error or "format", "1800"),
            reply_markup=cancel_kb(),
        )
        return
    data = await state.get_data()
    student_id = as_int(data.get("student_id"))
    s = (
        await students.change_price(owner(message), student_id, parsed.value)
        if student_id is not None
        else None
    )
    await state.clear()
    ui.delete(message.chat.id, data.get("prompt_id"))
    if not s:
        text, kb = await menu(students, prefs, owner(message))
        ui.reply(message, text, reply_markup=kb)
        return
    ui.confirm(
        message,
        f"✅ Стоимость изменена: {format_money(parsed.value)}.\n"
        f"Применяется только к будущим оплатам.\n\n{render_card(s)}",
        reply_markup=card_kb(s.id),
    )


# ---------- поиск ----------


@router.callback_query(F.data == "search")
async def on_search_start(cb: CallbackQuery, state: FSMContext, ui: FromDishka[Responder]) -> None:
    msg = message_of(cb)
    if msg is None:
        ui.callback(cb)
        return
    await state.set_state(Flow.search)
    await state.update_data(prompt_id=msg.message_id)
    ui.edit(msg, "🔍 Введите имя или его часть:", cancel_kb())
    ui.callback(cb)


@router.message(Flow.search)
async def on_search(
    message: Message,
    state: FSMContext,
    students: FromDishka[StudentService],
    ui: FromDishka[Responder],
) -> None:
    text = (message.text or "").strip()
    if not text:
        ui.reply(message, "Введите имя или его часть:", reply_markup=cancel_kb())
        return
    found = await students.search(owner(message), text)
    data = await state.get_data()
    await state.clear()
    ui.delete(message.chat.id, data.get("prompt_id"))
    view_text, kb = search_results_view(found, text)
    ui.reply(message, view_text, reply_markup=kb)
