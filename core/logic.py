"""
Бизнес-логика: получение поста, обработка ИИ, логирование, отправка на сайт.
Не содержит ничего связанного с GUI.
"""

import csv
import io
import json
import logging
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlparse
from PIL import Image
from google import genai

from core.config import (
    SECRET_TOKEN, BITRIX_API_URL, GEMINI_API_KEY,
    LOG_FILE, REQUESTS_LOG, DAILY_LIMIT, PREVIEW_MAX_LEN, DRAFT_FILE,
    LIBRETRANSLATE_URL, LIBRETRANSLATE_API_KEY,
)

log = logging.getLogger(__name__)
client = genai.Client(api_key=GEMINI_API_KEY)


# ─────────────────────────────────────────────
#  URL
# ─────────────────────────────────────────────

def clean_tg_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


# ─────────────────────────────────────────────
#  ПОВТОР ПРИ СЕТЕВЫХ СБОЯХ
# ─────────────────────────────────────────────

def with_retry(fn, *args, retries: int = 2, delay: float = 1.5,
               retry_on=(requests.ConnectionError, requests.Timeout),
               **kwargs):
    """
    Выполняет fn(*args, **kwargs), при сетевой ошибке повторяет до `retries` раз
    с нарастающей задержкой. Остальные исключения пробрасываются сразу.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except retry_on as exc:
            last_exc = exc
            if attempt < retries:
                wait = delay * (attempt + 1)
                log.warning("Сетевая ошибка (попытка %d/%d), повтор через %.1f с: %s",
                            attempt + 1, retries, wait, exc)
                time.sleep(wait)
    raise last_exc


# ─────────────────────────────────────────────
#  ЛОГ ПУБЛИКАЦИЙ
# ─────────────────────────────────────────────

def check_duplicate(tg_url: str) -> bool:
    if not os.path.isfile(LOG_FILE):
        return False
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('tg_url') == tg_url:
                return True
    return False


def log_publication(tg_url: str, title: str, bitrix_id):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['date', 'tg_url', 'title', 'bitrix_id'])
        writer.writerow([datetime.now().strftime("%d.%m.%Y %H:%M:%S"), tg_url, title, bitrix_id])


def read_publications() -> list[dict]:
    """Возвращает список публикаций из лог-файла (новые первыми)."""
    if not os.path.isfile(LOG_FILE):
        return []
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    return list(reversed(rows))


# ─────────────────────────────────────────────
#  ЛОГ ЗАПРОСОВ К GEMINI
# ─────────────────────────────────────────────

def log_request(model_name: str, request_type: str):
    file_exists = os.path.isfile(REQUESTS_LOG)
    with open(REQUESTS_LOG, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['date', 'model', 'type'])
        writer.writerow([datetime.now().strftime("%d.%m.%Y %H:%M:%S"), model_name, request_type])


def get_today_usage() -> dict[str, int]:
    usage: dict[str, int] = {}
    if not os.path.isfile(REQUESTS_LOG):
        return usage
    today = datetime.now().strftime("%d.%m.%Y")
    with open(REQUESTS_LOG, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('date', '').startswith(today):
                m = row.get('model', '')
                usage[m] = usage.get(m, 0) + 1
    return usage


def check_limit(model_name: str) -> tuple[bool, int, int]:
    usage = get_today_usage()
    used  = usage.get(model_name, 0)
    return used < DAILY_LIMIT, used, DAILY_LIMIT


# ─────────────────────────────────────────────
#  ЧЕРНОВИК
# ─────────────────────────────────────────────

def save_draft(state: dict):
    """Сохраняет текущее состояние в JSON-файл черновика."""
    with open(DRAFT_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_draft() -> dict | None:
    """Загружает черновик. Возвращает None если файл отсутствует или повреждён."""
    if not os.path.isfile(DRAFT_FILE):
        return None
    try:
        with open(DRAFT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def clear_draft():
    """Удаляет файл черновика после успешной публикации."""
    if os.path.isfile(DRAFT_FILE):
        os.remove(DRAFT_FILE)


# ─────────────────────────────────────────────
#  ВАЛИДАЦИЯ ПЕРЕД ПУБЛИКАЦИЕЙ
# ─────────────────────────────────────────────

def validate_publication(ru_title: str, ru_preview: str, ru_text: str,
                          preview_image_url: str | None) -> list[str]:
    """
    Проверяет данные перед отправкой.
    Возвращает список строк с описанием проблем (пустой список = всё ОК).
    """
    issues: list[str] = []

    if not ru_title.strip():
        issues.append("Заголовок не может быть пустым")
    elif re.search(r'<[^>]+>', ru_title):
        issues.append("Заголовок содержит HTML-теги — уберите их")

    if not ru_text.strip():
        issues.append("Основной текст не может быть пустым")

    if ru_preview and len(ru_preview) > PREVIEW_MAX_LEN:
        issues.append(
            f"Анонс слишком длинный: {len(ru_preview)} симв. (максимум {PREVIEW_MAX_LEN})"
        )

    if not preview_image_url:
        issues.append("Фото для анонса не выбрано")

    return issues


# ─────────────────────────────────────────────
#  ПАРСИНГ TELEGRAM
# ─────────────────────────────────────────────

def fetch_post_data(tg_url: str) -> tuple[str, list[str], bool, str | None]:
    """
    Возвращает (post_text, image_urls, has_video, post_dt).
    post_dt — дата/время поста в формате «ДД.ММ.ГГГГ ЧЧ:ММ:СС» (локальное время),
    либо None если тег <time> не найден.
    """
    embed_url = f"{tg_url}?embed=1"
    headers   = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    resp      = with_retry(requests.get, embed_url, headers=headers, timeout=15)
    resp.raise_for_status()

    soup      = BeautifulSoup(resp.text, 'html.parser')
    text_el   = soup.find('div', class_=re.compile(r'tgme_widget_message_text'))
    post_text = text_el.get_text(separator='\n') if text_el else ""
    has_video = bool(
        soup.find('video') or
        soup.find('a', class_=re.compile(r'tgme_widget_message_video'))
    )

    image_urls = []
    for wrap in soup.find_all('a', class_=re.compile(r'tgme_widget_message_photo_wrap')):
        if 'style' in wrap.attrs:
            m = re.search(r"background-image:url\('(.*?)'\)", wrap['style'])
            if m and m.group(1) not in image_urls:
                image_urls.append(m.group(1))

    # ── Время поста ──────────────────────────────────────────────
    # Telegram embed содержит: <time datetime="2024-01-15T12:34:56+00:00">
    post_dt: str | None = None
    time_el = soup.find('time', attrs={'datetime': True})
    if time_el:
        try:
            dt = datetime.fromisoformat(time_el['datetime'])
            dt_local = dt.astimezone().replace(tzinfo=None)
            post_dt = dt_local.strftime("%d.%m.%Y %H:%M:%S")
        except (ValueError, TypeError):
            pass

    return post_text, image_urls, has_video, post_dt


# ─────────────────────────────────────────────
#  ИИ — ПОЛНАЯ ГЕНЕРАЦИЯ
# ─────────────────────────────────────────────

def enhance_with_ai(raw_text: str, model_api: str, model_name: str) -> tuple[str, str, str]:
    """Полная генерация: заголовок + анонс + текст."""
    prompt = f"""
