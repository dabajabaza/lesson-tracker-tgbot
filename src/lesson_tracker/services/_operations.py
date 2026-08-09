"""Журнал операций — общий для всех сервисов, которые меняют ученика.

Снимок «до» пишется в ту же транзакцию, что и само изменение: отмена (п.16 ТЗ)
восстанавливает ученика именно из него, поэтому операция без снимка или снимок
без операции сделали бы отмену недостоверной.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from lesson_tracker.db.models import Operation, Student


async def record_operation(
    session: AsyncSession,
    before: Student,
    *,
    type: str,
    lessons_delta: int,
    balance_after: int,
    remainder_after: int,
    amount: int | None = None,
    new_price: int | None = None,
) -> None:
    """Явные keyword-only параметры, а не **fields.

    Это единственная запись, на которой держится отмена (п.16 ТЗ), и с
    **fields она была единственной записью, которую mypy не проверял вовсе:
    опечатка вида balance_afer= падала бы TypeError уже в денежной транзакции,
    а пропущенная колонка — IntegrityError, и оба раза человек видел бы
    «Не получилось выполнить действие» на каждой оплате.
    """
    session.add(
        Operation(
            owner_id=before.owner_id,
            student_id=before.id,
            snapshot_before=before.snapshot(),
            type=type,
            lessons_delta=lessons_delta,
            balance_after=balance_after,
            remainder_after=remainder_after,
            amount=amount,
            new_price=new_price,
        )
    )
