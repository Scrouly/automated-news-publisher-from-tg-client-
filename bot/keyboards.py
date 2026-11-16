"""
Построители текста карточки и инлайн-клавиатур для бота.
"""

from html import escape
from telegram import InlineKeyboardButton as IKB, InlineKeyboardMarkup

from core.config import PREVIEW_MAX_LEN
from core.logic import check_duplicate
from .state import Session


# ─────────────────────────────────────────────
#  ТЕКСТ КАРТОЧКИ
# ─────────────────────────────────────────────

def card_text(s: Session) -> str:
    """Формирует HTML-текст карточки публикации."""
    ru      = s.data["ru"]
    title   = ru.get("title",   "")
    preview = ru.get("preview", "")
    plen    = len(preview)

    if plen > PREVIEW_MAX_LEN:
        pmark = "🔴"
    elif plen > int(PREVIEW_MAX_LEN * 0.85):
        pmark = "🟡"
    else:
        pmark = "🟢"

    if s.by_status == "none":
        by_line = "—"
    elif s.by_status == "pending":
        by_line = "⏳ переводится..."
    elif s.by_status == "done":
        by_line = "✓ готов"
    else:
        by_line = f"✗ ошибка: {escape(s.by_error[:60])}"

    if s.preview_image_url and s.image_urls and s.preview_image_url in s.image_urls:
        idx        = s.image_urls.index(s.preview_image_url) + 1
        photo_line = f"Фото №{idx} из {len(s.image_urls)}"
    elif s.image_urls:
        photo_line = f"не выбрано  (доступно: {len(s.image_urls)})"
    else:
        photo_line = "нет фотографий"

    section    = "Новости" if s.target_section == "news" else "Профсоюзная жизнь"
    by_publish = "✓ да" if s.publish_by else "✗ нет"

    dup = ""
    if s.tg_url and check_duplicate(s.tg_url):
        dup = "⚠️ <b>Этот пост уже публиковался</b>\n\n"

    # Строка отложенной публикации
    sched_line = ""
    if s.scheduled_job_id:
        sched_line = "\n<b>⏰ Запланировано:</b> ✓ (используй 🕐 для отмены)\n"

    return (
        f"{dup}"
        f"<b>🗞 Заголовок</b>\n"
        f"{escape(title) or '<i>—</i>'}\n\n"
        f"<b>📝 Анонс</b>  {pmark} <code>{plen}/{PREVIEW_MAX_LEN}</code>\n"
        f"{escape(preview) or '<i>—</i>'}\n\n"
        f"<b>📅 Время:</b> <code>{escape(s.active_from) or '—'}</code>\n"
        f"<b>🖼 Фото:</b> {photo_line}\n"
        f"<b>📂 Раздел:</b> {section}\n"
        f"<b>🇧🇾 Публикация BY:</b> {by_publish}\n"
        f"<b>🇧🇾 Перевод:</b> {by_line}"
        f"{sched_line}"
    )


# ─────────────────────────────────────────────
#  КЛАВИАТУРА КАРТОЧКИ
# ─────────────────────────────────────────────

def card_keyboard(s: Session) -> InlineKeyboardMarkup:
    sn     = "📰 Новости ✓"   if s.target_section == "news"  else "📰 Новости"
    su     = "🤝 Профсоюз ✓"  if s.target_section == "union" else "🤝 Профсоюз"
    by_btn = "🇧🇾 BY вкл ✓"  if s.publish_by                 else "🇧🇾 BY выкл"
    sched_btn = "⏰ Отменить расписание" if s.scheduled_job_id else "🕐 Отложить"

    return InlineKeyboardMarkup([
        [IKB("✏️ Заголовок",      callback_data="edit_title"),
         IKB("🔄 Авто-заголовок", callback_data="regen_title")],
        [IKB("✏️ Анонс",          callback_data="edit_preview"),
         IKB("🔄 Авто-анонс",     callback_data="regen_preview")],
        [IKB("👁 Текст",           callback_data="view_text"),
         IKB("✏️ Текст",          callback_data="edit_text")],
        [IKB("🖼 Выбрать фото",    callback_data="pick_photo"),
         IKB("📅 Изменить время",  callback_data="edit_time")],
        [IKB(sn, callback_data="section_news"),
         IKB(su, callback_data="section_union")],
        [IKB(by_btn, callback_data="toggle_by"),
         IKB(sched_btn, callback_data="schedule_toggle")],
        [IKB("🚀  Опубликовать",  callback_data="publish")],
    ])


def photo_keyboard(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        IKB(f"✓ Выбрать фото №{idx + 1}", callback_data=f"photo_{idx}")
    ]])


def model_keyboard(current_name: str) -> InlineKeyboardMarkup:
    from core.config import MODELS
    rows = [
        [IKB(f"✓ {name}" if name == current_name else name,
             callback_data=f"model_{name}")]
        for name in MODELS
    ]
    return InlineKeyboardMarkup(rows)


def scheduled_jobs_keyboard(jobs: list[dict]) -> InlineKeyboardMarkup:
    """Клавиатура для управления запланированными публикациями."""
    rows = []
    for j in jobs:
        from datetime import datetime
        try:
            dt  = datetime.fromisoformat(j["run_at"]).strftime("%d.%m %H:%M")
        except Exception:
            dt  = "?"
        title = j.get("snap", {}).get("data", {}).get("ru", {}).get("title", "—")[:30]
        rows.append([IKB(f"❌ {dt}  {title}", callback_data=f"cancel_job_{j['job_id']}")])
    rows.append([IKB("Закрыть", callback_data="close_jobs")])
    return InlineKeyboardMarkup(rows)