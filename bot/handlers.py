"""
Все хендлеры Telegram-бота.
"""

import asyncio
import logging
import urllib.request
from datetime import datetime
from functools import wraps
from html import escape

from telegram import Update, Message
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest
from bs4 import BeautifulSoup

from . import state
from . import users
from . import scheduler
from .keyboards import (
    card_text, card_keyboard, photo_keyboard, model_keyboard,
    scheduled_jobs_keyboard,
)
from core.config import MODELS, PREVIEW_MAX_LEN
from core.logic import (
    clean_tg_url, check_duplicate, log_publication,
    fetch_post_data, enhance_with_ai, inject_font_size,
    send_to_bitrix, regenerate_title, regenerate_preview,
    translate_to_belarusian, validate_publication, read_publications,
)

log = logging.getLogger(__name__)

_DEFAULT_MODEL_NAME = list(MODELS.keys())[0]
_DEFAULT_MODEL_API  = MODELS[_DEFAULT_MODEL_NAME]

# Накопитель медиагрупп (forward): media_group_id → список Message
_media_groups: dict[str, list[Message]] = {}


# ─────────────────────────────────────────────
#  ДЕКОРАТОР АВТОРИЗАЦИИ
# ─────────────────────────────────────────────

def auth_required(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not users.is_allowed(uid):
            await update.effective_message.reply_text(
                f"⛔ Доступ запрещён.\nВаш ID: <code>{uid}</code>",
                parse_mode=ParseMode.HTML,
            )
            log.warning("Unauthorized: user_id=%d", uid)
            return
        return await fn(update, context)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not users.is_admin(uid):
            await update.effective_message.reply_text("⛔ Только для администратора.")
            return
        return await fn(update, context)
    return wrapper


# ─────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ
# ─────────────────────────────────────────────

def _model(sess: state.Session) -> tuple[str, str]:
    return (
        sess.model_name or _DEFAULT_MODEL_NAME,
        sess.model_api  or _DEFAULT_MODEL_API,
    )


async def _update_card(bot, chat_id: int, sess: state.Session):
    if not sess.card_message_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=sess.card_message_id,
            text=card_text(sess),
            parse_mode=ParseMode.HTML,
            reply_markup=card_keyboard(sess),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit_message_text: %s", e)


async def _delete_photo_messages(bot, chat_id: int, sess: state.Session):
    for mid in sess.photo_msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    sess.photo_msg_ids.clear()


def _reset_translation(sess: state.Session):
    sess.by_status  = "none"
    sess.by_error   = ""
    sess.data["by"] = {"title": "", "preview": "", "text": ""}


def _html_to_plain(html: str) -> str:
    """Убирает HTML-теги, возвращает читаемый текст."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator="\n").strip()
    except Exception:
        return html


# ─────────────────────────────────────────────
#  КОМАНДЫ — ОБЩИЕ
# ─────────────────────────────────────────────

@auth_required
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = escape(update.effective_user.first_name or "")

    # Проверяем есть ли сохранённая сессия
    saved = state.load_persisted(uid)
    if saved and saved.data["ru"].get("title"):
        state._store[uid] = saved
        sess = saved
        msg  = await update.message.reply_text(
            f"👋 Привет снова, {name}!\n\n"
            "Найдена незавершённая публикация. Восстанавливаю карточку...",
        )
        sess.card_message_id = msg.message_id
        await _update_card(context.bot, update.effective_chat.id, sess)
        return

    state.reset(uid)
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        "Отправь ссылку на пост или перешли сообщение из канала.\n\n"
        "<b>Команды:</b>\n"
        "/new — начать новую публикацию\n"
        "/scheduled — запланированные публикации\n"
        "/history — последние 10 публикаций\n"
        "/model — выбрать модель ИИ\n"
        "/cancel — отменить текущий ввод",
        parse_mode=ParseMode.HTML,
    )


@auth_required
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state.reset(update.effective_user.id)
    await update.message.reply_text(
        "📎 Отправь ссылку на пост или перешли сообщение из канала:"
    )


@auth_required
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    sess = state.get(uid)
    if sess.waiting_for:
        sess.waiting_for = None
        await update.message.reply_text("✗ Ввод отменён.")
        await _update_card(context.bot, update.effective_chat.id, sess)
    else:
        await update.message.reply_text("Нечего отменять.")


@auth_required
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = read_publications()[:10]
    if not rows:
        await update.message.reply_text("📋 История публикаций пуста.")
        return
    lines = ["<b>📋 Последние 10 публикаций:</b>\n"]
    for r in rows:
        lines.append(
            f"• <code>{escape(r.get('date', ''))}</code>  "
            f"ID <b>{escape(str(r.get('bitrix_id', '')))}</b>\n"
            f"  {escape(r.get('title', ''))}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@auth_required
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess    = state.get(update.effective_user.id)
    current = sess.model_name or _DEFAULT_MODEL_NAME
    await update.message.reply_text(
        f"Текущая модель: <b>{escape(current)}</b>\n\nВыберите:",
        parse_mode=ParseMode.HTML,
        reply_markup=model_keyboard(current),
    )


# ─────────────────────────────────────────────
#  КОМАНДЫ — РАСПИСАНИЕ
# ─────────────────────────────────────────────

@auth_required
async def cmd_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    jobs = scheduler.list_jobs(uid)
    if not jobs:
        await update.message.reply_text("🕐 Нет запланированных публикаций.")
        return
    await update.message.reply_text(
        f"<b>🕐 Запланировано публикаций: {len(jobs)}</b>\n"
        "Нажми ❌ чтобы отменить:",
        parse_mode=ParseMode.HTML,
        reply_markup=scheduled_jobs_keyboard(jobs),
    )


# ─────────────────────────────────────────────
#  КОМАНДЫ — УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ
# ─────────────────────────────────────────────

@admin_required
async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /adduser 123456789 [Имя] [заметка]
    """
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Использование: /adduser <user_id> [Имя] [заметка]\n"
            "Пример: /adduser 123456789 Иван главред"
        )
        return
    uid   = int(args[0])
    name  = args[1] if len(args) > 1 else ""
    note  = " ".join(args[2:]) if len(args) > 2 else ""
    u     = users.add(uid, name, note)
    await update.message.reply_text(
        f"✅ Пользователь добавлен:\n"
        f"ID: <code>{u.user_id}</code>\n"
        f"Имя: {escape(u.name) or '—'}\n"
        f"Заметка: {escape(u.note) or '—'}",
        parse_mode=ParseMode.HTML,
    )


