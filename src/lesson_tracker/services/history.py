"""История операций и отмена последнего действия."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Operation
from .students import StudentService


@dataclass
class UndoResult:
    status: str  # 'empty' | 'stale' | 'done'
    op: Operation | None = None


class HistoryService:
    def __init__(self, session: AsyncSession, students: StudentService) -> None:
        self._session = session
        self._students = students

    async def peek_last(self, owner_id: int) -> Operation | None:
        return await self._session.scalar(
            select(Operation)
            .where(Operation.owner_id == owner_id, Operation.undone == False)  # noqa: E712
            .order_by(Operation.id.desc())
            .limit(1)
        )

    async def undo_last(self, owner_id: int, expected_op_id: int | None) -> UndoResult:
        """Отмена (п.16): восстановить ученика из снимка, пометить операцию
        отменённой.

        expected_op_id — операция из диалога подтверждения. Если с момента
        показа диалога появились новые операции, отмена не выполняется: иначе
        нажатие «Да» отменило бы не то, что человек видел на экране.
        """
        op = await self.peek_last(owner_id)
        if op is None:
            return UndoResult("empty")
        if expected_op_id is None or op.id != expected_op_id:
            return UndoResult("stale")
        s = await self._students.get(owner_id, op.student_id)
        if s:
            s.restore(op.snapshot_before)
        op.undone = True
        return UndoResult("done", op)

    async def count(self, owner_id: int, sid: int) -> int:
        # `or 0` — только для типов: select(count()) всегда возвращает строку,
        # но scalar() объявлен как Optional.
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.owner_id == owner_id, Operation.student_id == sid)
            )
            or 0
        )

    async def page(self, owner_id: int, sid: int, page: int, page_size: int) -> list[Operation]:
        return list(
            await self._session.scalars(
                select(Operation)
                .where(Operation.owner_id == owner_id, Operation.student_id == sid)
                .order_by(Operation.id.desc())
                .limit(page_size)
                .offset(page * page_size)
            )
        )

    async def all_operations(self, owner_id: int) -> list[Operation]:
        return list(
            await self._session.scalars(
                select(Operation).where(Operation.owner_id == owner_id).order_by(Operation.id)
            )
        )
