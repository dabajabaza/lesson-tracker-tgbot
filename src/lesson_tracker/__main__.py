"""Точка входа: long polling. Запуск: python -m lesson_tracker (из корня репозитория)."""

import asyncio
import contextlib
import fcntl
import logging
import os
import socket
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import ErrorEvent
from alembic import command
from alembic.config import Config as AlembicConfig
from dishka import AsyncContainer
from dishka.integrations.aiogram import ContainerMiddleware, inject_router
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .bot.handlers import router
from .bot.handlers.admin import router as admin_router
from .bot.middlewares import (
    AccessGateMiddleware,
    AccessMiddleware,
    DbSessionMiddleware,
    FsmSessionMiddleware,
)
from .bot.storage import ReadOnlyFsmView
from .config import ROOT, Config, load_config
from .di import build_container
from .runtime.outbox import run_sender
from .runtime.watchdog import run_watchdog, sd_notify

_ERROR_TEXT = "⚠️ Не получилось выполнить действие. Попробуйте ещё раз."

# Беды старта, которые проходят сами. TelegramServerError (5xx) и
# TelegramRetryAfter (429) — прямые наследники TelegramAPIError, а не
# TelegramNetworkError, поэтому прежнее условие считало их фатальными: обычный
# 502 на getMe сразу после деплоя убивал процесс. С Restart=always и
# StartLimitBurst это означало, что четверть часа недоступности Telegram
# сжигала лимит рестартов и оставляла юнит в failed — до ручного
# systemctl reset-failed, ровно то, что цикл ретраев обязан предотвращать.
_RETRYABLE = (TelegramNetworkError, TelegramServerError, TelegramRetryAfter)
_LOCK_NAME = "lesson-tracker-selfhosted.lock"

# Тюнинг под нестабильную сеть (смена wifi/кабель, провалы прокси).
# aiogram ждёт (session_timeout + polling_timeout) на каждый getUpdates, поэтому
# при дефолтах (60+30) мёртвое соединение обнаруживалось лишь через ~90с. Ужимаем
# до ~35с — этого хватает для честного long-poll (сервер отвечает за ≤POLLING),
# а битый сокет отваливается быстро, и aiogram переподключается сам.
# ВНИМАНИЕ: это per-request default aiogram для ВСЕХ вызовов Bot API, а не
# только поллинга (getUpdates добавляет polling_timeout сверху сам). SendMessage
# в 15 с укладывается с запасом, а вот загрузка документов — нет: у неё свой
# бюджет, см. bot/_outgoing.py UPLOAD_TIMEOUT.
_SESSION_TIMEOUT = 15  # буфер поверх polling (сек)
_POLLING_TIMEOUT = 20  # длительность long-poll (сек) → detect ≤ 35с
# systemd watchdog: проба доступности Telegram и её темп. WatchdogSec в юните
# должен быть заметно больше interval (см. lesson-tracker-selfhosted.service).
_WATCHDOG_INTERVAL = 30
_WATCHDOG_PROBE_TIMEOUT = 10
# Стартовый ретрай связи с Telegram. Прокси (см. TELEGRAM_PROXY) может быть ещё
# не поднят / временно лежать в момент старта — не падаем, а ждём с backoff.
_CONNECT_RETRY_START = 3.0  # первая пауза (сек)
_CONNECT_RETRY_MAX = 30.0  # потолок паузы (сек)
# Сколько всего ждать связи, прежде чем сдаться. Бесконечный ретрай выглядел
# безопаснее, но убирал последний сигнал о неустранимой поломке: при опечатке в
# адресе прокси юнит навсегда оставался в activating, READY=1 не приходил,
# WatchdogSec не взводился, Restart=always не срабатывал, а `systemctl restart`
# в деплое висел без конца. С ограничением блип по-прежнему переживается, а
# постоянная поломка доходит до systemd как failed — то есть до оператора.
_CONNECT_BUDGET = 600.0  # сек
# Пока ждём под systemd (Type=notify), продлеваем стартовый таймаут, чтобы
# systemd не убил нас по TimeoutStartSec и не жёг лимит рестартов из-за блипа
# прокси. Запас с потолком над (_CONNECT_RETRY_MAX + таймаут запроса).
_START_EXTEND_USEC = 120 * 1_000_000


