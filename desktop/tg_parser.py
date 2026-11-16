"""
CLI-обёртка для публикации поста из Telegram.
Вся бизнес-логика находится в logic.py.
Использование: python tg_parser.py
"""

import asyncio
import json
import logging
import sys
import requests
from datetime import datetime

# Системный патч для Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from core.config import SECRET_TOKEN, BITRIX_API_URL, PREVIEW_MAX_LEN
from core.logic import (
    clean_tg_url, check_duplicate, log_publication,
    fetch_post_data, enhance_with_ai, inject_font_size,
)

# ── Логирование ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
)
log = logging.getLogger(__name__)

# Модель по умолчанию для CLI
_CLI_MODEL_API  = "gemini-2.5-flash"
_CLI_MODEL_NAME = "Gemini 2.5 Flash"


# ─────────────────────────────────────────────
#  CLI-УТИЛИТЫ
# ─────────────────────────────────────────────

def select_preview_image(image_urls: list) -> str | None:
    if not image_urls:
        log.warning("Фотографии в посте не найдены.")
        return None

    log.info("Найдено фотографий: %d", len(image_urls))
    for i, url in enumerate(image_urls, 1):
        print(f"  [{i}] {url}")

    while True:
        choice = input(
            f"\nВыберите номер фото для анонса [1-{len(image_urls)}] "
            f"или Enter чтобы использовать первое: "
        ).strip()
        if choice == '':
            return image_urls[0]
        if choice.isdigit() and 1 <= int(choice) <= len(image_urls):
            idx = int(choice) - 1
            log.info("Выбрано фото [%d]: %s", idx + 1, image_urls[idx])
            return image_urls[idx]
        log.warning("Введите число от 1 до %d", len(image_urls))


def confirm_and_edit(title: str, preview: str) -> tuple[str, str] | None:
    print("\n" + "─" * 60)
    print("📋 РЕЗУЛЬТАТ ГЕНЕРАЦИИ:")
    print(f"  Заголовок : {title}")
    print(f"  Анонс     : {preview}  ({len(preview)}/{PREVIEW_MAX_LEN} симв.)")
    print("─" * 60)

    while True:
        choice = input("Отправить? [y = да / e = редактировать / n = отмена]: ").strip().lower()

        if choice == 'y':
            return title, preview

        if choice == 'n':
            log.info("Публикация отменена пользователем.")
            return None

        if choice == 'e':
            print("\nОставьте поле пустым чтобы не менять его.")
            new_title = input(f"  Новый заголовок [{title}]: ").strip()
            if new_title:
                title = new_title
            new_preview = input(f"  Новый анонс [{preview}]: ").strip()
            if new_preview:
                if len(new_preview) > PREVIEW_MAX_LEN:
                    log.warning("Анонс обрезан до %d символов.", PREVIEW_MAX_LEN)
                    new_preview = new_preview[:PREVIEW_MAX_LEN].rsplit(' ', 1)[0] + '...'
                preview = new_preview
            print(f"\n✏️  Обновлено:")
            print(f"  Заголовок : {title}")
            print(f"  Анонс     : {preview}")
        else:
            log.warning("Введите y, e или n.")


# ─────────────────────────────────────────────
#  ОСНОВНАЯ ФУНКЦИЯ
# ─────────────────────────────────────────────

def process_telegram_post(tg_url: str):
    tg_url = clean_tg_url(tg_url)
    log.info("Скачиваю данные поста: %s", tg_url)

    if check_duplicate(tg_url):
        log.warning("Этот пост уже публиковался.")
        if input("Опубликовать повторно? [y/n]: ").strip().lower() != 'y':
            log.info("Пропускаем.")
            return

    try:
        post_text, image_urls, has_video, post_dt = fetch_post_data(tg_url)
    except requests.RequestException as e:
        log.error("Не удалось загрузить пост: %s", e)
        return

    if has_video:
        log.warning("В посте обнаружено видео — оно не будет загружено на сайт.")

    if not post_text.strip():
        log.error("Текст поста не найден. Публикация невозможна.")
        return

    if post_dt:
        active_from = post_dt
        log.info("Время поста: %s", active_from)
    else:
        active_from = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        log.warning("Время поста не найдено, используется текущее: %s", active_from)

    try:
        title, preview, html_text = enhance_with_ai(post_text, _CLI_MODEL_API, _CLI_MODEL_NAME)
    except Exception as e:
        log.error("Ошибка ИИ: %s", e)
        return

    result = confirm_and_edit(title, preview)
    if result is None:
        return
    title, preview = result

    html_text         = inject_font_size(html_text)
    preview_image_url = select_preview_image(image_urls)

    log.info("Заголовок: %s", title)
    log.info("Анонс    : %s", preview)
    log.info("Активность: %s", active_from)
    log.info("Картинок : %d", len(image_urls))

    payload = {
        'token':             SECRET_TOKEN,
        'title':             title,
        'preview':           preview,
        'text':              html_text,
        'image_urls':        image_urls,
        'preview_image_url': preview_image_url,
        'active_from':       active_from,
    }

    log.info("Отправка на сайт...")
    try:
        resp = requests.post(
            BITRIX_API_URL,
            data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
            timeout=60,
        )
        resp.raise_for_status()
        api_result = resp.json()
        if api_result.get('status') == 'success':
            bitrix_id = api_result.get('id')
            log_publication(tg_url, title, bitrix_id)
            log.info("Успех! Новость создана, ID: %s", bitrix_id)
        else:
            log.error("Ошибка Битрикса: %s", api_result.get('msg'))
    except requests.RequestException as e:
        log.error("Сбой соединения с сайтом: %s", e)
    except Exception as e:
        log.error("Неожиданная ошибка: %s", e)


# ─────────────────────────────────────────────
#  ТОЧКА ВХОДА
# ─────────────────────────────────────────────

if __name__ == "__main__":
    url_to_parse = input("Вставьте ссылку на пост в Telegram: ").strip()
    process_telegram_post(url_to_parse)