"""Тесты на уровне обработчиков: межтенантная изоляция в on_callback,
устойчивость к подделанной callback_data, сохранение сортировки,
персистентность FSM-состояния."""

from types import SimpleNamespace

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot import repo
from bot.handlers import on_callback
from bot.storage import SqlAlchemyStorage

A, B = 111, 222


class FakeBot:
    async def delete_message(self, chat_id, message_id):
        pass


class FakeMessage:
    def __init__(self, chat_id, message_id=100):
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.message_id = message_id
        self.bot = FakeBot()
        self.edits: list = []
        self.answers: list = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))
        return SimpleNamespace(message_id=999)


class FakeCallback:
    def __init__(self, uid, data, message):
        self.from_user = SimpleNamespace(id=uid)
        self.data = data
        self.message = message
        self.id = "q1"
        self.answers: list = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


async def _run(session, uid, data):
    msg = FakeMessage(chat_id=uid)
    cb = FakeCallback(uid, data, msg)
    await on_callback(cb, session, _state(uid))
    return cb, msg


# ---------- межтенантная изоляция ----------

async def test_foreign_card_not_visible(session):
    a = await repo.create_student(session, A, "Аня", 160000)
    _cb, msg = await _run(session, B, f"card:{a.id}")
    assert msg.edits and "не найден" in msg.edits[-1][0].lower()


async def test_foreign_charge_rejected(session):
    a = await repo.create_student(session, A, "Аня", 160000)
    cb, _msg = await _run(session, B, f"charge:{a.id}")
    assert cb.answers and "не найден" in (cb.answers[-1][0] or "").lower()
    fresh = await repo.get_student(session, A, a.id)
    assert fresh.balance == 0  # чужое списание не применилось


async def test_foreign_payment_rejected(session):
    a = await repo.create_student(session, A, "Аня", 160000)
    cb, _msg = await _run(session, B, f"pay:{a.id}")
    assert cb.answers and "не найден" in (cb.answers[-1][0] or "").lower()


async def test_foreign_undo_sees_nothing(session):
    a = await repo.create_student(session, A, "Аня", 160000)
    await repo.apply_payment(session, A, a.id, 160000)
    cb, _msg = await _run(session, B, "undo")
    assert cb.answers and "отменять нечего" in (cb.answers[-1][0] or "").lower()


# ---------- владелец: действие проходит ----------

async def test_owner_charge_applies(session):
    a = await repo.create_student(session, A, "Аня", 160000)
    _cb, msg = await _run(session, A, f"charge:{a.id}")
    fresh = await repo.get_student(session, A, a.id)
    assert fresh.balance == -1 and msg.edits


# ---------- подделанная callback_data не роняет обработчик ----------

async def test_malformed_callback_data(session):
    for data in ("card:abc", "charge:", "pay:xx"):
        cb, _msg = await _run(session, A, data)
        assert cb.answers and "устарел" in (cb.answers[-1][0] or "").lower()


# ---------- сохранение сортировки при возврате к списку ----------

async def test_sort_preserved_on_home(session):
    await repo.create_student(session, A, "Борис", 200000)
    anya = await repo.create_student(session, A, "Аня", 160000)
    await repo.apply_payment(session, A, anya.id, 800000)  # Аня: +5
    await _run(session, A, "list:due:0")           # выбрали «скоро оплата»
    _cb, msg = await _run(session, A, "home")       # вернулись к списку
    assert "скоро оплата" in msg.edits[-1][0]


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
