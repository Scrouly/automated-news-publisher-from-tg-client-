"""
Бизнес-логика: получение постов, обработка ИИ, перевод, логирование, отправка на сайт.
Не содержит ничего связанного с GUI.
"""

import csv
import difflib
import io
import json
import logging
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from datetime import datetime
from html import escape as _html_escape
from html import unescape as _html_unescape
from urllib.parse import urlparse
from PIL import Image
from google import genai

from core.config import (
    SECRET_TOKEN, BITRIX_API_URL, GEMINI_API_KEY,
    LOG_FILE, REQUESTS_LOG, DAILY_LIMIT, DAILY_LIMITS, PREVIEW_MAX_LEN, DRAFT_FILE,
    MODELS, THINKING_LEVEL, TRANSLATION_GLOSSARY,
)

log = logging.getLogger(__name__)
client = genai.Client(api_key=GEMINI_API_KEY)

TIME_FMT = "%d.%m.%Y %H:%M:%S"


# ─────────────────────────────────────────────
#  URL
# ─────────────────────────────────────────────

def clean_tg_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def parse_tg_urls(raw: str) -> list[str]:
    """
    Достаёт ссылки на посты t.me из произвольного текста (по одной на строку,
    через пробел или запятую), чистит параметры и убирает повторы, сохраняя порядок.
    Ссылки без номера поста (например, просто t.me/channel) игнорируются.
    """
    urls: list[str] = []
    for m in re.findall(r'(?:https?://)?(?:www\.)?(?:t|telegram)\.me/[^\s,;]+', raw or ""):
        if not m.startswith("http"):
            m = "https://" + m
        url = clean_tg_url(m).rstrip("/")
        if re.search(r'/\d+$', url) and url not in urls:
            urls.append(url)
    return urls


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
#  В колонке tg_url может быть несколько ссылок через пробел
#  (новость, собранная из нескольких постов).
# ─────────────────────────────────────────────

def _row_urls(row: dict) -> list[str]:
    return (row.get('tg_url') or '').split()


def check_duplicate(tg_url: str) -> bool:
    if not os.path.isfile(LOG_FILE):
        return False
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if tg_url in _row_urls(row):
                return True
    return False


def find_duplicates(urls: list[str]) -> list[str]:
    """Возвращает те из ссылок, которые уже публиковались."""
    if not os.path.isfile(LOG_FILE):
        return []
    seen: set[str] = set()
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            seen.update(_row_urls(row))
    return [u for u in urls if u in seen]


def log_publication(tg_url: "str | list[str]", title: str, bitrix_id):
    if isinstance(tg_url, (list, tuple)):
        tg_url = " ".join(tg_url)
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['date', 'tg_url', 'title', 'bitrix_id'])
        writer.writerow([datetime.now().strftime(TIME_FMT), tg_url, title, bitrix_id])


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
        writer.writerow([datetime.now().strftime(TIME_FMT), model_name, request_type])


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


def get_limit(model_name: str) -> int:
    return DAILY_LIMITS.get(model_name, DAILY_LIMIT)


