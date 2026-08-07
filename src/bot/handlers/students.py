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
from ..views import card_view, search_results_view
from ._common import as_int, delete_quietly, edit, menu, message_of, money_error, owner, parts_of

MAX_NAME_LEN = 80

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


# ---------- карточка ----------


@router.callback_query(F.data.startswith("card:"))
async def on_card(cb: CallbackQuery, students: FromDishka[StudentService]) -> None:
    msg = message_of(cb)
    _cmd, a1, _a2 = parts_of(cb)
    sid = as_int(a1)
    if msg is None or sid is None:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    await edit(msg, *await card_view(students, cb.from_user.id, sid))
    await cb.answer()


# ---------- добавление ученика ----------


@router.callback_query(F.data == "add")
async def on_add(cb: CallbackQuery, state: FSMContext) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    await state.set_state(Flow.new_name)
    await state.update_data(prompt_id=msg.message_id)
    await edit(msg, "➕ Введите имя нового ученика:", cancel_kb())
    await cb.answer()


@router.message(Flow.new_name)
async def on_new_name(
    message: Message, state: FSMContext, students: FromDishka[StudentService]
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите имя ученика:", reply_markup=cancel_kb())
        return
    name = re.sub(r"\s+", " ", text).strip()
    if len(name) > MAX_NAME_LEN:
        await message.answer("⚠️ Слишком длинное имя. Введите короче:", reply_markup=cancel_kb())
        return
    if await students.find_by_name(owner(message), name):
        await message.answer(
            f"⚠️ Ученик с именем «{name}» уже существует. Введите другое имя:",
            reply_markup=cancel_kb(),
        )
        return
    data = await state.get_data()
    await delete_quietly(message.bot, message.chat.id, data.get("prompt_id"))
    sent = await message.answer(
        f"Имя: {name}\n\nТеперь введите стоимость одного занятия, например: 1600",
        reply_markup=cancel_kb(),
    )
    await state.set_state(Flow.new_price)
    await state.update_data(name=name, prompt_id=sent.message_id)


@router.message(Flow.new_price)
async def on_new_price(
    message: Message, state: FSMContext, students: FromDishka[StudentService]
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите стоимость занятия:", reply_markup=cancel_kb())
        return
    parsed = parse_money_strict(text)
    if parsed.error or parsed.value is None:
        await message.answer(
            money_error("price", parsed.error or "format", "1600"), reply_markup=cancel_kb()
        )
        return
    data = await state.get_data()
    name = data.get("name")
    if not isinstance(name, str) or not name:
        # Состояние потерялось между шагами — просим имя заново, а не роняем
        # обработчик на name=None внутри сервиса.
        await state.set_state(Flow.new_name)
        await message.answer("Введите имя ученика:", reply_markup=cancel_kb())
        return
    student = await students.create(owner(message), name, parsed.value)
    if not student:
        await state.set_state(Flow.new_name)
        await message.answer(
            f"⚠️ Имя «{name}» уже занято. Введите другое имя:", reply_markup=cancel_kb()
        )
        return
    await state.clear()
    await delete_quietly(message.bot, message.chat.id, data.get("prompt_id"))
    await message.answer(
        f"✅ Ученик добавлен.\n\n{render_card(student)}", reply_markup=card_kb(student.id)
    )


# ---------- изменение стоимости ----------


@router.callback_query(F.data.startswith("price:"))
async def on_price(
    cb: CallbackQuery, state: FSMContext, students: FromDishka[StudentService]
) -> None:
    msg = message_of(cb)
    _cmd, a1, _a2 = parts_of(cb)
    sid = as_int(a1)
    if msg is None or sid is None:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    s = await students.get(cb.from_user.id, sid)
    if not s:
        await cb.answer("Ученик не найден", show_alert=True)
        return
    await state.set_state(Flow.price_change)
    await state.update_data(student_id=s.id, prompt_id=msg.message_id)
    await edit(
        msg,
        f"✏️ {s.name}\nТекущая стоимость: {format_money(s.price)}.\n\n"
        "Введите новую стоимость занятия:",
        cancel_kb(),
    )
    await cb.answer()


@router.message(Flow.price_change)
async def on_price_change(
    message: Message,
    state: FSMContext,
    students: FromDishka[StudentService],
    prefs: FromDishka[ViewPrefService],
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите новую стоимость занятия:", reply_markup=cancel_kb())
        return
    parsed = parse_money_strict(text)
    if parsed.error or parsed.value is None:
        await message.answer(
            money_error("price", parsed.error or "format", "1800"), reply_markup=cancel_kb()
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
    await delete_quietly(message.bot, message.chat.id, data.get("prompt_id"))
    if not s:
        text, kb = await menu(students, prefs, owner(message))
        await message.answer(text, reply_markup=kb)
        return
    await message.answer(
        f"✅ Стоимость изменена: {format_money(parsed.value)}.\n"
        f"Применяется только к будущим оплатам.\n\n{render_card(s)}",
        reply_markup=card_kb(s.id),
    )


# ---------- поиск ----------


@router.callback_query(F.data == "search")
async def on_search_start(cb: CallbackQuery, state: FSMContext) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    await state.set_state(Flow.search)
    await state.update_data(prompt_id=msg.message_id)
    await edit(msg, "🔍 Введите имя или его часть:", cancel_kb())
    await cb.answer()


@router.message(Flow.search)
async def on_search(
    message: Message, state: FSMContext, students: FromDishka[StudentService]
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите имя или его часть:", reply_markup=cancel_kb())
        return
    found = await students.search(owner(message), text)
    data = await state.get_data()
    await state.clear()
    await delete_quietly(message.bot, message.chat.id, data.get("prompt_id"))
    view_text, kb = search_results_view(found, text)
    await message.answer(view_text, reply_markup=kb)
