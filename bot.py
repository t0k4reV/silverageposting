#!/usr/bin/env python3
"""
Бот «Серебряный возраст» — автопостинг в MAX мессенджер.

Работает как демон с расписанием (APScheduler):
  07:00 MSK — генерация контента + картинок на весь день
  08:00 MSK — Пост 1: Утренняя открытка
  11:00 MSK — Пост 2: Лайфхак / Здоровье
  14:00 MSK — Пост 3: Ностальгия
  17:00 MSK — Пост 4: Рецепт
  21:00 MSK — Пост 5: Вечерняя открытка

Переменные окружения (обязательные):
  GROQ_API_KEY       — ключ Groq API
  MAX_BOT_TOKEN      — токен бота MAX
  MAX_CHAT_ID        — ID канала MAX
  WAVESPEED_API_KEY  — ключ WaveSpeed AI

Опциональные:
  GROQ_MODEL         — модель Groq (по умолчанию moonshotai/kimi-k2-instruct-0905)
  TZ                 — часовой пояс (по умолчанию Europe/Moscow)
  POST_TIMES         — время постов через запятую (по умолчанию 08:00,11:00,14:00,17:00,21:00)
  GENERATE_TIME      — время генерации (по умолчанию 07:00)

CLI (для тестирования):
  python3 bot.py --now              # сгенерировать и опубликовать прямо сейчас
  python3 bot.py --generate-only    # только сгенерировать, не публиковать
  python3 bot.py --post N           # опубликовать пост N (1-5) из сегодняшнего контента
  python3 bot.py --date 2026-04-01  # на конкретную дату
"""

import os
import sys
import re
import io
import time
import json
import signal
import logging
import datetime
import tempfile
import requests

# ── Логирование ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("silverage")

# ── Настройки из переменных окружения ────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "moonshotai/kimi-k2-instruct-0905")

MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN", "")
MAX_CHAT_ID = os.environ.get("MAX_CHAT_ID", "")
MAX_API_BASE = "https://platform-api.max.ru"

WAVESPEED_API_KEY = os.environ.get("WAVESPEED_API_KEY", "")
WAVESPEED_MODEL = os.environ.get("WAVESPEED_MODEL", "wavespeed-ai/flux-schnell")
WAVESPEED_API_BASE = "https://api.wavespeed.ai/api/v3"

# Pexels — бесплатные фото для открыток
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

TZ_NAME = os.environ.get("TZ", "Europe/Moscow")

# Время постинга (MSK по умолчанию)
POST_TIMES = os.environ.get("POST_TIMES", "08:00,11:00,14:00,17:00,21:00").split(",")
GENERATE_TIME = os.environ.get("GENERATE_TIME", "07:00")

# Пути к файлам
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
POSTS_FILE = os.path.join(DATA_DIR, "posts.txt")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
IMAGES_DIR = os.path.join(DATA_DIR, "images")

# Создаём директории
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

# ── Хранилище сегодняшних постов в памяти ────────────────
today_posts = {}  # {date_str: [post1, post2, ...]}

# ── Вспомогательные ──────────────────────────────────────
DAYS_RU = {
    0: "понедельник", 1: "вторник", 2: "среда",
    3: "четверг", 4: "пятница", 5: "суббота", 6: "воскресенье"
}
MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
}


def format_date(d):
    return f"{d.day} {MONTHS_RU[d.month]} {d.year} ({DAYS_RU[d.weekday()]})"


def now_msk():
    """Текущее время по Москве."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(TZ_NAME))
    except ImportError:
        # Fallback: UTC+3
        return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))


def today_msk():
    return now_msk().date()


# ── История тем (для дедупликации) ───────────────────────

def load_history():
    """Загружает историю использованных тем."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"topics": []}


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_previous_topics():
    history = load_history()
    # Последние 15 тем, только ключевые слова (экономим токены)
    topics = history.get("topics", [])[-15:]
    return ", ".join(topics)


def add_topics_to_history(topics_list):
    history = load_history()
    # Сохраняем только короткие ключевые слова (первые 5 слов темы)
    short = []
    for t in topics_list:
        words = t.split()[:5]
        short.append(" ".join(words))
    history["topics"].extend(short)
    # Храним максимум 100 тем
    history["topics"] = history["topics"][-100:]
    save_history(history)


# ── 1. Генерация текста через Groq ───────────────────────

