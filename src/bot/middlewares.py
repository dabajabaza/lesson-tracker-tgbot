"""Middleware: единица работы + сериализация пишущих апдейтов.

Одна область запроса = одна транзакция. Сессию отдаёт dishka (см. di.py),
сервисы (services/) НЕ коммитят — фиксирует DbSessionMiddleware, причём внутри
замка. Замок обязан накрывать коммит: уехав в закрытие скоупа, коммит оказался
бы за его пределами, и следующий апдейт читал бы старый баланс до фиксации
предыдущего.

Замок один на приложение, а не по пользователю. Пер-пользовательский замок
сериализовал только апдейты одного владельца, а SQLite допускает одного писателя
на всю базу: два владельца, нажавшие кнопку одновременно, открывали DEFERRED-
транзакции поверх общего снимка, и второй получал мгновенный «database is
locked» на повышении блокировки — операция терялась молча. Глобальный замок
делает такую гонку невозможной внутри процесса; `BEGIN IMMEDIATE` (см. db.py)
остаётся страховкой на внешнего писателя — миграцию, ручной скрипт.

Порядок внутри апдейта: обработчик → коммит → отправка. Обработчики не ходят в
сеть сами, они складывают намерения в Responder (см. ui.py), и сеть случается
после закрытия транзакции, за пределами замка.

Ради этого разворота всё и затевалось. Пока отправка стояла внутри транзакции,
блокировка записи держалась весь круг до Telegram и обратно — сотни
миллисекунд, — и бот упирался в единицы апдейтов в секунду независимо от того,
сколько их приходит. Теперь замок держится ровно на время работы с базой.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import Message, TelegramObject, Update
from dishka import AsyncContainer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from . import access
from .models import OutboxMessage, ProcessedUpdate
from .storage import SqlAlchemyStorage
from .ui import Responder

log = logging.getLogger(__name__)

# Ключ в data: апдейт отброшен контролем доступа и обработчика не видел.
# Живёт в общем словаре, потому что обе стороны — inner-middleware одного
# обсервера dp.update и получают буквально один и тот же объект.
ACCESS_DENIED = "access_denied"


class AccessGateMiddleware(BaseMiddleware):
    """Отсекает чужие апдейты ДО замка и до всякой транзакции.

    Проверка доступа жила внутри единицы работы, и это значило: каждый
    спам-месседж брал общий замок, открывал BEGIN IMMEDIATE (SELECT в
    is_allowed автобегинит транзакцию, а она у нас IMMEDIATE) и коммитил.
    Флаг ACCESS_DENIED прошлого раунда экономил только строку отметки — замок
    и транзакция оставались. Бот находится в поиске Telegram по имени (L10),
    поток чужих апдейтов штатен, и «✅ Оплата внесена» преподавателя стояла в
    очереди за спамом.

    Здесь тот же вопрос задаётся READONLY-соединением вне замка: отказ стоит
    один DEFERRED-SELECT и ничего больше. Редкий путь — посторонний с
    инвайт-кодом — пропускается внутрь: погашение должно остаться атомарным с
    обработкой апдейта, им занимается AccessMiddleware в транзакции.

    Регистрировать ПОСЛЕ ContainerMiddleware (нужен движок из контейнера) и
    ДО DbSessionMiddleware — в этом весь смысл.
    """

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids
        self._denials = _DenialLog()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        container: AsyncContainer = data["dishka_container"]
        engine = await container.get(AsyncEngine)
        if await access.is_allowed_readonly(engine, self.admin_ids, user.id):
            return await handler(event, data)

        if _invite_code_from_start(event) is not None:
            # Кандидат на погашение инвайта — редкость, ему можно внутрь:
            # действительность кода проверит AccessMiddleware в транзакции.
            return await handler(event, data)

        self._denials.log(
            user.id,
            "Отказано в доступе: user_id=%s username=%s тип=%s инвайт=False",
            user.id,
            user.username,
            type(event).__name__,
        )
        return None


class DbSessionMiddleware(BaseMiddleware):
    """Сессия из REQUEST-скоупа dishka + замок + коммит + идемпотентность.

    Регистрировать ПОСЛЕ ContainerMiddleware (нужен data["dishka_container"]).
    """

    def __init__(self, lock: asyncio.Lock) -> None:
        # Замок приходит снаружи, а не заводится здесь: тот же самый нужен
        # фоновому отправщику (outbox.py) — писатель в SQLite один на процесс,
        # и фоновая задача из этого правила не исключение.
        self._lock = lock

    async def _run(self, handler, event, data, ui: Responder):
        container: AsyncContainer = data["dishka_container"]
        session = await container.get(AsyncSession)
        data["session"] = session

        # Middleware висит на dp.update, поэтому event — это сам Update.
        # getattr, а не прямое обращение: в тестах сюда прилетают и голые
        # объекты сообщений, у которых update_id нет.
        update_id = getattr(event, "update_id", None)
        if update_id is not None:
            seen = await session.scalar(
                select(ProcessedUpdate.update_id).where(ProcessedUpdate.update_id == update_id)
            )
            if seen is not None:
                log.warning("Апдейт %s уже применён — повтор отброшен", update_id)
                # Откат обязателен, а не для порядка. Сам этот SELECT уже открыл
                # транзакцию, и по умолчанию она у нас IMMEDIATE (db.py), то есть
                # держит блокировку записи. Уйдя отсюда без отката, мы оставляли
                # её открытой до закрытия скоупа — а `_settle` ниже успевал взять
                # общий замок и полезть за той же блокировкой уже вторым
                # соединением. Бот вставал на все 5 секунд busy_timeout и падал с
                # «database is locked», причём именно на пути восстановления
                # после рестарта, ради которого идемпотентность и существует.
                await session.rollback()
                return None

        try:
            result = await handler(event, data)

            # Отметка ставится ПОСЛЕ обработчика — и только если апдейт вообще
            # был обработан. Проверка на повтор осталась до него: пропустить
            # дубль нельзя, а вот отмечать нечего, если ничего не случилось.
            # (Штатный чужой трафик сюда не доходит вовсе — его отсекает
            # AccessGateMiddleware до замка; ACCESS_DENIED остаётся для
            # редкого пути невалидного инвайт-кода.)
            #
            # Ставится add, а не отдельный коммит: коммит отметил бы апдейт
            # обработанным ДО фиксации изменения, и смерть процесса в этот
            # промежуток превратила бы риск дубля в риск потери. Строку
            # фиксирует общий коммит ниже — одной транзакцией с изменением.
            if update_id is not None and not data.get(ACCESS_DENIED):
                session.add(ProcessedUpdate(update_id=update_id))
            # Обещания доставки ложатся в ту же транзакцию, что и операция.
            await ui.persist(session)
            # Коммит внутри try не случайно: его сбой («database is locked» от
            # внешнего писателя, полный диск) должен пройти тот же путь, что и
            # сбой обработчика — явный откат и сброс намерений. Иначе открытая
            # транзакция записи переживала бы замок, а буфер сливал бы ответы
            # об операции, которой не случилось.
            await session.commit()
        except Exception:
            # Строго говоря, провайдер откатил бы и сам: ErrorsMiddleware у
            # aiogram 3 — внешнейший на dp.update (регистрируется первым в
            # Dispatcher.__init__), так что скоуп dishka закрывается — с
            # откатом — раньше, чем on_error получит слово. Явный откат тут
            # страховка на случай смены этих порядков, а вот сброс намерений
            # обязателен по-настоящему: апдейт не состоялся, говорить не о чем.
            await session.rollback()
            ui.discard()
            raise
        return result

    async def _settle(self, container: AsyncContainer, ui: Responder) -> None:
        """Закрыть хвосты, которые известны только после отправки.

        Второй короткой транзакцией — двумя миллисекундными вместо одной на
        весь круг до Telegram, ровно тот размен, ради которого затевался
        разворот порядка. Здесь три дела:

        * удалить строки outbox, чьи сообщения ушли (оставшиеся дожмёт
          outbox.py);
        * дописать id отправленных подсказок в состояние FSM — message_id
          существует только после отправки;
        * закрыть диалоги, чью подсказку не удалось показать вовсе.

        Обслуживания здесь больше нет: уборка обеих таблиц-очередей живёт у
        фонового отправщика (outbox.purge_expired), на его часовом таймере.
        Держать второй экземпляр той же машинерии на пути апдейта значило
        проверять таймер на каждом апдейте и уметь дважды ошибаться в одном
        и том же.

        Потеря этой транзакции (смерть процесса в узком окне после отправки)
        безобидна в обе стороны: неудалённую строку outbox отправщик пошлёт
        повторно — дубль ответа не страшен, — а невыясненный prompt_id стоит
        одной неубранной подсказки в чате.
        """
        delivered = ui.delivered()
        if not delivered and not ui.prompt_updates and not ui.failed_prompts:
            return
        factory = await container.get(async_sessionmaker[AsyncSession])
        async with self._lock, factory() as session:
            if delivered:
                await session.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(delivered)))
            storage = SqlAlchemyStorage(session)
            for key, message_id in ui.prompt_updates:
                # Гард от гонки: эта запись идёт ПОСЛЕ круга сети, и быстрый
                # следующий апдейт мог уже завершить диалог. Дописать prompt_id
                # в закрытый диалог значило бы воскресить пустую строку мусором
                # {"prompt_id": …}. Пропускаем; цена — одна неубранная
                # подсказка, тот же бюджет, что у потери самой транзакции
                # (см. L5).
                ctx = FSMContext(storage=storage, key=key)
                if await ctx.get_state() is None:
                    continue
                await ctx.update_data(prompt_id=message_id)
            for key in ui.failed_prompts:
                # Подсказку не удалось показать ни правкой, ни запасным
                # сообщением — а состояние диалога уже закоммичено. Невидимый
                # диалог хуже прерванного: человек решает, что нажатие не
                # прошло, и его следующее сообщение (телефон, цена из другого
                # разговора) молча становится ответом на вопрос, которого он
                # не видел. Закрываем диалог.
                ctx = FSMContext(storage=storage, key=key)
                if await ctx.get_state() is None:
                    continue
                await ctx.clear()
                log.warning("Диалог %s закрыт: подсказку не удалось показать", key)
            await session.commit()
        delivered.clear()
        ui.prompt_updates.clear()
        ui.failed_prompts.clear()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        container: AsyncContainer = data["dishka_container"]
        ui = await container.get(Responder)

        # aiogram обрабатывает апдейты конкурентно, а писатель в SQLite один.
        # Замок берётся на любой апдейт без исключений: отметка идемпотентности
        # ставится даже там, где нет event_from_user, то есть пишут все.
        async with self._lock:
            result = await self._run(handler, event, data, ui)

        # Сеть — уже вне замка и вне транзакции. Пока эти вызовы идут, база
        # свободна и следующий апдейт обрабатывается, а не ждёт.
        # finally: слив может подняться наверх (потерянный документ — см.
        # ui.flush), но отправленное всё равно обязано быть отмечено, иначе
        # фоновый отправщик пошлёт его второй раз.
        try:
            await ui.flush(data["bot"])
        finally:
            # Сбой уборки НЕ имеет права стать ошибкой апдейта. Операция
            # зафиксирована, ответ отправлен; пробросив исключение отсюда, мы бы
            # дослали пользователю «Не получилось выполнить действие» вслед за
            # «✅ Оплата внесена» — и он ввёл бы сумму заново. Всё, что здесь
            # может потеряться, теряется безболезненно: неудалённую строку
            # outbox отправщик пошлёт повторно, а невыясненный prompt_id стоит
            # одной неубранной подсказки.
            try:
                await self._settle(container, ui)
            except Exception:
                log.exception("Не удалось закрыть хвосты апдейта — операция при этом применена")
        return result


class FsmSessionMiddleware(BaseMiddleware):
    """Подменяет FSM-контекст на хранилище общей сессии запроса.

    Встроенный FSMContextMiddleware aiogram собирает data["state"] поверх
    хранилища диспетчера — у нас это read-only-обёртка (см. storage.py).
    Здесь контекст пересобирается с тем же ключом, но поверх request-скоупного
    SqlAlchemyStorage: записи состояния ложатся в ту же транзакцию, что и
    бизнес-изменения. FSMContext — это только пара (storage, key), подмена
    законна и дешева.

    Регистрировать ПОСЛЕ DbSessionMiddleware — не по зависимости данных, а
    чтобы запрошенное здесь хранилище получило ту же сессию из кэша скоупа.

    Здесь же обновляется data["raw_state"] — снимок, по которому aiogram
    сопоставляет StateFilter. Его снимает FSMContextMiddleware до входа в
    замок, поэтому при двух быстрых сообщениях подряд второе матчилось по
    состоянию, которое первое уже успело сбросить: обработчик Flow.new_price
    выигрывал фильтр, не находил имени в данных и уводил диалог обратно на
    «Введите имя ученика» — сразу после того, как ученик успешно создан.
    Перечитываем внутри замка, тем же хранилищем, что увидит обработчик.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if state is not None:
            container: AsyncContainer = data["dishka_container"]
            storage = await container.get(BaseStorage)
            data["state"] = FSMContext(storage=storage, key=state.key)
            data["raw_state"] = await storage.get_state(state.key)
        return await handler(event, data)


