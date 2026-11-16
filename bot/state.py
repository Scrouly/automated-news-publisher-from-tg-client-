"""
Хранилище сессий бота. Каждый пользователь имеет свою независимую сессию.
Сессии хранятся в памяти; активная сессия дополнительно сохраняется в
bot_sessions/<user_id>.json — восстанавливается после перезапуска.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger(__name__)
_SESSIONS_DIR = "data/bot_sessions"


@dataclass
class Session:
    # ── Данные поста ───────────────────────────────────────────────
    tg_url:             str       = ""
    post_text:          str       = ""
    image_urls:         list[str] = field(default_factory=list)
    preview_image_url:  Optional[str] = None
    active_from:        str       = ""

    # ── Контент (RU + BY) ─────────────────────────────────────────
    data: dict = field(default_factory=lambda: {
        "ru": {"title": "", "preview": "", "text": ""},
        "by": {"title": "", "preview": "", "text": ""},
    })

    # ── Настройки публикации ──────────────────────────────────────
    target_section: str  = "news"
    publish_by:     bool = True

    # ── Отложенная публикация ─────────────────────────────────────
    scheduled_job_id: Optional[str] = None   # job_id если пост запланирован

    # ── UI-состояние (не персистируется) ──────────────────────────
    card_message_id:  Optional[int] = None
    photo_msg_ids:    list[int]     = field(default_factory=list)
    # Чего ждём: "title"|"preview"|"text"|"time"|"schedule_time"|None
    waiting_for:      Optional[str] = None
    is_processing:    bool          = False

    # ── Перевод ───────────────────────────────────────────────────
    # "none" | "pending" | "done" | "error"
    by_status: str = "none"
    by_error:  str = ""

    # ── Модель ────────────────────────────────────────────────────
    model_name: str = ""
    model_api:  str = ""


_store: dict[int, Session] = {}

# Поля которые НЕ нужно сохранять на диск (чисто UI-состояние)
_TRANSIENT = {"card_message_id", "photo_msg_ids", "waiting_for",
              "is_processing", "by_status", "by_error"}


def get(user_id: int) -> Session:
    if user_id not in _store:
        _store[user_id] = Session()
    return _store[user_id]


def reset(user_id: int) -> Session:
    _store[user_id] = Session()
    _delete_persisted(user_id)
    return _store[user_id]


# ── Персистентность сессии ───────────────────────────────────────

def persist(user_id: int):
    """Сохраняет текущую сессию пользователя на диск."""
    sess = _store.get(user_id)
    if not sess or not sess.data["ru"].get("title"):
        return
    os.makedirs(_SESSIONS_DIR, exist_ok=True)
    path = _session_path(user_id)
    d    = asdict(sess)
    # Убираем переходные поля
    for key in _TRANSIENT:
        d.pop(key, None)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Ошибка сохранения сессии %d: %s", user_id, e)


def load_persisted(user_id: int) -> Session | None:
    """Загружает сохранённую сессию. Возвращает None если нет или пуста."""
    path = _session_path(user_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        # Восстанавливаем только известные поля
        allowed = {f.name for f in Session.__dataclass_fields__.values()
                   if f.name not in _TRANSIENT}
        clean = {k: v for k, v in d.items() if k in allowed}
        return Session(**clean)
    except Exception as e:
        log.error("Ошибка загрузки сессии %d: %s", user_id, e)
        return None


def _delete_persisted(user_id: int):
    path = _session_path(user_id)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _session_path(user_id: int) -> str:
    return os.path.join(_SESSIONS_DIR, f"{user_id}.json")
