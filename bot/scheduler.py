"""
Планировщик отложенных публикаций.
Использует APScheduler (AsyncIOScheduler).
Задачи хранятся в scheduled_jobs.json — выживают перезапуск.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from html import escape

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

log = logging.getLogger(__name__)

_JOBS_FILE = "data/scheduled_jobs.json"
_scheduler: AsyncIOScheduler | None = None
_bot = None   # telegram.Bot — устанавливается при старте

# Таймзона для планировщика. Измени на свою если нужно, например:
# "Europe/Minsk", "Europe/Moscow", "UTC"
SCHEDULER_TZ = "Europe/Minsk"


# ─────────────────────────────────────────────
#  ИНИЦИАЛИЗАЦИЯ
# ─────────────────────────────────────────────

def init(bot):
    """Вызывается один раз при старте. Запускает scheduler и восстанавливает задачи."""
    global _scheduler, _bot
    _bot = bot
    tz = pytz.timezone(SCHEDULER_TZ)
    _scheduler = AsyncIOScheduler(timezone=tz)
    _scheduler.start()
    log.info("Scheduler запущен (timezone: %s)", SCHEDULER_TZ)
    _restore_jobs()


def shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


# ─────────────────────────────────────────────
#  ДОБАВЛЕНИЕ / УДАЛЕНИЕ ЗАДАЧ
# ─────────────────────────────────────────────

def schedule(user_id: int, chat_id: int, run_at: datetime, session_snapshot: dict) -> str:
    """
    Ставит задачу на публикацию. Возвращает job_id.
    session_snapshot — копия sess.data + все поля нужные для отправки.
    """
    job_id = f"pub_{user_id}_{int(run_at.timestamp())}"

    _scheduler.add_job(
        _run_publish,
        trigger=DateTrigger(run_date=run_at),
        id=job_id,
        replace_existing=True,
        kwargs={"job_id": job_id, "user_id": user_id,
                "chat_id": chat_id, "snap": session_snapshot},
    )
    _persist(job_id, user_id, chat_id, run_at, session_snapshot)
    log.info("Задача запланирована: %s на %s", job_id, run_at.strftime("%d.%m.%Y %H:%M:%S"))
    return job_id


def cancel(job_id: str) -> bool:
    """Отменяет задачу по job_id."""
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass
    return _unpersist(job_id)


def list_jobs(user_id: int) -> list[dict]:
    """Возвращает список запланированных задач пользователя."""
    jobs = _load_all_jobs()
    return [j for j in jobs if j.get("user_id") == user_id]


def list_all_jobs() -> list[dict]:
    return _load_all_jobs()


# ─────────────────────────────────────────────
#  ВЫПОЛНЕНИЕ ЗАДАЧИ
# ─────────────────────────────────────────────

async def _run_publish(job_id: str, user_id: int, chat_id: int, snap: dict):
    """Выполняется планировщиком в назначенное время."""
    from core.logic import send_to_bitrix, log_publication, validate_publication

    log.info("Выполняю отложенную публикацию: %s", job_id)

    ru  = snap["data"]["ru"]
    by  = snap["data"].get("by", {})

    issues = validate_publication(
        ru["title"], ru["preview"], ru["text"], snap.get("preview_image_url")
    )
    if issues:
        await _bot.send_message(
            chat_id=chat_id,
            text=(
                "⏰ <b>Отложенная публикация не удалась</b>\n\n"
                "⚠️ Проблемы:\n• " + "\n• ".join(escape(i) for i in issues)
            ),
            parse_mode="HTML",
        )
        _unpersist(job_id)
        return

    try:
        result = await asyncio.to_thread(
            send_to_bitrix,
            ru["title"], ru["preview"], ru["text"],
            snap.get("image_urls", []), snap.get("preview_image_url"),
            snap.get("active_from", datetime.now().strftime("%d.%m.%Y %H:%M:%S")),
            by.get("title", ""), by.get("preview", ""), by.get("text", ""),
            snap.get("publish_by", False), snap.get("target_section", "news"),
        )
    except Exception as e:
        await _bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Отложенная публикация — ошибка соединения</b>\n{escape(str(e)[:200])}",
            parse_mode="HTML",
        )
        _unpersist(job_id)
        return

    if result.get("status") == "success":
        id_ru = result.get("id_ru") or result.get("id")
        id_by = result.get("id_by")
        log_publication(snap.get("tg_url", ""), ru["title"], id_ru)
        lines = [
            "⏰ <b>Отложенная публикация выполнена!</b>",
            f"📰 {escape(ru['title'])}",
            f"Русская версия: ID <b>{id_ru}</b>",
        ]
        if id_by:
            lines.append(f"Белорусская версия: ID <b>{id_by}</b>")
        await _bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
    else:
        msg = escape(str(result.get("msg", "Неизвестная ошибка")))
        await _bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Отложенная публикация — ошибка Битрикса</b>\n{msg}",
            parse_mode="HTML",
        )

    _unpersist(job_id)


# ─────────────────────────────────────────────
#  ПЕРСИСТЕНТНОСТЬ ЗАДАЧ
# ─────────────────────────────────────────────

def _load_all_jobs() -> list[dict]:
    if not os.path.isfile(_JOBS_FILE):
        return []
    try:
        with open(_JOBS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("jobs", [])
    except Exception:
        return []


def _save_all_jobs(jobs: list[dict]):
    try:
        with open(_JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump({"jobs": jobs}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Ошибка сохранения задач: %s", e)


def _persist(job_id: str, user_id: int, chat_id: int,
             run_at: datetime, snap: dict):
    jobs = _load_all_jobs()
    jobs = [j for j in jobs if j.get("job_id") != job_id]
    jobs.append({
        "job_id":   job_id,
        "user_id":  user_id,
        "chat_id":  chat_id,
        "run_at":   run_at.isoformat(),
        "snap":     snap,
    })
    _save_all_jobs(jobs)


def _unpersist(job_id: str) -> bool:
    jobs  = _load_all_jobs()
    new   = [j for j in jobs if j.get("job_id") != job_id]
    found = len(new) < len(jobs)
    _save_all_jobs(new)
    return found


def _restore_jobs():
    """Восстанавливает задачи из JSON после перезапуска бота."""
    jobs    = _load_all_jobs()
    now     = datetime.now()
    expired = []

    for j in jobs:
        try:
            run_at = datetime.fromisoformat(j["run_at"])
            if run_at <= now:
                expired.append(j)
                continue
            _scheduler.add_job(
                _run_publish,
                trigger=DateTrigger(run_date=run_at),
                id=j["job_id"],
                replace_existing=True,
                kwargs={
                    "job_id":  j["job_id"],
                    "user_id": j["user_id"],
                    "chat_id": j["chat_id"],
                    "snap":    j["snap"],
                },
            )
            log.info("Восстановлена задача %s на %s",
                     j["job_id"], run_at.strftime("%d.%m.%Y %H:%M:%S"))
        except Exception as e:
            log.error("Ошибка восстановления задачи %s: %s", j.get("job_id"), e)

    for j in expired:
        log.warning("Задача %s просрочена — выполняю сейчас", j["job_id"])
        asyncio.create_task(
            _run_publish(j["job_id"], j["user_id"], j["chat_id"], j["snap"])
        )
