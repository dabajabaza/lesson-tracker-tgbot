"""Оплаты, списания и возвраты занятий."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Student, now_ts
from ._operations import record_operation
from .students import StudentService


@dataclass
class PaymentResult:
    student: Student
    lessons: int
    remainder: int
    prev_remainder: int


class PaymentService:
    def __init__(self, session: AsyncSession, students: StudentService) -> None:
        self._session = session
        self._students = students

    async def apply(self, owner_id: int, sid: int, amount: int) -> PaymentResult | None:
        """Оплата (п.6–7 ТЗ): к сумме добавляется денежный остаток, считаются
        целые занятия, новый остаток сохраняется."""
        s = await self._students.get(owner_id, sid)
        if not s:
            return None
        prev_remainder = s.remainder
        total = amount + s.remainder
        lessons = total // s.price
        remainder = total % s.price
        await record_operation(
            self._session,
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
        self, owner_id: int, sid: int, op_type: str, delta: int
    ) -> Student | None:
        s = await self._students.get(owner_id, sid)
        if not s:
            return None
        await record_operation(
            self._session,
            s,
            type=op_type,
            lessons_delta=delta,
            balance_after=s.balance + delta,
            remainder_after=s.remainder,
        )
        s.balance += delta
        return s

    async def charge(self, owner_id: int, sid: int) -> Student | None:
        # п.8: баланс может уйти в минус — это долг, а не ошибка.
        return await self._shift_balance(owner_id, sid, "charge", -1)

    async def refund(self, owner_id: int, sid: int) -> Student | None:
        return await self._shift_balance(owner_id, sid, "refund", +1)
