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

from unittest.mock import patch

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import GetMe
from sqlalchemy import select

from lesson_tracker import outbox
from lesson_tracker.models import FsmRecord, OutboxMessage, now_ts
from lesson_tracker.services import StudentService
from tests.bot_harness import make_update_callback
from tests.reading import fsm_state, queued_messages, student_balances, student_prices

ADMIN = 1


async def _make_due(sessionmaker) -> None:
    """Приблизить срок повтора: имитируем, что штатное окно отправки прошло.

    Строка выдерживается OUTBOX_GRACE секунд (см. ui.persist), чтобы фоновый
    отправщик не дублировал ответ, который прямо сейчас отправляет штатный
    путь. Тесты про сам отправщик обязаны этот срок перешагнуть, а не ждать.
    """
    async with sessionmaker() as s:
        for row in await s.scalars(select(OutboxMessage)):
            row.next_attempt_at = now_ts() - 1
        await s.commit()


async def test_удачная_отправка_не_оставляет_следов(harness, sessionmaker):
    """Штатный путь: ответ ушёл сразу, очередь снова пуста. Иначе таблица
    росла бы на каждый апдейт, а фоновый отправщик слал бы дубли."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    await harness.send("1600", user_id=ADMIN)

    assert await student_balances(sessionmaker) == [("Лера", 0)]
    assert await queued_messages(sessionmaker) == []


async def test_сбой_отправки_оставляет_обещание_и_оно_дожимается(harness, sessionmaker):
    """Главный сценарий. Операция применена, ответ не ушёл — обещание осталось
    в очереди, и следующий заход отправщика его выполняет."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)

    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=ADMIN)
    del harness.session.fail_on["SendMessage"]

    assert await student_balances(sessionmaker) == [("Лера", 0)], "операция обязана быть применена"
    queued = await queued_messages(sessionmaker)
    assert [row.method for row in queued] == ["SendMessage"]
    assert "Ученик добавлен" in queued[0].payload

    harness.session.clear()
    await _make_due(sessionmaker)
    sent, carry = await outbox.deliver_batch(harness.bot, sessionmaker, harness.write_lock)

    assert sent == 1
    assert any("Ученик добавлен" in t for t in harness.session.sent_texts())
    assert await queued_messages(sessionmaker) == [], "доставленное обещание должно исчезнуть"
    assert await student_balances(sessionmaker) == [("Лера", 0)], (
        "повтор доставки не трогает данные"
    )


async def test_повторная_доставка_не_повторяет_операцию(harness, sessionmaker):
    """Дожимается ОТВЕТ, а не действие. Иначе очередь стала бы вторым, тихим
    способом добавить ученика."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=ADMIN)
    del harness.session.fail_on["SendMessage"]

    assert await student_balances(sessionmaker) == [("Лера", 0)]
    await _make_due(sessionmaker)
    await outbox.deliver_batch(harness.bot, sessionmaker, harness.write_lock)
    assert await student_balances(sessionmaker) == [("Лера", 0)], "ученик не должен задвоиться"


async def test_свежая_строка_не_видна_отправщику(harness, sessionmaker):
    """Отправщик не имеет права трогать то, что прямо сейчас отправляет
    штатный путь: иначе обычный ответ приходит дважды — а для денег это ровно
    тот двусмысленный сигнал, которого вся схема и избегает."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=ADMIN)
    del harness.session.fail_on["SendMessage"]

    assert len(await queued_messages(sessionmaker)) == 1, "обещание должно быть записано"
    assert (await outbox.deliver_batch(harness.bot, sessionmaker, harness.write_lock))[0] == 0
    assert len(await queued_messages(sessionmaker)) == 1, "строка должна дождаться своего срока"


async def test_правка_экрана_не_кладётся_в_очередь(harness, session, sessionmaker):
    """Очередь несёт сообщения, а не экраны.

    Отложенная правка вернула бы пользователя на экран, с которого он уже
    ушёл: списание не доставилось, человек нажал «⬅️ К списку», а через минуту
    отправщик поверх списка вернул карточку с устаревшим балансом.
    """
    students = StudentService(session)
    a = await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    harness.session.fail_on["EditMessageText"] = RuntimeError("сеть упала")
    await harness.dp.feed_update(
        harness.bot, make_update_callback(f"charge:{a.id}", user_id=ADMIN, update_id=8001)
    )
    del harness.session.fail_on["EditMessageText"]

    assert await student_balances(sessionmaker) == [("Аня", -1)], "списание применено"
    assert await queued_messages(sessionmaker) == [], "правку повторять нельзя"