def generate_posts(target_date):
    """Генерирует 5 постов через Groq API."""
    date_str = format_date(target_date)
    previous_topics = get_previous_topics()

    previous_block = ""
    if previous_topics:
        previous_block = f"\nНЕ повторяй темы: {previous_topics}\n"

    system_prompt = """Копирайтер канала «Серебряный возраст ✨» (MAX). Аудитория: женщины 50-70, Россия.
Тон: тёплая подруга, кратко, душевно, лёгкий юмор. Абзацы через пустую строку, списки с новой строки.
Посты 2,3,4: 80-120 слов, эмодзи-маркеры (💚📖🍲✅).
Посты 1,5 (открытки): текст=ТОЛЬКО призыв+ссылка, пожелание в «✉️ Надпись на открытке» (2-4 строки).
Картинки: БЕЗ людей на переднем плане, только ЦВЕТНЫЕ (не ч/б)."""

    date_tag = target_date.strftime('%d%b').lower()
    link = "[«Серебряный возраст ✨»](https://max.ru/id540697837513_biz)"
    user_prompt = f"""Сегодня {date_str}. 5 постов:
1. 🌅 ОТКРЫТКА утро (дата, сезон, праздник) 2. 💚 ЛАЙФХАК 3. 📖 НОСТАЛЬГИЯ (СССР) 4. 🍲 РЕЦЕПТ 5. 🌙 ОТКРЫТКА вечер
Посты 2,3,4: 80-120 слов. Посты 1,5: текст=призыв+ссылка, пожелание в «✉️ Надпись на открытке».
{previous_block}Конец постов 2,3,4: призыв + {link}. Постов 1,5: призыв + {link} (без пожелания в тексте!).
Ссылка markdown, НЕ отдельно! Промпты картинок: NO people in foreground, vibrant colors, NO black-and-white.

ФОРМАТ:

===================================================
СЕРЕБРЯНЫЙ ВОЗРАСТ — Контент на {date_str}
===================================================

---
📌 ПОСТ 1 — УТРЕННЯЯ ОТКРЫТКА
Тема: [тема]
---

Перешлите открытку тому, кого хотите обнять 💌

Ваш канал: {link}

✉️ Надпись на открытке:
[2-4 строки душевного пожелания с датой/сезоном/праздником]

📸 Картинка: post1_morning_{date_tag}.png

🔍 Поиск фото (2-3 слова, англ., без людей):
[например: spring flowers sunrise, garden morning dew, blooming sakura]

---
📌 ПОСТ 2 — ЛАЙФХАК / ЗДОРОВЬЕ
Тема: [тема]
---

[текст 80-120 слов]

[призыв]

Ваши полезные советы: {link}

📸 Картинка: post2_health_{date_tag}.png

🖼 Промпт для картинки (англ., no people, colorful):
[промпт]

---
📌 ПОСТ 3 — НОСТАЛЬГИЯ
Тема: [тема]
---

[текст 80-120 слов]

[призыв]

Ваш уютный уголок: {link}

📸 Картинка: post3_nostalgia_{date_tag}.png

🖼 Промпт для картинки (англ., no people, colorful):
[промпт]

---
📌 ПОСТ 4 — РЕЦЕПТ
Тема: [тема]
---

[текст 80-120 слов]

[призыв]

Рецепты с душой: {link}

📸 Картинка: post4_recipe_{date_tag}.png

🖼 Промпт для картинки (англ., no people, colorful):
[промпт]

---
📌 ПОСТ 5 — ВЕЧЕРНЯЯ ОТКРЫТКА
Тема: [тема]
---

Перешлите открытку подруге — пусть и у неё вечер будет тёплым 💌

Ваш уютный вечер: {link} 🌙

✉️ Надпись на открытке:
[2-4 строки вечернего пожелания, тёплые слова на ночь]

📸 Картинка: post5_evening_{date_tag}.png

🔍 Поиск фото (2-3 слова, англ.):
[например: cozy candle evening, warm tea blanket, sunset window]

===================================================
КОНЕЦ КОНТЕНТА НА {date_str.upper()}
==================================================="""

    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY)
    log.info(f"Генерирую контент на {date_str}...")

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.9,
        max_tokens=8000,
    )

    content = response.choices[0].message.content
    log.info(f"Контент получен ({len(content)} символов)")
    return content