def check_limit(model_name: str) -> tuple[bool, int, int]:
    used  = get_today_usage().get(model_name, 0)
    limit = get_limit(model_name)
    return used < limit, used, limit


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
                          preview_image_url: str | None,
                          active_from: str | None = None) -> list[str]:
    """
    Проверяет данные перед отправкой.
    Возвращает список строк с описанием проблем (пустой список = всё ОК).
    active_from — если передан и не пуст, должен быть в формате ДД.ММ.ГГГГ ЧЧ:ММ:СС.
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

    if active_from:
        try:
            datetime.strptime(active_from, TIME_FMT)
        except ValueError:
            issues.append("Начало активности: нужен формат ДД.ММ.ГГГГ ЧЧ:ММ:СС")

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
    post_text = ""
    if text_el:
        # Переносы строк — только из <br>. Раньше get_text(separator='\n') ставил
        # перенос между любыми узлами и рвал предложения на каждом <b>/<a>/<i>.
        for br in text_el.find_all('br'):
            br.replace_with('\n')
        post_text = text_el.get_text().strip()
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
            post_dt = dt_local.strftime(TIME_FMT)
        except (ValueError, TypeError):
            pass

    return post_text, image_urls, has_video, post_dt


@dataclass
class MergedPost:
    """Результат загрузки одного или нескольких постов как одного материала."""
    urls:        list[str]
    text:        str                 # текст постов; при нескольких — с метками [ПОСТ i из n]
    image_urls:  list[str] = field(default_factory=list)
    has_video:   bool = False
    post_dt:     str | None = None   # время самого раннего поста
    posts_count: int = 1             # сколько постов загружено
    text_posts:  int = 1             # в скольких из них был текст


def fetch_posts(urls: list[str]) -> MergedPost:
    """
    Загружает несколько постов и склеивает их в один материал для ИИ.
    - тексты идут в порядке ссылок, каждый помечен [ПОСТ i из n];
    - фото собираются из всех постов без повторов (порядок сохраняется);
    - посты без текста (одни фото) допустимы — из них берутся только фотографии;
    - время активности — время самого раннего поста.
    """
    if not urls:
        raise ValueError("Не указано ни одной ссылки на пост")

    texts: list[str] = []
    images: list[str] = []
    has_video = False
    dts: list[datetime] = []

    for url in urls:
        try:
            text, imgs, video, dt = fetch_post_data(url)
        except requests.RequestException as exc:
            raise RuntimeError(f"Не удалось загрузить {url}: {exc}") from exc
        if text.strip():
            texts.append(text.strip())
        for u in imgs:
            if u not in images:
                images.append(u)
        has_video = has_video or video
        if dt:
            try:
                dts.append(datetime.strptime(dt, TIME_FMT))
            except ValueError:
                pass

    if len(texts) <= 1:
        merged_text = texts[0] if texts else ""
    else:
        n = len(texts)
        merged_text = "\n\n".join(f"[ПОСТ {i} из {n}]\n{t}" for i, t in enumerate(texts, 1))

    return MergedPost(
        urls=list(urls),
        text=merged_text,
        image_urls=images,
        has_video=has_video,
        post_dt=min(dts).strftime(TIME_FMT) if dts else None,
        posts_count=len(urls),
        text_posts=len(texts),
    )


# ─────────────────────────────────────────────
#  ИИ — ОБЩИЙ ВЫЗОВ С ПЕРЕКЛЮЧЕНИЕМ МОДЕЛЕЙ
# ─────────────────────────────────────────────

_RETRYABLE_CODES = (429, 500, 502, 503, 504)

# Если SDK или модель не принимает thinking_level — отключаем этот параметр до
# конца работы программы (запросы идут с размышлениями по умолчанию, но не падают).
_thinking_disabled = False


def _parse_json(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            raise ValueError(f"ИИ не вернул валидный JSON. Ответ: {raw[:300]}")
        try:
            data = json.loads(match.group())
        except ValueError:
            raise ValueError(f"ИИ вернул повреждённый JSON. Ответ: {raw[:300]}")
    if not isinstance(data, dict):
        raise ValueError(f"ИИ вернул не JSON-объект. Ответ: {raw[:300]}")
    return data


def _build_config(json_mode: bool) -> dict:
    cfg: dict = {}
    if json_mode:
        cfg["response_mime_type"] = "application/json"
    if THINKING_LEVEL and not _thinking_disabled:
        cfg["thinking_config"] = {"thinking_level": THINKING_LEVEL}
    return cfg


def _generate(prompt: str, model_api: str, model_name: str,
              request_type: str, json_mode: bool = False) -> str:
    """
    Вызывает Gemini. Если выбранная модель ответила 429 (лимит) или 5xx — после
    одного повтора при 5xx переходит на следующую модель из MODELS. Модели, у которых
    исчерпан локальный дневной лимит, пропускаются. Запрос пишется в лог под именем
    той модели, которая реально ответила. Время и число токенов каждого запроса
    пишутся в лог (data/app.log) — по ним видно, на что уходит время.
    """
    global _thinking_disabled

    candidates = [(model_name, model_api)] + [
        (n, a) for n, a in MODELS.items() if a != model_api
    ]

    last_exc: Exception | None = None
    for name, api in candidates:
        if not check_limit(name)[0]:
            log.info("Пропускаю %s: локальный дневной лимит исчерпан", name)
            continue
        attempt = 0
        while attempt < 2:
            cfg = _build_config(json_mode)
            kwargs = {"config": cfg} if cfg else {}
            started = time.monotonic()
            try:
                response = client.models.generate_content(
                    model=api, contents=prompt, **kwargs)
            except Exception as exc:
                last_exc = exc
                code = getattr(exc, "code", None)
                if code in _RETRYABLE_CODES:
                    if code != 429 and attempt == 0:
                        attempt += 1
                        time.sleep(2)
                        continue
                    log.warning("%s: ошибка %s — перехожу на следующую модель", name, code)
                    break
                if "thinking_config" in cfg and "thinking" in str(exc).lower():
                    # Старый SDK или модель не знает thinking_level: пробуем без него.
                    log.warning(
                        "Параметр thinking_level не принят (%s: %s). Отключаю его; "
                        "обновите SDK: pip install -U google-genai",
                        type(exc).__name__, exc)
                    _thinking_disabled = True
                    continue
                raise
            elapsed = time.monotonic() - started
            usage = getattr(response, "usage_metadata", None)
            log.info("Gemini %s [%s]: %.1f с (вход %s, мысли %s, ответ %s ток.)",
                     name, request_type, elapsed,
                     getattr(usage, "prompt_token_count", "?"),
                     getattr(usage, "thoughts_token_count", "?"),
                     getattr(usage, "candidates_token_count", "?"))
            log_request(name, request_type)
            text = getattr(response, "text", None)
            if not text or not text.strip():
                raise ValueError(
                    "ИИ вернул пустой ответ (возможно, сработал фильтр безопасности)")
            return text.strip()

    if last_exc is not None:
        raise RuntimeError(
            "Не удалось получить ответ ни от одной модели (лимиты исчерпаны?): "
            f"{last_exc}") from last_exc
    raise RuntimeError("Дневной лимит исчерпан для всех моделей. Попробуйте завтра.")


# ─────────────────────────────────────────────
#  АНОНС: КОНТРОЛЬ ПОХОЖЕСТИ НА ЗАГОЛОВОК
# ─────────────────────────────────────────────

PREVIEW_SIMILARITY_LIMIT = 0.6
_STOP_STEMS = {"долом"}   # «Доломит» есть почти в каждом заголовке и анонсе — не считаем повтором


def _norm(text: str) -> str:
    return re.sub(r'[^a-zа-я0-9]+', ' ', text.lower().replace('ё', 'е')).strip()


def _stems(text: str) -> set[str]:
    """Грубые «основы» слов (первые 5 букв): ловят одно слово в разных падежах."""
    return {w[:5] for w in _norm(text).split() if len(w) > 3} - _STOP_STEMS


def similarity(title: str, preview: str) -> float:
    """0..1: насколько анонс повторяет заголовок (общие слова + общая строка)."""
    a, b = _stems(title), _stems(preview)
    if not a or not b:
        return 0.0
    overlap = len(a & b) / len(a)   # доля слов заголовка, которые уже есть в анонсе
    ratio = difflib.SequenceMatcher(None, _norm(title), _norm(preview)).ratio()
    return max(overlap, ratio)


def preview_too_similar(title: str, preview: str) -> bool:
    return similarity(title, preview) >= PREVIEW_SIMILARITY_LIMIT


def fit_preview(preview: str, limit: int = PREVIEW_MAX_LEN) -> str:
    """Обрезает анонс до limit символов (вместе с многоточием), лучше по концу предложения."""
    preview = preview.strip()
    if len(preview) <= limit:
        return preview
    cut = preview[:limit]
    end = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
    if end >= limit * 0.5:
        return cut[:end + 1]
    cut = preview[:limit - 1].rsplit(' ', 1)[0].rstrip('.,;:—- ')
    return cut + '…'


_PREVIEW_RULES = """\
АНОНС (preview) — самостоятельный текст для ленты новостей: строго 1–2 предложения, не более {max_len} символов.
ГЛАВНОЕ ПРАВИЛО: анонс НЕ пересказывает заголовок. Читатель видит заголовок прямо над анонсом, поэтому анонс обязан добавить то, чего в заголовке нет: когда и где это было, кто участвовал, что именно произошло, какой результат или цифра.
- Не начинай анонс теми же словами, что и заголовок, и не повторяй формулировку заголовка другими словами.
- Не используй сказуемое и ключевые слова заголовка, если без них можно обойтись.
- Бери только факты из текста, ничего не выдумывай. Кавычки — только «ёлочки».