_MAX_TRACKED_IDS = 256


class _DenialLog:
    """Первый отказ для каждого id пишем как WARNING, последующие — как DEBUG.

    Иначе спам-бот забил бы лог. Множество ограничено сверху: при переполнении
    очищаем целиком — снова получим всплеск WARNING'ов, что для личного бота
    приемлемее, чем неограниченный рост памяти.
    """

    def __init__(self) -> None:
        self._seen: set[int] = set()

    def log(self, key: int, message: str, *args: object) -> None:
        if len(self._seen) >= _MAX_TRACKED_IDS:
            self._seen.clear()
        if key in self._seen:
            log.debug(message, *args)
        else:
            self._seen.add(key)
            log.warning(message, *args)


def _invite_code_from_start(event: TelegramObject) -> str | None:
    """Код из deep-link `/start <код>`, если он там есть.

    Принимает и Update, и голое сообщение. Это не удобство: AccessMiddleware
    висит на `dp.update`, то есть получает именно Update, и версия, умевшая
    только Message, всегда возвращала None — единственный самостоятельный вход
    для приглашённого был мёртв, а тесты этого не видели, потому что дёргали
    middleware голым Message в обход диспетчера.
    """
    if isinstance(event, Update):
        event = event.message  # type: ignore[assignment]
    if not isinstance(event, Message) or not event.text:
        return None
    if not event.text.startswith("/start"):
        return None
    parts = event.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