@admin_required
async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removeuser 123456789
    """
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /removeuser <user_id>")
        return
    uid = int(args[0])
    if users.remove(uid):
        await update.message.reply_text(f"✅ Пользователь <code>{uid}</code> удалён.",
                                         parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ Пользователь <code>{uid}</code> не найден.",
                                         parse_mode=ParseMode.HTML)


@admin_required
async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = users.get_all()
    if not all_users:
        await update.message.reply_text("Список пользователей пуст.")
        return
    lines = [f"<b>👥 Пользователи ({len(all_users)}):</b>\n"]
    for u in all_users:
        admin_mark = " 👑" if users.is_admin(u.user_id) else ""
        lines.append(
            f"• <code>{u.user_id}</code>{admin_mark}"
            + (f"  <b>{escape(u.name)}</b>" if u.name else "")
            + (f"  <i>{escape(u.note)}</i>"  if u.note else "")
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────
#  ВХОДЯЩИЙ ТЕКСТ
# ─────────────────────────────────────────────

@auth_required
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    sess = state.get(uid)
    text = (update.message.text or "").strip()

    if sess.waiting_for:
        await _handle_field_input(update, context, sess, text)
        return

    if "t.me/" in text or "telegram.me/" in text:
        await _start_fetch(update, context, sess, text)
        return

    await update.message.reply_text(
        "Отправь ссылку на пост, перешли сообщение из канала, или используй /new."
    )


async def _handle_field_input(update, context, sess: state.Session, text: str):
    field   = sess.waiting_for
    chat_id = update.effective_chat.id

    if field == "title":
        sess.data["ru"]["title"] = text
        _reset_translation(sess)
        sess.waiting_for = None
        state.persist(update.effective_user.id)
        await _update_card(context.bot, chat_id, sess)
        _launch_translate(context.bot, chat_id, sess)

    elif field == "preview":
        if len(text) > PREVIEW_MAX_LEN:
            text = text[:PREVIEW_MAX_LEN].rsplit(" ", 1)[0] + "..."
            await update.message.reply_text(f"⚠️ Анонс обрезан до {PREVIEW_MAX_LEN} символов.")
        sess.data["ru"]["preview"] = text
        _reset_translation(sess)
        sess.waiting_for = None
        state.persist(update.effective_user.id)
        await _update_card(context.bot, chat_id, sess)
        _launch_translate(context.bot, chat_id, sess)

    elif field == "text":
        # Оборачиваем в параграфы и применяем inject_font_size
        paragraphs = [f"<p>{p.strip()}</p>" for p in text.split("\n\n") if p.strip()]
        html       = inject_font_size("".join(paragraphs)) if paragraphs else text
        sess.data["ru"]["text"] = html
        _reset_translation(sess)
        sess.waiting_for = None
        state.persist(update.effective_user.id)
        await update.message.reply_text("✅ Текст обновлён.")
        await _update_card(context.bot, chat_id, sess)
        _launch_translate(context.bot, chat_id, sess)

    elif field == "time":
        try:
            datetime.strptime(text, "%d.%m.%Y %H:%M:%S")
            sess.active_from = text
            sess.waiting_for = None
            state.persist(update.effective_user.id)
            await _update_card(context.bot, chat_id, sess)
        except ValueError:
            await update.message.reply_text(
                "⚠️ Неверный формат. Нужно: <code>ДД.ММ.ГГГГ ЧЧ:ММ:СС</code>\n"
                "Например: <code>15.03.2025 09:30:00</code>\n\nПопробуй ещё раз или /cancel:",
                parse_mode=ParseMode.HTML,
            )

    elif field == "schedule_time":
        try:
            run_at = datetime.strptime(text, "%d.%m.%Y %H:%M")
            if run_at <= datetime.now():
                await update.message.reply_text(
                    "⚠️ Время должно быть в будущем. Попробуй ещё раз или /cancel:"
                )
                return

            uid     = update.effective_user.id
            snap    = _make_snapshot(sess)
            job_id  = scheduler.schedule(uid, chat_id, run_at, snap)
            sess.scheduled_job_id = job_id
            sess.waiting_for      = None
            state.persist(uid)

            await update.message.reply_text(
                f"⏰ <b>Публикация запланирована!</b>\n"
                f"Время: <code>{run_at.strftime('%d.%m.%Y %H:%M')}</code>\n\n"
                "Можно закрыть бота — публикация произойдёт автоматически.\n"
                "Для отмены нажми «⏰ Отменить расписание» на карточке.",
                parse_mode=ParseMode.HTML,
            )
            await _update_card(context.bot, chat_id, sess)

        except ValueError:
            await update.message.reply_text(
                "⚠️ Неверный формат. Нужно: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
                "Например: <code>15.03.2025 14:30</code>\n\nПопробуй ещё раз или /cancel:",
                parse_mode=ParseMode.HTML,
            )


def _make_snapshot(sess: state.Session) -> dict:
    """Создаёт снимок сессии для планировщика."""
    return {
        "tg_url":            sess.tg_url,
        "image_urls":        sess.image_urls,
        "preview_image_url": sess.preview_image_url,
        "active_from":       sess.active_from,
        "data":              sess.data,
        "target_section":    sess.target_section,
        "publish_by":        sess.publish_by,
    }


# ─────────────────────────────────────────────
#  ПЕРЕСЫЛКА ПОСТА (FORWARD)
# ─────────────────────────────────────────────

@auth_required
async def on_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает пересланные сообщения из Telegram-каналов."""
    uid  = update.effective_user.id
    sess = state.get(uid)
    msg  = update.message

    if sess.is_processing:
        await msg.reply_text("⏳ Уже обрабатываю пост, подожди...")
        return

    # Медиагруппа (альбом из нескольких фото)
    if msg.media_group_id:
        await _accumulate_media_group(update, context, sess)
        return

    # Одиночное сообщение
    text    = msg.text or msg.caption or ""
    photos  = []
    if msg.photo:
        photos.append(msg.photo[-1].file_id)   # наибольшее разрешение

    if not text.strip():
        await msg.reply_text("⚠️ В пересланном сообщении нет текста.")
        return

    await _process_forward(update, context, sess, text, photos)


