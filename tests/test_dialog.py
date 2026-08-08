"""Диалоги целиком, через настоящий диспетчер.

Это тот тест, которого не хватило 07.08.2026. Тогда идемпотентность апдейтов
проверялась вызовом middleware напрямую — и не заметила, что сессия запроса
блокирует запись FSM. В проде это выглядело так: пользователь вводит имя
ученика, получает «Не получилось выполнить действие», ученик не сохраняется.

Здесь диалог проходит по всему пути: тот же диспетчер, что в проде, те же
middleware, то же FSM-хранилище, та же схема из миграций.
"""

from tests.reading import student_prices

ADMIN = 1  # совпадает с TEST_ADMIN_IDS в conftest


async def test_добавление_ученика_проходит_весь_диалог(harness, sessionmaker):
    """Кнопка → имя → цена → ученик в базе.

    Падение на любом шаге означает, что два писателя в SQLite снова спорят за
    блокировку: FSM пишет состояние, обработчик — данные.
    """
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    await harness.send("1600", user_id=ADMIN)

    assert await student_prices(sessionmaker) == [("Лера", 160000)], "цена хранится в копейках"

    replies = " ".join(harness.session.sent_texts())
    assert "Не получилось выполнить действие" not in replies


async def test_диалог_переживает_пересборку_диспетчера(harness, sessionmaker, container):
    """Состояние диалога живёт в БД именно ради этого: деплой перезапускает
    процесс, а начатый ввод не должен пропадать.

    Здесь пересборка диспетчера изображает рестарт: FSM-состояние читается из
    той же базы, и диалог продолжается с того же места.
    """
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)

    # «Рестарт»: новый диспетчер поверх той же базы. Роутеры объявлены
    # синглтонами модуля и Router живёт с одним родителем, поэтому перед
    # пересборкой их надо отцепить — как это делает фикстура между тестами.
    from bot.__main__ import build_dispatcher
    from tests.conftest import _SHARED_ROUTERS, TEST_ADMIN_IDS

    await harness.dp.emit_shutdown()
    for router in _SHARED_ROUTERS:
        router._parent_router = None  # noqa: SLF001
    harness.dp = build_dispatcher(container, TEST_ADMIN_IDS)
    await harness.dp.emit_startup()

    await harness.send("1600", user_id=ADMIN)

    assert await student_prices(sessionmaker) == [("Лера", 160000)]


async def test_посторонний_не_проходит_дальше_доступа(harness, sessionmaker):
    """Бот молчит незнакомцам, и диалог для них не начинается."""
    await harness.click("add", user_id=999)
    await harness.send("Чужой", user_id=999)
    await harness.send("1600", user_id=999)

    assert await student_prices(sessionmaker) == []
