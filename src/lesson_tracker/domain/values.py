"""Результаты операций, которые сервисы отдают наверх.

Обе структуры держат живую ORM-сущность: `PaymentResult.student` и
`UndoResult.op` — это `Student` и `Operation` из текущей сессии, а не их копии.
Отсюда направление зависимости `domain -> db`, такое же, как у соседнего
проекта (см. его D12): сущность одновременно и запись в БД, и модель для
экрана, и второго набора классов под то же самое не заводим.
"""

from dataclasses import dataclass

from lesson_tracker.db.models import Operation, Student


@dataclass
class PaymentResult:
    student: Student
    lessons: int
    remainder: int
    prev_remainder: int


@dataclass
class UndoResult:
    status: str  # 'empty' | 'stale' | 'done'
    op: Operation | None = None
