"""Границы хранилища, которые нужно проверять на входе.

Это не часть ORM-схемы, а предикат валидации: значения приходят снаружи
(callback_data подделывается клиентом, /allow набирается руками), и всё, что
не влезает в 64-битный INTEGER, драйвер отвергает уже на привязке параметра —
OverflowError вместо «ничего не найдено».
"""

# Границы 64-битного INTEGER в SQLite. Python-числа безразмерны.
SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1


def fits_in_db(value: int) -> bool:
    return SQLITE_INT_MIN <= value <= SQLITE_INT_MAX
