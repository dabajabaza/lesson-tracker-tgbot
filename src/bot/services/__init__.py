"""Слой сервисов: бизнес-логика поверх БД.

Сервисы НЕ коммитят — фиксирует единица работы (DbSessionMiddleware). Каждый
живёт в области запроса и получает сессию инъекцией через dishka (см. di.py),
поэтому все они пишут в одну транзакцию.
"""

from .history import HistoryService, UndoResult
from .payments import PaymentResult, PaymentService
from .prefs import ViewPrefService
from .students import StudentService

__all__ = [
    "HistoryService",
    "PaymentResult",
    "PaymentService",
    "StudentService",
    "UndoResult",
    "ViewPrefService",
]