async def _establish_connection(bot: Bot):
    """Ждём доступности Telegram через настроенный прокси/сеть и возвращаем get_me.

    Провал прокси или сети на старте — транзиентная беда: ретраимся с
    экспоненциальным backoff вместо exit(1), иначе редкий блип прокси жёг бы
    лимит рестартов юнита и оставлял сервис в failed. Фатальные ошибки API
    (битый токен → 401 и пр.) НЕ маскируем — пробрасываем, ретрай тут не поможет.
    """
    delay = _CONNECT_RETRY_START
    attempt = 0
    deadline = time.monotonic() + _CONNECT_BUDGET
    while True:
        attempt += 1
        try:
            # me(), а не get_me(): aiogram запомнит результат, и обработчикам
            # (/invite) уже не придётся ходить в сеть внутри транзакции.
            me = await bot.me()
            await bot.delete_webhook(drop_pending_updates=False)
            return me
        except Exception as e:
            if isinstance(e, TelegramAPIError) and not isinstance(e, _RETRYABLE):
                raise  # 401/битый токен/битые настройки — ретрай не спасёт
            if time.monotonic() >= deadline:
                logging.error(
                    "Telegram недоступен %.0f с (%d попыток) — сдаюсь, чтобы поломку "
                    "стало видно супервизору",
                    _CONNECT_BUDGET,
                    attempt,
                )
                raise
            # ProxyConnectionError, сеть, таймаут, DNS — ждём и пробуем снова.
            sd_notify(f"EXTEND_TIMEOUT_USEC={_START_EXTEND_USEC}")
            logging.warning(
                "Telegram недоступен на старте (попытка %d): %r. Повтор через %gс.",
                attempt,
                e,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _CONNECT_RETRY_MAX)


_ALREADY_RUNNING = "Бот уже запущен — вторая копия запрещена (конфликт getUpdates и записи в БД)."


def _acquire_single_instance_lock():
    r"""Гард от второй копии: две копии дерутся за getUpdates (Telegram 409) и
    параллельно пишут в одну БД.

    Оба варианта дают одну и ту же гарантию — блокировку снимает ядро вместе со
    смертью процесса, поэтому она не может протухнуть:

    * Linux — абстрактный unix-сокет (ведущий \0), не оставляет файла на диске.
    * Остальные ОС (сервер на FreeBSD) — flock на обычном файле. Абстрактного
      пространства имён там нет: bind("\0...") падает с ENOENT, и прежний код
      принимал это за «уже запущен», из-за чего бот не стартовал вовсе.
    """
    if sys.platform.startswith("linux"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind("\0" + _LOCK_NAME)  # ведущий \0 → абстрактное пространство имён
        except OSError:
            # from None: причина OSError («адрес занят») пользователю не нужна,
            # важен только вердикт «вторая копия».
            raise SystemExit(_ALREADY_RUNNING) from None
        return sock

    lock_path = os.environ.get("LOCK_FILE") or os.path.join("/tmp", _LOCK_NAME)
    handle = open(lock_path, "w")  # noqa: SIM115 — держим открытым до конца процесса
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(_ALREADY_RUNNING) from None
    return handle


async def on_error(event: ErrorEvent):
    # Ни одна ошибка не должна остаться без реакции: кнопка не «крутится»,
    # а в диалоге пользователь видит понятное сообщение.
    logging.exception("Ошибка обработчика: %s", event.exception)
    upd = event.update
    try:
        if upd.callback_query is not None:
            await upd.callback_query.answer(_ERROR_TEXT, show_alert=True)
        elif upd.message is not None:
            await upd.message.answer(_ERROR_TEXT)
    except Exception:
        pass
    return True


def _run_migrations(db_url: str) -> None:
    """Приводит схему к голове. Заменяет прежний create_all: теперь схема
    описана миграциями, и тесты гоняют ровно те же, что и прод, — иначе они
    незаметно расходятся."""
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def build_dispatcher(
    container: AsyncContainer, admin_ids: frozenset[int], write_lock: asyncio.Lock
) -> Dispatcher:
    """Собирает диспетчер в том единственном порядке, который имеет значение.

    Отдельная функция, а не тело main(), потому что этим же путём диспетчер
    строят тесты: пока сборка одна на двоих, тестовая обвязка не может
    разойтись с продом. Раньше шва не было, и тесты дёргали middleware в
    вакууме — из-за чего пропустили взаимную блокировку сессии запроса и
    FSM-хранилища, положившую бота в проде.

    ContainerMiddleware зарегистрирован вручную и ТОЛЬКО на dp.update —
    сознательно вместо setup_dishka. Тот вешает middleware на все обсерверы,
    и один апдейт открывал бы ДВА независимых REQUEST-скоупа (update- и
    message-уровня) с двумя разными сессиями — второй писатель, от которого
    мы только что избавились, вернулся бы через чёрный ход. Проверено по
    исходникам dishka 1.10.1 (integrations/aiogram.py: цикл по всем
    router.observers). auto_inject не используется, но инъекции — да: их
    навешивает явный inject_router(dp) в конце (L12). Обработчики объявляют
    FromDishka[...] и без этого вызова упадут на диспетчеризации.
    """
    # Хранилище диспетчера — только чтение (raw_state для фильтров): встроенный
    # FSMContextMiddleware aiogram читает его ДО открытия области запроса.
    dp = Dispatcher(storage=ReadOnlyFsmView())
    # admin_ids кладём в workflow-данные — aiogram отдаст их обработчикам,
    # объявившим одноимённый параметр.
    dp["admin_ids"] = admin_ids

    dp.update.outer_middleware(ContainerMiddleware(container))
    # Гейт доступа — ДО единицы работы: чужой апдейт не должен стоить ни
    # замка, ни транзакции записи. См. AccessGateMiddleware.
    dp.update.middleware(AccessGateMiddleware(admin_ids))
    # Один замок записи на процесс, и это инвариант L4, а не деталь: тот же
    # объект берёт фоновый отправщик очереди. Параметр ОБЯЗАТЕЛЬНЫЙ, без
    # умолчания: default вида `write_lock or Lock()` тихо чеканил бы второй
    # замок каждому, кто забыл его передать, — единственный писатель
    # расщеплялся бы на двух, и проигравший умирал бы «database is locked»
    # на busy_timeout. Забытый аргумент должен падать на сборке, не в бою.
    dp.update.middleware(DbSessionMiddleware(write_lock))
    # После DbSessionMiddleware: хранилище должно получить ту же сессию из
    # кэша области запроса.
    dp.update.middleware(FsmSessionMiddleware())
    # Строго после сессии: AccessMiddleware читает allowed_users из data["session"].
    dp.update.middleware(AccessMiddleware(admin_ids))

    # Админский роутер — раньше основного: там catch-all обработчики.
    dp.include_router(admin_router)
    dp.include_router(router)

    dp.errors.register(on_error)

    # Инъекция FromDishka в обработчики. Вызывается явно, потому что
    # setup_dishka мы не используем (он же навешивал бы и лишние контейнеры).
    # inject_router обходит все вложенные роутеры и пропускает update-обсервер,
    # так что наши update-middleware остаются нетронутыми. Один вызов вместо
    # двух десятков декораторов @inject — забыть его в новом обработчике
    # невозможно.
    inject_router(dp)
    return dp


async def _run_bot(cfg: Config) -> None:

    container = build_container(cfg)

    session = AiohttpSession(proxy=cfg.proxy, timeout=_SESSION_TIMEOUT)
    bot = Bot(token=cfg.token, session=session)

    write_lock = asyncio.Lock()
    dp = build_dispatcher(container, cfg.admin_ids, write_lock)

    if not cfg.admin_ids:
        logging.warning(
            "ADMIN_IDS пуст — /invite и /allow недоступны, новых пользователей впустить нечем."
        )

    # Устанавливаем связь с Telegram, переживая недоступность прокси/сети на старте.
    me = await _establish_connection(bot)
    logging.info("Бот @%s запущен (long polling).", me.username)

    # Связь установлена — сообщаем systemd о готовности (Type=notify) и запускаем
    # watchdog-пробу параллельно поллингу. Оба — no-op вне systemd notify.
    sd_notify("READY=1")
    watchdog = asyncio.create_task(
        run_watchdog(bot, interval=_WATCHDOG_INTERVAL, probe_timeout=_WATCHDOG_PROBE_TIMEOUT)
    )
    # Дожимает ответы, чья отправка не состоялась (обычно — потому что процесс
    # умер между коммитом и отправкой; деплой убивает бота намеренно).
    sessionmaker = await container.get(async_sessionmaker[AsyncSession])
    engine = await container.get(AsyncEngine)
    sender = asyncio.create_task(run_sender(bot, sessionmaker, engine, write_lock))
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            polling_timeout=_POLLING_TIMEOUT,
        )
    finally:
        watchdog.cancel()
        sender.cancel()
        # Дождаться, а не бросить: cancel() лишь планирует CancelledError, и
        # закрытие контейнера (dispose движка) наперегонки с раскруткой
        # отправщика оставляло бы его транзакцию брошенной посреди дозаписи —
        # доставленная строка выживает, и следующий старт шлёт её второй раз.
        await asyncio.gather(watchdog, sender, return_exceptions=True)
        await container.close()


