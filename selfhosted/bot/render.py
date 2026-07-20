"""Тексты и форматирование (карточка, история, статусы). Чистые функции."""

from datetime import datetime, timedelta, timezone

from .money import format_money

# Часовой пояс отображения: Москва (UTC+3).
MSK = timezone(timedelta(hours=3))

# Единственная карта «тип операции → подпись» — для истории и экспорта.
OP_LABELS = {
    "payment": "Оплата",
    "charge": "Списан урок",
    "refund": "Возврат урока",
    "price_change": "Изменение стоимости",
}


def status_emoji(balance: int) -> str:
    if balance >= 3:
        return "🟢"
    if balance == 2:
        return "🟡"
    if balance == 1:
        return "🟠"
    return "🔴"


def lessons_word(n: int) -> str:
    """1 занятие, 2 занятия, 5 занятий."""
    a = abs(n) % 100
    d = a % 10
    if 10 < a < 20:
        return "занятий"
    if d == 1:
        return "занятие"
    if 2 <= d <= 4:
        return "занятия"
    return "занятий"


def format_datetime(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m.%Y %H:%M")


def format_date(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m.%Y")


def list_button_label(s) -> str:
    return f"{status_emoji(s.balance)} {s.name} · {s.balance} зан. · {format_money(s.price)}"


def render_card(s) -> str:
    """Карточка ученика (п.5 ТЗ)."""
    lines = [
        f"👤 {s.name}",
        "",
        f"{status_emoji(s.balance)} Осталось занятий: {s.balance}",
        f"💵 Стоимость занятия: {format_money(s.price)}",
        f"💰 Денежный остаток: {format_money(s.remainder)}",
    ]
    if s.balance < 0:
        lines.append(f"⚠️ Долг: {abs(s.balance)} {lessons_word(s.balance)}")
    lines.append("")
    if s.last_payment_at:
        lines += [
            f"📅 Последняя оплата: {format_date(s.last_payment_at)}",
            f"   Сумма: {format_money(s.last_payment_amount)}",
            f"   Добавлено занятий: {s.last_payment_lessons}",
        ]
    else:
        lines.append("📅 Оплат ещё не было")
    return "\n".join(lines)


def render_operation(op) -> str:
    """Строка истории (п.10 ТЗ)."""
    if op.type == "payment":
        body = (
            f"{OP_LABELS['payment']} {format_money(op.amount)}: "
            f"+{op.lessons_delta} {lessons_word(op.lessons_delta)}, "
            f"остаток {format_money(op.remainder_after)}"
        )
    elif op.type == "charge":
        body = f"{OP_LABELS['charge']}: −1 занятие (осталось {op.balance_after})"
    elif op.type == "refund":
        body = f"{OP_LABELS['refund']}: +1 занятие (осталось {op.balance_after})"
    elif op.type == "price_change":
        old = op.snapshot_before.get("price")
        body = f"{OP_LABELS['price_change']}: {format_money(old)} → {format_money(op.new_price)}"
    else:
        body = op.type
    line = f"{format_datetime(op.created_at)} — {body}"
    return f"❌ {line} (отменено)" if op.undone else line


def describe_operation(op, student_name: str) -> str:
    """Короткое описание для подтверждения отмены."""
    if op.type == "payment":
        what = f"оплату {format_money(op.amount)} (+{op.lessons_delta} {lessons_word(op.lessons_delta)})"
    elif op.type == "charge":
        what = "списание урока"
    elif op.type == "refund":
        what = "возврат урока"
    elif op.type == "price_change":
        what = f"изменение стоимости на {format_money(op.new_price)}"
    else:
        what = op.type
    return f"{what} — {student_name}, {format_datetime(op.created_at)}"
