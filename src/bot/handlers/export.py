"""Выгрузка учеников и истории документами (п.17–18 ТЗ)."""

from functools import partial

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message
from dishka import FromDishka

from ..export_data import history_rows, rows_to_csv, rows_to_xlsx, students_rows
from ..keyboards import export_kb
from ..services import HistoryService, StudentService
from ..ui import Responder
from ._common import message_of

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


@router.callback_query(F.data == "export")
async def on_export_menu(cb: CallbackQuery, ui: FromDishka[Responder]) -> None:
    msg = message_of(cb)
    if msg is None:
        ui.callback(cb)
        return
    ui.edit(msg, "📤 Что экспортировать?", export_kb())
    ui.callback(cb)


@router.callback_query(F.data.in_({"exp_csv", "exp_xlsx"}))
async def on_export(
    cb: CallbackQuery,
    students: FromDishka[StudentService],
    history: FromDishka[HistoryService],
    ui: FromDishka[Responder],
) -> None:
    msg = message_of(cb)
    if msg is None:
        ui.callback(cb)
        return
    fmt = "csv" if cb.data == "exp_csv" else "xlsx"
    await _send_export(ui, msg, students, history, cb.from_user.id, fmt)
    # Без «Готово»: тост уходил раньше, чем файлы, и врал при неудачной
    # выгрузке — человек видел успех, а документа не получал. Подтверждение
    # здесь — сами файлы; провал их отправки поднимется наверх (см. ui.flush).
    ui.callback(cb)


async def _send_export(
    ui: Responder,
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
        ui.reply(msg, "Экспортировать нечего — данных пока нет.")
        return
    names = {s.id: s.name for s in rows}
    srows = students_rows(rows)
    hrows = history_rows(ops, names)
    # Данные читаются здесь (внутри транзакции), а вот сборка файла отложена:
    # partial отдаётся в Responder и выполняется при сливе, вне замка записи.
    if fmt == "csv":
        docs = [
            (partial(rows_to_csv, srows), "students.csv", "👥 Ученики"),
            (partial(rows_to_csv, hrows), "history.csv", "📜 История операций"),
        ]
    else:
        docs = [
            (partial(rows_to_xlsx, srows, "Ученики"), "students.xlsx", "👥 Ученики"),
            (partial(rows_to_xlsx, hrows, "История"), "history.xlsx", "📜 История операций"),
        ]
    for build, filename, caption in docs:
        ui.document(msg, build, filename, caption=caption)
