"""Персистентное FSM-хранилище на SQLAlchemy: состояние диалога переживает
рестарт процесса (в serverless-версии оно жило в таблице sessions)."""

from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import FsmRecord


class SqlAlchemyStorage(BaseStorage):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self._sessionmaker = sessionmaker

    @staticmethod
    def _key(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id}:{key.destiny}"

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        async with self._sessionmaker() as session:
            rec = await session.get(FsmRecord, self._key(key))
            if rec is None:
                if value is None:
                    return
                session.add(FsmRecord(key=self._key(key), state=value, data={}))
            else:
                rec.state = value
            await session.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._sessionmaker() as session:
            rec = await session.get(FsmRecord, self._key(key))
            return rec.state if rec else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        async with self._sessionmaker() as session:
            rec = await session.get(FsmRecord, self._key(key))
            if rec is None:
                session.add(FsmRecord(key=self._key(key), state=None, data=dict(data)))
            else:
                rec.data = dict(data)  # новый объект — SQLAlchemy заметит изменение JSON
            await session.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._sessionmaker() as session:
            rec = await session.get(FsmRecord, self._key(key))
            return dict(rec.data) if rec and rec.data else {}

    async def close(self) -> None:
        pass
