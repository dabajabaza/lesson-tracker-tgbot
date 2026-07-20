"""Генерация CSV из строк-таблиц."""

import re


def _escape_cell(value) -> str:
    s = "" if value is None else str(value)
    # Защита от CSV formula injection: ведущие = + - @ Excel/Sheets исполняют
    # как формулу. Обычные числа (−1, 1600,50) не трогаем.
    if re.match(r"^[=+@-]", s) and not re.fullmatch(r"-?\d+(,\d+)?", s):
        s = "'" + s
    if re.search(r'[";\n]', s):
        s = '"' + s.replace('"', '""') + '"'
    return s


def to_csv(rows: list[list]) -> str:
    """Разделитель «;» — так CSV открывается в русском Excel без настройки."""
    return "\n".join(";".join(_escape_cell(c) for c in row) for row in rows)


def to_csv_bytes(rows: list[list]) -> bytes:
    # BOM — чтобы Excel распознал UTF-8 и не ломал кириллицу.
    return ("﻿" + to_csv(rows)).encode("utf-8")
