"""Свойства маршрутизации, которые легко потерять при разрезании обработчиков.

Раньше всё это держалось на порядке веток одного большого if/elif и на паре
строк в его начале. Теперь порядок задаётся подключением роутеров, а общее
правило — middleware; и то, и другое ломается молча. Поэтому пинним тестами.
"""

from sqlalchemy import select

from bot import repo
from bot.models import Student

ADMIN = 1


def _last_answer(harness) -> str:
    answers = harness.session.calls_of("AnswerCallbackQuery")
    assert answers, "на колбэк обязан быть ответ, иначе кнопка «крутится»"
    return answers[-1].text or ""


async def _students(sessionmaker) -> list[str]:
    async with sessionmaker() as s:
        return list(await s.scalars(select(Student.name)))


async def test_start_прерывает_диалог_а_не_становится_именем(harness, sessionmaker):
    """У /start нет фильтра состояния, поэтому он обязан выигрывать у
    FSM-обработчика. Подключи роутер меню не первым — и «/start» посреди
    добавления ученика был бы принят за имя."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("/start", user_id=ADMIN)

    assert await _students(sessionmaker) == []
    # Диалог сброшен: следующее число уже не воспринимается как цена.
    await harness.send("1600", user_id=ADMIN)
    assert await _students(sessionmaker) == []


async def test_нажатие_кнопки_сбрасывает_начатый_ввод(harness, session, sessionmaker):
    """«Введите сумму» для одного ученика не должно пережить переход к другому."""
    a = await repo.create_student(session, ADMIN, "Аня", 160000)
    b = await repo.create_student(session, ADMIN, "Боря", 200000)
    await session.commit()

    await harness.click(f"pay:{a.id}", user_id=ADMIN)  # начали оплату Ане
    await harness.click(f"card:{b.id}", user_id=ADMIN)  # ушли к Боре
    await harness.send("5000", user_id=ADMIN)  # это уже не сумма

    async with sessionmaker() as chk:
        fresh_a = await repo.get_student(chk, ADMIN, a.id)
        fresh_b = await repo.get_student(chk, ADMIN, b.id)
    assert fresh_a.balance == 0, "оплата не должна была примениться к Ане"
    assert fresh_b.balance == 0, "и к Боре тоже"


async def test_noop_не_сбрасывает_ввод(harness, session, sessionmaker):
    """Единственное исключение из правила выше: noop — это неактивная кнопка
    вроде номера страницы, она не должна ломать начатый ввод."""
    a = await repo.create_student(session, ADMIN, "Аня", 160000)
    await session.commit()

    await harness.click(f"pay:{a.id}", user_id=ADMIN)
    await harness.click("noop", user_id=ADMIN)
    await harness.send("1600", user_id=ADMIN)

    async with sessionmaker() as chk:
        fresh = await repo.get_student(chk, ADMIN, a.id)
    assert fresh.balance == 1, "оплата обязана была примениться"


async def test_нераспознанный_колбэк_получает_ответ(harness):
    """Хвостовой роутер: без него кнопка с незнакомыми данными оставила бы
    вечно крутящийся индикатор."""
    await harness.click("такой_команды_нет", user_id=ADMIN)
    assert _last_answer(harness) == ""


async def test_подделанный_id_не_роняет_обработчик(harness):
    for data in ("card:abc", "charge:", "pay:xx", "hist:zz", "price:!"):
        await harness.click(data, user_id=ADMIN)
        assert "устарел" in _last_answer(harness).lower(), f"на {data} ждём «Кнопка устарела»"
