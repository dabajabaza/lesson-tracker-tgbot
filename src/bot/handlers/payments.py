"""Оплата, списание и возврат занятия."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from dishka import FromDishka

from ..keyboards import cancel_kb, card_kb
from ..money import format_money, parse_money_strict
from ..render import lessons_word, render_card
from ..services import PaymentService, StudentService, ViewPrefService
from ..states import Flow
from ..views import card_view
from ._common import as_int, delete_quietly, edit, menu, message_of, money_error, owner, parts_of

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


@router.callback_query(F.data.startswith("pay:"))
async def on_pay(
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
    await state.set_state(Flow.payment_amount)
    await state.update_data(student_id=s.id, prompt_id=msg.message_id)
    note = f"\nДенежный остаток {format_money(s.remainder)} будет учтён." if s.remainder > 0 else ""
    await edit(
        msg,
        f"💵 {s.name}\nСтоимость занятия: {format_money(s.price)}.{note}\n\nВведите сумму оплаты:",
        cancel_kb(),
    )
    await cb.answer()


@router.message(Flow.payment_amount)
async def on_payment_amount(
    message: Message,
    state: FSMContext,
    payments: FromDishka[PaymentService],
    students: FromDishka[StudentService],
    prefs: FromDishka[ViewPrefService],
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Отправьте, пожалуйста, текстовое сообщение.", reply_markup=cancel_kb()
        )
        return
    parsed = parse_money_strict(text)
    # parsed.value is None вне error-ветки не бывает, но типам это неизвестно.
    if parsed.error or parsed.value is None:
        await message.answer(
            money_error("amount", parsed.error or "format", "1600"), reply_markup=cancel_kb()
        )
        return
    data = await state.get_data()
    # student_id мог не пережить потерю состояния — тогда ведём себя как при
    # «ученик не найден» ниже: показываем меню.
    student_id = as_int(data.get("student_id"))
    res = (
        await payments.apply(owner(message), student_id, parsed.value)
        if student_id is not None
        else None
    )
    await state.clear()
    await delete_quietly(message.bot, message.chat.id, data.get("prompt_id"))
    if not res:
        text, kb = await menu(students, prefs, owner(message))
        await message.answer(text, reply_markup=kb)
        return
    lines = [f"✅ Оплата {format_money(parsed.value)} внесена."]
    if res.prev_remainder > 0:
        lines.append(f"Учтён прежний остаток: {format_money(res.prev_remainder)}.")
    lines.append(f"Добавлено: {res.lessons} {lessons_word(res.lessons)}.")
    if res.remainder > 0:
        lines.append(
            f"Денежный остаток: {format_money(res.remainder)} — будет учтён при следующей оплате."
        )
    await message.answer(
        "\n".join(lines) + "\n\n" + render_card(res.student), reply_markup=card_kb(res.student.id)
    )


@router.callback_query(F.data.startswith("charge:"))
async def on_charge(
    cb: CallbackQuery, payments: FromDishka[PaymentService], students: FromDishka[StudentService]
) -> None:
    msg = message_of(cb)
    _cmd, a1, _a2 = parts_of(cb)
    sid = as_int(a1)
    if msg is None or sid is None:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    s = await payments.charge(cb.from_user.id, sid)
    if not s:
        await cb.answer("Ученик не найден", show_alert=True)
        return
    await edit(msg, *await card_view(students, cb.from_user.id, s.id))
    await cb.answer(
        f"⚠️ Урок списан. Долг: {abs(s.balance)}"
        if s.balance < 0
        else f"➖ Урок списан. Осталось: {s.balance}"
    )


@router.callback_query(F.data.startswith("refund:"))
async def on_refund(
    cb: CallbackQuery, payments: FromDishka[PaymentService], students: FromDishka[StudentService]
) -> None:
    msg = message_of(cb)
    _cmd, a1, _a2 = parts_of(cb)
    sid = as_int(a1)
    if msg is None or sid is None:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    s = await payments.refund(cb.from_user.id, sid)
    if not s:
        await cb.answer("Ученик не найден", show_alert=True)
        return
    await edit(msg, *await card_view(students, cb.from_user.id, s.id))
    await cb.answer(f"↩️ Урок возвращён. Осталось: {s.balance}")
