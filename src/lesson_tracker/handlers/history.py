"""История операций и отмена последнего действия."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery
from dishka import FromDishka

from ..keyboards import undo_confirm_kb
from ..render import describe_operation
from ..services import HistoryService, StudentService, ViewPrefService
from ..ui import Responder
from ..views import card_view, history_view
from ._common import as_int, paged_target_of, parts_of, screen_of, show_menu

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


@router.callback_query(F.data.startswith("hist:"))
async def on_history(
    cb: CallbackQuery,
    students: FromDishka[StudentService],
    history: FromDishka[HistoryService],
    ui: FromDishka[Responder],
) -> None:
    target = paged_target_of(cb, ui)
    if target is None:
        return
    msg, sid, page = target
    ui.edit(msg, *await history_view(students, history, cb.from_user.id, sid, page))
    ui.callback(cb)


@router.callback_query(F.data == "undo")
async def on_undo(
    cb: CallbackQuery,
    students: FromDishka[StudentService],
    history: FromDishka[HistoryService],
    ui: FromDishka[Responder],
) -> None:
    msg = screen_of(cb, ui)
    if msg is None:
        return
    op = await history.peek_last(cb.from_user.id)
    if not op:
        ui.callback(cb, "Отменять нечего — операций ещё не было", show_alert=True)
        return
    s = await students.get(cb.from_user.id, op.student_id)
    ui.edit(
        msg,
        f"↩️ Отменить последнее действие?\n\n{describe_operation(op, s.name if s else '?')}",
        undo_confirm_kb(op.id),
    )
    ui.callback(cb)


@router.callback_query(F.data.startswith("undo_yes"))
async def on_undo_yes(
    cb: CallbackQuery,
    students: FromDishka[StudentService],
    history: FromDishka[HistoryService],
    prefs: FromDishka[ViewPrefService],
    ui: FromDishka[Responder],
) -> None:
    msg = screen_of(cb, ui)
    if msg is None:
        return
    _cmd, a1, _a2 = parts_of(cb)
    res = await history.undo_last(cb.from_user.id, as_int(a1))
    if res.status == "empty":
        await show_menu(cb, ui, students, prefs, "Отменять нечего", show_alert=True)
        return
    if res.status == "stale":
        await show_menu(
            cb,
            ui,
            students,
            prefs,
            "⚠️ Появились новые операции — отмена не выполнена. "
            "Откройте «Отменить действие» ещё раз.",
            show_alert=True,
        )
        return
    assert res.op is not None  # status == "done" гарантирует операцию
    # durable: отмена операции — изменение баланса, и карточка о нём
    # единственное свидетельство.
    ui.edit(msg, *await card_view(students, cb.from_user.id, res.op.student_id), durable=True)
    ui.callback(cb, "✅ Действие отменено")


@router.callback_query(F.data == "undo_no")
async def on_undo_no(
    cb: CallbackQuery,
    students: FromDishka[StudentService],
    prefs: FromDishka[ViewPrefService],
    ui: FromDishka[Responder],
) -> None:
    await show_menu(cb, ui, students, prefs)
