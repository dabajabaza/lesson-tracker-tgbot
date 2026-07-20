"""Точка входа: long polling. Запуск: python -m bot (из каталога selfhosted/)."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from .config import load_config
from .db import init_models, make_sessionmaker
from .handlers import router
from .middlewares import DbSessionMiddleware


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    sessionmaker = make_sessionmaker(cfg.db_url)
    await init_models(sessionmaker)

    session = AiohttpSession(proxy=cfg.proxy) if cfg.proxy else None
    bot = Bot(token=cfg.token, session=session)

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    dp.include_router(router)

    @dp.errors()
    async def on_error(event: ErrorEvent):
        # Ни одна ошибка не должна оставить кнопку «крутиться».
        logging.exception("Ошибка обработчика: %s", event.exception)
        cq = event.update.callback_query
        if cq is not None:
            try:
                await cq.answer("⚠️ Не получилось выполнить действие. Попробуйте ещё раз.", show_alert=True)
            except Exception:
                pass
        return True

    me = await bot.get_me()
    logging.info("Бот @%s запущен (long polling).", me.username)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
