"""Тексты и форматирование (карточка, история, статусы). Чистые функции."""

from datetime import datetime, timedelta, timezone

from .models import Operation, Student
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


def list_button_label(s: Student) -> str:
    return f"{status_emoji(s.balance)} {s.name} · {s.balance} зан. · {format_money(s.price)}"


def render_card(s: Student) -> str:
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
        # Каждое поле гардится отдельно, как в export_data.students_rows: база
        # пришла живой из serverless-версии (L1), и строка с датой оплаты, но
        # NULL-суммой — не гипотеза. format_money(None) ронял карточку, а с
        # ней каждый тап card:, «К карточке» и подтверждение любой операции по
        # этому ученику — навсегда, без пути починить из бота.
        lines.append(f"📅 Последняя оплата: {format_date(s.last_payment_at)}")
        if s.last_payment_amount is not None:
            lines.append(f"   Сумма: {format_money(s.last_payment_amount)}")
        if s.last_payment_lessons is not None:
            lines.append(f"   Добавлено занятий: {s.last_payment_lessons}")
    else:
        lines.append("📅 Оплат ещё не было")
    return "\n".join(lines)


def render_operation(op: Operation) -> str:
    """Строка истории (п.10 ТЗ)."""
    if op.type == "payment":
        amount_text = format_money(op.amount) if op.amount is not None else "?"
        body = (
            f"{OP_LABELS['payment']} {amount_text}: "
            f"+{op.lessons_delta} {lessons_word(op.lessons_delta)}, "
            f"остаток {format_money(op.remainder_after)}"
        )
    elif op.type == "charge":
        body = f"{OP_LABELS['charge']}: −1 занятие (осталось {op.balance_after})"
    elif op.type == "refund":
        body = f"{OP_LABELS['refund']}: +1 занятие (осталось {op.balance_after})"
    elif op.type == "price_change":
        # .get не случайно — ключа может не быть (snapshot_before это
        # нетипизированный JSON, унаследованный из serverless), но результат
        # раньше шёл прямо в format_money и ронял всю «Историю» этого ученика
        # навсегда. Неизвестную старую цену честно показываем как «?».
        old = op.snapshot_before.get("price")
        old_text = format_money(old) if old is not None else "?"
        new_text = format_money(op.new_price) if op.new_price is not None else "?"
        body = f"{OP_LABELS['price_change']}: {old_text} → {new_text}"
    else:
        body = op.type
    line = f"{format_datetime(op.created_at)} — {body}"
    return f"❌ {line} (отменено)" if op.undone else line


def describe_operation(op: Operation, student_name: str) -> str:
    """Короткое описание для подтверждения отмены."""
    # Nullable-поля гардятся так же, как в render_operation: аннотация
    # Operation сразу подсветила, что format_money(None) достижим и здесь —
    # оплата без суммы или смена цены без новой цены (живые данные из
    # serverless, L1) роняли бы подтверждение отмены.
    if op.type == "payment":
        amount_text = format_money(op.amount) if op.amount is not None else "?"
        what = f"оплату {amount_text} (+{op.lessons_delta} {lessons_word(op.lessons_delta)})"
    elif op.type == "charge":
        what = "списание урока"
    elif op.type == "refund":
        what = "возврат урока"
    elif op.type == "price_change":
        price_text = format_money(op.new_price) if op.new_price is not None else "?"
        what = f"изменение стоимости на {price_text}"
    else:
        what = op.type
    return f"{what} — {student_name}, {format_datetime(op.created_at)}"