async def _accumulate_media_group(update, context, sess: state.Session):
    """
    Накапливает сообщения медиагруппы в течение 1.5 с, затем обрабатывает.
    """
    uid   = update.effective_user.id
    msg   = update.message
    mg_id = msg.media_group_id

    if mg_id not in _media_groups:
        _media_groups[mg_id] = []
        # Запускаем таймер обработки
        asyncio.create_task(
            _process_media_group_after(
                update, context, sess, mg_id, uid,
                update.effective_chat.id
            )
        )

    _media_groups[mg_id].append(msg)


async def _process_media_group_after(update, context, sess: state.Session,
                                      mg_id: str, uid: int, chat_id: int):
    """Ждёт 1.5 с пока соберутся все части альбома, потом обрабатывает."""
    await asyncio.sleep(1.5)
    messages = _media_groups.pop(mg_id, [])
    if not messages:
        return

    # Берём текст из первого сообщения у которого он есть
    text   = ""
    photos = []
    for m in sorted(messages, key=lambda x: x.message_id):
        if not text and (m.text or m.caption):
            text = m.text or m.caption or ""
        if m.photo:
            photos.append(m.photo[-1].file_id)

    if not text.strip():
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ В пересланном альбоме нет текста."
        )
        return

    # Создаём псевдо-update для переиспользования _process_forward
    await _process_forward(update, context, sess, text, photos)


