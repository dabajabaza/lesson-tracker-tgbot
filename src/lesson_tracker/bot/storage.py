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
  aiogram читает raw_state ДО того, как у нас появляется область запроса. Этот
  снимок НИКОГДА не доживает до потребителя: FsmSessionMiddleware безусловно
  перечитывает состояние запросной сессией внутри замка и перезаписывает
  data["raw_state"] раньше, чем выполнится первый фильтр (фильтры идут при
  propagate_event, после update-middleware). Поэтому чтение здесь — пустышка,
  и это не оптимизация вдогонку: раньше каждый апдейт платил за отбрасываемое
  значение соединением из пула, транзакцией и круговым запросом.

  Запись по-прежнему бросает RuntimeError: инвариант «пишет только область
  запроса» — свойство кода, а не договорённость. Если будущая версия aiogram
  начнёт писать этим путём, мы узнаем из упавшего теста, а не из «database is
  locked» в проде.
"""

from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy.ext.asyncio import AsyncSession

from lesson_tracker.db.models import FsmRecord


def _key(key: StorageKey) -> str:
    """Строковый ключ строки FSM — из ВСЕХ полей StorageKey.

    business_connection_id включён не «на всякий случай»: без него ключ
    перестаёт быть однозначным. Диалог в бизнес-чате и обычный диалог того же
    человека схлопывались бы в одну строку, и «Введите сумму оплаты» из одного
    затирало бы состояние другого — оплата ушла бы не тому ученику.
    """
    return (
        f"{key.bot_id}:{key.chat_id}:{key.user_id}:"
        f"{key.thread_id}:{key.business_connection_id}:{key.destiny}"
    )


class SqlAlchemyStorage(BaseStorage):
    """Хранилище области запроса: пишет в общую сессию, не коммитит."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        row_key = _key(key)
        rec = await self._session.get(FsmRecord, row_key)
        if rec is None:
            if value is None:
                return
            self._session.add(FsmRecord(key=row_key, state=value, data={}))
        elif value is None and not rec.data:
            # Пустая строка (нет ни состояния, ни данных) никому не нужна, а
            # раньше жила вечно: clear() только обнулял поля, DELETE не делал
            # никто, и каждый когда-либо открывавший диалог носил свою строку
            # навсегда — её читали на каждом апдейте оба хранилища.
            await self._session.delete(rec)
        else:
            rec.state = value

    async def get_state(self, key: StorageKey) -> str | None:
        rec = await self._session.get(FsmRecord, _key(key))
        return rec.state if rec else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        row_key = _key(key)
        rec = await self._session.get(FsmRecord, row_key)
        if rec is None:
            # Как и в set_state: пустое значение при отсутствующей строке —
            # ничего. Без этой ветки FSMContext.clear() (а это set_state(None)
            # + set_data({})) вставлял пустую строку КАЖДОМУ, кто нажал любую
            # кнопку: ClearStateOnCallbackMiddleware зовёт clear() на каждом
            # нажатии. Навигация превращалась в запись — с блокировкой записи и
            # кадром WAL, — а таблица копила по мусорной строке на человека,
            # который ни одного диалога не открывал.
            if not data:
                return
            self._session.add(FsmRecord(key=row_key, state=None, data=dict(data)))
        elif not data and rec.state is None:
            # Второй шаг clear() (set_state(None) уже прошёл): строка пуста —
            # удаляем, см. комментарий в set_state.
            await self._session.delete(rec)
        else:
            rec.data = dict(data)  # новый объект — SQLAlchemy заметит изменение JSON

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        rec = await self._session.get(FsmRecord, _key(key))
        return dict(rec.data) if rec and rec.data else {}

    async def close(self) -> None:
        pass


class ReadOnlyFsmView(BaseStorage):
    """Хранилище уровня диспетчера: заглушка на чтение, запрет на запись.

    Чтения возвращают «пусто» БЕЗ похода в базу — см. докстринг модуля: снимок
    raw_state, который aiogram собирает поверх этого хранилища, безусловно
    перезаписывается FsmSessionMiddleware до первого фильтра. Настоящее чтение
    одно, запросной сессией, внутри замка.
    """

    async def get_state(self, key: StorageKey) -> str | None:
        return None

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return {}

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
