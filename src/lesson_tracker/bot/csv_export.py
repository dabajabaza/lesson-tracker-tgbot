"""Генерация CSV из строк-таблиц через стандартный модуль csv."""

import csv
import io
import re


def guard_formula(value) -> str | int | float:
    """Защита от formula injection: ведущие = + - @ Excel/Sheets исполняют
    как формулу.

    Числа возвращаются КАК ЕСТЬ, а не строкой: int сам по себе формулой не
    станет, а строкование ломало .xlsx — каждая числовая ячейка приезжала
    текстом, =SUM() по колонке возвращал 0, сортировка по «Осталось занятий»
    ставила 10 перед 4, и Excel зеленил всю таблицу флагом «число как текст».
    """
    if value is None:
        return ""
    if isinstance(value, int | float):
        return value
    s = str(value)
    if re.match(r"^[=+@-]", s) and not re.fullmatch(r"-?\d+(,\d+)?", s):
        s = "'" + s
    return s


def to_csv(rows: list[list]) -> str:
    """Разделитель «;» — так CSV открывается в русском Excel без настройки.
    csv.writer сам квотит поля с «;», кавычками и любыми переводами строки (\\r, \\n)."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    for row in rows:
        writer.writerow([guard_formula(c) for c in row])
    return buf.getvalue().rstrip("\n")


def to_csv_bytes(rows: list[list]) -> bytes:
    # BOM — чтобы Excel распознал UTF-8 и не ломал кириллицу.
    return ("﻿" + to_csv(rows)).encode("utf-8")
