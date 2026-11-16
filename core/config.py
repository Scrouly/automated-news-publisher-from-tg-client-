import os
from dotenv import load_dotenv

load_dotenv()

SECRET_TOKEN   = os.environ["SECRET_TOKEN"]
BITRIX_API_URL = os.environ["BITRIX_API_URL"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# ── Файлы ────────────────────────────────────
# Папка data/ в .gitignore, поэтому на свежем клоне её нет — создаём сами.
DATA_DIR     = "data"
LOG_FILE     = f"{DATA_DIR}/publications_log.csv"
REQUESTS_LOG = f"{DATA_DIR}/requests_log.csv"
DRAFT_FILE   = f"{DATA_DIR}/draft.json"
os.makedirs(DATA_DIR, exist_ok=True)

# ── Модели ИИ (бесплатный тариф Gemini API) ──
# Порядок важен: первая модель — по умолчанию, остальные — резерв, на которые
# приложение переключается само, если выбранная вернула 429 (лимит исчерпан).
# Актуальность сверена с ai.google.dev на ноябрь 2026. Семейство 2.5 и
# gemini-3-flash-preview убраны: Google оставляет их только тем, кто ими уже
# пользовался, а 2.5 Pro с 1 декабря перенаправляется на новые модели.
MODELS = {
    "Gemini 3.8 Flash":      "gemini-3.8-flash",
    "Gemini 3.6 Flash":      "gemini-3.6-flash",
    "Gemini 3.5 Flash Lite": "gemini-3.5-flash-lite",
    "Gemini 3.1 Flash Lite": "gemini-3.1-flash-lite",
}

# Google больше не публикует лимиты бесплатного тарифа в документации — реальные
# цифры по вашему проекту смотрите на https://aistudio.google.com/rate-limit.
# Это ЛОКАЛЬНЫЙ счётчик запросов в сутки на каждую модель (индикатор в шапке).
# Общее значение можно задать в .env (DAILY_LIMIT=...), для отдельной модели —
# в DAILY_LIMITS по её названию из MODELS.
DAILY_LIMIT  = int(os.environ.get("DAILY_LIMIT", "20"))
DAILY_LIMITS: dict[str, int] = {}

PREVIEW_MAX_LEN = 150

# Уровень «размышлений» моделей Gemini 3 ("low" / "medium" / "high").
# По умолчанию Google включает размышления на полную, из-за чего ответ на
# простую задачу (перевод, анонс) занимает десятки секунд. "low" — самый быстрый
# уровень, поддерживаемый всеми моделями из MODELS ("minimal" у Gemini 3.8 Flash
# не поддерживается и даёт ошибку). None — не передавать параметр совсем.
THINKING_LEVEL = "low"

# ── Перевод на белорусский (через ИИ) ────────
# Устойчивые названия, которые модель обязана переводить ИМЕННО так.
# Проверьте написание по официальным документам предприятия и дополните список.
TRANSLATION_GLOSSARY = {
    "ОАО «Доломит»": "ААТ «Даламіт»",
}

# ── Цветовая схема (только для десктопа) ─────
C = {
    "bg": "#0E0F13", "surface": "#16181F", "surface2": "#1E2029",
    "border": "#2A2D3A", "border_hi": "#3D4157",
    "accent": "#4F7FFF", "accent_dark": "#3A6AE8",
    "success": "#2ECC8A", "warning": "#F59E0B", "danger": "#F04D5E",
    "text": "#E8EAF2", "text_dim": "#7A7F9A", "text_faint": "#3D4157",
    "white": "#FFFFFF", "selected_bg": "#1A2540", "selected_bd": "#4F7FFF",
}
