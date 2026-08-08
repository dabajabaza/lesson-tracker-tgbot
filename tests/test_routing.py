"""Свойства маршрутизации, которые легко потерять при разрезании обработчиков.

Раньше всё это держалось на порядке веток одного большого if/elif и на паре
строк в его начале. Теперь порядок задаётся подключением роутеров, а общее
правило — middleware; и то, и другое ломается молча. Поэтому пинним тестами.
"""

import asyncio

from sqlalchemy import select

from bot.models import FsmRecord, Student
from bot.services import StudentService
from tests.bot_harness import make_update_message

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


async def test_нажатие_кнопки_сбрасывает_начатый_ввод(harness, session, sessionmaker, students):
    """«Введите сумму» для одного ученика не должно пережить переход к другому."""
    a = await students.create(ADMIN, "Аня", 160000)
    b = await students.create(ADMIN, "Боря", 200000)
    await session.commit()

    await harness.click(f"pay:{a.id}", user_id=ADMIN)  # начали оплату Ане
    await harness.click(f"card:{b.id}", user_id=ADMIN)  # ушли к Боре
    await harness.send("5000", user_id=ADMIN)  # это уже не сумма

    async with sessionmaker() as chk:
        fresh_a = await StudentService(chk).get(ADMIN, a.id)
        fresh_b = await StudentService(chk).get(ADMIN, b.id)
    assert fresh_a.balance == 0, "оплата не должна была примениться к Ане"
    assert fresh_b.balance == 0, "и к Боре тоже"


async def test_noop_не_сбрасывает_ввод(harness, session, sessionmaker, students):
    """Единственное исключение из правила выше: noop — это неактивная кнопка
    вроде номера страницы, она не должна ломать начатый ввод."""
    a = await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    await harness.click(f"pay:{a.id}", user_id=ADMIN)
    await harness.click("noop", user_id=ADMIN)
    await harness.send("1600", user_id=ADMIN)

    async with sessionmaker() as chk:
        fresh = await StudentService(chk).get(ADMIN, a.id)
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


async def test_два_быстрых_ответа_не_возвращают_диалог_назад(harness, sessionmaker):
    """StateFilter обязан смотреть на состояние внутри замка, а не на снимок
    до него.

    Снимок raw_state берёт FSMContextMiddleware aiogram ещё до того, как
    апдейт войдёт в общий замок. Два быстрых сообщения на шаге «введите
    стоимость» получали один и тот же снимок; второе выигрывало фильтр
    Flow.new_price уже после того, как первое создало ученика и очистило
    состояние, не находило имени и уводило диалог обратно на «Введите имя
    ученика». Преподаватель только что успешно добавил ученика — и снова
    видит вопрос про имя, а следующее его сообщение молча уходит в имя.
    """
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)

    await asyncio.gather(
        harness.dp.feed_update(
            harness.bot, make_update_message("1600", user_id=ADMIN, update_id=7101)
        ),
        harness.dp.feed_update(
            harness.bot, make_update_message("1600", user_id=ADMIN, update_id=7102)
        ),
    )

    async with sessionmaker() as s:
        names = list(await s.scalars(select(Student.name)))
        states = [r.state for r in await s.scalars(select(FsmRecord))]

    assert names == ["Лера"], "ученик должен быть создан ровно один раз"
    assert states in ([None], []), f"диалог не должен остаться открытым: {states}"


async def test_админская_команда_прерывает_начатый_ввод(harness, session, sessionmaker):
    """Роутер админки подключён раньше основного и забирает апдейт целиком,
    поэтому сбросить диалог обязан он сам — как это делают /start и /menu.

    Иначе: преподаватель нажал «Внести оплату», вместо суммы набрал /access —
    и следующее же число, отправленное по любому поводу, молча ушло бы в
    оплату этому ученику.
    """
    students = StudentService(session)
    a = await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    await harness.click(f"pay:{a.id}", user_id=ADMIN)
    await harness.send("/access", user_id=ADMIN)
    await harness.send("500", user_id=ADMIN)

    async with sessionmaker() as s:
        fresh = await StudentService(s).get(ADMIN, a.id)
    assert fresh.balance == 0, "число после админской команды не должно стать оплатой"
