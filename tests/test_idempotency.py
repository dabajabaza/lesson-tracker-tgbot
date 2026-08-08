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

from unittest.mock import patch

from bot import access
from bot.models import ProcessedUpdate
from bot.services import StudentService
from tests.bot_harness import make_update_message
from tests.reading import processed_update_ids, student_prices

ADMIN = 1


async def test_повтор_апдейта_не_создаёт_второго_ученика(harness, sessionmaker):
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)

    update = make_update_message("1600", user_id=ADMIN, update_id=777)
    await harness.dp.feed_update(harness.bot, update)
    # Ровно это Telegram присылает, если процесс умер до подтверждения offset.
    await harness.dp.feed_update(harness.bot, update)

    assert await student_prices(sessionmaker) == [("Лера", 160000)], "ученик не должен задвоиться"
    assert 777 in await processed_update_ids(sessionmaker)


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

    assert await student_prices(sessionmaker) == [("Лера", 160000), ("Женя", 180000)]
    marks = await processed_update_ids(sessionmaker)
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


async def test_упавший_обработчик_не_отмечается_и_повторяется(harness, sessionmaker):
    """Отметка живёт в одной транзакции с изменением: откатилось изменение —
    откатилась и она. Иначе потерянный апдейт числился бы применённым, и
    повторить его было бы уже нельзя.

    Роняем сохранение, а не отправку: отправка теперь идёт ПОСЛЕ коммита
    (см. middlewares.py) и на судьбу транзакции влиять не может.
    """
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)

    update = make_update_message("1600", user_id=ADMIN, update_id=555)
    with patch.object(StudentService, "create", side_effect=RuntimeError("диск отвалился")):
        await harness.dp.feed_update(harness.bot, update)

    assert await student_prices(sessionmaker) == []
    assert 555 not in await processed_update_ids(sessionmaker), (
        "неприменённый апдейт отмечать нельзя"
    )

    # И повторная доставка того же апдейта проходит нормально.
    await harness.dp.feed_update(harness.bot, update)
    assert await student_prices(sessionmaker) == [("Лера", 160000)]


async def test_старые_отметки_вычищаются(harness, sessionmaker, monkeypatch):
    """Иначе таблица растёт вечно. TTL — неделя при суточном хранении у
    Telegram, с запасом."""
    import bot.middlewares as mw

    async with sessionmaker() as s:
        s.add(ProcessedUpdate(update_id=1, created_at=1))  # 1970 год
        await s.commit()

    monkeypatch.setattr(mw, "_PRUNE_EVERY", 0)  # чистка на ближайшем апдейте
    await harness.send("привет", user_id=ADMIN)

    marks = await processed_update_ids(sessionmaker)
    assert 1 not in marks, "просроченная отметка должна быть удалена"
    assert marks, "свежая отметка текущего апдейта должна остаться"


async def test_чужой_апдейт_не_пишет_в_базу(harness, sessionmaker):
    """Спам не должен стоить строки в таблице и блокировки записи.

    Бот находится в поиске Telegram по имени (L10), так что поток чужих
    апдейтов — штатное явление. Отметка ставилась ДО обработчика, поэтому
    каждое сообщение постороннего фиксировалось коммитом: таблица росла, а
    общий замок записи, ради которого затевалось снятие потолка, тратился на
    того, кому бот всё равно не отвечает. От следующего спам-сообщения с новым
    update_id отметка при этом не спасает — платили ни за что.
    """
    await harness.dp.feed_update(
        harness.bot, make_update_message("привет", user_id=999, update_id=4001)
    )

    assert harness.session.calls == [], "постороннему бот не отвечает"
    assert await processed_update_ids(sessionmaker) == [], "и не пишет о нём в базу"


async def test_допущенный_позже_обрабатывается_нормально(harness, sessionmaker):
    """Отказ не должен «съедать» апдейт навсегда: отметки нет, значит повтор
    того же update_id после выдачи доступа пройдёт как обычно."""
    update = make_update_message("привет", user_id=999, update_id=4002)
    await harness.dp.feed_update(harness.bot, update)
    assert harness.session.calls == []

    async with sessionmaker() as s:
        await access.allow_user(s, 999, "vasya")
        await s.commit()

    await harness.dp.feed_update(harness.bot, update)
    assert harness.session.calls, "допущенный обязан получить ответ"
    assert 4002 in await processed_update_ids(sessionmaker), "теперь отметка нужна"
