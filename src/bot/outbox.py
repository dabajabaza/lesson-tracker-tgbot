"""Фоновая дожимка недоставленных ответов.

Штатный путь — отправка сразу после коммита (middlewares.py): пользователь
видит ответ синхронно, строка outbox тут же удаляется, и сюда ничего не
доходит. Этот отправщик существует ради того, чего в штатном пути нет:
пережившей падение процесса записи. Бота убивают при каждом деплое, и апдейт,
чья транзакция зафиксировалась за миг до SIGTERM, иначе остался бы без ответа —
операция применена, а человек об этом не знает и вводит её заново.

Гарантия — «хотя бы один раз». Дубль ответа безобиден: сообщение придёт
дважды. Потеря — нет: неотвеченная оплата провоцирует повторный ввод.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage, TelegramMethod
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .db import READONLY
from .models import OutboxMessage, now_ts

log = logging.getLogger(__name__)

# Как часто заглядывать в очередь. Штатно она пуста, а SELECT по индексу
# ничего не стоит.
POLL_INTERVAL = 5.0
# Сколько строк забирать за раз — чтобы длинная очередь не держала замок.
BATCH = 20
# Как часто выносить просроченное. Не на каждом тике: это запись под общим
# замком, а таблица штатно пуста.
PURGE_EVERY = 3600
# Пауза перед повтором: 60с, 2м, 4м… с потолком в час.
BACKOFF_BASE = 30
BACKOFF_MAX = 3600
# Через сколько сдаться. Ответ суточной давности пользователю уже не нужен, а
# таблица не должна расти вечно.
TTL = 24 * 3600

# Восстанавливаем только то, что сами кладём (см. models.OutboxMessage).
# Явный список, а не поиск класса по имени: строка приходит из базы и не должна
# уметь назвать произвольный метод Bot API.
#
# Правок здесь нет намеренно (см. ui.Responder.edit): отложенная правка
# возвращает пользователя на экран, с которого он уже ушёл.
_METHODS: dict[str, type[TelegramMethod]] = {
    SendMessage.__name__: SendMessage,
}


def _revive(row_id: int, method: str, payload: str) -> TelegramMethod | None:
    """Метод aiogram из строки, или None если строка безнадёжна."""
    factory = _METHODS.get(method)
    if factory is None:
        log.error("Строка outbox %s ссылается на неизвестный метод %r", row_id, method)
        return None
    try:
        return factory.model_validate_json(payload)
    except Exception:
        # Формат payload изменился несовместимо (или строку правили руками) —
        # держать её незачем, она уже не отправится никогда.
        log.exception("Не удалось разобрать строку outbox %s", row_id)
        return None


@dataclass(slots=True)
class _Outcome:
    """Исход разосланной пачки, который ещё не записан в базу.

    Пока он не записан, слать дальше НЕЛЬЗЯ: строки `done` уже доставлены, но
    всё ещё числятся в очереди, и следующий заход отправил бы их снова — по
    разу каждые пять секунд, пока база не станет записываемой. «Хотя бы один
    раз» выродился бы в неограниченный цикл дублей: человек получал бы
    «✅ Оплата внесена» до бесконечности.
    """

    done: list[int] = field(default_factory=list)
    retry: list[tuple[int, int, int]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.done or self.retry)


async def _has_due(engine: AsyncEngine) -> bool:
    """Есть ли в очереди строки к отправке — БЕЗ замка и без записи.

    Пустой тик поллера раньше стоил BEGIN IMMEDIATE под общим замком —
    17 тысяч транзакций записи в сутки ради штатно пустой таблицы, и пока
    внешний писатель держал файл, каждый тик блокировал апдейты людей на
    busy_timeout. READONLY-чтение в WAL не трогает блокировку записи вовсе.
    """
    async with engine.connect() as conn:
        ro = await conn.execution_options(**{READONLY: True})
        found = await ro.scalar(
            select(OutboxMessage.id).where(OutboxMessage.next_attempt_at <= now_ts()).limit(1)
        )
    return found is not None


async def _apply_outcome(
    sessionmaker: async_sessionmaker[AsyncSession], lock: asyncio.Lock, outcome: _Outcome
) -> None:
    """Записать судьбу пачки: доставленное удалить, недоставленное отложить."""
    async with lock, sessionmaker() as session:
        if outcome.done:
            await session.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(outcome.done)))
        # Одним UPDATE на группу, а не по строке. Строки уже прочитаны при
        # отборе, и повторные SELECT'ы (до BATCH штук) держали бы общий замок
        # на ровном месте — как раз когда очередь полна после сбоя связи.
        for when, group in _grouped_by_time(outcome.retry):
            await session.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id.in_([row_id for row_id, _ in group]))
                .values(next_attempt_at=when, attempts=group[0][1])
            )
        await session.commit()


async def deliver_batch(
    bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], lock: asyncio.Lock
) -> tuple[int, _Outcome | None]:
    """Одна попытка разгрести очередь: (сколько ушло, незаписанный исход).

    Исход None — всё записано; иначе вызывающий ОБЯЗАН дозаписать его прежде,
    чем отправлять дальше (см. _Outcome). run_sender так и делает; тестам это
    тоже видно явно, а не через проглоченный лог.

    Три шага, и разделены они не для красоты: прочитать под замком, отправить
    БЕЗ него, записать результат снова под замком. Отправляй мы под замком —
    фоновая задача воспроизвела бы ровно тот дефект, ради устранения которого
    всё затевалось, и держала бы базу на всё время круга до Telegram.
    """
    now = now_ts()
    async with lock, sessionmaker() as session:
        rows = list(
            await session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.next_attempt_at <= now)
                .order_by(OutboxMessage.id)
                .limit(BATCH)
            )
        )
        # Забираем значениями, а не объектами: сессия закроется раньше, чем мы
        # пойдём в сеть, и обращение к отвязанной строке упало бы.
        pending = [(r.id, r.method, r.payload, r.created_at, r.attempts) for r in rows]
    if not pending:
        return 0, None

    outcome = _Outcome()
    done = outcome.done  # доставлено или безнадёжно — в обоих случаях удалить
    retry = outcome.retry  # id, попытка, когда повторить
    sent = 0
    for row_id, method_name, payload, created_at, attempts in pending:
        if now_ts() - created_at > TTL:
            log.warning("Строка outbox %s просрочена (%s) — выброшена", row_id, method_name)
            done.append(row_id)
            continue
        method = _revive(row_id, method_name, payload)
        if method is None:
            done.append(row_id)
            continue
        try:
            await bot(method)
        except TelegramRetryAfter as exc:
            # Telegram сам сказал, когда возвращаться — не спорим.
            retry.append((row_id, attempts + 1, now_ts() + exc.retry_after))
        except Exception as exc:
            delay = min(BACKOFF_BASE * 2 ** (attempts + 1), BACKOFF_MAX)
            retry.append((row_id, attempts + 1, now_ts() + delay))
            log.warning("Повтор доставки %s не удался (попытка %s): %s", row_id, attempts + 1, exc)
        else:
            done.append(row_id)
            sent += 1
            log.info("Отложенная доставка %s (%s) выполнена", row_id, method_name)

    try:
        await _apply_outcome(sessionmaker, lock, outcome)
    except Exception:
        log.exception("Исход пачки не записан — отправка приостановлена до записи")
        return sent, outcome
    return sent, None


def _grouped_by_time(retry: list[tuple[int, int, int]]):
    """Группы (когда повторить, [(id, попытка)…]) — по одинаковому сроку.

    Строки из одной пачки почти всегда получают один и тот же срок и одну и ту
    же попытку, так что групп выходит одна-две.
    """
    groups: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for row_id, attempts, when in retry:
        groups.setdefault((when, attempts), []).append((row_id, attempts))
    return [(when, rows) for (when, _attempts), rows in groups.items()]


async def purge_expired(sessionmaker: async_sessionmaker[AsyncSession], lock: asyncio.Lock) -> None:
    """Убрать просроченное, до чего не дошли руки в deliver_batch.

    Строка с далёким next_attempt_at в выборку не попадает, а протухнуть
    успевает — без этого она осталась бы в таблице навсегда.
    """
    async with lock, sessionmaker() as session:
        await session.execute(
            delete(OutboxMessage).where(OutboxMessage.created_at < now_ts() - TTL)
        )
        await session.commit()


async def run_sender(
    bot: Bot,
    sessionmaker: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    lock: asyncio.Lock,
    *,
    interval: float = POLL_INTERVAL,
) -> None:
    """Вечный цикл. Отменяется вместе с поллингом при остановке бота.

    lock — тот же замок записи, что и у middleware: писатель в SQLite один, и
    фоновая задача не исключение.
    """
    purged_at = 0.0
    carry: _Outcome | None = None
    while True:
        try:
            if carry:
                # Сначала дозаписать судьбу уже РАЗОСЛАННОЙ пачки. Слать при
                # незаписанном исходе нельзя: доставленные строки ещё числятся
                # в очереди, и каждый новый заход дублировал бы их — «Оплата
                # внесена» приходила бы человеку раз в пять секунд, пока база
                # не оживёт.
                await _apply_outcome(sessionmaker, lock, carry)
                carry = None
            # Уборка — раз в час, как и чистка отметок идемпотентности.
            # На каждом тике она открывала запись под общим замком 17 тысяч
            # раз в сутки — ради таблицы, которая в штатном режиме пуста.
            if time.monotonic() - purged_at >= PURGE_EVERY:
                await purge_expired(sessionmaker, lock)
                purged_at = time.monotonic()
            # Пустая очередь — штатное состояние, и проверяется оно READONLY-
            # чтением без замка: см. _has_due.
            if await _has_due(engine):
                _sent, carry = await deliver_batch(bot, sessionmaker, lock)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Живучесть важнее: разгребём на следующем круге. carry уцелел —
            # если упала именно дозапись, следующий круг начнёт с неё.
            log.exception("Сбой фонового отправщика")
        await asyncio.sleep(interval)
