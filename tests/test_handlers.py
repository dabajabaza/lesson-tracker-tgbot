"""Тесты обработчиков: межтенантная изоляция, подделанная callback_data,
сохранение сортировки, персистентность FSM-состояния.

Прогон идёт через настоящий диспетчер (harness), а не вызовом on_callback с
самодельными объектами: подделки в вакууме не проходят isinstance-гарды
обработчика и не видят middleware. Так эти тесты однажды пропустили взаимную
блокировку сессий — больше им это не светит.
"""

from aiogram.fsm.storage.base import StorageKey

from bot import repo
from bot.storage import SqlAlchemyStorage

A, B = 111, 222  # два преподавателя; оба в TEST_ADMIN_IDS (см. conftest)


def _last_edit(harness) -> str:
    edits = harness.session.calls_of("EditMessageText")
    assert edits, "обработчик должен был отредактировать сообщение"
    return edits[-1].text


def _last_answer(harness) -> str:
    answers = harness.session.calls_of("AnswerCallbackQuery")
    assert answers, "обработчик должен был ответить на колбэк"
    return answers[-1].text or ""


# ---------- межтенантная изоляция ----------


async def test_foreign_card_not_visible(harness, session):
    a = await repo.create_student(session, A, "Аня", 160000)
    await harness.click(f"card:{a.id}", user_id=B)
    assert "не найден" in _last_edit(harness).lower()


async def test_foreign_charge_rejected(harness, session):
    a = await repo.create_student(session, A, "Аня", 160000)
    await harness.click(f"charge:{a.id}", user_id=B)
    assert "не найден" in _last_answer(harness).lower()
    fresh = await repo.get_student(session, A, a.id)
    assert fresh.balance == 0  # чужое списание не применилось


async def test_foreign_payment_rejected(harness, session):
    a = await repo.create_student(session, A, "Аня", 160000)
    await harness.click(f"pay:{a.id}", user_id=B)
    assert "не найден" in _last_answer(harness).lower()


async def test_foreign_undo_sees_nothing(harness, session):
    a = await repo.create_student(session, A, "Аня", 160000)
    await repo.apply_payment(session, A, a.id, 160000)
    await harness.click("undo", user_id=B)
    assert "отменять нечего" in _last_answer(harness).lower()


# ---------- владелец: действие проходит ----------


async def test_owner_charge_applies(harness, session, sessionmaker):
    a = await repo.create_student(session, A, "Аня", 160000)
    await harness.click(f"charge:{a.id}", user_id=A)
    # Обработчик коммитил в собственной сессии; читаем свежей, а не протухшим
    # кэшем этой (expire_all в async-сессии кончается MissingGreenlet на
    # ленивой перезагрузке).
    async with sessionmaker() as check:
        fresh = await repo.get_student(check, A, a.id)
    assert fresh.balance == -1
    assert harness.session.calls_of("EditMessageText")


# ---------- подделанная callback_data не роняет обработчик ----------


async def test_malformed_callback_data(harness):
    for data in ("card:abc", "charge:", "pay:xx"):
        await harness.click(data, user_id=A)
        assert "устарел" in _last_answer(harness).lower()


# ---------- сохранение сортировки при возврате к списку ----------


async def test_sort_preserved_on_home(harness, session):
    await repo.create_student(session, A, "Борис", 200000)
    anya = await repo.create_student(session, A, "Аня", 160000)
    await repo.apply_payment(session, A, anya.id, 800000)  # Аня: +5
    await harness.click("list:due:0", user_id=A)  # выбрали «скоро оплата»
    await harness.click("home", user_id=A)  # вернулись к списку
    assert "скоро оплата" in _last_edit(harness)


# ---------- персистентность FSM (переживает «рестарт») ----------


async def test_persistent_fsm_storage(sessionmaker):
    st = SqlAlchemyStorage(sessionmaker)
    key = StorageKey(bot_id=1, chat_id=5, user_id=5)
    await st.set_state(key, "Flow:payment_amount")
    await st.set_data(key, {"student_id": 7})
    # эмулируем рестарт процесса: новый storage поверх той же БД
    st2 = SqlAlchemyStorage(sessionmaker)
    assert await st2.get_state(key) == "Flow:payment_amount"
    assert await st2.get_data(key) == {"student_id": 7}
    await st.set_state(key, None)
    assert await st2.get_state(key) is None
