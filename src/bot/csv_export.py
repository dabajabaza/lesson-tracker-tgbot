"""Генерация CSV из строк-таблиц через стандартный модуль csv."""

import csv
import io
import re


def guard_formula(value) -> str:
    """Защита от CSV formula injection: ведущие = + - @ Excel/Sheets исполняют
    как формулу. Обычные числа (−1, 1600,50) не трогаем."""
    s = "" if value is None else str(value)
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
