"""FSM-состояния диалогов (ожидание текстового ввода)."""

from aiogram.fsm.state import State, StatesGroup


class Flow(StatesGroup):
    payment_amount = State()  # ждём сумму оплаты
    new_name = State()        # ждём имя нового ученика
    new_price = State()       # ждём стоимость нового ученика
    price_change = State()    # ждём новую стоимость
    search = State()          # ждём поисковый запрос
