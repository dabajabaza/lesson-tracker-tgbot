"""Выгрузка учеников и истории документами (п.17–18 ТЗ)."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message
from dishka import FromDishka

from ..csv_export import to_csv_bytes
from ..export_data import (
    history_rows,
    rows_to_xlsx,
    snapshot_operations,
    snapshot_students,
    students_rows,
)
from ..keyboards import export_kb
from ..services import HistoryService, StudentService
from ..ui import Responder
from ._common import screen_of

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


@router.callback_query(F.data == "export")
async def on_export_menu(cb: CallbackQuery, ui: FromDishka[Responder]) -> None:
    msg = screen_of(cb, ui)
    if msg is None:
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
    msg = screen_of(cb, ui)
    if msg is None:
        return
    fmt = "csv" if cb.data == "exp_csv" else "xlsx"
    # «Часики» гасятся ПЕРВЫМИ, без текста: сборка и загрузка двух файлов на
    # большой истории занимает дольше, чем Telegram держит окно ответа на
    # нажатие, — тост после документов молча отклонялся, и кнопка крутилась,
    # пока клиент не сдастся. «Готово» обещать нечем: подтверждение — сами
    # файлы, а о провале их отправки говорит запасное сообщение ui.document
    # (наверх из слива ничего не поднимается — см. ui.flush).
    ui.callback(cb)
    await _send_export(ui, msg, students, history, cb.from_user.id, fmt)


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
    # В транзакции — только снимок сырых атрибутов. Форматирование тысяч ячеек
    # и сборка файла уезжают в отложенный вызов (Responder исполнит его в
    # отдельном потоке, после коммита): под замком CPU-работе не место.
    s_snap = snapshot_students(rows)
    h_snap = snapshot_operations(ops, names)
    if fmt == "csv":
        docs = [
            (lambda: to_csv_bytes(students_rows(s_snap)), "students.csv", "👥 Ученики"),
            (lambda: to_csv_bytes(history_rows(h_snap)), "history.csv", "📜 История операций"),
        ]
    else:
        docs = [
            (
                lambda: rows_to_xlsx(students_rows(s_snap), "Ученики"),
                "students.xlsx",
                "👥 Ученики",
            ),
            (
                lambda: rows_to_xlsx(history_rows(h_snap), "История"),
                "history.xlsx",
                "📜 История операций",
            ),
        ]
    for build, filename, caption in docs:
        ui.document(msg, build, filename, caption=caption)
