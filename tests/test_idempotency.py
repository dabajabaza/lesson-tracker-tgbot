"""Повторно доставленный апдейт не применяется дважды.

Telegram подтверждает апдейт только следующим вызовом getUpdates, поэтому
процесс, умерший в промежутке, получает тот же update_id заново — а деплой
убивает бота намеренно. Для учёта денег дубль хуже потери: пропавшее сообщение
пользователь повторит сам, а лишняя оплата тихо испортит баланс.

Первая попытка этой защиты (07.08.2026) была откачена: незакоммиченная отметка
забирала блокировку записи SQLite, а FSM-хранилище писало в отдельной сессии и
падало с «database is locked». Тесты тогда дёргали middleware напрямую и этого
не увидели — здесь всё идёт через настоящий диспетчер.
"""

from sqlalchemy import select

from bot.models import ProcessedUpdate, Student
from tests.bot_harness import make_update_message

ADMIN = 1


async def _students(sessionmaker) -> list[tuple[str, int]]:
    async with sessionmaker() as s:
        return [(st.name, st.price) for st in await s.scalars(select(Student))]


async def _marks(sessionmaker) -> list[int]:
    async with sessionmaker() as s:
        return sorted(await s.scalars(select(ProcessedUpdate.update_id)))


async def test_повтор_апдейта_не_создаёт_второго_ученика(harness, sessionmaker):
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)

    update = make_update_message("1600", user_id=ADMIN, update_id=777)
    await harness.dp.feed_update(harness.bot, update)
    # Ровно это Telegram присылает, если процесс умер до подтверждения offset.
    await harness.dp.feed_update(harness.bot, update)

    assert await _students(sessionmaker) == [("Лера", 160000)], "ученик не должен задвоиться"
    assert 777 in await _marks(sessionmaker)


async def test_разные_апдейты_обрабатываются_оба(harness, sessionmaker):
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    await harness.dp.feed_update(
        harness.bot, make_update_message("1600", user_id=ADMIN, update_id=801)
    )

    await harness.click("add", user_id=ADMIN)
    await harness.send("Женя", user_id=ADMIN)
    await harness.dp.feed_update(
        harness.bot, make_update_message("1800", user_id=ADMIN, update_id=802)
    )

    assert await _students(sessionmaker) == [("Лера", 160000), ("Женя", 180000)]
    marks = await _marks(sessionmaker)
    assert 801 in marks and 802 in marks


async def test_повтор_не_даёт_второго_ответа(harness):
    """Дубль отбрасывается до обработчика, значит и второго подтверждения
    пользователь не увидит — иначе оно читалось бы как вторая оплата."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)

    update = make_update_message("1600", user_id=ADMIN, update_id=903)
    await harness.dp.feed_update(harness.bot, update)
    after_first = len(harness.session.sent_texts())

    await harness.dp.feed_update(harness.bot, update)

    assert len(harness.session.sent_texts()) == after_first


async def test_упавший_апдейт_не_отмечается_и_повторяется(harness, sessionmaker):
    """Отметка живёт в одной транзакции с изменением: откатилось изменение —
    откатилась и она. Иначе потерянный апдейт числился бы применённым, и
    повторить его было бы уже нельзя."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)

    update = make_update_message("1600", user_id=ADMIN, update_id=555)
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.dp.feed_update(harness.bot, update)
    del harness.session.fail_on["SendMessage"]

    assert await _students(sessionmaker) == []
    assert 555 not in await _marks(sessionmaker), "неприменённый апдейт отмечать нельзя"

    # И повторная доставка того же апдейта проходит нормально.
    await harness.dp.feed_update(harness.bot, update)
    assert await _students(sessionmaker) == [("Лера", 160000)]


async def test_старые_отметки_вычищаются(harness, sessionmaker, monkeypatch):
    """Иначе таблица растёт вечно. TTL — неделя при суточном хранении у
    Telegram, с запасом."""
    import bot.middlewares as mw

    async with sessionmaker() as s:
        s.add(ProcessedUpdate(update_id=1, created_at=1))  # 1970 год
        await s.commit()

    monkeypatch.setattr(mw, "_PRUNE_EVERY", 0)  # чистка на ближайшем апдейте
    await harness.send("привет", user_id=ADMIN)

    marks = await _marks(sessionmaker)
    assert 1 not in marks, "просроченная отметка должна быть удалена"
    assert marks, "свежая отметка текущего апдейта должна остаться"
