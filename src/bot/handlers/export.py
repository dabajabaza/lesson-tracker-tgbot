"""Выгрузка учеников и истории документами (п.17–18 ТЗ)."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from dishka import FromDishka

from ..export_data import history_rows, rows_to_csv, rows_to_xlsx, students_rows
from ..keyboards import export_kb
from ..services import HistoryService, StudentService
from ._common import edit, message_of

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


@router.callback_query(F.data == "export")
async def on_export_menu(cb: CallbackQuery) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    await edit(msg, "📤 Что экспортировать?", export_kb())
    await cb.answer()


@router.callback_query(F.data.in_({"exp_csv", "exp_xlsx"}))
async def on_export(
    cb: CallbackQuery, students: FromDishka[StudentService], history: FromDishka[HistoryService]
) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    fmt = "csv" if cb.data == "exp_csv" else "xlsx"
    await _send_export(msg, students, history, cb.from_user.id, fmt)
    await cb.answer("Готово")


async def _send_export(
    msg: Message,
    students: StudentService,
    history: HistoryService,
    uid: int,
    fmt: str,
) -> None:
    """Каждый пользователь выгружает только свои данные."""
    rows = await students.list_all(uid, "name")
    ops = await history.all_operations(uid)
    if not rows and not ops:
        await msg.answer("Экспортировать нечего — данных пока нет.")
        return
    names = {s.id: s.name for s in rows}
    srows = students_rows(rows)
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
        await msg.answer_document(BufferedInputFile(data, filename), caption=caption)
