import os
from dotenv import load_dotenv

load_dotenv()

SECRET_TOKEN   = os.environ["SECRET_TOKEN"]
BITRIX_API_URL = os.environ["BITRIX_API_URL"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# ── Telegram Bot ─────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

def _parse_ids(key: str) -> list[int]:
    return [int(x.strip()) for x in os.environ.get(key, "").split(",") if x.strip().isdigit()]

# Начальный список разрешённых пользователей (bootstrap).
# После первого запуска список живёт в allowed_users.json
ALLOWED_USER_IDS = _parse_ids("ALLOWED_USER_IDS")

# Администраторы — могут использовать /adduser /removeuser /listusers
ADMIN_USER_IDS   = _parse_ids("ADMIN_USER_IDS")

# ── Файлы ────────────────────────────────────
LOG_FILE     = "data/publications_log.csv"
REQUESTS_LOG = "data/requests_log.csv"
DRAFT_FILE   = "data/draft.json"

# ── Перевод (резерв LibreTranslate) ──────────
# Публичный libretranslate.com часто требует платный api_key и не всегда
# поддерживает белорусский - при желании укажите свой self-hosted инстанс.
LIBRETRANSLATE_URL     = os.environ.get("LIBRETRANSLATE_URL", "https://libretranslate.com")
LIBRETRANSLATE_API_KEY = os.environ.get("LIBRETRANSLATE_API_KEY", "")

# ── Модели ───────────────────────────────────
MODELS = {
    "Gemini 2.5 Flash":      "gemini-2.5-flash",
    "Gemini 2.5 Flash Lite": "gemini-2.5-flash-lite",
    "Gemini 3 Flash":        "gemini-3-flash-preview",
}
DAILY_LIMIT     = 20
PREVIEW_MAX_LEN = 150

# ── Цветовая схема (только для десктопа) ─────
C = {
    "bg": "#0E0F13", "surface": "#16181F", "surface2": "#1E2029",
    "border": "#2A2D3A", "border_hi": "#3D4157",
    "accent": "#4F7FFF", "accent_dark": "#3A6AE8",
    "success": "#2ECC8A", "warning": "#F59E0B", "danger": "#F04D5E",
    "text": "#E8EAF2", "text_dim": "#7A7F9A", "text_faint": "#3D4157",
    "white": "#FFFFFF", "selected_bg": "#1A2540", "selected_bd": "#4F7FFF",
}