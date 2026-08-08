"""Свойства единицы работы, ради которых затевался переезд.

07.08.2026 бот лёг в проде: FSM-хранилище писало в собственной сессии и
упёрлось в блокировку записи сессии запроса («database is locked»). Эти тесты
фиксируют новое устройство: писатель один, состояние диалога и бизнес-запись
живут в одной транзакции, а запись мимо неё структурно невозможна.
"""

import pytest
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import FsmRecord, Student
from bot.storage import ReadOnlyFsmView, SqlAlchemyStorage, _key
from tests.reading import fsm_state, student_names

KEY = StorageKey(bot_id=1, chat_id=42, user_id=42)


async def test_fsm_и_бизнес_запись_фиксируются_одной_транзакцией(container, sessionmaker):
    async with container() as scope:
        session = await scope.get(AsyncSession)
        storage = await scope.get(BaseStorage)

        await storage.set_state(KEY, "Flow:new_price")
        session.add(Student(owner_id=42, name="Лера", name_lower="лера", price=160000))
        await session.commit()  # в бою это делает DbSessionMiddleware

    assert await fsm_state(sessionmaker) == "Flow:new_price"
    assert await student_names(sessionmaker) == ["Лера"]


async def test_исключение_откатывает_и_состояние_и_данные(container, sessionmaker):
    """Обе записи исчезают вместе. Ни «состояние без данных», ни «данные без
    состояния» не существуют — промежуточных исходов у транзакции нет."""
    with pytest.raises(RuntimeError, match="обработчик упал"):
        async with container() as scope:
            session = await scope.get(AsyncSession)
            storage = await scope.get(BaseStorage)

            await storage.set_state(KEY, "Flow:new_price")
            session.add(Student(owner_id=42, name="Лера", name_lower="лера", price=160000))
            # Коммита не будет: исключение уходит в провайдер сессии (dishka
            # передаёт его через asend), и тот откатывает всё разом.
            raise RuntimeError("обработчик упал")

    assert await fsm_state(sessionmaker) is None
    assert await student_names(sessionmaker) == []


async def test_хранилище_и_сессия_запроса_делят_одну_транзакцию(container):
    """Хранилище видит незакоммиченную запись сессии — значит, соединение
    общее. Два разных соединения друг для друга невидимы до коммита."""
    async with container() as scope:
        session = await scope.get(AsyncSession)
        storage = await scope.get(BaseStorage)
        assert isinstance(storage, SqlAlchemyStorage)

        # Ключ строим той же функцией, что и хранилище: литерал здесь однажды
        # разошёлся с форматом и тест упал не по делу.
        session.add(FsmRecord(key=_key(KEY), state="Flow:probe", data={}))
        assert await storage.get_state(KEY) == "Flow:probe"


async def test_хранилище_диспетчера_отказывается_писать(container):
    """Инвариант «пишет только область запроса» — свойство кода: если будущая
    версия aiogram начнёт писать через хранилище диспетчера, это громко упадёт
    здесь, а не всплывёт как «database is locked» в проде."""
    view = ReadOnlyFsmView()

    assert await view.get_state(KEY) is None  # чтение — заглушка, всегда пусто

    with pytest.raises(RuntimeError, match="мимо области запроса"):
        await view.set_state(KEY, "Flow:new_price")
    with pytest.raises(RuntimeError, match="мимо области запроса"):
        await view.set_data(KEY, {"probe": 1})


async def test_сбой_отправки_не_отменяет_записанное(harness, sessionmaker):
    """Сеть больше не участвует в транзакции, и провал отправки её не рушит.

    Порядок стал «обработчик → коммит → отправка»: пока отправка стояла внутри
    транзакции, блокировка записи держалась весь круг до Telegram, и бот упирался
    в единицы апдейтов в секунду. Плата за разворот — сбой отправки оставляет
    пользователя в неведении о применённой операции, и закрывает это очередь
    доставки (см. test_outbox): операция применена ровно один раз, а ответ
    дожимается ретраями.

    Что здесь важно проверить именно сейчас — что провал сети не отменяет
    записанное молча и не создаёт дубля при повторе.
    """
    await harness.click("add", user_id=1)
    await harness.send("Лера", user_id=1)

    # Сеть падает ровно на финальном ответе «Ученик добавлен».
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=1)
    del harness.session.fail_on["SendMessage"]

    assert await student_names(sessionmaker) == ["Лера"], "коммит уже случился до отправки"

    # Повтор того же диалога не задваивает ученика: имя занято.
    await harness.click("add", user_id=1)
    await harness.send("Лера", user_id=1)
    await harness.send("1600", user_id=1)
    assert await student_names(sessionmaker) == ["Лера"]