class AccessMiddleware(BaseMiddleware):
    """Окончательное решение о доступе — внутри транзакции.

    Штатный поток чужих апдейтов сюда не доходит: их отсекает
    AccessGateMiddleware до замка. Здесь остаются два дела, которым нужна
    транзакция: погашение инвайта (атомарно с обработкой апдейта — упал
    обработчик, инвайт не сгорел) и повторная проверка допуска, закрывающая
    щель между READONLY-чтением гейта и этим моментом (/deny между ними —
    микросекунды, но проверка стоит один SELECT по уже открытой сессии).

    Всем недопущенным бот не отвечает ничего: сообщение вида «доступ запрещён»
    подтвердило бы, что бот жив, и приглашало бы долбиться дальше.

    Регистрировать ПОСЛЕ DbSessionMiddleware — берёт готовую сессию из data.
    """

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids
        # Свой на экземпляр, а не модульный синглтон: иначе состояние
        # рейт-лимита переживает пересборку диспетчера и течёт между тестами —
        # отказ, записанный в одном, глушит WARNING в другом, и проверка
        # «первый отказ виден в логе» проходит или падает от порядка запуска.
        self._denials = _DenialLog()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        session: AsyncSession = data["session"]
        allowed = await access.is_allowed(session, self.admin_ids, user.id)

        invite_attempted = False
        if not allowed:
            code = _invite_code_from_start(event)
            invite_attempted = code is not None
            if code and await access.redeem_invite(session, code, user.id, user.username):
                allowed = True
                log.info("Инвайт погашен: user_id=%s username=%s", user.id, user.username)

        if not allowed:
            self._denials.log(
                user.id,
                "Отказано в доступе: user_id=%s username=%s тип=%s инвайт=%s",
                user.id,
                user.username,
                type(event).__name__,
                invite_attempted,
            )
            # Сообщаем единице работы, что отмечать нечего: обработчик апдейт
            # не видел, и строка в processed_updates была бы платой за чужой
            # спам — записью в базу и блокировкой записи на каждое сообщение.
            data[ACCESS_DENIED] = True
            return None

        return await handler(event, data)
