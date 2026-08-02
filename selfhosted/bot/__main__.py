"""Точка входа: long polling. Запуск: python -m bot (из каталога selfhosted/)."""

import asyncio
import fcntl
import logging
import os
import socket
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import ErrorEvent

from .admin import router as admin_router
from .config import load_config
from .db import create_db, init_models
from .handlers import router
from .middlewares import AccessMiddleware, DbSessionMiddleware
from .storage import SqlAlchemyStorage
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
            me = await bot.get_me()
            await bot.delete_webhook(drop_pending_updates=False)
            return me
        except Exception as e:
            if isinstance(e, TelegramAPIError) and not isinstance(e, TelegramNetworkError):
                raise  # 401/битый токен/битые настройки — ретрай не спасёт
            # ProxyConnectionError, сеть, таймаут, DNS — ждём и пробуем снова.
            sd_notify(f"EXTEND_TIMEOUT_USEC={_START_EXTEND_USEC}")
            logging.warning(
                "Telegram недоступен на старте (попытка %d): %r. Повтор через %gс.",
                attempt, e, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _CONNECT_RETRY_MAX)


_ALREADY_RUNNING = (
    "Бот уже запущен — вторая копия запрещена (конфликт getUpdates и записи в БД)."
)


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
            raise SystemExit(_ALREADY_RUNNING)
        return sock

    lock_path = os.environ.get("LOCK_FILE") or os.path.join("/tmp", _LOCK_NAME)
    handle = open(lock_path, "w")  # noqa: SIM115 — держим открытым до конца процесса
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(_ALREADY_RUNNING)
    return handle


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    lock = _acquire_single_instance_lock()  # держим ссылку до конца процесса
    cfg = load_config()

    engine, sessionmaker = create_db(cfg.db_url)
    await init_models(engine)

    session = AiohttpSession(proxy=cfg.proxy, timeout=_SESSION_TIMEOUT)
    bot = Bot(token=cfg.token, session=session)

    dp = Dispatcher(storage=SqlAlchemyStorage(sessionmaker))
    # admin_ids кладём в workflow-данные — aiogram отдаст их обработчикам,
    # объявившим одноимённый параметр.
    dp["admin_ids"] = cfg.admin_ids

    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    # Строго после сессии: AccessMiddleware читает allowed_users из data["session"].
    dp.update.middleware(AccessMiddleware(cfg.admin_ids))

    # Админский роутер — раньше основного: там catch-all обработчики.
    dp.include_router(admin_router)
    dp.include_router(router)

    if not cfg.admin_ids:
        logging.warning(
            "ADMIN_IDS пуст — /invite и /allow недоступны, новых пользователей впустить нечем."
        )

    @dp.errors()
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

    # Устанавливаем связь с Telegram, переживая недоступность прокси/сети на старте.
    me = await _establish_connection(bot)
    logging.info("Бот @%s запущен (long polling).", me.username)

    # Связь установлена — сообщаем systemd о готовности (Type=notify) и запускаем
    # watchdog-пробу параллельно поллингу. Оба — no-op вне systemd notify.
    sd_notify("READY=1")
    watchdog = asyncio.create_task(
        run_watchdog(bot, interval=_WATCHDOG_INTERVAL, probe_timeout=_WATCHDOG_PROBE_TIMEOUT)
    )
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            polling_timeout=_POLLING_TIMEOUT,
        )
    finally:
        watchdog.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # SystemExit (напр. от гарда второй копии) намеренно НЕ глушим
