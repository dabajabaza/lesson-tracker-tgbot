"""Денежные суммы — всегда в копейках (int), чтобы не терять точность на float."""

import re
from dataclasses import dataclass

# Разумный потолок для занятий репетитора: 10 млн ₽ (в копейках).
MAX_MONEY = 10_000_000 * 100


@dataclass
class MoneyParse:
    value: int | None = None
    # None | 'format' (не число) | 'range' (больше лимита) | 'zero' (ноль)
    error: str | None = None


def parse_money_strict(raw) -> MoneyParse:
    """Разбирает ввод «1600», «1 600», «1600.50», «1600,50» → копейки."""
    s = re.sub(r"\s+", "", str(raw)).replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        return MoneyParse(error="format")
    rub, _, kop = s.partition(".")
    value = int(rub) * 100 + (int(kop.ljust(2, "0")) if kop else 0)
    if value > MAX_MONEY:
        return MoneyParse(error="range")
    if value <= 0:
        return MoneyParse(error="zero")
    return MoneyParse(value=value)


def format_money(kopecks: int) -> str:
    """160000 → «1 600 ₽», 160050 → «1 600,50 ₽» (разделитель тысяч — пробел)."""
    sign = "-" if kopecks < 0 else ""
    rub, kop = divmod(abs(kopecks), 100)
    rub_str = f"{rub:,}".replace(",", " ")
    frac = f",{kop:02d}" if kop else ""
    return f"{sign}{rub_str}{frac} ₽"


def to_rubles(kopecks: int) -> str:
    """Для CSV/Excel: 160050 → «1600,50» (десятичная запятая под русский Excel)."""
    sign = "-" if kopecks < 0 else ""
    rub, kop = divmod(abs(kopecks), 100)
    return f"{sign}{rub},{kop:02d}"
