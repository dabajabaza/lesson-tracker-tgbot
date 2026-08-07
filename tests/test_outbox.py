"""Обещание доставки переживает падение отправки.

Порядок «обработчик → коммит → отправка» снял потолок пропускной способности,
но взамен разорвал связь между «операция применена» и «пользователь об этом
узнал». Для учёта денег это опасно: не увидев подтверждения, преподаватель
вводит сумму заново — и оплата задваивается.

Очередь эту дыру закрывает. Обещание ответить пишется в ту же транзакцию, что и
операция; удаётся отправить — строка исчезает; не удаётся — её дожимает
фоновый отправщик. Гарантия «хотя бы один раз»: дубль ответа безобиден,
потеря — нет.
"""

import asyncio
from unittest.mock import patch

from sqlalchemy import select

from bot import outbox
from bot.models import OutboxMessage, Student
from bot.services import StudentService
from tests.bot_harness import make_update_callback

ADMIN = 1


async def _queued(sessionmaker) -> list[OutboxMessage]:
    async with sessionmaker() as s:
        return list(await s.scalars(select(OutboxMessage).order_by(OutboxMessage.id)))


async def _students(sessionmaker) -> list[tuple[str, int]]:
    async with sessionmaker() as s:
        return [(st.name, st.balance) for st in await s.scalars(select(Student))]


async def test_удачная_отправка_не_оставляет_следов(harness, sessionmaker):
    """Штатный путь: ответ ушёл сразу, очередь снова пуста. Иначе таблица
    росла бы на каждый апдейт, а фоновый отправщик слал бы дубли."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    await harness.send("1600", user_id=ADMIN)

    assert await _students(sessionmaker) == [("Лера", 0)]
    assert await _queued(sessionmaker) == []


async def test_сбой_отправки_оставляет_обещание_и_оно_дожимается(harness, sessionmaker):
    """Главный сценарий. Операция применена, ответ не ушёл — обещание осталось
    в очереди, и следующий заход отправщика его выполняет."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)

    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=ADMIN)
    del harness.session.fail_on["SendMessage"]

    assert await _students(sessionmaker) == [("Лера", 0)], "операция обязана быть применена"
    queued = await _queued(sessionmaker)
    assert [row.method for row in queued] == ["SendMessage"]
    assert "Ученик добавлен" in queued[0].payload

    harness.session.clear()
    sent = await outbox.deliver_batch(harness.bot, sessionmaker, asyncio.Lock())

    assert sent == 1
    assert any("Ученик добавлен" in t for t in harness.session.sent_texts())
    assert await _queued(sessionmaker) == [], "доставленное обещание должно исчезнуть"
    assert await _students(sessionmaker) == [("Лера", 0)], "повтор доставки не трогает данные"


async def test_повторная_доставка_не_повторяет_операцию(harness, session, sessionmaker):
    """Дожимается ОТВЕТ, а не действие. Иначе очередь стала бы вторым, тихим
    способом списать урок."""
    students = StudentService(session)
    a = await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    harness.session.fail_on["EditMessageText"] = RuntimeError("сеть упала")
    await harness.dp.feed_update(
        harness.bot, make_update_callback(f"charge:{a.id}", user_id=ADMIN, update_id=8001)
    )
    del harness.session.fail_on["EditMessageText"]

    assert await _students(sessionmaker) == [("Аня", -1)]
    await outbox.deliver_batch(harness.bot, sessionmaker, asyncio.Lock())
    assert await _students(sessionmaker) == [("Аня", -1)], "баланс не должен уехать в -2"


async def test_неудача_дожимки_переносит_попытку(harness, sessionmaker):
    """Сеть лежит и на повторе: строка остаётся, но следующая попытка
    откладывается — иначе отправщик колотился бы в неё каждые пять секунд."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=ADMIN)

    sent = await outbox.deliver_batch(harness.bot, sessionmaker, asyncio.Lock())

    assert sent == 0
    queued = await _queued(sessionmaker)
    assert len(queued) == 1
    assert queued[0].attempts == 1
    assert queued[0].next_attempt_at > queued[0].created_at, "повтор обязан быть отложен"


async def test_откат_не_оставляет_обещания(harness, sessionmaker):
    """Обещание живёт в одной транзакции с операцией: не случилось операции —
    не должно остаться и обещания ответить о ней."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)

    with patch.object(StudentService, "create", side_effect=RuntimeError("диск отвалился")):
        await harness.send("1600", user_id=ADMIN)

    assert await _students(sessionmaker) == []
    assert await _queued(sessionmaker) == []


async def test_просроченное_обещание_выбрасывается(harness, sessionmaker):
    """Ответ суточной давности пользователю уже не нужен, а таблица не должна
    расти вечно."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=ADMIN)
    del harness.session.fail_on["SendMessage"]

    async with sessionmaker() as s:
        row = (await s.scalars(select(OutboxMessage))).one()
        row.created_at -= outbox.TTL + 1
        await s.commit()

    await outbox.purge_expired(sessionmaker, asyncio.Lock())
    assert await _queued(sessionmaker) == []


async def test_нечитаемое_обещание_не_застревает(harness, sessionmaker):
    """Строка, которую нечем отправить (формат payload разошёлся с кодом),
    удаляется, а не блокирует очередь навсегда."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=ADMIN)
    del harness.session.fail_on["SendMessage"]

    async with sessionmaker() as s:
        row = (await s.scalars(select(OutboxMessage))).one()
        row.payload = "{это не json"
        await s.commit()

    await outbox.deliver_batch(harness.bot, sessionmaker, asyncio.Lock())
    assert await _queued(sessionmaker) == []