def save_posts_to_file(content):
    """Сохраняет контент в posts.txt."""
    separator = "\n\n" if os.path.exists(POSTS_FILE) and os.path.getsize(POSTS_FILE) > 0 else ""
    with open(POSTS_FILE, "a", encoding="utf-8") as f:
        f.write(separator + content + "\n")
    log.info(f"Контент сохранён в {POSTS_FILE}")


# ── 2. Картинки ──────────────────────────────────────────

def fetch_pexels_photo(query, filename):
    """Скачивает фото с Pexels по запросу. Бесплатно, 200 req/month."""
    filepath = os.path.join(IMAGES_DIR, filename)
    log.info(f"Ищу фото на Pexels: '{query}' → {filename}")

    if not PEXELS_API_KEY:
        log.warning("PEXELS_API_KEY не задан, пропускаю Pexels")
        return None

    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={
                "query": query,
                "per_page": 15,
                "orientation": "landscape",
                "size": "medium",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        photos = data.get("photos", [])
        if not photos:
            log.warning(f"Pexels: нет результатов по запросу '{query}'")
            return None

        # Берём случайное фото из топ-15
        import random
        photo = random.choice(photos)
        img_url = photo["src"]["large"]  # 940px wide

        img_resp = requests.get(img_url, timeout=30)
        img_resp.raise_for_status()

        with open(filepath, "wb") as f:
            f.write(img_resp.content)

        size_kb = os.path.getsize(filepath) // 1024
        photographer = photo.get("photographer", "?")
        log.info(f"Фото скачано: {filename} ({size_kb} KB, by {photographer})")
        return filepath

    except Exception as e:
        log.error(f"Pexels ошибка: {str(e)[:150]}")
        return None


def generate_image(prompt_text, filename):
    """Генерирует картинку через WaveSpeed AI (FLUX Schnell)."""
    clean_prompt = re.sub(r'--\w+\s+[\d.:]+', '', prompt_text).strip()
    # Гарантируем: без людей на переднем плане, цветная картинка
    if "no people" not in clean_prompt.lower():
        clean_prompt += ", no people in foreground, vibrant warm colors"
    filepath = os.path.join(IMAGES_DIR, filename)
    log.info(f"Генерирую картинку: {filename}...")

    if not WAVESPEED_API_KEY:
        log.error("WAVESPEED_API_KEY не задан!")
        return None

    headers = {
        "Authorization": f"Bearer {WAVESPEED_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            f"{WAVESPEED_API_BASE}/{WAVESPEED_MODEL}",
            headers=headers,
            json={
                "prompt": clean_prompt,
                "size": "1024*768",
                "num_images": 1,
                "seed": -1,
                "output_format": "jpeg",
                "enable_sync_mode": True,
            },
            timeout=120,
        )
        resp.raise_for_status()
        task = resp.json()
        status = task.get("data", {}).get("status", "")

        if status == "completed":
            outputs = task["data"].get("outputs", [])
            if outputs:
                img_url = outputs[0]
                img_resp = requests.get(img_url, timeout=60)
                img_resp.raise_for_status()
                with open(filepath, "wb") as f:
                    f.write(img_resp.content)
                size_kb = os.path.getsize(filepath) // 1024
                ms = task["data"].get("executionTime", 0)
                log.info(f"Картинка готова: {filename} ({size_kb} KB, {ms}ms)")
                return filepath

        # Fallback: polling
        task_id = task.get("data", {}).get("id")
        if not task_id:
            log.error(f"Нет task_id: {task}")
            return None

        log.info(f"Задача {task_id}, жду результат...")
        for attempt in range(30):
            time.sleep(2)
            result_resp = requests.get(
                f"{WAVESPEED_API_BASE}/predictions/{task_id}/result",
                headers=headers,
                timeout=30,
            )
            result = result_resp.json()
            st = result.get("data", {}).get("status", "")
            if st == "completed":
                outputs = result["data"].get("outputs", [])
                if outputs:
                    img_resp = requests.get(outputs[0], timeout=60)
                    with open(filepath, "wb") as f:
                        f.write(img_resp.content)
                    size_kb = os.path.getsize(filepath) // 1024
                    log.info(f"Картинка готова: {filename} ({size_kb} KB)")
                    return filepath
            elif st == "failed":
                log.error(f"Генерация провалилась: {result['data'].get('error', '?')}")
                return None

        log.error(f"Таймаут генерации картинки: {filename}")
        return None

    except Exception as e:
        log.error(f"Ошибка генерации картинки: {str(e)[:200]}")
        return None


def overlay_text_on_image(filepath, text):
    """Накладывает текст пожелания на картинку-открытку."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter

        # Открываем изображение (может быть JPEG несмотря на расширение .png)
        img = Image.open(filepath)
        img = img.convert("RGBA")
        w, h = img.size

        # Полупрозрачный слой для подложки текста
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Шрифт (Georgia Bold — есть кириллица)
        font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
        font_path = os.path.join(font_dir, "GeorgiaBold.ttf")
        if not os.path.exists(font_path):
            # Fallback: системный шрифт macOS
            for fp in ["/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
                        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"]:
                if os.path.exists(fp):
                    font_path = fp
                    break
            else:
                font_path = None

        # Подбираем размер шрифта
        font_size = int(h * 0.065)
        if font_path:
            font = ImageFont.truetype(font_path, font_size)
        else:
            font = ImageFont.load_default()

        # Убираем эмодзи (шрифт их не поддерживает) и разбиваем на строки
        import unicodedata
        clean_text = "".join(
            c for c in text
            if unicodedata.category(c) not in ("So", "Sk", "Cn")
            or c in ("«", "»", "—", "–", "…", "✨")
        )
        # Убираем символы-вариаторы и ZWJ
        clean_text = re.sub(r'[\ufe0f\ufe0e\u200d\u200b]', '', clean_text)
        lines = [line.strip() for line in clean_text.split("\n") if line.strip()]

        # Считаем размер текстового блока
        line_spacing = int(font_size * 0.4)
        total_height = len(lines) * font_size + (len(lines) - 1) * line_spacing
        max_line_width = 0
        line_bboxes = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            lw = bbox[2] - bbox[0]
            line_bboxes.append(lw)
            max_line_width = max(max_line_width, lw)

        # Позиция: по центру, слегка ниже середины
        pad_x, pad_y = 40, 25
        block_x = (w - max_line_width) // 2
        block_y = (h - total_height) // 2 + int(h * 0.05)

        # Полупрозрачная подложка
        draw.rounded_rectangle(
            [block_x - pad_x, block_y - pad_y,
             block_x + max_line_width + pad_x, block_y + total_height + pad_y],
            radius=20,
            fill=(255, 255, 255, 160),
        )

        # Рисуем текст
        y = block_y
        for i, line in enumerate(lines):
            lw = line_bboxes[i]
            x = (w - lw) // 2  # центрируем каждую строку
            # Тень
            draw.text((x + 2, y + 2), line, font=font, fill=(80, 60, 40, 120))
            # Основной текст
            draw.text((x, y), line, font=font, fill=(60, 30, 10, 255))
            y += font_size + line_spacing

        # Объединяем
        result = Image.alpha_composite(img, overlay).convert("RGB")
        result.save(filepath, "JPEG", quality=95)
        log.info(f"Надпись наложена на {os.path.basename(filepath)}")
        return filepath

    except Exception as e:
        log.error(f"Ошибка наложения текста: {e}")
        return filepath


# ── 3. Парсинг постов ────────────────────────────────────

def parse_posts(content):
    """Парсит сгенерированный контент в список постов."""
    posts = []
    blocks = re.split(r'---\s*\n📌\s*ПОСТ\s+(\d+)', content)

    for i in range(1, len(blocks), 2):
        post_num = int(blocks[i])
        block = blocks[i + 1] if i + 1 < len(blocks) else ""

        # Тема
        topic_match = re.search(r'Тема:\s*(.+)', block)
        topic = topic_match.group(1).strip() if topic_match else f"Пост {post_num}"

        # Промпт для картинки (AI-генерация)
        prompt_match = re.search(r'Промпт для (?:генерации картинки|generating picture):\s*\n(.+?)(?:\n\n|\n---)', block, re.DOTALL)
        if not prompt_match:
            prompt_match = re.search(r'Промпт для (?:генерации картинки|generating picture):\s*\n(.+)', block)
        img_prompt = prompt_match.group(1).strip() if prompt_match else ""

        # Поисковый запрос для фото (Pexels — для открыток)
        search_match = re.search(r'🔍\s*Поиск фото[^:]*:\s*\n(.+?)(?:\n\n|\n---)', block, re.DOTALL)
        if not search_match:
            search_match = re.search(r'🔍\s*Поиск фото[^:]*:\s*\n(.+)', block)
        photo_query = search_match.group(1).strip() if search_match else ""

        # Имя файла картинки
        img_match = re.search(r'Картинка:\s*(\S+\.png)', block)
        img_filename = img_match.group(1) if img_match else f"post{post_num}.png"

        # Надпись на открытке (для постов 1 и 5)
        card_text = ""
        card_match = re.search(r'✉️\s*Надпись на открытке:\s*\n(.*?)(?:\n\n📸|\n📸)', block, re.DOTALL)
        if card_match:
            card_text = card_match.group(1).strip()

        # Текст поста (от "---\n\n" до "✉️ Надпись" или "📸 Картинка:")
        if card_text:
            # Открытка: текст до надписи
            text_match = re.search(r'---\s*\n\n(.*?)(?:\n\n✉️|\n✉️)', block, re.DOTALL)
        else:
            # Обычный пост: текст до картинки
            text_match = re.search(r'---\s*\n\n(.*?)(?:\n\n📸\s*Картинка:|\n📸\s*Картинка:)', block, re.DOTALL)
            if not text_match:
                text_match = re.search(r'---\s*\n(.*?)(?:\n📸)', block, re.DOTALL)
        post_text = text_match.group(1).strip() if text_match else ""

        posts.append({
            "num": post_num,
            "topic": topic,
            "text": post_text,
            "img_prompt": img_prompt,
            "img_filename": img_filename,
            "img_path": None,
            "card_text": card_text,
            "photo_query": photo_query,  # поиск фото для открыток
        })

    return posts


# ── 4. Публикация в MAX ──────────────────────────────────

def max_api(method, endpoint, **kwargs):
    """Запрос к MAX Bot API."""
    url = f"{MAX_API_BASE}{endpoint}"
    headers = {"Authorization": MAX_BOT_TOKEN}
    if method == "GET":
        resp = requests.get(url, headers=headers, params=kwargs.get("params"), timeout=30)
    elif method == "POST":
        if "files" in kwargs:
            resp = requests.post(url, headers=headers, params=kwargs.get("params"),
                                 files=kwargs["files"], timeout=60)
        elif "json" in kwargs:
            resp = requests.post(url, headers=headers, params=kwargs.get("params"),
                                 json=kwargs["json"], timeout=30)
        else:
            resp = requests.post(url, headers=headers, params=kwargs.get("params"), timeout=30)
    return resp


def max_upload_image(filepath):
    """Загружает картинку в MAX и возвращает токен фото."""
    # Шаг 1: получить URL для загрузки
    resp = max_api("POST", "/uploads", params={"type": "image"})
    if resp.status_code != 200:
        log.error(f"Ошибка получения upload URL: {resp.status_code} {resp.text}")
        return None

    data = resp.json()
    upload_url = data.get("url")
    if not upload_url:
        log.error(f"Нет URL в ответе: {data}")
        return None

    # Шаг 2: загрузить файл
    with open(filepath, "rb") as f:
        upload_resp = requests.post(
            upload_url,
            files={"data": (os.path.basename(filepath), f, "image/png")},
            timeout=60
        )

    if upload_resp.status_code != 200:
        log.error(f"Ошибка загрузки файла: {upload_resp.status_code} {upload_resp.text}")
        return None

    # Ответ: {"photos": {"<id>": {"token": "..."}}}
    result = upload_resp.json()
    photos = result.get("photos", {})
    for photo_id, photo_info in photos.items():
        token = photo_info.get("token")
        if token:
            return token
    log.error(f"Нет токена в ответе: {result}")
    return None


def max_send_post(text, image_path=None):
    """Отправляет пост в канал MAX."""
    body = {"text": text, "format": "markdown"}

    if image_path and os.path.exists(image_path):
        log.info(f"Загружаю картинку: {os.path.basename(image_path)}")
        photo_token = max_upload_image(image_path)
        if photo_token:
            body["attachments"] = [{"type": "image", "payload": {"token": photo_token}}]

    resp = max_api("POST", "/messages", params={"chat_id": MAX_CHAT_ID}, json=body)

    if resp.status_code == 200:
        msg = resp.json().get("message", {})
        url = msg.get("url", "")
        log.info(f"Опубликовано! {url}")
        return True
    else:
        log.error(f"Ошибка публикации: {resp.status_code} {resp.text}")
        return False


def check_max_connection():
    """Проверяет подключение к MAX."""
    try:
        resp = max_api("GET", "/me")
        if resp.status_code == 200:
            bot_info = resp.json()
            log.info(f"MAX бот: {bot_info.get('name', '?')} (@{bot_info.get('username', '?')})")
            return True
        else:
            log.error(f"MAX авторизация: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        log.error(f"MAX недоступен: {e}")
        return False


# ── 5. Основные задачи ───────────────────────────────────

def task_generate(target_date=None):
    """Генерирует контент + картинки на день. Сохраняет в today_posts."""
    global today_posts

    if target_date is None:
        target_date = today_msk()

    date_key = target_date.isoformat()
    date_str = format_date(target_date)

    log.info(f"{'='*50}")
    log.info(f"ГЕНЕРАЦИЯ КОНТЕНТА на {date_str}")
    log.info(f"{'='*50}")

    # Проверяем ключи
    missing = []
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")
    if not WAVESPEED_API_KEY:
        missing.append("WAVESPEED_API_KEY")
    if not MAX_BOT_TOKEN:
        missing.append("MAX_BOT_TOKEN")
    if not MAX_CHAT_ID:
        missing.append("MAX_CHAT_ID")
    if missing:
        log.error(f"Не заданы переменные: {', '.join(missing)}")
        return False

    try:
        # 1. Генерация текста
        content = generate_posts(target_date)
        save_posts_to_file(content)

        # 2. Парсинг
        posts = parse_posts(content)
        log.info(f"Распарсено постов: {len(posts)}")
        for p in posts:
            log.info(f"  {p['num']}. {p['topic'][:60]}")

        if len(posts) < 5:
            log.warning(f"Ожидалось 5 постов, получено {len(posts)}")

        # 3. Сохраняем темы в историю
        topics = [p["topic"] for p in posts if p["topic"]]
        add_topics_to_history(topics)

        # 4. Генерация картинок
        log.info("Генерация картинок...")
        for i, post in enumerate(posts):
            filepath = None

            if post.get("photo_query"):
                # Открытка — берём фото с Pexels
                filepath = fetch_pexels_photo(post["photo_query"], post["img_filename"])
                if not filepath and post["img_prompt"]:
                    # Fallback на WaveSpeed если Pexels не сработал
                    log.info("Pexels не сработал, генерирую через WaveSpeed...")
                    filepath = generate_image(post["img_prompt"], post["img_filename"])
            elif post["img_prompt"]:
                # Обычный пост — генерация через WaveSpeed
                filepath = generate_image(post["img_prompt"], post["img_filename"])

            post["img_path"] = filepath

            # Для открыток — накладываем надпись
            if filepath and post.get("card_text"):
                overlay_text_on_image(filepath, post["card_text"])

            if i < len(posts) - 1:
                time.sleep(1)

        # 5. Сохраняем в память
        today_posts[date_key] = posts
        log.info(f"Контент на {date_str} готов! ({len(posts)} постов)")
        return True

    except Exception as e:
        log.error(f"Ошибка генерации: {e}", exc_info=True)
        return False


def task_publish_post(post_index, target_date=None):
    """Публикует конкретный пост (0-4) в MAX."""
    if target_date is None:
        target_date = today_msk()

    date_key = target_date.isoformat()
    date_str = format_date(target_date)

    posts = today_posts.get(date_key)
    if not posts:
        log.warning(f"Нет контента на {date_str}, генерирую...")
        if not task_generate(target_date):
            return False
        posts = today_posts.get(date_key)

    if not posts or post_index >= len(posts):
        log.error(f"Пост {post_index + 1} не найден (всего {len(posts) if posts else 0})")
        return False

    post = posts[post_index]
    post_names = ["Утренняя открытка", "Лайфхак/Здоровье", "Ностальгия", "Рецепт", "Вечерняя открытка"]
    name = post_names[post_index] if post_index < len(post_names) else f"Пост {post_index + 1}"

    log.info(f"{'='*50}")
    log.info(f"ПУБЛИКАЦИЯ: {name} — {post['topic'][:50]}")
    log.info(f"{'='*50}")

    if not post["text"]:
        log.error("Пустой текст поста!")
        return False

    return max_send_post(post["text"], post.get("img_path"))


def task_now(target_date=None):
    """Генерирует и публикует все 5 постов сразу."""
    if target_date is None:
        target_date = today_msk()

    if not task_generate(target_date):
        return False

    if not check_max_connection():
        return False

    date_key = target_date.isoformat()
    posts = today_posts.get(date_key, [])

    log.info(f"Публикую {len(posts)} постов...")
    for i in range(len(posts)):
        task_publish_post(i, target_date)
        if i < len(posts) - 1:
            log.info("Пауза 10 сек...")
            time.sleep(10)

    log.info("Все посты опубликованы!")
    return True


# ── 6. Планировщик ───────────────────────────────────────

def run_scheduler():
    """Запускает бота в режиме планировщика (для сервера/Railway)."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ_NAME)
    except ImportError:
        import pytz
        tz = pytz.timezone(TZ_NAME)

    scheduler = BlockingScheduler(timezone=tz)

    # Парсим время генерации
    gen_h, gen_m = map(int, GENERATE_TIME.split(":"))

    # Задача генерации — каждый день
    scheduler.add_job(
        task_generate,
        CronTrigger(hour=gen_h, minute=gen_m, timezone=tz),
        id="generate",
        name=f"Генерация контента ({GENERATE_TIME} MSK)",
        misfire_grace_time=3600,
    )

    # Задачи публикации — 5 постов в день
    post_names = ["Утренняя открытка", "Лайфхак/Здоровье", "Ностальгия", "Рецепт", "Вечерняя открытка"]
    for i, time_str in enumerate(POST_TIMES[:5]):
        h, m = map(int, time_str.strip().split(":"))
        scheduler.add_job(
            task_publish_post,
            CronTrigger(hour=h, minute=m, timezone=tz),
            args=[i],
            id=f"publish_{i+1}",
            name=f"Пост {i+1}: {post_names[i]} ({time_str} MSK)",
            misfire_grace_time=3600,
        )

    # Проверяем подключение
    log.info("=" * 60)
    log.info("  СЕРЕБРЯНЫЙ ВОЗРАСТ — Бот автопостинга")
    log.info("=" * 60)
    log.info(f"  Часовой пояс: {TZ_NAME}")
    log.info(f"  Генерация: {GENERATE_TIME}")
    log.info(f"  Публикация: {', '.join(POST_TIMES[:5])}")
    log.info(f"  Модель: {GROQ_MODEL}")
    log.info(f"  Картинки: {WAVESPEED_MODEL}")
    log.info("=" * 60)

    if check_max_connection():
        log.info("MAX подключение ОК")
    else:
        log.warning("MAX не подключен! Проверьте MAX_BOT_TOKEN и MAX_CHAT_ID")

    # Если сегодня ещё не генерировали — проверяем, не пропущена ли генерация
    current = now_msk()
    gen_time_today = current.replace(hour=gen_h, minute=gen_m, second=0, microsecond=0)
    date_key = today_msk().isoformat()

    if date_key not in today_posts and current > gen_time_today:
        log.info("Генерация на сегодня ещё не выполнена — запускаю...")
        task_generate()

        # Публикуем пропущенные посты
        for i, time_str in enumerate(POST_TIMES[:5]):
            h, m = map(int, time_str.strip().split(":"))
            post_time = current.replace(hour=h, minute=m, second=0, microsecond=0)
            if current > post_time:
                log.info(f"Пропущенный пост {i+1} ({time_str}) — публикую...")
                task_publish_post(i)
                time.sleep(5)

    log.info("Планировщик запущен. Ожидаю расписание...")

    # Graceful shutdown
    def shutdown(signum, frame):
        log.info("Получен сигнал остановки, завершаю...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    scheduler.start()


# ── 7. CLI ───────────────────────────────────────────────

def main():
    # Парсим дату
    target_date = None
    for i, arg in enumerate(sys.argv):
        if arg == "--date" and i + 1 < len(sys.argv):
            target_date = datetime.date.fromisoformat(sys.argv[i + 1])
    if target_date is None:
        target_date = today_msk()

    if "--now" in sys.argv:
        # Генерация + публикация всех постов сразу
        task_now(target_date)

    elif "--generate-only" in sys.argv:
        # Только генерация
        task_generate(target_date)

    elif "--post" in sys.argv:
        # Публикация конкретного поста
        idx = sys.argv.index("--post")
        if idx + 1 < len(sys.argv):
            post_num = int(sys.argv[idx + 1])
            task_generate(target_date)  # генерируем, если нет
            task_publish_post(post_num - 1, target_date)
        else:
            print("Укажите номер поста: --post 1")

    else:
        # Режим демона (по умолчанию)
        run_scheduler()


if __name__ == "__main__":
    main()
