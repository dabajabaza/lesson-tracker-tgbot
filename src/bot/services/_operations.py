"""Журнал операций — общий для всех сервисов, которые меняют ученика.

Снимок «до» пишется в ту же транзакцию, что и само изменение: отмена (п.16 ТЗ)
восстанавливает ученика именно из него, поэтому операция без снимка или снимок
без операции сделали бы отмену недостоверной.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Operation, Student


async def record_operation(session: AsyncSession, before: Student, **fields) -> None:
    session.add(
        Operation(
            owner_id=before.owner_id,
            student_id=before.id,
            snapshot_before=before.snapshot(),
            **fields,
        )
    )