async def _process_forward(update, context, sess: state.Session,
                             text: str, photo_file_ids: list[str]):
    """Общий путь обработки пересланного поста."""
    uid     = update.effective_user.id
    chat_id = update.effective_chat.id

    # Скачиваем фото с серверов Telegram (file_id → bytes)
    image_bytes_list: list[bytes] = []
    for fid in photo_file_ids:
        try:
            tg_file    = await context.bot.get_file(fid)
            img_bytes  = await tg_file.download_as_bytearray()
            image_bytes_list.append(bytes(img_bytes))
        except Exception as e:
            log.warning("Не удалось скачать фото %s: %s", fid, e)

    sess.is_processing = True
    status_msg = await update.message.reply_text("🤖 Генерирую текст через ИИ...")
    sess.card_message_id = status_msg.message_id

    asyncio.create_task(
        _run_forward_ai(context.bot, chat_id, uid, sess, text, image_bytes_list)
    )


async def _run_forward_ai(bot, chat_id: int, uid: int, sess: state.Session,
                           raw_text: str, image_bytes_list: list[bytes]):
    """Фоновая задача: AI-генерация для пересланного поста."""
    try:
        mn, ma = _model(sess)
        title, preview, html = await asyncio.to_thread(
            enhance_with_ai, raw_text, ma, mn
        )
        html = inject_font_size(html)

        # Загружаем фото на сервер Telegram и получаем URL через костыль:
        # Telegram не хранит URL для file_id, поэтому сохраняем bytes отдельно
        # и будем передавать их напрямую при выборе фото
        sess._forward_photo_bytes = image_bytes_list  # временное поле
        # Создаём псевдо-URL список чтобы работал picker
        sess.image_urls        = [f"__forward__{i}" for i in range(len(image_bytes_list))]
        sess.preview_image_url = sess.image_urls[0] if sess.image_urls else None

        sess.data["ru"]    = {"title": title, "preview": preview, "text": html}
        sess.data["by"]    = {"title": "", "preview": "", "text": ""}
        sess.active_from   = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        sess.tg_url        = ""
        sess.post_text     = raw_text
        sess.is_processing = False

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=sess.card_message_id,
            text=card_text(sess),
            parse_mode=ParseMode.HTML,
            reply_markup=card_keyboard(sess),
        )

        state.persist(uid)

        if sess.publish_by:
            _launch_translate(bot, chat_id, sess)

    except Exception as e:
        sess.is_processing = False
        log.error("_run_forward_ai error: %s", e, exc_info=True)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=sess.card_message_id,
                text=f"❌ Ошибка: {escape(str(e)[:300])}",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
