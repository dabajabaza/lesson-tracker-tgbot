"""Сборка данных для экспорта (п.17–18 ТЗ) в CSV и Excel.

Self-hosted, в отличие от serverless, умеет отправлять файлы — поэтому экспорт
идёт настоящими документами .csv и .xlsx (serverless слал CSV текстом)."""

from io import BytesIO

from .csv_export import to_csv_bytes
from .money import to_rubles
from .render import OP_LABELS, format_datetime

STUDENT_HEADERS = [
    "ID",
    "Имя",
    "Стоимость занятия, руб",
    "Осталось занятий",
    "Денежный остаток, руб",
    "Последняя оплата",
    "Сумма последней оплаты, руб",
    "Занятий в последней оплате",
]
HISTORY_HEADERS = [
    "Дата и время",
    "ID ученика",
    "Имя",
    "Операция",
    "Сумма, руб",
    "Изменение занятий",
    "Занятий после",
    "Денежный остаток после, руб",
    "Новая стоимость, руб",
    "Отменено",
]


def students_rows(students) -> list[list]:
    # Аннотация обязательна: без неё тип выводится из строки заголовков как
    # list[list[str]], и числовые ячейки ниже перестают проходить проверку.
    rows: list[list[object]] = [list(STUDENT_HEADERS)]
    for s in students:
        rows.append(
            [
                s.id,
                s.name,
                to_rubles(s.price),
                s.balance,
                to_rubles(s.remainder),
                format_datetime(s.last_payment_at) if s.last_payment_at else "",
                to_rubles(s.last_payment_amount) if s.last_payment_amount is not None else "",
                s.last_payment_lessons if s.last_payment_lessons is not None else "",
            ]
        )
    return rows


def history_rows(operations, names: dict[int, str]) -> list[list]:
    rows: list[list[object]] = [list(HISTORY_HEADERS)]
    for op in operations:
        rows.append(
            [
                format_datetime(op.created_at),
                op.student_id,
                names.get(op.student_id) or op.snapshot_before.get("name", ""),
                OP_LABELS.get(op.type, op.type),
                to_rubles(op.amount) if op.amount is not None else "",
                op.lessons_delta or 0,
                op.balance_after,
                to_rubles(op.remainder_after),
                to_rubles(op.new_price) if op.new_price is not None else "",
                "да" if op.undone else "",
            ]
        )
    return rows


def rows_to_csv(rows) -> bytes:
    return to_csv_bytes(rows)


def rows_to_xlsx(rows, sheet_title: str) -> bytes:
    """Excel .xlsx через openpyxl. Импорт ленивый — модуль не нужен для CSV/тестов логики."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None  # у свежего Workbook активный лист есть всегда
    ws.title = sheet_title
    for row in rows:
        ws.append(list(row))
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
