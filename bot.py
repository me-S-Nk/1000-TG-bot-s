import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonWebApp, WebAppInfo

from config import settings
from database.database import async_session_factory, close_db, init_db
from database.repositories import seed_default_data
from handlers import (
    admin_router,
    bots_router,
    categories_router,
    errors_router,
    search_router,
    start_router,
)
from middlewares import (
    AdminMiddleware,
    DbSessionMiddleware,
    RateLimitMiddleware,
)
from webapp_server import start_webapp_runner

# Configure structured logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot")

webapp_runner = None


async def on_startup(bot: Bot) -> None:
    """Startup initialization tasks."""
    global webapp_runner
    logger.info("Starting Telegram Bot «1000 мелочей»...")

    # Ensure DB schema is ready
    try:
        await init_db()
    except Exception as e:
        logger.warning("Auto schema init notice (might already be managed by Alembic): %s", e)

    # Seed default categories and bots idempotently
    try:
        async with async_session_factory() as session:
            cats_seeded, bots_seeded = await seed_default_data(session)
            if cats_seeded or bots_seeded:
                logger.info("Database initialized: seeded %d categories and %d bots.", cats_seeded, bots_seeded)
    except Exception as e:
        logger.error("Failed to seed initial data: %s", e)

    # Launch WebApp background server
    try:
        webapp_runner = await start_webapp_runner(
            host=settings.WEBAPP_HOST,
            port=settings.WEBAPP_PORT
        )
    except Exception as e:
        logger.error("Failed to start WebApp server: %s", e)

    # Configure Telegram Menu Button for WebApp
    if settings.WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Каталог",
                    web_app=WebAppInfo(url=settings.WEBAPP_URL)
                )
            )
            logger.info("Chat menu button set to Mini App: %s", settings.WEBAPP_URL)
        except Exception as e:
            logger.warning("Failed to configure chat menu button: %s", e)

    bot_info = await bot.get_me()
    logger.info("Bot @%s (ID: %d) successfully started in long-polling mode!", bot_info.username, bot_info.id)
    logger.info("Admin IDs configured: %s", list(settings.admin_ids))


async def on_shutdown(bot: Bot) -> None:
    """Graceful shutdown cleanup."""
    global webapp_runner
    logger.info("Shutting down bot...")
    if webapp_runner:
        try:
            await webapp_runner.cleanup()
            logger.info("WebApp server stopped.")
        except Exception as e:
            logger.warning("Error stopping WebApp server: %s", e)

    await bot.session.close()
    await close_db()
    logger.info("Bot successfully stopped.")


def setup_dispatcher() -> Dispatcher:
    """Create and configure the aiogram Dispatcher."""
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # 1. Global Rate Limiting Middleware (outer)
    rate_limit_mw = RateLimitMiddleware()
    dp.message.outer_middleware(rate_limit_mw)
    dp.callback_query.outer_middleware(rate_limit_mw)

    # 2. Database Session Injection Middleware (outer)
    db_session_mw = DbSessionMiddleware()
    dp.message.outer_middleware(db_session_mw)
    dp.callback_query.outer_middleware(db_session_mw)

    # 3. Admin Detection Middleware (outer)
    admin_mw = AdminMiddleware()
    dp.message.outer_middleware(admin_mw)
    dp.callback_query.outer_middleware(admin_mw)

    # 4. Include Routers in logical precedence order
    dp.include_router(errors_router)
    dp.include_router(start_router)
    dp.include_router(categories_router)
    dp.include_router(bots_router)
    dp.include_router(search_router)
    dp.include_router(admin_router)

    # Register lifecycle hooks
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    return dp


async def main() -> None:
    """Main application entry point."""
    if not settings.BOT_TOKEN or settings.BOT_TOKEN == "1234567890:ABCDefghIJKlmnoPQRstuvWXYZ123456789":
        logger.critical("BOT_TOKEN is not set or using default placeholder! Please provide a valid token in .env")

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp = setup_dispatcher()

    try:
        # Delete pending updates on launch to avoid processing backlog
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=True
        )
    finally:
        await bot.session.close()
        await close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process terminated by user/system.")
