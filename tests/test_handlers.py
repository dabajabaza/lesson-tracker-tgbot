"""Тесты обработчиков: межтенантная изоляция, подделанная callback_data,
сохранение сортировки, персистентность FSM-состояния.

Прогон идёт через настоящий диспетчер (harness), а не вызовом on_callback с
самодельными объектами: подделки в вакууме не проходят isinstance-гарды
обработчика и не видят middleware. Так эти тесты однажды пропустили взаимную
блокировку сессий — больше им это не светит.
"""

from aiogram.fsm.storage.base import StorageKey

from bot.services import StudentService
from bot.storage import SqlAlchemyStorage
from tests.reading import last_callback_answer, last_edit

A, B = 111, 222  # два преподавателя; оба в TEST_ADMIN_IDS (см. conftest)


async def test_foreign_card_not_visible(harness, session, students):
    a = await students.create(A, "Аня", 160000)
    await session.commit()  # repo не коммитит: подготовка фиксирует сама
    await harness.click(f"card:{a.id}", user_id=B)
    assert "не найден" in last_edit(harness).lower()


async def test_foreign_charge_rejected(harness, session, students):
    a = await students.create(A, "Аня", 160000)
    await session.commit()
    await harness.click(f"charge:{a.id}", user_id=B)
    assert "не найден" in last_callback_answer(harness).lower()
    fresh = await students.get(A, a.id)
    assert fresh.balance == 0  # чужое списание не применилось


async def test_foreign_payment_rejected(harness, session, students):
    a = await students.create(A, "Аня", 160000)
    await session.commit()
    await harness.click(f"pay:{a.id}", user_id=B)
    assert "не найден" in last_callback_answer(harness).lower()


async def test_foreign_undo_sees_nothing(harness, session, students, payments):
    a = await students.create(A, "Аня", 160000)
    await payments.apply(A, a.id, 160000)
    await session.commit()
    await harness.click("undo", user_id=B)
    assert "отменять нечего" in last_callback_answer(harness).lower()


# ---------- владелец: действие проходит ----------


async def test_owner_charge_applies(harness, session, sessionmaker, students):
    a = await students.create(A, "Аня", 160000)
    await session.commit()
    await harness.click(f"charge:{a.id}", user_id=A)
    # Обработчик коммитил в собственной сессии; читаем свежей, а не протухшим
    # кэшем этой (expire_all в async-сессии кончается MissingGreenlet на
    # ленивой перезагрузке).
    async with sessionmaker() as check:
        fresh = await StudentService(check).get(A, a.id)
    assert fresh.balance == -1
    assert harness.session.calls_of("EditMessageText")


# ---------- подделанная callback_data не роняет обработчик ----------


async def test_malformed_callback_data(harness):
    for data in ("card:abc", "charge:", "pay:xx"):
        await harness.click(data, user_id=A)
        assert "устарел" in last_callback_answer(harness).lower()


# ---------- сохранение сортировки при возврате к списку ----------


async def test_sort_preserved_on_home(harness, session, students, payments):
    await students.create(A, "Борис", 200000)
    anya = await students.create(A, "Аня", 160000)
    await payments.apply(A, anya.id, 800000)  # Аня: +5
    await session.commit()
    await harness.click("list:due:0", user_id=A)  # выбрали «скоро оплата»
    await harness.click("home", user_id=A)  # вернулись к списку
    assert "скоро оплата" in last_edit(harness)


# ---------- персистентность FSM (переживает «рестарт») ----------


async def test_persistent_fsm_storage(sessionmaker):
    """Состояние, записанное в одной сессии и закоммиченное, читается из
    другой — это и есть «переживает рестарт процесса»."""
    key = StorageKey(bot_id=1, chat_id=5, user_id=5)
    async with sessionmaker() as s1:
        st = SqlAlchemyStorage(s1)
        await st.set_state(key, "Flow:payment_amount")
        await st.set_data(key, {"student_id": 7})
        await s1.commit()  # в бою это делает DbSessionMiddleware

    # эмулируем рестарт процесса: новая сессия поверх той же БД
    async with sessionmaker() as s2:
        st2 = SqlAlchemyStorage(s2)
        assert await st2.get_state(key) == "Flow:payment_amount"
        assert await st2.get_data(key) == {"student_id": 7}
        await st2.set_state(key, None)
        await s2.commit()

    async with sessionmaker() as s3:
        assert await SqlAlchemyStorage(s3).get_state(key) is None