#  ЗАГРУЗКА И ОБРАБОТКА ПОСТА (по URL)
# ─────────────────────────────────────────────

async def _start_fetch(update, context, sess: state.Session, url: str):
    if sess.is_processing:
        await update.message.reply_text("⏳ Уже обрабатываю пост, подожди...")
        return

    url  = clean_tg_url(url)
    sess.tg_url = url

    msg  = await update.message.reply_text("⏳ Загружаю пост из Telegram...")
    sess.card_message_id = msg.message_id
    sess.is_processing   = True

    asyncio.create_task(
        _run_fetch(context.bot, update.effective_chat.id, update.effective_user.id, sess)
    )


async def _run_fetch(bot, chat_id: int, uid: int, sess: state.Session):
    try:
        post_text, image_urls, has_video, post_dt = await asyncio.to_thread(
            fetch_post_data, sess.tg_url
        )

        if not post_text.strip():
            await bot.edit_message_text(
                chat_id=chat_id, message_id=sess.card_message_id,
                text="❌ Текст поста не найден.",
            )
            sess.is_processing = False
            return

        sess.post_text         = post_text
        sess.image_urls        = image_urls
        sess.preview_image_url = image_urls[0] if image_urls else None
        sess.active_from       = post_dt or datetime.now().strftime("%d.%m.%Y %H:%M:%S")

        await bot.edit_message_text(
            chat_id=chat_id, message_id=sess.card_message_id,
            text="🤖 Генерирую текст через ИИ...",
        )

        mn, ma = _model(sess)
        title, preview, html = await asyncio.to_thread(enhance_with_ai, post_text, ma, mn)
        html = inject_font_size(html)

        sess.data["ru"]    = {"title": title, "preview": preview, "text": html}
        sess.data["by"]    = {"title": "", "preview": "", "text": ""}
        sess.is_processing = False

        await bot.edit_message_text(
            chat_id=chat_id, message_id=sess.card_message_id,
            text=card_text(sess), parse_mode=ParseMode.HTML,
            reply_markup=card_keyboard(sess),
        )

        if has_video:
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ В посте есть видео — оно не будет загружено на сайт.",
            )

        state.persist(uid)

        if sess.publish_by:
            _launch_translate(bot, chat_id, sess)

    except Exception as e:
        sess.is_processing = False
        log.error("_run_fetch error: %s", e, exc_info=True)
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=sess.card_message_id,
                text=f"❌ Ошибка: {escape(str(e)[:300])}",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
#  ПЕРЕВОД
# ─────────────────────────────────────────────

def _launch_translate(bot, chat_id: int, sess: state.Session):
    if sess.publish_by:
        asyncio.create_task(_run_translate(bot, chat_id, sess))


async def _run_translate(bot, chat_id: int, sess: state.Session):
    sess.by_status = "pending"
    await _update_card(bot, chat_id, sess)
    try:
        ru = sess.data["ru"]
        tb, pb, xb = await asyncio.to_thread(
            translate_to_belarusian, ru["title"], ru["preview"], ru["text"],
        )
        sess.data["by"] = {"title": tb, "preview": pb, "text": xb}
        sess.by_status  = "done"
    except Exception as e:
        sess.by_status = "error"
        sess.by_error  = str(e)[:80]
        log.error("Translation error: %s", e)
    await _update_card(bot, chat_id, sess)


