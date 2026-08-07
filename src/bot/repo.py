"""Бизнес-логика поверх БД. Все функции принимают owner_id — пользователь
видит и меняет только свои данные (мультитенантность)."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Operation, Student, UiPref, now_ts


async def get_student(session: AsyncSession, owner_id: int, sid: int) -> Student | None:
    return await session.scalar(
        select(Student).where(Student.owner_id == owner_id, Student.id == sid)
    )


async def list_students(session: AsyncSession, owner_id: int, sort: str = "name") -> list[Student]:
    rows = list(await session.scalars(select(Student).where(Student.owner_id == owner_id)))
    # Учеников немного — сортируем в Python (п.15 ТЗ).
    if sort == "bal":
        rows.sort(key=lambda s: s.name.lower())
        rows.sort(key=lambda s: -s.balance)
    elif sort == "due":
        rows.sort(key=lambda s: s.name.lower())
        rows.sort(key=lambda s: s.balance)
    else:
        rows.sort(key=lambda s: s.name.lower())
    return rows


async def search_students(session: AsyncSession, owner_id: int, query: str) -> list[Student]:
    q = query.strip().lower()
    return [s for s in await list_students(session, owner_id, "name") if q in s.name_lower]


async def find_by_name_lower(session: AsyncSession, owner_id: int, name: str) -> Student | None:
    return await session.scalar(
        select(Student).where(
            Student.owner_id == owner_id, Student.name_lower == name.strip().lower()
        )
    )


async def create_student(
    session: AsyncSession, owner_id: int, name: str, price: int
) -> Student | None:
    """Создаёт ученика; None, если имя пустое или у этого владельца занято
    (без учёта регистра)."""
    clean = (name or "").strip()
    if not clean:
        return None
    if await find_by_name_lower(session, owner_id, clean):
        return None
    s = Student(owner_id=owner_id, name=clean, name_lower=clean.lower(), price=price)
    # Гонка двух одновременных апдейтов на UniqueConstraint(owner_id, name_lower):
    # раньше её ловил session.commit() прямо здесь. Теперь repo не коммитит
    # (единица работы фиксируется в middleware), поэтому нарушение констрейнта
    # надо поймать в момент записи — SAVEPOINT + flush. Откатывается только
    # вставка, транзакция запроса живёт дальше.
    try:
        async with session.begin_nested():
            session.add(s)
    except IntegrityError:
        return None
    return s


async def _record(session: AsyncSession, before: Student, **fields) -> None:
    session.add(
        Operation(
            owner_id=before.owner_id,
            student_id=before.id,
            snapshot_before=before.snapshot(),
            **fields,
        )
    )


@dataclass
class PaymentResult:
    student: Student
    lessons: int
    remainder: int
    prev_remainder: int


async def apply_payment(
    session: AsyncSession, owner_id: int, sid: int, amount: int
) -> PaymentResult | None:
    """Оплата (п.6–7 ТЗ): к сумме добавляется денежный остаток, считаются целые
    занятия, новый остаток сохраняется."""
    s = await get_student(session, owner_id, sid)
    if not s:
        return None
    prev_remainder = s.remainder
    total = amount + s.remainder
    lessons = total // s.price
    remainder = total % s.price
    await _record(
        session,
        s,
        type="payment",
        amount=amount,
        lessons_delta=lessons,
        balance_after=s.balance + lessons,
        remainder_after=remainder,
    )
    s.balance += lessons
    s.remainder = remainder
    s.last_payment_at = now_ts()
    s.last_payment_amount = amount
    s.last_payment_lessons = lessons
    return PaymentResult(s, lessons, remainder, prev_remainder)


async def _shift_balance(
    session: AsyncSession, owner_id: int, sid: int, op_type: str, delta: int
) -> Student | None:
    s = await get_student(session, owner_id, sid)
    if not s:
        return None
    await _record(
        session,
        s,
        type=op_type,
        lessons_delta=delta,
        balance_after=s.balance + delta,
        remainder_after=s.remainder,
    )
    s.balance += delta
    return s


async def charge_lesson(session, owner_id, sid):  # п.8: баланс может уйти в минус (долг)
    return await _shift_balance(session, owner_id, sid, "charge", -1)


async def refund_lesson(session, owner_id, sid):  # п.9
    return await _shift_balance(session, owner_id, sid, "refund", +1)


async def change_price(session, owner_id, sid, new_price) -> Student | None:
    """Изменение стоимости (п.12): только для будущих оплат, история не пересчитывается."""
    s = await get_student(session, owner_id, sid)
    if not s:
        return None
    await _record(
        session,
        s,
        type="price_change",
        lessons_delta=0,
        balance_after=s.balance,
        remainder_after=s.remainder,
        new_price=new_price,
    )
    s.price = new_price
    return s


async def peek_last_operation(session: AsyncSession, owner_id: int) -> Operation | None:
    return await session.scalar(
        select(Operation)
        .where(Operation.owner_id == owner_id, Operation.undone == False)  # noqa: E712
        .order_by(Operation.id.desc())
        .limit(1)
    )


@dataclass
class UndoResult:
    status: str  # 'empty' | 'stale' | 'done'
    op: Operation | None = None


async def undo_last_operation(
    session: AsyncSession, owner_id: int, expected_op_id: int | None
) -> UndoResult:
    """Отмена (п.16): восстановить ученика из снимка, пометить операцию отменённой.
    expected_op_id — операция из диалога подтверждения; если появились новые
    операции, отмена не выполняется (защита от гонки между кликами)."""
    op = await peek_last_operation(session, owner_id)
    if op is None:
        return UndoResult("empty")
    if expected_op_id is None or op.id != expected_op_id:
        return UndoResult("stale")
    s = await get_student(session, owner_id, op.student_id)
    if s:
        s.restore(op.snapshot_before)
    op.undone = True
    return UndoResult("done", op)


async def count_history(session: AsyncSession, owner_id: int, sid: int) -> int:
    # `or 0` — только для типов: select(count()) всегда возвращает строку,
    # но scalar() объявлен как Optional.
    return (
        await session.scalar(
            select(func.count())
            .select_from(Operation)
            .where(Operation.owner_id == owner_id, Operation.student_id == sid)
        )
        or 0
    )


async def get_history(session, owner_id, sid, page, page_size) -> list[Operation]:
    return list(
        await session.scalars(
            select(Operation)
            .where(Operation.owner_id == owner_id, Operation.student_id == sid)
            .order_by(Operation.id.desc())
            .limit(page_size)
            .offset(page * page_size)
        )
    )


async def get_all_operations(session, owner_id) -> list[Operation]:
    return list(
        await session.scalars(
            select(Operation).where(Operation.owner_id == owner_id).order_by(Operation.id)
        )
    )


# ---------- настройки отображения списка (п.15 ТЗ) ----------


async def get_view_pref(session: AsyncSession, owner_id: int) -> tuple[str, int]:
    p = await session.get(UiPref, owner_id)
    return (p.sort, p.page) if p else ("name", 0)


async def set_view_pref(session: AsyncSession, owner_id: int, sort: str, page: int) -> None:
    p = await session.get(UiPref, owner_id)
    if p is None:
        session.add(UiPref(owner_id=owner_id, sort=sort, page=page))
    else:
        p.sort = sort
        p.page = page
