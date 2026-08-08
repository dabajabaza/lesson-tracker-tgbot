"""Сборка данных для экспорта (п.17–18 ТЗ) в CSV и Excel.

Self-hosted, в отличие от serverless, умеет отправлять файлы — поэтому экспорт
идёт настоящими документами .csv и .xlsx (serverless слал CSV текстом)."""

from io import BytesIO

from .csv_export import guard_formula
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


# Снимки против форматирования: обработчик работает внутри транзакции и под
# общим замком записи, поэтому там разрешено только ДЁШЕВОЕ — прочитать
# атрибуты ORM-объектов в кортежи. Форматирование каждой ячейки (to_rubles,
# format_datetime — это тысячи вызовов на большой истории) живёт в
# *_rows и выполняется уже при сборке файла: в отложенном вызове, в отдельном
# потоке, после коммита. Иначе «Экспорт» снова останавливал бы все чужие
# апдейты, фоновый отправщик и пробу сторожа — короче, чем при сборке xlsx в
# обработчике, но тем же самым способом.


def snapshot_students(students) -> list[tuple]:
    """Сырые атрибуты учеников — единственное, что читается в транзакции."""
    return [
        (
            s.id,
            s.name,
            s.price,
            s.balance,
            s.remainder,
            s.last_payment_at,
            s.last_payment_amount,
            s.last_payment_lessons,
        )
        for s in students
    ]


def snapshot_operations(operations, names: dict[int, str]) -> list[tuple]:
    return [
        (
            op.created_at,
            op.student_id,
            names.get(op.student_id) or op.snapshot_before.get("name", ""),
            op.type,
            op.amount,
            op.lessons_delta,
            op.balance_after,
            op.remainder_after,
            op.new_price,
            op.undone,
        )
        for op in operations
    ]


def students_rows(snapshot: list[tuple]) -> list[list]:
    # Аннотация обязательна: без неё тип выводится из строки заголовков как
    # list[list[str]], и числовые ячейки ниже перестают проходить проверку.
    rows: list[list[object]] = [list(STUDENT_HEADERS)]
    for sid, name, price, balance, remainder, paid_at, paid_amount, paid_lessons in snapshot:
        rows.append(
            [
                sid,
                name,
                to_rubles(price),
                balance,
                to_rubles(remainder),
                format_datetime(paid_at) if paid_at else "",
                to_rubles(paid_amount) if paid_amount is not None else "",
                paid_lessons if paid_lessons is not None else "",
            ]
        )
    return rows


def history_rows(snapshot: list[tuple]) -> list[list]:
    rows: list[list[object]] = [list(HISTORY_HEADERS)]
    for (
        created_at,
        sid,
        name,
        op_type,
        amount,
        delta,
        balance,
        remainder,
        price,
        undone,
    ) in snapshot:
        rows.append(
            [
                format_datetime(created_at),
                sid,
                name,
                OP_LABELS.get(op_type, op_type),
                to_rubles(amount) if amount is not None else "",
                delta or 0,
                balance,
                to_rubles(remainder),
                to_rubles(price) if price is not None else "",
                "да" if undone else "",
            ]
        )
    return rows


def rows_to_xlsx(rows, sheet_title: str) -> bytes:
    """Excel .xlsx через openpyxl. Импорт ленивый — модуль не нужен для CSV/тестов логики.

    Тот же экран от формул, что и в CSV. openpyxl отдаёт ячейку, начинающуюся
    с «=», как настоящую формулу (data_type == "f"): ученик с именем
    «=HYPERLINK(...)» превращал выгрузку преподавателя в исполняемый документ,
    а кривое выражение — в предложение Excel «восстановить файл». Защита была
    только на пути CSV, хотя источник данных общий.
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None  # у свежего Workbook активный лист есть всегда
    ws.title = sheet_title
    for row in rows:
        ws.append([guard_formula(cell) for cell in row])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