def main() -> None:
    """Синхронная точка входа: замок, миграции, бот.

    Замок берётся ПЕРВЫМ, и это не косметика. Миграции — самый опасный писатель
    в базу: вторая копия (ручной `python -m lesson_tracker` рядом с юнитом, или деплой,
    рестартующий бота, пока старый процесс ещё жив) успевала бы применить
    `upgrade head` к живой базе и только потом умереть с «Бот уже запущен».
    Для миграции с batch-режимом это означает пересборку таблицы под работающим
    ботом: строки, записанные им в этот момент, просто исчезают.

    Порядок «миграции до asyncio.run» вынужденный, а не стилистический: alembic
    внутри поднимает свой цикл событий, и позвать его из уже работающего
    asyncio.run нельзя — получим «cannot be called from a running event loop».
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Ссылку держит кадр main() на всё время asyncio.run: ядро снимает
    # блокировку вместе со смертью процесса, так что достаточно не дать
    # сборщику мусора закрыть сокет (или файл) раньше времени.
    _lock = _acquire_single_instance_lock()
    cfg = load_config()
    logging.info("Применяю миграции БД")
    _run_migrations(cfg.db_url)
    asyncio.run(_run_bot(cfg))


if __name__ == "__main__":
    # SystemExit (напр. от гарда второй копии) намеренно НЕ глушим.
    with contextlib.suppress(KeyboardInterrupt):
        main()
