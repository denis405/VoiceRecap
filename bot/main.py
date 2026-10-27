import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher

from bot.access import AccessMiddleware
from bot.config import settings
from bot.db import Database
from bot.handlers import admin, user
from bot.openrouter import OpenRouter
from bot.profile import setup_profile

log = logging.getLogger("bot")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    await db.connect()
    api = OpenRouter(settings.openrouter_api_key, settings.stt_model, settings.summary_model)
    bot = Bot(settings.bot_token)

    dp = Dispatcher(db=db, api=api, jobs=asyncio.Semaphore(settings.max_concurrent_jobs))
    access = AccessMiddleware(db, settings.admin_id)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_routers(admin.router, user.router)

    try:
        await setup_profile(bot, db, settings.admin_id)
        me = await bot.get_me()
        log.info(
            "Starting @%s (stt=%s, lang=%s, summary=%s)",
            me.username, settings.stt_model, settings.stt_language or "auto", settings.summary_model,
        )
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await api.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
