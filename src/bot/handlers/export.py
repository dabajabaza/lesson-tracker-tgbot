"""Выгрузка учеников и истории документами (п.17–18 ТЗ)."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from .. import repo
from ..export_data import history_rows, rows_to_csv, rows_to_xlsx, students_rows
from ..keyboards import export_kb
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
async def on_export(cb: CallbackQuery, session: AsyncSession) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    await _send_export(msg, session, cb.from_user.id, "csv" if cb.data == "exp_csv" else "xlsx")
    await cb.answer("Готово")


async def _send_export(msg: Message, session: AsyncSession, uid: int, fmt: str) -> None:
    """Каждый пользователь выгружает только свои данные."""
    students = await repo.list_students(session, uid, "name")
    ops = await repo.get_all_operations(session, uid)
    if not students and not ops:
        await msg.answer("Экспортировать нечего — данных пока нет.")
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
        await msg.answer_document(BufferedInputFile(data, filename), caption=caption)
