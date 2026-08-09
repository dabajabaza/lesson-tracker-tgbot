"""Middleware диспетчера, по одному файлу на ответственность.

* `unit_of_work` — транзакция на апдейт, общий замок, коммит, идемпотентность;
* `access` — контроль доступа: дешёвый отказ до замка и погашение инвайта
  внутри транзакции;
* `fsm` — подмена FSM-контекста на хранилище общей сессии запроса;
* `state` — сброс незавершённого ввода при нажатии кнопки (роутер-скоупный,
  вешается в handlers/__init__.py, а не в build_dispatcher);
* `keys` — ключи в data, через которые middleware договариваются.

Порядок регистрации на dp.update задаёт build_dispatcher (см. ARCHITECTURE.md
L6) и он несёт смысл: гейт доступа обязан идти до единицы работы, а подмена
FSM — после неё.
"""

from .access import AccessGateMiddleware, AccessMiddleware
from .fsm import FsmSessionMiddleware
from .keys import ACCESS_DENIED
from .state import ClearStateOnCallbackMiddleware
from .unit_of_work import DbSessionMiddleware

__all__ = [
    "ACCESS_DENIED",
    "AccessGateMiddleware",
    "AccessMiddleware",
    "ClearStateOnCallbackMiddleware",
    "DbSessionMiddleware",
    "FsmSessionMiddleware",
]
