"""Точка входа: long polling. Запуск: python -m bot (из корня репозитория)."""

import asyncio
import contextlib
import fcntl
import logging
import os
import socket
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import ErrorEvent
from alembic import command
from alembic.config import Config as AlembicConfig
from dishka import AsyncContainer
from dishka.integrations.aiogram import ContainerMiddleware, inject_router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .admin import router as admin_router
from .config import ROOT, load_config
from .di import build_container
from .handlers import router
from .middlewares import AccessMiddleware, DbSessionMiddleware, FsmSessionMiddleware
from .outbox import run_sender
from .storage import ReadOnlyFsmView
from .watchdog import run_watchdog, sd_notify

_ERROR_TEXT = "⚠️ Не получилось выполнить действие. Попробуйте ещё раз."
_LOCK_NAME = "lesson-tracker-selfhosted.lock"

# Тюнинг под нестабильную сеть (смена wifi/кабель, провалы прокси).
# aiogram ждёт (session_timeout + polling_timeout) на каждый getUpdates, поэтому
# при дефолтах (60+30) мёртвое соединение обнаруживалось лишь через ~90с. Ужимаем
# до ~35с — этого хватает для честного long-poll (сервер отвечает за ≤POLLING),
# а битый сокет отваливается быстро, и aiogram переподключается сам.
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
    while True:
        attempt += 1
        try:
            # me(), а не get_me(): aiogram запомнит результат, и обработчикам
            # (/invite) уже не придётся ходить в сеть внутри транзакции.
            me = await bot.me()
            await bot.delete_webhook(drop_pending_updates=False)
            return me
        except Exception as e:
            if isinstance(e, TelegramAPIError) and not isinstance(e, TelegramNetworkError):
                raise  # 401/битый токен/битые настройки — ретрай не спасёт
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


def build_dispatcher(container: AsyncContainer, admin_ids: frozenset[int]) -> Dispatcher:
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
    router.observers). auto_inject не используется: обработчики берут session
    из data, FromDishka-инъекций в них нет.
    """
    # Хранилище диспетчера — только чтение (raw_state для фильтров): встроенный
    # FSMContextMiddleware aiogram читает его ДО открытия области запроса.
    dp = Dispatcher(storage=ReadOnlyFsmView(container))
    # admin_ids кладём в workflow-данные — aiogram отдаст их обработчикам,
    # объявившим одноимённый параметр.
    dp["admin_ids"] = admin_ids

    dp.update.outer_middleware(ContainerMiddleware(container))
    # Один замок записи на процесс: его же берёт фоновый отправщик очереди.
    # Кладём в данные диспетчера, чтобы точка входа могла до него дотянуться.
    write_lock = asyncio.Lock()
    dp["write_lock"] = write_lock
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


async def _run_bot() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    _lock = _acquire_single_instance_lock()  # держим ссылку до конца процесса
    cfg = load_config()

    container = build_container(cfg)

    session = AiohttpSession(proxy=cfg.proxy, timeout=_SESSION_TIMEOUT)
    bot = Bot(token=cfg.token, session=session)

    dp = build_dispatcher(container, cfg.admin_ids)

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
    sender = asyncio.create_task(run_sender(bot, sessionmaker, dp["write_lock"]))
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            polling_timeout=_POLLING_TIMEOUT,
        )
    finally:
        watchdog.cancel()
        sender.cancel()
        await container.close()


def main() -> None:
    """Синхронная точка входа: миграции, потом бот.

    Порядок вынужденный, а не стилистический. alembic внутри поднимает свой
    цикл событий, поэтому вызвать его из уже работающего asyncio.run нельзя —
    получим «cannot be called from a running event loop».
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = load_config()
    logging.info("Применяю миграции БД")
    _run_migrations(cfg.db_url)
    asyncio.run(_run_bot())


if __name__ == "__main__":
    # SystemExit (напр. от гарда второй копии) намеренно НЕ глушим.
    with contextlib.suppress(KeyboardInterrupt):
        main()
