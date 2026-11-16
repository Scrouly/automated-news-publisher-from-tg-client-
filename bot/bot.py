"""
Доломит — Telegram-бот публикатор.
Точка входа: python bot.py
Режим: polling (локальный запуск)
"""

import asyncio
import logging
import sys

from telegram import Update
from telegram.ext import Application

from . import scheduler
from . import users
from .handlers import setup_handlers
from core.config import TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, ADMIN_USER_IDS

# ─────────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/bot.log", encoding="utf-8"),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────

async def post_init(application: Application):
    """Вызывается после инициализации, до polling."""
    users.init(ALLOWED_USER_IDS, ADMIN_USER_IDS)
    scheduler.init(application.bot)
    log.info("Инициализация завершена")
    log.info("Пользователей: %d", len(users.get_all()))
    log.info("Администраторов: %s", ADMIN_USER_IDS or "не задано")


async def post_shutdown(application: Application):
    scheduler.shutdown()


def main():
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN не задан в .env")
        sys.exit(1)

    if not ADMIN_USER_IDS:
        log.warning("ADMIN_USER_IDS не задан — команды /adduser /removeuser недоступны")

    log.info("Запуск бота (polling)...")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    setup_handlers(app)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()