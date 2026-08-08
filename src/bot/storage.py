"""Персистентное FSM-хранилище: состояние диалога переживает рестарт процесса.

Осознанное расхождение с chain-health (их D5 держит FSM в памяти): здесь
диалог — это ввод оплаты, и деплой не должен его обрывать.

Два класса под две роли:

* SqlAlchemyStorage — рабочее хранилище области запроса. Живёт поверх ТОЙ ЖЕ
  сессии, что и бизнес-логика, ничего не коммитит: состояние диалога и
  денежная запись фиксируются одной транзакцией (коммит — в middleware).
  Прежняя версия открывала собственные сессии и коммитила сама — 07.08.2026
  это кончилось «database is locked» в проде: вторая пишущая сессия упёрлась
  в блокировку первой.

* ReadOnlyFsmView — хранилище уровня диспетчера. Встроенный FSMContextMiddleware
  aiogram читает raw_state ДО того, как у нас появляется область запроса, — ему
  нужно откуда-то читать. Читает коротким собственным соединением, помеченным
  READONLY, а любая попытка записи бросает RuntimeError: инвариант «пишет только
  область запроса» — свойство кода, а не договорённость. Если будущая версия
  aiogram начнёт писать этим путём, мы узнаем из упавшего теста, а не из
  «database is locked» в проде.

  Пометка READONLY (см. db.py) не украшение. Это чтение идёт ДО общего замка
  записи, и пока транзакция открывалась как IMMEDIATE, каждый апдейт забирал
  блокировку записи SQLite снаружи всякой сериализации: замок переставал
  что-либо гарантировать, а на нагрузке сверх busy_timeout апдейт умирал с
  «database is locked» в точке, где нет ни отката, ни отметки идемпотентности —
  пользователь просто видел «Не получилось выполнить действие».
"""

from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from dishka import AsyncContainer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .db import READONLY
from .models import FsmRecord


def _key(key: StorageKey) -> str:
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id}:{key.destiny}"


class SqlAlchemyStorage(BaseStorage):
    """Хранилище области запроса: пишет в общую сессию, не коммитит."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        rec = await self._session.get(FsmRecord, _key(key))
        if rec is None:
            if value is None:
                return
            self._session.add(FsmRecord(key=_key(key), state=value, data={}))
        else:
            rec.state = value

    async def get_state(self, key: StorageKey) -> str | None:
        rec = await self._session.get(FsmRecord, _key(key))
        return rec.state if rec else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        rec = await self._session.get(FsmRecord, _key(key))
        if rec is None:
            self._session.add(FsmRecord(key=_key(key), state=None, data=dict(data)))
        else:
            rec.data = dict(data)  # новый объект — SQLAlchemy заметит изменение JSON

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        rec = await self._session.get(FsmRecord, _key(key))
        return dict(rec.data) if rec and rec.data else {}

    async def close(self) -> None:
        pass


class ReadOnlyFsmView(BaseStorage):
    """Хранилище уровня диспетчера: только чтение raw_state для фильтров."""

    def __init__(self, container: AsyncContainer) -> None:
        # Контейнер, а не движок: на момент сборки диспетчера движок ещё не
        # создан (он живёт в APP-скоупе и рождается при первом обращении).
        self._container = container

    async def _read(self, key: StorageKey):  # noqa: ANN202
        """Строка FSM коротким читающим соединением, или None.

        Соединением, а не сессией: пометка READONLY ставится на соединение, а
        через сессию её пришлось бы протаскивать вручную на каждый вызов —
        забыть один раз означало бы вернуть блокировку записи вне замка.
        """
        engine = await self._container.get(AsyncEngine)
        async with engine.connect() as conn:
            ro = await conn.execution_options(**{READONLY: True})
            row = await ro.execute(
                select(FsmRecord.state, FsmRecord.data).where(FsmRecord.key == _key(key))
            )
            return row.first()

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self._read(key)
        return row.state if row else None

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self._read(key)
        return dict(row.data) if row and row.data else {}

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        raise RuntimeError(
            "Запись FSM мимо области запроса запрещена: состояние пишется в общую "
            "транзакцию через SqlAlchemyStorage (см. di.py). Если вы видите эту "
            "ошибку, aiogram начал писать через хранилище диспетчера."
        )

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await self.set_state(key)  # тот же запрет, то же сообщение

    async def close(self) -> None:
        pass
