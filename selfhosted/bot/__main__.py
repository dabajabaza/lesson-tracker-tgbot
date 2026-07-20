"""Точка входа: long polling. Запуск: python -m bot (из каталога selfhosted/)."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import ErrorEvent

from .config import load_config
from .db import create_db, init_models
from .handlers import router
from .middlewares import DbSessionMiddleware
from .storage import SqlAlchemyStorage

_ERROR_TEXT = "⚠️ Не получилось выполнить действие. Попробуйте ещё раз."


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    engine, sessionmaker = create_db(cfg.db_url)
    await init_models(engine)

    session = AiohttpSession(proxy=cfg.proxy) if cfg.proxy else None
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
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