Примеры пары «заголовок → анонс»:
Заголовок: Масленица в микрорайоне Руба прошла весело и вкусно!
ПЛОХО (повтор): Масленица в микрорайоне Руба прошла весело и вкусно.
ХОРОШО: В минувшие выходные работники ОАО «Доломит», жители и гости микрорайона собрались, чтобы весело проводить зиму и встретить весну.

Заголовок: Работники ОАО «Доломит» почтили память павших героев.
ПЛОХО (повтор): Работники ОАО «Доломит» почтили память павших героев.
ХОРОШО: В День защитников Отечества представители предприятия приняли участие в церемонии возложения цветов к памятнику Неизвестному солдату."""


# ─────────────────────────────────────────────
#  ИИ — ПОЛНАЯ ГЕНЕРАЦИЯ
# ─────────────────────────────────────────────

_ENHANCE_PROMPT = """\
Ты — ведущий редактор пресс-службы предприятия ОАО «Доломит».
Твоя задача: переработать {source_desc} в профессиональную новостную заметку для официального сайта.

### ГЛАВНЫЕ ПРАВИЛА:
1. КАВЫЧКИ: используй исключительно «ёлочки». КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО удалять их или заменять на "".
2. ЗАГОЛОВОК (title): короткий, ёмкий, заканчивается точкой или восклицательным знаком. Это отдельное поле, в тексте новости он НЕ дублируется.
3. {preview_rules}
4. ПОЛНЫЙ ТЕКСТ (text):
   - {text_rule}
   - НЕ включай заголовок в начало текста: текст начинается сразу с первого предложения новости.
   - Если ты использовал предложение для анонса, оно ВСЁ РАВНО остаётся в полном тексте.
   - Разбей текст на логические абзацы, обернув каждый в тег <p>.
   - Удали эмодзи, хештеги (#новости) и призывы «подпишись на наш канал».

### ПРИМЕРЫ ЗАГОЛОВКОВ:
- Работники ОАО «Доломит» почтили память павших героев.
- Масленица в микрорайоне Руба прошла весело и вкусно!
- В ОАО «Доломит» прошёл Единый день информирования.
- 29.01.2026 с рабочим визитом в ОАО «Доломит» прибыли представители компании CUEC.

### ИСХОДНЫЙ МАТЕРИАЛ:
{raw_text}

### ФОРМАТ ОТВЕТА
Верни ТОЛЬКО JSON-объект с тремя строковыми полями:
{{"title": "заголовок", "preview": "анонс", "text": "полный текст с тегами <p>"}}
"""


def enhance_with_ai(raw_text: str, model_api: str, model_name: str,
                    posts_count: int = 1) -> tuple[str, str, str]:
    """
    Полная генерация: заголовок + анонс + текст.
    posts_count > 1 — исходник состоит из нескольких постов [ПОСТ i из n],
    которые нужно объединить в одну новость.
    """
    if posts_count > 1:
        source_desc = (f"{posts_count} постов из Telegram об одном событии или теме "
                       "(они помечены [ПОСТ i из n])")
        text_rule = (
            f"Объедини все {posts_count} поста в ОДНУ связную новость: расположи факты в "
            "логичном порядке, убери повторы между постами, но сохрани ВСЕ уникальные факты, "
            "цифры, фамилии, даты и цитаты. Метки [ПОСТ …] в ответ не включай."
        )
    else:
        source_desc = "текст из Telegram"
        text_rule = "Должен содержать ВЕСЬ исходный текст сообщения без сокращений."

    prompt = _ENHANCE_PROMPT.format(
        source_desc=source_desc,
        preview_rules=_PREVIEW_RULES.format(max_len=PREVIEW_MAX_LEN),
        text_rule=text_rule,
        raw_text=raw_text,
    )
    raw  = _generate(prompt, model_api, model_name, 'full', json_mode=True)
    data = _parse_json(raw)

    title   = str(data.get('title',   '') or '').strip()
    preview = str(data.get('preview', '') or '').strip()
    text    = str(data.get('text',    '') or '').strip()

    if not title or not text:
        raise ValueError("ИИ вернул пустой заголовок или текст.")

    # Анонс почти повторяет заголовок — один раз просим переписать с конкретным замечанием.
    if preview_too_similar(title, preview):
        log.info("Анонс слишком похож на заголовок (%.2f) — переписываю", similarity(title, preview))
        try:
            preview = regenerate_preview(text, model_api, model_name,
                                         title=title, previous=preview)
        except Exception as exc:
            log.warning("Не удалось переписать анонс: %s", exc)

    return title, fit_preview(preview), text


# ─────────────────────────────────────────────
#  ИИ — ПЕРЕГЕНЕРАЦИЯ ЗАГОЛОВКА
# ─────────────────────────────────────────────

def regenerate_title(current_text: str, model_api: str, model_name: str,
                     previous: str = "") -> str:
    prev_rule = (f"- Не повторяй предыдущий вариант: «{previous}»\n" if previous else "")
    prompt = f"""
Ты — редактор пресс-службы ОАО «Доломит».
Придумай НОВЫЙ вариант заголовка для следующего текста новости.

ПРАВИЛА:
- Строго 1 короткое предложение, в конце точка или восклицательный знак
- Используй кавычки «ёлочки», не заменяй их на ""
{prev_rule}- Заголовок должен быть ёмким и информативным

Текст новости:
{current_text}

Ответь ТОЛЬКО заголовком, без кавычек вокруг него, пояснений и JSON.
"""
    result = _generate(prompt, model_api, model_name, 'title')
    return result.strip().strip('"').strip("'")


# ─────────────────────────────────────────────
#  ИИ — ПЕРЕГЕНЕРАЦИЯ АНОНСА
# ─────────────────────────────────────────────

def regenerate_preview(current_text: str, model_api: str, model_name: str,
                       title: str = "", previous: str = "") -> str:
    extra = ""
    if title:
        extra += f"\nЗаголовок новости (анонс НЕ должен его повторять): {title}\n"
    if previous:
        extra += (f"\nПредыдущий вариант анонса (не подходит — слишком похож на заголовок "
                  f"или повторяет его): {previous}\n")
    prompt = f"""
Ты — редактор пресс-службы ОАО «Доломит».
Напиши НОВЫЙ вариант анонса для следующей новости.

{_PREVIEW_RULES.format(max_len=PREVIEW_MAX_LEN)}
{extra}
Текст новости:
{current_text}

Ответь ТОЛЬКО анонсом, без пояснений и JSON.
"""
    result = _generate(prompt, model_api, model_name, 'preview')
    return fit_preview(result.strip().strip('"').strip("'"))


# ─────────────────────────────────────────────
#  HTML / ФОРМАТИРОВАНИЕ
# ─────────────────────────────────────────────

def html_to_text(html: str) -> str:
    """HTML новости -> обычный текст с абзацами (для запросов к ИИ: без лишних тегов)."""
    soup = BeautifulSoup(html or "", "html.parser")
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    paras = [p for p in paras if p]
    return "\n\n".join(paras) if paras else soup.get_text(" ", strip=True)


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
#  ПЕРЕВОД НА БЕЛОРУССКИЙ (через ИИ)
#  Один запрос на всю новость: заголовок + анонс + текст вместе, чтобы терминология
#  и имена были согласованы. HTML ИИ не видит: код вынимает из него текстовые
#  фрагменты, отдаёт их списком и вставляет переводы обратно. Поэтому теги и
#  атрибуты гарантированно не портятся, а ответ короче (нет разметки в токенах).
#  Если ИИ вернул не то количество фрагментов или русский текст — один повтор.
# ─────────────────────────────────────────────

_TRANSLATE_PROMPT = """\
Ты — профессиональный переводчик и редактор с русского на белорусский язык. \
Переведи новость для официального сайта ОАО «Доломит».

ТРЕБОВАНИЯ:
1. Литературный белорусский язык по действующим правилам (официальная орфография, «наркамаўка»), НЕ тарашкевица. Пиши естественно по-белорусски, а не калькой с русского: правильные предлоги, падежи и согласование, числительные, даты в белорусской форме («29 студзеня 2026 года»), чередование у/ва, з/са, і/й.
2. Поле fragments — последовательные части ОДНОГО текста (абзацы или куски абзацев между HTML-тегами; сами теги тебе не передаются). Переведи каждый фрагмент и верни список fragments ТОЧНО той же длины и в том же порядке: ничего не объединяй, не разделяй, не пропускай и не переставляй. Первый и последний символы-пробелы фрагментов сохраняй.
3. Кавычки — только «ёлочки», как в оригинале. Числа, телефоны, ссылки и слова латиницей не меняй.
4. Имена и фамилии людей передавай по-белорусски (Иван Петров → Іван Пятроў). Географические названия — в белорусской форме (Витебск → Віцебск).
5. Ничего не добавляй и не сокращай. Заголовок переводи как заголовок, анонс — как анонс (пустое поле оставь пустым).
{glossary_rule}
Верни ТОЛЬКО JSON-объект с полями title, preview, fragments.
{retry_note}
ИСХОДНЫЕ ДАННЫЕ (JSON):
{payload}
"""


_TAG_SPLIT = re.compile(r'(<[^>]*>)')


def _split_html(html: str) -> tuple[list[str], list[int]]:
    """
    Режет HTML на части: чётные индексы — текст, нечётные — теги (и комментарии).
    Возвращает (части, индексы текстовых частей, которые нужно переводить).
    Пробельные части, содержимое script/style и комментарии не переводятся.
    """
    parts = _TAG_SPLIT.split(html or "")
    idx: list[int] = []
    skip: str | None = None
    for i, part in enumerate(parts):
        if i % 2 == 1:
            m = re.match(r'<\s*(/?)\s*(script|style)\b', part, re.IGNORECASE)
            if m:
                skip = None if m.group(1) else m.group(2).lower()
            continue
        if skip is None and _html_unescape(part).strip():
            idx.append(i)
    return parts, idx


def _looks_belarusian(text: str) -> bool:
    """
    В белорусском алфавите нет букв «и», «щ», «ъ». Если их заметная доля среди
    кириллицы — ИИ, скорее всего, вернул русский текст. Короткие тексты не проверяем.
    """
    plain = re.sub(r'<[^>]+>', ' ', text or '').lower()
    letters = re.findall(r'[а-яёіў]', plain)
    if len(letters) < 150:
        return True
    foreign = sum(1 for ch in letters if ch in 'ищъ')
    return foreign / len(letters) <= 0.02


def _fragments_problem(src: list[str], out) -> str | None:
    if not isinstance(out, list) or not all(isinstance(x, str) for x in out):
        return "поле fragments должно быть списком строк"
    if len(out) != len(src):
        return (f"в fragments {len(out)} элементов, а в исходных данных {len(src)}: "
                "верни список ТОЧНО той же длины, ничего не объединяя и не пропуская")
    if any(not o.strip() for o in out):
        return "среди fragments есть пустые элементы: переведи каждый фрагмент"
    if not _looks_belarusian(" ".join(out)):
        return "в тексте остались русские слова (буквы «и», «щ», «ъ»): переведи ВСЁ на белорусский"
    return None


def translate_to_belarusian(title: str, preview: str, html_text: str,
                             model_api: str | None = None,
                             model_name: str | None = None) -> tuple[str, str, str]:
    """Переводит новость на белорусский одним запросом к ИИ. Возвращает (title, preview, text)."""
    if not model_api or not model_name:
        model_name, model_api = next(iter(MODELS.items()))

    parts, idx = _split_html(html_text)
    src = [_html_unescape(parts[i]).strip() for i in idx]

    glossary = "\n".join(f"   - «{ru}» → «{by}»" for ru, by in TRANSLATION_GLOSSARY.items())
    glossary_rule = (
        "6. Устойчивые названия переводи строго так:\n" + glossary + "\n"
    ) if glossary else ""
    payload = json.dumps(
        {"title": title, "preview": preview, "fragments": src}, ensure_ascii=False)

    retry_note = ""
    problem = ""
    for attempt in range(2):
        prompt = _TRANSLATE_PROMPT.format(
            glossary_rule=glossary_rule, retry_note=retry_note, payload=payload)
        raw  = _generate(prompt, model_api, model_name, 'translate', json_mode=True)
        data = _parse_json(raw)

        t_by = str(data.get('title',   '') or '').strip()
        p_by = str(data.get('preview', '') or '').strip()
        frags = data.get('fragments')
        if frags is None and not src:
            frags = []

        if not t_by:
            problem = "пустой заголовок в ответе"
        else:
            problem = _fragments_problem(src, frags) or ""
        if not problem:
            out_parts = list(parts)
            for i, new in zip(idx, frags):
                raw   = parts[i]
                lead  = raw[:len(raw) - len(raw.lstrip())]
                trail = raw[len(raw.rstrip()):]
                out_parts[i] = lead + _html_escape(new.strip(), quote=False) + trail
            return t_by, fit_preview(p_by), "".join(out_parts)

        log.warning("Перевод не прошёл проверку (попытка %d/2): %s", attempt + 1, problem)
        retry_note = f"\nВНИМАНИЕ: в предыдущем ответе была ошибка — {problem}. Исправь её.\n"

    raise ValueError(f"ИИ не смог корректно перевести текст: {problem}")


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