# ─────────────────────────────────────────────
#  ПЕРЕГЕНЕРАЦИЯ
# ─────────────────────────────────────────────

async def _run_regen(bot, chat_id: int, sess: state.Session, kind: str):
    mn, ma = _model(sess)
    src    = sess.data["ru"]["text"] or sess.post_text
    try:
        if kind == "title":
            result = await asyncio.to_thread(regenerate_title,   src, ma, mn)
            sess.data["ru"]["title"]   = result
        else:
            result = await asyncio.to_thread(regenerate_preview, src, ma, mn)
            sess.data["ru"]["preview"] = result
        _reset_translation(sess)
        await _update_card(bot, chat_id, sess)
        _launch_translate(bot, chat_id, sess)
    except Exception as e:
        log.error("_run_regen (%s): %s", kind, e)
        await bot.send_message(chat_id=chat_id,
                                text=f"❌ Ошибка перегенерации: {escape(str(e)[:150])}")
        await _update_card(bot, chat_id, sess)


# ─────────────────────────────────────────────
#  ВЫБОР ФОТО
# ─────────────────────────────────────────────

async def _show_photo_picker(bot, chat_id: int, sess: state.Session):
    if not sess.image_urls:
        await bot.send_message(chat_id=chat_id, text="⚠️ В посте нет фотографий.")
        return

    await _delete_photo_messages(bot, chat_id, sess)
    await bot.send_message(chat_id=chat_id,
                            text=f"🖼 Выберите фото ({len(sess.image_urls)} шт.):")

    forward_bytes = getattr(sess, "_forward_photo_bytes", [])

    for i, url in enumerate(sess.image_urls):
        try:
            # Фото из forward — уже байты
            if url.startswith("__forward__") and i < len(forward_bytes):
                photo_data = forward_bytes[i]
            else:
                # Фото из URL — скачиваем
                def _download(img_url):
                    req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req) as resp:
                        return resp.read()
                photo_data = await asyncio.to_thread(_download, url)

            msg = await bot.send_photo(
                chat_id=chat_id, photo=photo_data,
                caption=f"Фото №{i + 1}", reply_markup=photo_keyboard(i),
            )
        except Exception as e:
            log.error("Ошибка отправки фото %s: %s", url, e)
            msg = await bot.send_message(
                chat_id=chat_id, text=f"Фото №{i + 1}  (превью недоступно)",
                reply_markup=photo_keyboard(i),
            )
        sess.photo_msg_ids.append(msg.message_id)


# ─────────────────────────────────────────────
#  ПУБЛИКАЦИЯ
# ─────────────────────────────────────────────