async def test_неудача_дожимки_переносит_попытку(harness, sessionmaker):
    """Сеть лежит и на повторе: строка остаётся, но следующая попытка
    откладывается — иначе отправщик колотился бы в неё каждые пять секунд."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=ADMIN)

    await _make_due(sessionmaker)
    sent, carry = await outbox.deliver_batch(harness.bot, sessionmaker, harness.write_lock)

    assert sent == 0
    queued = await queued_messages(sessionmaker)
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

    assert await student_balances(sessionmaker) == []
    assert await queued_messages(sessionmaker) == []


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

    await outbox.purge_expired(sessionmaker, harness.write_lock)
    assert await queued_messages(sessionmaker) == []


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

    await _make_due(sessionmaker)
    await outbox.deliver_batch(harness.bot, sessionmaker, harness.write_lock)
    assert await queued_messages(sessionmaker) == []


async def test_провал_выгрузки_виден_пользователю(harness, session):
    """Документ не персистентен: не ушёл — значит потерян навсегда, и сказать
    об этом надо сообщением, а не через обработчик ошибок.

    Две ошибки подряд были в этом месте. Сначала тост «Готово» уходил
    независимо от судьбы файлов — пользователь видел успех и не получал
    ничего. Потом провал стали поднимать наверх, но до человека он всё равно
    не доходил: ответ на нажатие кнопки Telegram принимает ровно один раз, а
    он к тому моменту уже отправлен, так что обработчик ошибок молча получал
    отказ и пользователь видел просто остановившиеся «часики».
    """
    students = StudentService(session)
    await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    harness.session.fail_on["SendDocument"] = RuntimeError("сеть упала")
    await harness.click("exp_csv", user_id=ADMIN)

    answers = [m.text or "" for m in harness.session.calls_of("AnswerCallbackQuery")]
    assert not any("Готово" in t for t in answers), "успех обещать нечем"
    sent = [m.text or "" for m in harness.session.calls_of("SendMessage")]
    assert any("Не получилось отправить файл" in t for t in sent), (
        "о потере документа надо сказать сообщением — «часиками» уже нечем"
    )


async def test_сбой_уборки_не_превращает_успех_в_ошибку(harness, sessionmaker, monkeypatch):
    """`_settle` — обслуживание после отправки. Его провал не имеет права
    дослать «Попробуйте ещё раз» вслед за «Ученик добавлен»: преподаватель
    введёт данные заново, и операция задвоится.
    """
    import lesson_tracker.middlewares as mw

    async def boom(self, container, ui):
        raise RuntimeError("вторая транзакция не открылась")

    monkeypatch.setattr(mw.DbSessionMiddleware, "_settle", boom)

    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    await harness.send("1600", user_id=ADMIN)

    assert await student_balances(sessionmaker) == [("Лера", 0)], "операция обязана быть применена"
    texts = harness.session.sent_texts()
    assert any("Ученик добавлен" in t for t in texts)
    assert not any("Не получилось" in t for t in texts), "уборка не должна пугать пользователя"


async def test_сорванная_подсказка_не_оставляет_невидимый_диалог(harness, session, sessionmaker):
    """Состояние диалога коммитится, а подсказка о нём — обычная правка экрана.

    Не доставилась правка — диалог оставался открытым и невидимым: человек
    решал, что нажатие не прошло, а следующее же введённое им число (телефон,
    цена из другого места) уходило в оплату этому ученику. Теперь содержимое
    подсказки досылается отдельным сообщением тем же сливом.
    """
    students = StudentService(session)
    a = await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    harness.session.fail_on["EditMessageText"] = RuntimeError("сообщение слишком старое")
    await harness.click(f"pay:{a.id}", user_id=ADMIN)
    del harness.session.fail_on["EditMessageText"]

    # Именно SendMessage: sent_texts() собирает текст и с правок, и с вызовов,
    # которые упали, — по нему нельзя отличить «показали» от «пытались».
    sent = [m.text or "" for m in harness.session.calls_of("SendMessage")]
    assert any("Введите сумму оплаты" in t for t in sent), "открытый диалог обязан быть виден"


async def test_сорванная_правка_не_выдаётся_за_ошибку_операции(harness, session, sessionmaker):
    """Списание применено; сорванная правка карточки — не повод пугать.

    Раньше провал правки поднимался наверх, и поверх успешного «Урок списан»
    приходило «Не получилось выполнить действие. Попробуйте ещё раз» — прямое
    приглашение списать урок второй раз.
    """
    students = StudentService(session)
    a = await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    harness.session.fail_on["EditMessageText"] = RuntimeError("сеть упала")
    await harness.dp.feed_update(
        harness.bot, make_update_callback(f"charge:{a.id}", user_id=ADMIN, update_id=8100)
    )
    del harness.session.fail_on["EditMessageText"]

    assert await student_balances(sessionmaker) == [("Аня", -1)]
    everything = harness.session.sent_texts() + [
        m.text or "" for m in harness.session.calls_of("AnswerCallbackQuery")
    ]
    assert not any("Попробуйте ещё раз" in t for t in everything), "операция удалась, пугать нечем"


async def test_списание_имеет_персистентное_подтверждение(harness, session, sessionmaker):
    """Смерть процесса между коммитом и отправкой не должна оставлять урок
    списанным без единого следа в чате: иначе преподаватель спишет второй раз.

    Правку воспроизводить нельзя — она отбросит человека на покинутый экран, —
    поэтому в очередь идёт её запасное сообщение.
    """
    students = StudentService(session)
    a = await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    harness.session.fail_on["EditMessageText"] = RuntimeError("сеть упала")
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.dp.feed_update(
        harness.bot, make_update_callback(f"charge:{a.id}", user_id=ADMIN, update_id=8101)
    )
    del harness.session.fail_on["EditMessageText"], harness.session.fail_on["SendMessage"]

    queued = await queued_messages(sessionmaker)
    assert [row.method for row in queued] == ["SendMessage"], (
        "подтверждение обязано ждать в очереди"
    )

    harness.session.clear()
    await _make_due(sessionmaker)
    assert (await outbox.deliver_batch(harness.bot, sessionmaker, harness.write_lock))[0] == 1
    assert await student_balances(sessionmaker) == [("Аня", -1)], "дожимается ответ, а не операция"


async def test_подсказка_диалога_не_попадает_в_очередь(harness, session, sessionmaker):
    """Текст подсказки осмысленен только сейчас: доставленный через минуту, он
    приходит в чат, где диалога уже нет."""
    students = StudentService(session)
    a = await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    harness.session.fail_on["EditMessageText"] = RuntimeError("сеть упала")
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.click(f"price:{a.id}", user_id=ADMIN)
    del harness.session.fail_on["EditMessageText"], harness.session.fail_on["SendMessage"]

    assert await queued_messages(sessionmaker) == [], "подсказку повторять нельзя"


async def test_сорванная_reply_подсказка_закрывает_диалог(harness, session, sessionmaker):
    """Невидимый диалог на reply-пути: состояние Flow.new_price коммитится, а
    подсказка «Теперь введите стоимость» — обычная отправка без запасного
    варианта. Не ушла — человек не видит ничего, решает что нажатие не прошло,
    и следующее число, набранное по любому поводу, становилось ценой урока и
    создавало ученика. Теперь недоставленная подсказка закрывает диалог.
    """
    await harness.click("add", user_id=ADMIN)
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("Лера", user_id=ADMIN)  # подсказка о цене не уходит
    del harness.session.fail_on["SendMessage"]

    assert await fsm_state(sessionmaker) is None, "невидимый диалог должен быть закрыт"

    await harness.send("1600", user_id=ADMIN)  # «цена» из другого разговора
    assert await student_balances(sessionmaker) == [], "число не должно стать ценой урока"


async def test_fallback_правки_переписывает_prompt_id(harness, session, sessionmaker):
    """Правка-подсказка ушла запасным СООБЩЕНИЕМ — у подсказки новый id.

    Без переписывания prompt_id следующий шаг диалога удалял бы карточку под
    кнопкой (старый id), а осиротевшая подсказка оставалась висеть.
    """
    students = StudentService(session)
    a = await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    harness.session.fail_on["EditMessageText"] = RuntimeError("сообщение старое")
    await harness.click(f"pay:{a.id}", user_id=ADMIN, message_id=9000)
    del harness.session.fail_on["EditMessageText"]

    fallback_ids = harness.session.sent_message_ids("Введите сумму оплаты")
    assert fallback_ids, "подсказка обязана уйти запасным сообщением"

    harness.session.clear()
    await harness.send("1600", user_id=ADMIN)

    deleted = [m.message_id for m in harness.session.calls_of("DeleteMessage")]
    assert deleted == fallback_ids, (
        f"удалить надо подсказку {fallback_ids}, а не карточку: удалено {deleted}"
    )
    assert await student_balances(sessionmaker) == [("Аня", 1)], "оплата должна пройти"


async def test_поздний_prompt_id_не_воскрешает_закрытый_диалог(harness, sessionmaker):
    """prompt_id пишется после круга сети, и быстрый следующий апдейт успевает
    завершить диалог раньше. Дописать id в закрытый диалог значило бы
    воскресить пустую строку мусором {"prompt_id": …} — и она жила бы вечно.
    """
    import lesson_tracker.middlewares as mw

    original = mw.DbSessionMiddleware._settle
    settles: list = []

    async def delayed_settle(self, container, ui):
        # Придерживаем ХВОСТ апдейта «Лера»: его prompt_id-запись выполнится
        # уже после того, как следующее сообщение завершит диалог.
        settles.append((self, container, ui))

    mw.DbSessionMiddleware._settle = delayed_settle  # type: ignore[method-assign]
    try:
        await harness.click("add", user_id=ADMIN)
        await harness.send("Лера", user_id=ADMIN)  # ставит Flow.new_price + prompt
    finally:
        mw.DbSessionMiddleware._settle = original  # type: ignore[method-assign]

    await harness.send("1600", user_id=ADMIN)  # завершает диалог, clear()

    for self_, container, ui in settles:
        await original(self_, container, ui)  # опоздавшие хвосты доезжают

    assert await fsm_state(sessionmaker) is None, "закрытый диалог не должен воскреснуть"
    async with sessionmaker() as s:
        fsm_rows = list(await s.scalars(select(FsmRecord)))
    assert fsm_rows == [], f"мусорная строка FSM: {[(r.key, r.state, r.data) for r in fsm_rows]}"


async def test_clear_удаляет_строку_fsm(harness, sessionmaker):
    """clear() раньше только обнулял поля: каждый открывавший диалог носил
    пустую строку вечно, и её читали на каждом апдейте оба хранилища."""
    await harness.click("add", user_id=ADMIN)  # открыли диалог — строка есть
    async with sessionmaker() as s:
        assert list(await s.scalars(select(FsmRecord))) != []

    await harness.send("/start", user_id=ADMIN)  # /start прерывает диалог

    async with sessionmaker() as s:
        rows = list(await s.scalars(select(FsmRecord)))
    assert rows == [], f"после clear() строка должна исчезнуть: {[(r.key, r.state) for r in rows]}"


async def test_сбой_дозаписи_не_зацикливает_дубли(harness, sessionmaker, monkeypatch):
    """Пачка разослана, а записать её судьбу не вышло (внешний писатель держит
    базу, полный диск). Раньше цикл проглатывал исключение и через пять секунд
    слал ту же пачку снова — «Оплата внесена» приходила бы человеку каждые
    пять секунд до оживления базы. Теперь незаписанный исход возвращается
    вызывающему, и слать дальше без его дозаписи нельзя.
    """
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=ADMIN)
    del harness.session.fail_on["SendMessage"]
    await _make_due(sessionmaker)

    real_apply = outbox._apply_outcome
    boom = {"left": 1}

    async def flaky_apply(sm, lock, outcome):
        if boom["left"]:
            boom["left"] -= 1
            raise RuntimeError("база занята внешним писателем")
        await real_apply(sm, lock, outcome)

    monkeypatch.setattr(outbox, "_apply_outcome", flaky_apply)

    harness.session.clear()
    sent, carry = await outbox.deliver_batch(harness.bot, sessionmaker, harness.write_lock)
    assert sent == 1 and carry, "исход обязан вернуться незаписанным"
    first_batch = len(harness.session.calls_of("SendMessage"))

    # Как поступает run_sender: сначала дозапись, потом новые отправки.
    await outbox._apply_outcome(sessionmaker, harness.write_lock, carry)
    assert not await outbox._has_due(await _engine_of(sessionmaker))
    assert len(harness.session.calls_of("SendMessage")) == first_batch, (
        "до дозаписи исхода ни одно сообщение не должно уйти повторно"
    )
    assert await queued_messages(sessionmaker) == []


async def _engine_of(sessionmaker):
    return sessionmaker.kw["bind"]


async def test_пустой_тик_поллера_не_трогает_блокировку_записи(harness, sessionmaker, container):
    """Штатное состояние очереди — пустая, и узнавать это надо бесплатно.

    Раньше каждый тик открывал BEGIN IMMEDIATE под общим замком — 17 тысяч
    транзакций записи в сутки ради пустой таблицы, а при живом внешнем
    писателе каждый тик ещё и блокировал апдейты людей на busy_timeout.
    """
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncEngine

    engine = await container.get(AsyncEngine)
    immediate: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, *_args):
        if statement.startswith("BEGIN IMMEDIATE"):
            immediate.append(statement)

    try:
        assert not await outbox._has_due(engine), "очередь пуста"
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert immediate == [], "пустая проверка очереди не должна открывать запись"


def _bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=GetMe(), message=text)


async def test_not_modified_это_успех_а_не_повод_для_fallback(harness, session, sessionmaker):
    """Типизированное исключение, а не RuntimeError: до сих пор все сбои в
    тестах были голыми исключениями, и ветки on_error в _send не исполнялись
    ни разу. «message is not modified» — повторный тап той же кнопки: экран
    уже правильный, и слать запасное сообщение (дубль карточки в чат) или
    держать durable-строку в очереди нельзя.
    """
    students = StudentService(session)
    a = await students.create(ADMIN, "Аня", 160000)
    await session.commit()

    harness.session.fail_on["EditMessageText"] = _bad_request(
        "Bad Request: message is not modified"
    )
    await harness.dp.feed_update(
        harness.bot, make_update_callback(f"charge:{a.id}", user_id=ADMIN, update_id=8200)
    )
    del harness.session.fail_on["EditMessageText"]

    assert await student_balances(sessionmaker) == [("Аня", -1)]
    sent = [m.text or "" for m in harness.session.calls_of("SendMessage")]
    assert sent == [], f"запасное сообщение не должно уходить на not modified: {sent}"
    assert await queued_messages(sessionmaker) == [], (
        "durable-правка при not modified считается доставленной"
    )


async def test_retry_after_переносит_попытку_на_срок_телеграма(harness, sessionmaker):
    """429 несёт срок в себе: очередь обязана уважать retry_after, а не свой
    экспоненциальный backoff."""
    await harness.click("add", user_id=ADMIN)
    await harness.send("Лера", user_id=ADMIN)
    harness.session.fail_on["SendMessage"] = RuntimeError("сеть упала")
    await harness.send("1600", user_id=ADMIN)
    del harness.session.fail_on["SendMessage"]
    await _make_due(sessionmaker)

    harness.session.fail_on["SendMessage"] = TelegramRetryAfter(
        method=GetMe(), message="Too Many Requests: retry after 42", retry_after=42
    )
    before = now_ts()
    sent, carry = await outbox.deliver_batch(harness.bot, sessionmaker, harness.write_lock)
    del harness.session.fail_on["SendMessage"]

    assert sent == 0 and carry is None
    (row,) = await queued_messages(sessionmaker)
    assert before + 42 <= row.next_attempt_at <= now_ts() + 42, (
        f"срок повторения обязан прийти из retry_after: {row.next_attempt_at - before}"
    )


async def test_подсказка_не_остаётся_висеть_при_быстром_следующем_шаге(harness, sessionmaker):
    """Гонка `_settle` с быстрым следующим сообщением — с проверкой ЦЕЛЕЙ
    удаления, а не только состояния FSM.

    prompt_id пишется после круга сети. Сообщение, пришедшее в это окно,
    читает состояние ДО записи — то есть не знает id только что отправленной
    подсказки. Раньше это кончалось так: следующий шаг удалял «ничего» (старый
    указатель вёл на уже удалённое сообщение), диалог завершался, а свежая
    подсказка «Теперь введите стоимость» оставалась в чате навсегда.

    Теперь `_settle`, обнаружив закрытый диалог, убирает подсказку сам — id у
    него на руках.
    """
    import lesson_tracker.middlewares as mw

    await harness.click("add", user_id=ADMIN)

    original = mw.DbSessionMiddleware._settle
    held: list = []

    async def hold(self, container, ui, bot):
        held.append((self, container, ui, bot))

    mw.DbSessionMiddleware._settle = hold  # type: ignore[method-assign]
    try:
        await harness.send("Лера", user_id=ADMIN)  # ставит Flow.new_price + подсказку
    finally:
        mw.DbSessionMiddleware._settle = original  # type: ignore[method-assign]

    prompt_ids = harness.session.sent_message_ids("Теперь введите стоимость")
    assert prompt_ids, "подсказка о стоимости обязана уйти"

    await harness.send("1600", user_id=ADMIN)  # успевает раньше _settle: диалог закрыт
    assert await student_prices(sessionmaker) == [("Лера", 160000)]

    harness.session.clear()
    for args in held:  # опоздавший хвост доезжает
        await original(*args)

    deleted = [m.message_id for m in harness.session.calls_of("DeleteMessage")]
    assert deleted == prompt_ids, (
        f"осиротевшую подсказку {prompt_ids} надо убрать из чата, удалено: {deleted}"
    )