Ты — ведущий редактор пресс-службы предприятия ОАО «Доломит». 
Твоя задача: переработать текст из Telegram в профессиональную новостную заметку для официального сайта.

### ГЛАВНЫЕ ПРАВИЛА:
1. КАВЫЧКИ: Используй исключительно «елочки». КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО их удалять или заменять на "".
2. ЗАГОЛОВОК (title): Должен быть коротким, емким и заканчиваться точкой или восклицательным знаком. Это отдельное поле, оно НЕ должно дублироваться внутри текста новости.
3. АНОНС (preview): Строго 1-2 предложения (до {PREVIEW_MAX_LEN} символов). Это краткая выжимка для ленты новостей.
4. ПОЛНЫЙ ТЕКСТ (text): 
   - Должен содержать ВЕСЬ исходный текст сообщения без сокращений.
   - НЕ ВКЛЮЧАЙ заголовок в начало текста. Текст должен начинаться сразу с первого предложения новости.
   - Если ты использовал первое предложение для анонса (preview), оно ВСЕ РАВНО должно остаться в полном тексте (text).
   - Разбей текст на логические абзацы, обернув каждый в тег <p>. 
   - Удали эмодзи, хештеги (#новости) и призывы «подпишись на наш канал».

Исходный текст:
{raw_text}

### ПРИМЕРЫ ЗАГОЛОВКОВ (для поля "title"):
- Работники ОАО «Доломит» почтили память павших героев.
- Масленица в микрорайоне Руба прошла весело и вкусно!
- В ОАО «Доломит» прошёл Единый день информирования.
- 29.01.2026 с рабочим визитом в ОАО «Доломит» прибыли представители компании CUEC.

### ПРИМЕРЫ АНОНСОВ (для поля "preview"):
- В День защитников Отечества, работники ОАО «Доломит» приняли участие в церемонии возложения цветов к памятнику Неизвестному солдату.
- В минувшие выходные работники ОАО «Доломит», жители и гости микрорайона собрались, чтобы весело проводить зиму и встретить весну.
- Тема: «Оборонительная мощь белорусской армии и патриотизм в современных условиях».
- С 12 по 15 февраля выставка объединила более 180 ведущих учреждений образования и организаций.

### ФОРМАТ ОТВЕТА (JSON):
{{{{
    "title": "Только заголовок",
    "preview": "Только краткий анонс до {PREVIEW_MAX_LEN} симв.",
    "text": "Полный текст поста без заголовка в начале, с тегами <p>"
}}}}
"""
    response = client.models.generate_content(model=model_api, contents=prompt)
    log_request(model_name, 'full')

    match = re.search(r'\{.*\}', response.text.strip(), re.DOTALL)
    if not match:
        raise ValueError(f"ИИ не вернул валидный JSON. Ответ: {response.text[:300]}")

    ai_data = json.loads(match.group())
    title   = ai_data.get('title',   '').strip()
    preview = ai_data.get('preview', '').strip()
    text    = ai_data.get('text',    '').strip()

    if not title or not text:
        raise ValueError("ИИ вернул пустой заголовок или текст.")

    if len(preview) > PREVIEW_MAX_LEN:
        preview = preview[:PREVIEW_MAX_LEN].rsplit(' ', 1)[0].rstrip('.,;:') + '...'

    return title, preview, text


# ─────────────────────────────────────────────
#  ИИ — ПЕРЕГЕНЕРАЦИЯ ЗАГОЛОВКА
# ─────────────────────────────────────────────

def regenerate_title(current_text: str, model_api: str, model_name: str) -> str:
    prompt = f"""
Ты — редактор пресс-службы ОАО «Доломит».
Придумай НОВЫЙ вариант заголовка для следующего текста новости.

ПРАВИЛА:
- Строго 1 короткое предложение
- Используй кавычки «елочки», не заменяй их на ""
- Не повторяй предыдущий заголовок
- Заголовок должен быть ёмким и информативным

Текст новости:
{current_text}

Ответь ТОЛЬКО заголовком, без кавычек, пояснений и JSON.
"""
    response = client.models.generate_content(model=model_api, contents=prompt)
    log_request(model_name, 'title')
    return response.text.strip().strip('"').strip("'")


# ─────────────────────────────────────────────
#  ИИ — ПЕРЕГЕНЕРАЦИЯ АНОНСА
# ─────────────────────────────────────────────

def regenerate_preview(current_text: str, model_api: str, model_name: str) -> str:
    prompt = f"""
Ты — редактор пресс-службы ОАО «Доломит».
Напиши НОВЫЙ вариант анонса для следующей новости.

ПРАВИЛА:
- Строго 1-2 предложения, не более {PREVIEW_MAX_LEN} символов
- Используй кавычки «елочки»
- Анонс — это самостоятельный элемент, НЕ первое предложение текста
- Не повторяй предыдущий анонс

Текст новости:
{current_text}

Ответь ТОЛЬКО анонсом, без пояснений и JSON.
"""
    response = client.models.generate_content(model=model_api, contents=prompt)
    log_request(model_name, 'preview')
    result = response.text.strip().strip('"').strip("'")
    if len(result) > PREVIEW_MAX_LEN:
        result = result[:PREVIEW_MAX_LEN].rsplit(' ', 1)[0] + '...'
    return result


# ─────────────────────────────────────────────
#  HTML / ФОРМАТИРОВАНИЕ
# ─────────────────────────────────────────────

def inject_font_size(html_text: str) -> str:
    def wrap_paragraph(match):
        inner = match.group(1).strip()
        inner = re.sub(r'<span[^>]*font-size[^>]*>(.*?)</span>', r'\1', inner, flags=re.DOTALL)
        inner = inner.strip()
        if not inner:
            return ''
        return f'<p>\n\t<span style="font-size: 15pt;">{inner}</span>\n</p>'
    return re.sub(r'<p[^>]*>(.*?)</p>', wrap_paragraph, html_text, flags=re.DOTALL)


# ─────────────────────────────────────────────
#  ИЗОБРАЖЕНИЯ
#  Возвращает PIL.Image (без GUI-зависимостей).
#  Конвертация в ImageTk выполняется на уровне виджетов.
# ─────────────────────────────────────────────

def download_image_thumbnail(url: str, size: tuple = (240, 160)) -> Image.Image | None:
    """Скачивает изображение и возвращает PIL.Image с letterbox-паддингом."""
    try:
        resp  = with_retry(requests.get, url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        img   = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img.thumbnail(size, Image.LANCZOS)
        thumb = Image.new("RGB", size, (22, 24, 31))
        thumb.paste(img, ((size[0] - img.width) // 2, (size[1] - img.height) // 2))
        return thumb
    except Exception:
        return None


# ─────────────────────────────────────────────
#  ПЕРЕВОД НА БЕЛОРУССКИЙ
# ─────────────────────────────────────────────
#
# GoogleTranslator из deep_translator - это НЕ официальный API, а обёртка
# над бесплатным виджетом translate.google.com. У него негласный и довольно
# низкий лимит запросов, и после нескольких быстрых запросов подряд Google
# начинает отвечать 429 (TooManyRequests) на некоторое время (обычно от
# десятков секунд до нескольких минут). Поэтому ниже сделано три вещи:
#   1. Кэш переводов на диске - одинаковый текст (шаблонные фразы, имена,
#      должности, повторяющиеся в новостях организации) переводится только
#      один раз и берётся из кэша при повторе.
#   2. Общий "тормоз" между запросами - гарантированная пауза между ЛЮБЫМИ
#      двумя обращениями к Google, а не только внутри одного html.
#   3. Ограниченный retry (2 попытки) + бан-кулдаун на Google, чтобы не
#      тратить время на заведомо провальные запросы, и цепочка резервных
#      переводчиков: MyMemory, а если и он не отвечает - LibreTranslate.

TRANSLATION_CACHE_FILE = "data/translation_cache.json"

_translation_cache: dict[str, str] = {}
_cache_loaded = False

_last_translate_call = 0.0
_MIN_INTERVAL = 1.2  # сек. между запросами к переводчику, чтобы не словить бан

_google_banned_until = 0.0
_GOOGLE_BAN_COOLDOWN = 300  # сек. — после подтверждённого бана не дёргаем Google это время


def _load_translation_cache() -> None:
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        if os.path.exists(TRANSLATION_CACHE_FILE):
            with open(TRANSLATION_CACHE_FILE, "r", encoding="utf-8") as f:
                _translation_cache.update(json.load(f))
    except Exception:
        log.warning("Не удалось загрузить кэш переводов, начинаем с пустого.")


def _save_translation_cache() -> None:
    try:
        os.makedirs(os.path.dirname(TRANSLATION_CACHE_FILE), exist_ok=True)
        with open(TRANSLATION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_translation_cache, f, ensure_ascii=False)
    except Exception:
        log.warning("Не удалось сохранить кэш переводов.")


def _throttle() -> None:
    """Гарантирует паузу не меньше _MIN_INTERVAL между обращениями к сервису."""
    global _last_translate_call
    wait = _MIN_INTERVAL - (time.time() - _last_translate_call)
    if wait > 0:
        time.sleep(wait)
    _last_translate_call = time.time()


def _translate_via_libretranslate(text: str, fallback_exc: Exception | None = None) -> str:
    """Третий резерв. Публичный libretranslate.com не всегда поддерживает
    белорусский и часто требует платный api_key - для надёжности лучше
    указать свой self-hosted URL в LIBRETRANSLATE_URL."""
    from deep_translator import LibreTranslator
    try:
        _throttle()
        kwargs = {"source": "ru", "target": "be", "base_url": LIBRETRANSLATE_URL}
        if LIBRETRANSLATE_API_KEY:
            kwargs["api_key"] = LIBRETRANSLATE_API_KEY
        result = LibreTranslator(**kwargs).translate(text)
        if not result:
            raise ValueError("LibreTranslate вернул пустой ответ")
        _translation_cache[text] = result
        _save_translation_cache()
        return result
    except Exception as exc:
        log.error("LibreTranslate тоже не смог перевести (%r): %s", text[:60], exc)
        raise (fallback_exc or exc) from exc


def _translate_via_mymemory(text: str, fallback_exc: Exception | None = None) -> str:
    """Резервный перевод через MyMemory. Ошибки логируются явно, а не глотаются."""
    from deep_translator import MyMemoryTranslator
    try:
        _throttle()
        result = MyMemoryTranslator(source="ru-RU", target="be-BY").translate(text)
        if not result:
            raise ValueError("MyMemory вернул пустой ответ")
        _translation_cache[text] = result
        _save_translation_cache()
        return result
    except Exception as exc:
        log.error("MyMemory тоже не смог перевести (%r): %s", text[:60], exc)
        return _translate_via_libretranslate(text, fallback_exc or exc)


def _translate_one(text: str) -> str:
    """
    Переводит одну строку ru→be с кэшем, троттлингом, ограниченным retry
    и резервным переводчиком. Если Google подтверждённо забанен, следующие
    _GOOGLE_BAN_COOLDOWN секунд идём сразу в MyMemory, не тратя время на
    заведомо провальные попытки на каждом узле HTML.
    """
    global _google_banned_until
    from deep_translator import GoogleTranslator
    from deep_translator.exceptions import TooManyRequests

    _load_translation_cache()
    if text in _translation_cache:
        return _translation_cache[text]

    if time.time() < _google_banned_until:
        return _translate_via_mymemory(text)

    last_exc = None
    for attempt in range(2):  # 2+4 = 6 сек максимум ожидания на узел, не 62
        _throttle()
        try:
            result = GoogleTranslator(source="ru", target="be").translate(text)
            _translation_cache[text] = result
            _save_translation_cache()
            return result
        except TooManyRequests as exc:
            last_exc = exc
            wait = 2 * (2 ** attempt)
            log.warning("Google Translate: 429, пауза %.0f с (попытка %d/2)",
                        wait, attempt + 1)
            time.sleep(wait)

    # Google не ответил за 2 попытки - считаем забаненным на _GOOGLE_BAN_COOLDOWN
    # секунд и в течение этого времени сразу идём в MyMemory, без повторных 429.
    _google_banned_until = time.time() + _GOOGLE_BAN_COOLDOWN
    log.warning("Google Translate забанен, пауза на %d с, переключаюсь на MyMemory.",
                _GOOGLE_BAN_COOLDOWN)
    return _translate_via_mymemory(text, last_exc)


def _translate(text: str) -> str:
    if not text or not text.strip():
        return text
    return _translate_one(text)


def _translate_html(html: str) -> str:
    if not html or not html.strip():
        return html
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(string=True):
        text = node.strip()
        if text:
            try:
                translated = _translate_one(text)
                if translated:
                    node.replace_with(node.replace(text, translated))
            except Exception as exc:
                log.error("Не удалось перевести фрагмент (%r): %s", text[:60], exc)
                # оставляем как есть, не рушим весь пост
    return str(soup)


def translate_to_belarusian(title: str, preview: str,
                             html_text: str) -> tuple[str, str, str]:
    title_by   = _translate(title)
    preview_by = _translate(preview)
    text_by    = _translate_html(html_text)
    return title_by, preview_by, text_by


# ─────────────────────────────────────────────
#  ОТПРАВКА НА САЙТ
# ─────────────────────────────────────────────

def send_to_bitrix(title: str, preview: str, html_text: str,
                   image_urls: list, preview_image_url: str | None,
                   active_from: str,
                   title_by: str = "", preview_by: str = "",
                   text_by: str = "", publish_by: bool = False,
                   target_section: str = "news") -> dict:
    payload = {
        'token':             SECRET_TOKEN,
        'title':             title,
        'preview':           preview,
        'text':              html_text,
        'image_urls':        image_urls,
        'preview_image_url': preview_image_url,
        'active_from':       active_from,
        'publish_by':        publish_by,
        'title_by':          title_by,
        'preview_by':        preview_by,
        'text_by':           text_by,
        'target_section':    target_section,
    }
    headers = {'Content-Type': 'application/json'}
    resp = with_retry(
        requests.post, BITRIX_API_URL,
        data=json.dumps(payload), headers=headers, timeout=60,
    )
    resp.raise_for_status()
    return resp.json()