async def _do_publish(bot, chat_id: int, uid: int, sess: state.Session):
    ru = sess.data["ru"]
    by = sess.data["by"]

    # Для forward-постов preview_image_url — псевдо-URL, передаём None
    actual_preview_url = (
        None if sess.preview_image_url and sess.preview_image_url.startswith("__forward__")
        else sess.preview_image_url
    )

    issues = validate_publication(ru["title"], ru["preview"], ru["text"], actual_preview_url)
    if issues:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ <b>Перед публикацией устраните:</b>\n\n• " + "\n• ".join(
                escape(i) for i in issues),
            parse_mode=ParseMode.HTML,
        )
        return

    if sess.publish_by and not by.get("title"):
        await bot.send_message(chat_id=chat_id,
                                text="⚠️ Перевод ещё не готов. Подождите или отключите 🇧🇾.")
        return

    active_from = sess.active_from or datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    wait_msg    = await bot.send_message(chat_id=chat_id, text="🚀 Отправляю на сайт...")

    # Для forward: передаём реальные URL изображений (пусты — forward не имеет URL)
    real_image_urls = [u for u in sess.image_urls if not u.startswith("__forward__")]

    try:
        result = await asyncio.to_thread(
            send_to_bitrix,
            ru["title"], ru["preview"], ru["text"],
            real_image_urls, actual_preview_url, active_from,
            by.get("title", ""), by.get("preview", ""), by.get("text", ""),
            sess.publish_by, sess.target_section,
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка соединения: {escape(str(e)[:200])}")
        return

    if result.get("status") == "success":
        id_ru  = result.get("id_ru") or result.get("id")
        id_by  = result.get("id_by")
        err_by = result.get("err_by")
        log_publication(sess.tg_url, ru["title"], id_ru)

        lines = ["✅ <b>Новость опубликована!</b>", f"Русская версия: ID <b>{id_ru}</b>"]
        if id_by:
            lines.append(f"Белорусская версия: ID <b>{id_by}</b>")
        elif err_by:
            lines.append(f"Белорусская версия — ошибка: {escape(str(err_by))}")

        await wait_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)

        if sess.card_message_id:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=sess.card_message_id, reply_markup=None)
            except Exception:
                pass

        state.reset(uid)
    else:
        msg = escape(str(result.get("msg", "Неизвестная ошибка")))
        await wait_msg.edit_text(f"❌ Ошибка Битрикса: {msg}", parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────
#  CALLBACK-ХЕНДЛЕР
# ─────────────────────────────────────────────

@auth_required
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    uid     = update.effective_user.id
    data    = query.data
    sess    = state.get(uid)
    chat_id = update.effective_chat.id

    if data.startswith("model_"):
        name = data[len("model_"):]
        if name in MODELS:
            sess.model_name = name
            sess.model_api  = MODELS[name]
            try:
                await query.edit_message_text(
                    f"✓ Модель выбрана: <b>{escape(name)}</b>", parse_mode=ParseMode.HTML)
            except BadRequest:
                pass
        return

    if data.startswith("photo_"):
        idx = int(data[len("photo_"):])
        if 0 <= idx < len(sess.image_urls):
            sess.preview_image_url = sess.image_urls[idx]
        await _delete_photo_messages(context.bot, chat_id, sess)
        state.persist(uid)
        await _update_card(context.bot, chat_id, sess)
        return

    if data.startswith("cancel_job_"):
        job_id = data[len("cancel_job_"):]
        if scheduler.cancel(job_id):
            if sess.scheduled_job_id == job_id:
                sess.scheduled_job_id = None
            await query.edit_message_text("✅ Публикация отменена.")
        else:
            await query.edit_message_text("⚠️ Задача не найдена (уже выполнена?).")
        return

    if data == "close_jobs":
        try:
            await query.delete_message()
        except Exception:
            pass
        return

    match data:

        case "view_text":
            html  = sess.data["ru"].get("text", "")
            plain = _html_to_plain(html)
            if not plain:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ Текст пустой.")
                return
            # Telegram ограничивает 4096 символов
            for chunk in [plain[i:i+4000] for i in range(0, len(plain), 4000)]:
                await context.bot.send_message(chat_id=chat_id, text=chunk)

        case "edit_text":
            sess.waiting_for = "text"
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✏️ Отправь новый текст статьи.\n\n"
                    "Разделяй абзацы пустой строкой — каждый станет тегом <code>&lt;p&gt;</code>.\n"
                    "HTML-теги можно оставить — они будут сохранены.\n\n"
                    "(или /cancel)"
                ),
                parse_mode=ParseMode.HTML,
            )

        case "edit_title":
            sess.waiting_for = "title"
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✏️ Текущий заголовок:\n"
                    f"<i>{escape(sess.data['ru']['title'])}</i>\n\n"
                    "Отправь новый заголовок (или /cancel):"
                ),
                parse_mode=ParseMode.HTML,
            )

        case "regen_title":
            if not sess.post_text:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ Нет текста поста.")
                return
            await context.bot.send_message(chat_id=chat_id, text="🔄 Генерирую новый заголовок...")
            asyncio.create_task(_run_regen(context.bot, chat_id, sess, "title"))

        case "edit_preview":
            sess.waiting_for = "preview"
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✏️ Текущий анонс:\n"
                    f"<i>{escape(sess.data['ru']['preview'])}</i>\n\n"
                    f"Отправь новый анонс до {PREVIEW_MAX_LEN} символов (или /cancel):"
                ),
                parse_mode=ParseMode.HTML,
            )

        case "regen_preview":
            if not sess.post_text:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ Нет текста поста.")
                return
            await context.bot.send_message(chat_id=chat_id, text="🔄 Генерирую новый анонс...")
            asyncio.create_task(_run_regen(context.bot, chat_id, sess, "preview"))

        case "edit_time":
            sess.waiting_for = "time"
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"📅 Текущее время: <code>{escape(sess.active_from)}</code>\n\n"
                    "Отправь новое время:\n"
                    "<code>ДД.ММ.ГГГГ ЧЧ:ММ:СС</code>\n"
                    "Например: <code>15.03.2025 09:30:00</code>\n\n(или /cancel)"
                ),
                parse_mode=ParseMode.HTML,
            )

        case "pick_photo":
            await _show_photo_picker(context.bot, chat_id, sess)

        case "section_news":
            sess.target_section = "news"
            state.persist(uid)
            await _update_card(context.bot, chat_id, sess)

        case "section_union":
            sess.target_section = "union"
            state.persist(uid)
            await _update_card(context.bot, chat_id, sess)

        case "toggle_by":
            sess.publish_by = not sess.publish_by
            state.persist(uid)
            await _update_card(context.bot, chat_id, sess)

        case "schedule_toggle":
            if sess.scheduled_job_id:
                # Отменяем
                scheduler.cancel(sess.scheduled_job_id)
                sess.scheduled_job_id = None
                state.persist(uid)
                await _update_card(context.bot, chat_id, sess)
                await context.bot.send_message(chat_id=chat_id,
                                                text="✅ Отложенная публикация отменена.")
            else:
                # Планируем — спрашиваем время
                if not sess.data["ru"].get("title"):
                    await context.bot.send_message(chat_id=chat_id,
                                                    text="⚠️ Сначала загрузи пост.")
                    return
                sess.waiting_for = "schedule_time"
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🕐 Когда опубликовать?\n\n"
                        "Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
                        "Например: <code>15.03.2025 14:30</code>\n\n(или /cancel)"
                    ),
                    parse_mode=ParseMode.HTML,
                )

        case "publish":
            await _do_publish(context.bot, chat_id, uid, sess)

        case _:
            log.warning("Unknown callback_data: %s", data)


# ─────────────────────────────────────────────
#  РЕГИСТРАЦИЯ ХЕНДЛЕРОВ
# ─────────────────────────────────────────────

def setup_handlers(application):
    # Команды — общие
    application.add_handler(CommandHandler("start",    cmd_start))
    application.add_handler(CommandHandler("new",      cmd_new))
    application.add_handler(CommandHandler("cancel",   cmd_cancel))
    application.add_handler(CommandHandler("history",  cmd_history))
    application.add_handler(CommandHandler("model",    cmd_model))
    application.add_handler(CommandHandler("scheduled", cmd_scheduled))

    # Команды — управление пользователями (только для admin)
    application.add_handler(CommandHandler("adduser",    cmd_adduser))
    application.add_handler(CommandHandler("removeuser", cmd_removeuser))
    application.add_handler(CommandHandler("listusers",  cmd_listusers))

    # Callback (инлайн-кнопки)
    application.add_handler(CallbackQueryHandler(on_callback))

    # Пересланные сообщения из каналов
    application.add_handler(MessageHandler(
        filters.FORWARDED & ~filters.COMMAND, on_forward
    ))

    # Обычный текст (ссылки + ввод полей)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.FORWARDED, on_message
    ))