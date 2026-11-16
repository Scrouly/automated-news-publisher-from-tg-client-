"""
Управление списком разрешённых пользователей.
Хранится в allowed_users.json — можно менять без перезапуска.
При первом запуске bootstrap из ALLOWED_USER_IDS в .env.
"""

import json
import logging
import os
from dataclasses import dataclass, asdict

log = logging.getLogger(__name__)

_FILE = "data/allowed_users.json"


@dataclass
class BotUser:
    user_id: int
    name:    str  = ""
    note:    str  = ""   # произвольная заметка (напр. «главред»)


# ── Внутреннее состояние ─────────────────────────────────────────

_users: dict[int, BotUser] = {}     # user_id → BotUser
_admins: set[int]           = set()  # из ADMIN_USER_IDS в .env


def init(allowed_ids: list[int], admin_ids: list[int]):
    """
    Вызывается один раз при старте бота.
    Загружает JSON; если файл отсутствует — создаёт из allowed_ids.
    """
    global _admins
    _admins = set(admin_ids)

    if os.path.isfile(_FILE):
        _load()
        # Дополняем из .env если там есть новые id
        for uid in allowed_ids:
            if uid not in _users:
                _users[uid] = BotUser(user_id=uid)
                log.info("Добавлен пользователь из .env: %d", uid)
        _save()
    else:
        # Первый запуск — создаём файл
        for uid in allowed_ids:
            _users[uid] = BotUser(user_id=uid)
        _save()
        log.info("allowed_users.json создан (%d пользователей)", len(_users))


def is_allowed(user_id: int) -> bool:
    if not _users:          # если список пуст — закрытый доступ
        return False
    return user_id in _users


def is_admin(user_id: int) -> bool:
    return user_id in _admins


def add(user_id: int, name: str = "", note: str = "") -> BotUser:
    u = BotUser(user_id=user_id, name=name, note=note)
    _users[user_id] = u
    _save()
    log.info("Пользователь добавлен: %d (%s)", user_id, name)
    return u


def remove(user_id: int) -> bool:
    if user_id in _users:
        del _users[user_id]
        _save()
        log.info("Пользователь удалён: %d", user_id)
        return True
    return False


def get_all() -> list[BotUser]:
    return list(_users.values())


def get(user_id: int) -> BotUser | None:
    return _users.get(user_id)


# ── Персистентность ──────────────────────────────────────────────

def _load():
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _users.clear()
        for item in data.get("users", []):
            u = BotUser(**item)
            _users[u.user_id] = u
    except Exception as e:
        log.error("Ошибка загрузки %s: %s", _FILE, e)


def _save():
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": [asdict(u) for u in _users.values()]},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Ошибка сохранения %s: %s", _FILE, e)
