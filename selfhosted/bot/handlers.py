"""Обработчики сообщений и колбэков. Мультитенантность: owner_id = from_user.id."""

import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from . import repo
from .export_data import (
    history_rows,
    rows_to_csv,
    rows_to_xlsx,
    students_rows,
)
from .keyboards import (
    cancel_kb,
    card_kb,
    export_kb,
    sort_menu_kb,
    undo_confirm_kb,
)
from .money import MAX_MONEY, format_money, parse_money_strict
from .render import describe_operation, lessons_word, render_card
from .states import Flow
from .views import card_view, history_view, main_menu_view, search_results_view

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


# ---------- утилиты ----------

def _money_error(kind: str, error: str, example: str) -> str:
    noun = "Стоимость" if kind == "price" else "Сумма"
    acc = "стоимость" if kind == "price" else "сумму"
    if error == "range":
        return f"⚠️ Слишком большая {noun.lower()} — максимум {format_money(MAX_MONEY)}. Введите {acc} поменьше:"
    if error == "zero":
        return f"⚠️ {noun} должна быть больше нуля. Введите число, например: {example}"
    return f"⚠️ Это не похоже на {acc}. Введите число в рублях, например: {example}"


async def _delete_quietly(bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass  # сообщение уже удалено — не критично


# ---------- команды и текстовый ввод (FSM) ----------

@router.message(Command("start", "menu"))
async def on_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    text, kb = await main_menu_view(session, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.message(Flow.payment_amount)
async def on_payment_amount(message: Message, session: AsyncSession, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправьте, пожалуйста, текстовое сообщение.", reply_markup=cancel_kb())
        return
    parsed = parse_money_strict(text)
    if parsed.error:
        await message.answer(_money_error("amount", parsed.error, "1600"), reply_markup=cancel_kb())
        return
    data = await state.get_data()
    res = await repo.apply_payment(session, message.from_user.id, data.get("student_id"), parsed.value)
    await state.clear()
    if not res:
        text, kb = await main_menu_view(session, message.from_user.id)
        await message.answer(text, reply_markup=kb)
        return
    await _delete_quietly(message.bot, message.chat.id, data.get("prompt_id"))
    lines = [f"✅ Оплата {format_money(parsed.value)} внесена."]
    if res.prev_remainder > 0:
        lines.append(f"Учтён прежний остаток: {format_money(res.prev_remainder)}.")
    lines.append(f"Добавлено: {res.lessons} {lessons_word(res.lessons)}.")
    if res.remainder > 0:
        lines.append(f"Денежный остаток: {format_money(res.remainder)} — будет учтён при следующей оплате.")
    await message.answer("\n".join(lines) + "\n\n" + render_card(res.student), reply_markup=card_kb(res.student.id))


@router.message(Flow.new_name)
async def on_new_name(message: Message, session: AsyncSession, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите имя ученика:", reply_markup=cancel_kb())
        return
    name = re.sub(r"\s+", " ", text).strip()
    if len(name) > 80:
        await message.answer("⚠️ Слишком длинное имя. Введите короче:", reply_markup=cancel_kb())
        return
    if await repo.find_by_name_lower(session, message.from_user.id, name):
        await message.answer(f"⚠️ Ученик с именем «{name}» уже существует. Введите другое имя:", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    await _delete_quietly(message.bot, message.chat.id, data.get("prompt_id"))
    sent = await message.answer(
        f"Имя: {name}\n\nТеперь введите стоимость одного занятия, например: 1600",
        reply_markup=cancel_kb(),
    )
    await state.set_state(Flow.new_price)
    await state.update_data(name=name, prompt_id=sent.message_id)


@router.message(Flow.new_price)
async def on_new_price(message: Message, session: AsyncSession, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите стоимость занятия:", reply_markup=cancel_kb())
        return
    parsed = parse_money_strict(text)
    if parsed.error:
        await message.answer(_money_error("price", parsed.error, "1600"), reply_markup=cancel_kb())
        return
    data = await state.get_data()
    student = await repo.create_student(session, message.from_user.id, data.get("name"), parsed.value)
    if not student:
        await state.set_state(Flow.new_name)
        await message.answer(f"⚠️ Имя «{data.get('name')}» уже занято. Введите другое имя:", reply_markup=cancel_kb())
        return
    await state.clear()
    await _delete_quietly(message.bot, message.chat.id, data.get("prompt_id"))
    await message.answer(f"✅ Ученик добавлен.\n\n{render_card(student)}", reply_markup=card_kb(student.id))


@router.message(Flow.price_change)
async def on_price_change(message: Message, session: AsyncSession, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите новую стоимость занятия:", reply_markup=cancel_kb())
        return
    parsed = parse_money_strict(text)
    if parsed.error:
        await message.answer(_money_error("price", parsed.error, "1800"), reply_markup=cancel_kb())
        return
    data = await state.get_data()
    s = await repo.change_price(session, message.from_user.id, data.get("student_id"), parsed.value)
    await state.clear()
    if not s:
        text, kb = await main_menu_view(session, message.from_user.id)
        await message.answer(text, reply_markup=kb)
        return
    await _delete_quietly(message.bot, message.chat.id, data.get("prompt_id"))
    await message.answer(
        f"✅ Стоимость изменена: {format_money(parsed.value)}.\n"
        f"Применяется только к будущим оплатам.\n\n{render_card(s)}",
        reply_markup=card_kb(s.id),
    )


@router.message(Flow.search)
async def on_search(message: Message, session: AsyncSession, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите имя или его часть:", reply_markup=cancel_kb())
        return
    found = await repo.search_students(session, message.from_user.id, text)
    data = await state.get_data()
    await state.clear()
    await _delete_quietly(message.bot, message.chat.id, data.get("prompt_id"))
    view_text, kb = search_results_view(found, text)
    await message.answer(view_text, reply_markup=kb)


@router.message(StateFilter(None))
async def on_any_message(message: Message, session: AsyncSession) -> None:
    # Вне диалога любое сообщение показывает главное меню.
    text, kb = await main_menu_view(session, message.from_user.id)
    await message.answer(text, reply_markup=kb)


# ---------- колбэки (навигация по кнопкам) ----------

@router.callback_query()
async def on_callback(cb: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    uid = cb.from_user.id
    msg = cb.message
    if msg is None:
        await cb.answer()
        return

    parts = (cb.data or "").split(":")
    cmd = parts[0]
    a1 = parts[1] if len(parts) > 1 else None
    a2 = parts[2] if len(parts) > 2 else None

    # Любое нажатие (кроме noop) прерывает незавершённый текстовый ввод — иначе
    # «Введите сумму» для ученика A переживёт переход к ученику B.
    if cmd != "noop":
        await state.clear()

    async def edit(text, kb) -> None:
        try:
            await msg.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return  # повторное нажатие — ничего не поменялось
            raise

    if cmd == "noop":
        await cb.answer()

    elif cmd == "home":
        await edit(*await main_menu_view(session, uid))
        await cb.answer()

    elif cmd == "list":
        await edit(*await main_menu_view(session, uid, a1, int(a2 or 0)))
        await cb.answer()

    elif cmd == "card":
        await edit(*await card_view(session, uid, int(a1)))
        await cb.answer()

    elif cmd == "hist":
        await edit(*await history_view(session, uid, int(a1), int(a2 or 0)))
        await cb.answer()

    elif cmd == "pay":
        s = await repo.get_student(session, uid, int(a1))
        if not s:
            await cb.answer("Ученик не найден", show_alert=True)
            return
        await state.set_state(Flow.payment_amount)
        await state.update_data(student_id=s.id, prompt_id=msg.message_id)
        note = f"\nДенежный остаток {format_money(s.remainder)} будет учтён." if s.remainder > 0 else ""
        await edit(
            f"💵 {s.name}\nСтоимость занятия: {format_money(s.price)}.{note}\n\nВведите сумму оплаты:",
            cancel_kb(),
        )
        await cb.answer()

    elif cmd == "charge":
        s = await repo.charge_lesson(session, uid, int(a1))
        if not s:
            await cb.answer("Ученик не найден", show_alert=True)
            return
        await edit(*await card_view(session, uid, s.id))
        await cb.answer(
            f"⚠️ Урок списан. Долг: {abs(s.balance)}" if s.balance < 0
            else f"➖ Урок списан. Осталось: {s.balance}"
        )

    elif cmd == "refund":
        s = await repo.refund_lesson(session, uid, int(a1))
        if not s:
            await cb.answer("Ученик не найден", show_alert=True)
            return
        await edit(*await card_view(session, uid, s.id))
        await cb.answer(f"↩️ Урок возвращён. Осталось: {s.balance}")

    elif cmd == "price":
        s = await repo.get_student(session, uid, int(a1))
        if not s:
            await cb.answer("Ученик не найден", show_alert=True)
            return
        await state.set_state(Flow.price_change)
        await state.update_data(student_id=s.id, prompt_id=msg.message_id)
        await edit(
            f"✏️ {s.name}\nТекущая стоимость: {format_money(s.price)}.\n\nВведите новую стоимость занятия:",
            cancel_kb(),
        )
        await cb.answer()

    elif cmd == "add":
        await state.set_state(Flow.new_name)
        await state.update_data(prompt_id=msg.message_id)
        await edit("➕ Введите имя нового ученика:", cancel_kb())
        await cb.answer()

    elif cmd == "search":
        await state.set_state(Flow.search)
        await state.update_data(prompt_id=msg.message_id)
        await edit("🔍 Введите имя или его часть:", cancel_kb())
        await cb.answer()

    elif cmd == "sortmenu":
        await edit("↕️ Выберите сортировку:", sort_menu_kb())
        await cb.answer()

    elif cmd == "cancel":
        await edit(*await main_menu_view(session, uid))
        await cb.answer("Отменено")

    elif cmd == "undo":
        op = await repo.peek_last_operation(session, uid)
        if not op:
            await cb.answer("Отменять нечего — операций ещё не было", show_alert=True)
            return
        s = await repo.get_student(session, uid, op.student_id)
        await edit(
            f"↩️ Отменить последнее действие?\n\n{describe_operation(op, s.name if s else '?')}",
            undo_confirm_kb(op.id),
        )
        await cb.answer()

    elif cmd == "undo_yes":
        res = await repo.undo_last_operation(session, uid, int(a1) if a1 else None)
        if res.status == "empty":
            await edit(*await main_menu_view(session, uid))
            await cb.answer("Отменять нечего", show_alert=True)
            return
        if res.status == "stale":
            await edit(*await main_menu_view(session, uid))
            await cb.answer(
                "⚠️ Появились новые операции — отмена не выполнена. Откройте «Отменить действие» ещё раз.",
                show_alert=True,
            )
            return
        await edit(*await card_view(session, uid, res.op.student_id))
        await cb.answer("✅ Действие отменено")

    elif cmd == "undo_no":
        await edit(*await main_menu_view(session, uid))
        await cb.answer()

    elif cmd == "export":
        await edit("📤 Что экспортировать?", export_kb())
        await cb.answer()

    elif cmd in ("exp_xlsx", "exp_csv"):
        await _send_export(cb, session, uid, "csv" if cmd == "exp_csv" else "xlsx")
        await cb.answer("Готово")

    else:
        await cb.answer()


async def _send_export(cb: CallbackQuery, session: AsyncSession, uid: int, fmt: str) -> None:
    """Экспорт учеников и истории документами (п.17–18 ТЗ).
    Каждый пользователь выгружает только свои данные."""
    students = await repo.list_students(session, uid, "name")
    ops = await repo.get_all_operations(session, uid)
    if not students and not ops:
        await cb.message.answer("Экспортировать нечего — данных пока нет.")
        return
    names = {s.id: s.name for s in students}
    srows = students_rows(students)
    hrows = history_rows(ops, names)
    if fmt == "csv":
        docs = [
            (rows_to_csv(srows), "students.csv", "👥 Ученики"),
            (rows_to_csv(hrows), "history.csv", "📜 История операций"),
        ]
    else:
        docs = [
            (rows_to_xlsx(srows, "Ученики"), "students.xlsx", "👥 Ученики"),
            (rows_to_xlsx(hrows, "История"), "history.xlsx", "📜 История операций"),
        ]
    for data, filename, caption in docs:
        await cb.message.answer_document(BufferedInputFile(data, filename), caption=caption)
