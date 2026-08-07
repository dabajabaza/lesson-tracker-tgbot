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

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import OutboxMessage, now_ts

log = logging.getLogger(__name__)

# Как часто заглядывать в очередь. Штатно она пуста, а SELECT по индексу
# ничего не стоит.
POLL_INTERVAL = 5.0
# Сколько строк забирать за раз — чтобы длинная очередь не держала замок.
BATCH = 20
# Пауза перед повтором: 60с, 2м, 4м… с потолком в час.
BACKOFF_BASE = 30
BACKOFF_MAX = 3600
# Через сколько сдаться. Ответ суточной давности пользователю уже не нужен, а
# таблица не должна расти вечно.
TTL = 24 * 3600

# Восстанавливаем только то, что сами кладём (см. models.OutboxMessage).
# Явный список, а не поиск класса по имени: строка приходит из базы и не должна
# уметь назвать произвольный метод Bot API.
_METHODS: dict[str, type[TelegramMethod]] = {
    SendMessage.__name__: SendMessage,
    EditMessageText.__name__: EditMessageText,
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


async def deliver_batch(
    bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], lock: asyncio.Lock
) -> int:
    """Одна попытка разгрести очередь. Возвращает, сколько сообщений ушло.

    Три шага, и разделены они не для красоты: прочитать под замком, отправить
    БЕЗ него, записать результат снова под замком. Отправляй мы под замком —
    фоновая задача воспроизвела бы ровно тот дефект, ради устранения которого
    всё затевалось, и держала бы базу на всё время круга до Telegram.

    Отдельная функция, а не тело цикла: так её зовут тесты, не заводя задачу
    и не борясь со временем.
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
        return 0

    done: list[int] = []  # доставлено или безнадёжно — в обоих случаях удалить
    retry: list[tuple[int, int, int]] = []  # id, попытка, когда повторить
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

    async with lock, sessionmaker() as session:
        if done:
            await session.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(done)))
        for row_id, attempts, when in retry:
            row = await session.get(OutboxMessage, row_id)
            if row is not None:
                row.attempts = attempts
                row.next_attempt_at = when
        await session.commit()
    return sent


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
    lock: asyncio.Lock,
    *,
    interval: float = POLL_INTERVAL,
) -> None:
    """Вечный цикл. Отменяется вместе с поллингом при остановке бота.

    lock — тот же замок записи, что и у middleware: писатель в SQLite один, и
    фоновая задача не исключение.
    """
    while True:
        try:
            await purge_expired(sessionmaker, lock)
            await deliver_batch(bot, sessionmaker, lock)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Живучесть важнее: разгребём на следующем круге.
            log.exception("Сбой фонового отправщика")
        await asyncio.sleep(interval)
