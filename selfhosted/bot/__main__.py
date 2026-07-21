"""Точка входа: long polling. Запуск: python -m bot (из каталога selfhosted/)."""

import asyncio
import logging
import socket

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import ErrorEvent

from .config import load_config
from .db import create_db, init_models
from .handlers import router
from .middlewares import DbSessionMiddleware
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


def _acquire_single_instance_lock() -> socket.socket:
    """Гард от второй копии: две копии дерутся за getUpdates (Telegram 409) и
    параллельно пишут в одну БД. Абстрактный unix-сокет эксклюзивен на уровне
    ядра и освобождается вместе с процессом — протухнуть не может."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind("\0" + _LOCK_NAME)  # ведущий \0 → абстрактное пространство имён
    except OSError:
        raise SystemExit(
            "Бот уже запущен — вторая копия запрещена (конфликт getUpdates и записи в БД).\n"
            "Проверьте сервис: systemctl --user status lesson-tracker-selfhosted"
        )
    return sock


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    lock = _acquire_single_instance_lock()  # держим ссылку до конца процесса
    cfg = load_config()

    engine, sessionmaker = create_db(cfg.db_url)
    await init_models(engine)

    session = AiohttpSession(proxy=cfg.proxy, timeout=_SESSION_TIMEOUT)
    bot = Bot(token=cfg.token, session=session)

    dp = Dispatcher(storage=SqlAlchemyStorage(sessionmaker))
    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    dp.include_router(router)

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

    me = await bot.get_me()
    logging.info("Бот @%s запущен (long polling).", me.username)
    await bot.delete_webhook(drop_pending_updates=False)

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
