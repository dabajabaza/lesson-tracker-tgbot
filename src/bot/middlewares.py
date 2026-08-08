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
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import Message, TelegramObject, Update
from dishka import AsyncContainer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import access
from .models import OutboxMessage, ProcessedUpdate, now_ts
from .storage import SqlAlchemyStorage
from .ui import Responder

log = logging.getLogger(__name__)

# Telegram держит неподтверждённые апдейты сутки, так что недели отметок с
# запасом хватает, а таблица не растёт бесконечно.
_MARK_TTL = 7 * 24 * 3600
_PRUNE_EVERY = 3600


class DbSessionMiddleware(BaseMiddleware):
    """Сессия из REQUEST-скоупа dishka + замок + коммит + идемпотентность.

    Регистрировать ПОСЛЕ ContainerMiddleware (нужен data["dishka_container"]).
    """

    def __init__(self, lock: asyncio.Lock) -> None:
        # Замок приходит снаружи, а не заводится здесь: тот же самый нужен
        # фоновому отправщику (outbox.py) — писатель в SQLite один на процесс,
        # и фоновая задача из этого правила не исключение.
        self._lock = lock
        self._pruned_at = 0.0

    def _prune_due(self) -> bool:
        return time.monotonic() - self._pruned_at >= _PRUNE_EVERY

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
                return None
            # Только add: отдельный коммит отметил бы апдейт обработанным ДО
            # того, как случилось изменение, и смерть процесса в этот промежуток
            # превратила бы риск дубля в риск потери. Строку зафиксирует общий
            # коммит ниже — вместе с бизнес-изменением, одной транзакцией.
            session.add(ProcessedUpdate(update_id=update_id))

        try:
            result = await handler(event, data)
        except Exception:
            # Откат здесь, а не только в провайдере: обработчик ошибок aiogram
            # живёт снаружи скоупа и успел бы отправить «попробуйте ещё раз»
            # раньше, чем провайдер откатил бы хвост. Заодно выбрасываем
            # накопленные намерения: апдейт не состоялся, говорить не о чем.
            await session.rollback()
            ui.discard()
            raise
        # Обещания доставки ложатся в ту же транзакцию, что и операция.
        await ui.persist(session)
        await session.commit()
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
        * раз в час вычистить просроченные отметки идемпотентности.

        Чистка живёт ЗДЕСЬ, а не в транзакции апдейта. Пока она делила
        транзакцию с оплатой, сбой обслуживающего DELETE откатывал оплату:
        преподаватель видел «Не получилось выполнить действие» из-за уборки
        мусора, а деньги не записывались. Обслуживание не имеет права ронять
        бизнес-операцию, и разнести их по разным транзакциям — единственный
        способ это гарантировать.

        Потеря этой транзакции (смерть процесса в узком окне после отправки)
        безобидна в обе стороны: неудалённую строку outbox отправщик пошлёт
        повторно — дубль ответа не страшен, — а невыясненный prompt_id стоит
        одной неубранной подсказки в чате.
        """
        delivered = ui.delivered()
        prune = self._prune_due()
        if not delivered and not ui.prompt_updates and not prune:
            return
        factory = await container.get(async_sessionmaker[AsyncSession])
        async with self._lock, factory() as session:
            if delivered:
                await session.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(delivered)))
            storage = SqlAlchemyStorage(session)
            for key, message_id in ui.prompt_updates:
                await FSMContext(storage=storage, key=key).update_data(prompt_id=message_id)
            if prune:
                await session.execute(
                    delete(ProcessedUpdate).where(ProcessedUpdate.created_at < now_ts() - _MARK_TTL)
                )
            await session.commit()
        # Отметку времени двигаем только после успешного коммита: иначе сбой
        # уборки отложил бы её ещё на час, и таблица росла бы тихо.
        if prune:
            self._pruned_at = time.monotonic()
        delivered.clear()
        ui.prompt_updates.clear()

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
            data["fsm_storage"] = storage
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


_denials = _DenialLog()


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
    """Молча отбрасывает апдейты от тех, кому нельзя.

    Единственный вход для постороннего — действующая одноразовая ссылка
    `/start <код>`. Всем остальным бот не отвечает ничего: сообщение вида
    «доступ запрещён» подтвердило бы, что бот жив, и приглашало бы долбиться
    дальше.

    Регистрировать ПОСЛЕ DbSessionMiddleware — берёт готовую сессию из data.
    Погашение инвайта не коммитится здесь: оно зафиксируется общим коммитом,
    только если апдейт обработан целиком. Упал обработчик — инвайт не сгорел,
    человек проходит по ссылке ещё раз.
    """

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids

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
            _denials.log(
                user.id,
                "Отказано в доступе: user_id=%s username=%s тип=%s инвайт=%s",
                user.id,
                user.username,
                type(event).__name__,
                invite_attempted,
            )
            return None

        return await handler(event, data)
