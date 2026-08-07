"""История операций и отмена последнего действия."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery
from dishka import FromDishka

from ..keyboards import undo_confirm_kb
from ..render import describe_operation
from ..services import HistoryService, StudentService, ViewPrefService
from ..views import card_view, history_view
from ._common import as_int, edit, menu, message_of, parts_of

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


@router.callback_query(F.data.startswith("hist:"))
async def on_history(
    cb: CallbackQuery, students: FromDishka[StudentService], history: FromDishka[HistoryService]
) -> None:
    msg = message_of(cb)
    _cmd, a1, a2 = parts_of(cb)
    sid = as_int(a1)
    if msg is None or sid is None:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    await edit(msg, *await history_view(students, history, cb.from_user.id, sid, as_int(a2) or 0))
    await cb.answer()


@router.callback_query(F.data == "undo")
async def on_undo(
    cb: CallbackQuery, students: FromDishka[StudentService], history: FromDishka[HistoryService]
) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    op = await history.peek_last(cb.from_user.id)
    if not op:
        await cb.answer("Отменять нечего — операций ещё не было", show_alert=True)
        return
    s = await students.get(cb.from_user.id, op.student_id)
    await edit(
        msg,
        f"↩️ Отменить последнее действие?\n\n{describe_operation(op, s.name if s else '?')}",
        undo_confirm_kb(op.id),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("undo_yes"))
async def on_undo_yes(
    cb: CallbackQuery,
    students: FromDishka[StudentService],
    history: FromDishka[HistoryService],
    prefs: FromDishka[ViewPrefService],
) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    _cmd, a1, _a2 = parts_of(cb)
    res = await history.undo_last(cb.from_user.id, as_int(a1))
    if res.status == "empty":
        await edit(msg, *await menu(students, prefs, cb.from_user.id))
        await cb.answer("Отменять нечего", show_alert=True)
        return
    if res.status == "stale":
        await edit(msg, *await menu(students, prefs, cb.from_user.id))
        await cb.answer(
            "⚠️ Появились новые операции — отмена не выполнена. "
            "Откройте «Отменить действие» ещё раз.",
            show_alert=True,
        )
        return
    assert res.op is not None  # status == "done" гарантирует операцию
    await edit(msg, *await card_view(students, cb.from_user.id, res.op.student_id))
    await cb.answer("✅ Действие отменено")


@router.callback_query(F.data == "undo_no")
async def on_undo_no(
    cb: CallbackQuery, students: FromDishka[StudentService], prefs: FromDishka[ViewPrefService]
) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    await edit(msg, *await menu(students, prefs, cb.from_user.id))
    await cb.answer()
