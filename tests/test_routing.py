"""Свойства маршрутизации, которые легко потерять при разрезании обработчиков.

Раньше всё это держалось на порядке веток одного большого if/elif и на паре
строк в его начале. Теперь порядок задаётся подключением роутеров, а общее
правило — middleware; и то, и другое ломается молча. Поэтому пинним тестами.
"""

import asyncio

from sqlalchemy import select

from lesson_tracker.db.models import FsmRecord, Operation, Student
from lesson_tracker.services import StudentService, access
from tests.helpers.bot_harness import make_update_message
from tests.helpers.reading import last_callback_answer, student_names

ADMIN = 1


async def test_start_interrupts_the_dialog_instead_of_becoming_a_name(harness, sessionmaker):
    """У /start нет фильтра состояния, поэтому он обязан выигрывать у
    FSM-обработчика. Подключи роутер меню не первым — и «/start» посреди
    добавления ученика был бы принят за имя."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("/start", user_id=ADMIN)

    assert await student_names(sessionmaker) == []
    # Диалог сброшен: следующее число уже не воспринимается как цена.
    await harness.send("1600", user_id=ADMIN)
    assert await student_names(sessionmaker) == []


async def test_pressing_a_button_resets_input_in_progress(harness, session, sessionmaker, students):
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


async def test_noop_does_not_reset_input(harness, session, sessionmaker, students):
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


async def test_an_unrecognized_callback_gets_an_answer(harness):
    """Хвостовой роутер: без него кнопка с незнакомыми данными оставила бы
    вечно крутящийся индикатор."""
    await harness.click("такой_команды_нет", user_id=ADMIN)
    assert last_callback_answer(harness) == ""


async def test_a_forged_id_does_not_crash_the_handler(harness):
    """Подделанный id не роняет обработчик."""
    for data in ("card:abc", "charge:", "pay:xx", "hist:zz", "price:!"):
        await harness.click(data, user_id=ADMIN)
        assert "устарел" in last_callback_answer(harness).lower(), (
            f"на {data} ждём «Кнопка устарела»"
        )


async def test_two_fast_replies_do_not_send_the_dialog_backwards(harness, sessionmaker):
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


async def test_an_admin_command_interrupts_input_in_progress(harness, session, sessionmaker):
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
    # Сумма заведомо больше стоимости занятия: будь диалог жив, оплата
    # применилась бы и это было бы видно и по балансу, и по журналу операций.
    await harness.send("1600", user_id=ADMIN)

    async with sessionmaker() as s:
        fresh = await StudentService(s).get(ADMIN, a.id)
        operations = list(await s.scalars(select(Operation)))
    assert fresh.balance == 0, "число после админской команды не должно стать оплатой"
    assert operations == [], "и в журнале операций взяться неоткуда"


async def test_an_admin_command_interrupts_the_dialog_for_non_admins_too(
    harness, session, sessionmaker
):
    """Роутер админки поглощает команду у кого угодно — значит и диалог обязан
    сбрасывать у кого угодно.

    Фильтр Command срабатывает для всех, привилегии проверяет уже обработчик.
    Пока сброс стоял после проверки прав, допущенный неадмин (их впускает
    инвайт) попадал в ту же ловушку, от которой избавили админов: команда
    молча съедена, диалог оплаты жив, следующее число уходит в оплату.
    """
    guest = 555
    async with sessionmaker() as s:
        await access.allow_user(s, guest, "гость")
        await s.commit()

    students = StudentService(session)
    a = await students.create(guest, "Аня", 160000)
    await session.commit()

    await harness.click(f"pay:{a.id}", user_id=guest)
    await harness.send("/access", user_id=guest)
    await harness.send("1600", user_id=guest)

    async with sessionmaker() as s:
        fresh = await StudentService(s).get(guest, a.id)
        operations = list(await s.scalars(select(Operation)))
    assert fresh.balance == 0, "число после админской команды не должно стать оплатой"
    assert operations == [], "и в журнале операций взяться неоткуда"
