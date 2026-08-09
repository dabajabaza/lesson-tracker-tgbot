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
from ..ui import Responder
from ..views import card_view
from ._common import as_int, menu, money_error, owner, target_of

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


@router.callback_query(F.data.startswith("pay:"))
async def on_pay(
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
    await state.set_state(Flow.payment_amount)
    await state.update_data(student_id=s.id, prompt_id=msg.message_id)
    note = f"\nДенежный остаток {format_money(s.remainder)} будет учтён." if s.remainder > 0 else ""
    ui.edit(
        msg,
        f"💵 {s.name}\nСтоимость занятия: {format_money(s.price)}.{note}\n\nВведите сумму оплаты:",
        cancel_kb(),
        prompt_for=state.key,
    )
    ui.callback(cb)


@router.message(Flow.payment_amount)
async def on_payment_amount(
    message: Message,
    state: FSMContext,
    payments: FromDishka[PaymentService],
    students: FromDishka[StudentService],
    prefs: FromDishka[ViewPrefService],
    ui: FromDishka[Responder],
) -> None:
    text = (message.text or "").strip()
    if not text:
        ui.reply(message, "Отправьте, пожалуйста, текстовое сообщение.", reply_markup=cancel_kb())
        return
    parsed = parse_money_strict(text)
    # parsed.value is None вне error-ветки не бывает, но типам это неизвестно.
    if parsed.error or parsed.value is None:
        ui.reply(
            message,
            money_error("amount", parsed.error or "format", "1600"),
            reply_markup=cancel_kb(),
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
    ui.delete(message.chat.id, data.get("prompt_id"))
    if not res:
        # НЕ `text`: эта переменная выше — проверенный ввод пользователя, и
        # перепривязка молча подсовывала бы текст экрана любому будущему
        # логу/аудиту суммы ниже по функции.
        menu_text, kb = await menu(students, prefs, owner(message))
        ui.reply(message, menu_text, reply_markup=kb)
        return
    lines = [f"✅ Оплата {format_money(parsed.value)} внесена."]
    if res.prev_remainder > 0:
        lines.append(f"Учтён прежний остаток: {format_money(res.prev_remainder)}.")
    lines.append(f"Добавлено: {res.lessons} {lessons_word(res.lessons)}.")
    if res.remainder > 0:
        lines.append(
            f"Денежный остаток: {format_money(res.remainder)} — будет учтён при следующей оплате."
        )
    ui.confirm(
        message,
        "\n".join(lines) + "\n\n" + render_card(res.student),
        reply_markup=card_kb(res.student.id),
    )


async def _shift(
    cb: CallbackQuery,
    ui: Responder,
    students: StudentService,
    apply,
    toast,
) -> None:
    """Общая форма списания и возврата: оба меняют баланс на один урок и
    отчитываются карточкой плюс тостом. Различаются ровно двумя вещами —
    методом сервиса и текстом тоста, — и держать это двумя почти одинаковыми
    обработчиками значило чинить их по очереди и однажды забыть один.
    """
    target = target_of(cb, ui)
    if target is None:
        return
    msg, sid = target
    s = await apply(cb.from_user.id, sid)
    if not s:
        ui.callback(cb, "Ученик не найден", show_alert=True)
        return
    # durable: карточка здесь — единственное подтверждение операции с балансом,
    # тост гаснет сам и в очередь не идёт.
    ui.edit(msg, *await card_view(students, cb.from_user.id, s.id), durable=True)
    ui.callback(cb, toast(s))


@router.callback_query(F.data.startswith("charge:"))
async def on_charge(
    cb: CallbackQuery,
    payments: FromDishka[PaymentService],
    students: FromDishka[StudentService],
    ui: FromDishka[Responder],
) -> None:
    await _shift(
        cb,
        ui,
        students,
        payments.charge,
        lambda s: (
            f"⚠️ Урок списан. Долг: {abs(s.balance)}"
            if s.balance < 0
            else f"➖ Урок списан. Осталось: {s.balance}"
        ),
    )


@router.callback_query(F.data.startswith("refund:"))
async def on_refund(
    cb: CallbackQuery,
    payments: FromDishka[PaymentService],
    students: FromDishka[StudentService],
    ui: FromDishka[Responder],
) -> None:
    await _shift(
        cb,
        ui,
        students,
        payments.refund,
        lambda s: f"↩️ Урок возвращён. Осталось: {s.balance}",
    )
