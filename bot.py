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
    # Последние 100 тем
    return "\n".join(history.get("topics", [])[-100:])


def add_topics_to_history(topics_list):
    history = load_history()
    history["topics"].extend(topics_list)
    # Храним максимум 200 тем
    history["topics"] = history["topics"][-200:]
    save_history(history)


# ── 1. Генерация текста через Groq ───────────────────────

def generate_posts(target_date):
    """Генерирует 5 постов через Groq API."""
    date_str = format_date(target_date)
    previous_topics = get_previous_topics()

    previous_block = ""
    if previous_topics:
        previous_block = f"""

⛔ ЗАПРЕЩЁННЫЕ ТЕМЫ (уже были, НЕ повторяй!):
{previous_topics}
Придумай СОВЕРШЕННО ДРУГИЕ темы!
"""

    system_prompt = """Ты — талантливый копирайтер и главный редактор канала «Серебряный возраст» для аудитории 50+.

ТВОЙ СТИЛЬ:
- Пишешь тёплые, душевные, РАЗВЁРНУТЫЕ посты. Не короткие отписки, а полноценные тексты.
- Каждый пост — как письмо от доброй мудрой подруги, которая знает жизнь.
- Используешь конкретные детали, образы, запахи, звуки — чтобы читатель УВИДЕЛ и ПОЧУВСТВОВАЛ.
- Знаешь русские/советские праздники, традиции, приметы, рецепты.
- Умеешь вызвать ностальгию и тёплые чувства.

ВАЖНЫЕ ПРАВИЛА:
- Посты должны быть ДЛИННЫМИ и СОДЕРЖАТЕЛЬНЫМИ (каждый минимум 150-200 слов).
- Рецепт должен быть ПОЛНЫМ с точными ингредиентами и пошаговой инструкцией.
- Лайфхак/здоровье — конкретный, практический, с пошаговыми действиями.
- Ностальгия — с живыми деталями, конкретными названиями, ценами, вещами из того времени.
- Промпты для картинок — ДЕТАЛЬНЫЕ, на английском, с описанием стиля, цветов, настроения.

ПРИМЕР ХОРОШЕГО ПОСТА (ностальгия):
---
📖 А помните?..

Помните, как по субботам ходили в кино? Это был целый ритуал! Сначала — очередь у кассы, где все разглядывали афиши и спорили, на какой сеанс идти. Потом — заветный билетик с номером ряда и места.

В фойе пахло попкорном (хотя тогда чаще — пирожками и лимонадом). Гас свет, и зал замирал. На экране мелькали титры «Мосфильма» или «Ленфильма», и начиналось волшебство.

А в антракте — стаканчик мороженого! Пломбир за 15 копеек в хрустящем вафельном стаканчике. Ели быстро, пока не растаял, и бежали обратно в зал.

Какие фильмы запомнились вам больше всего? 😊

💌 Вспомнили что-то тёплое? Перешлите друзьям — вместе вспоминать веселее!

Ваш уютный уголок: «Серебряный возраст» https://max.ru/id540697837513_biz
---

ПРИМЕР ХОРОШЕГО ПОСТА (лайфхак):
---
💚 Как вернуть белизну тюлю без кипячения и дорогой химии?

Простой совет от опытных хозяек! 🏡 ✨

Со временем любимые занавески желтеют от солнца или сереют от пыли. Но есть старый, проверенный способ.

Всё, что вам понадобится — это обычная пищевая сода и соль.

1. ✅ Замочите тюль в тёплой воде (около 30-40 градусов).
2. ✅ Добавьте 5 столовых ложек соли и 2 столовые ложки соды.
3. ✅ Оставьте на 1-2 часа. Соль «вытянет» серость, а сода сработает как мягкий отбеливатель.
4. ✅ Прополощите и постирайте на деликатном режиме.

Хорошим советом грех не поделиться! Перешлите эту хитрость подругам-хозяйкам 👇 ✈️ 💌

Ваши полезные советы: Серебряный возраст 🫂 https://max.ru/id540697837513_biz
---

Пиши ИМЕННО В ТАКОМ развёрнутом стиле! Не сокращай!"""

    date_tag = target_date.strftime('%d%b').lower()
    user_prompt = f"""Сегодня {date_str}.

Сгенерируй контент на сегодня — ровно 5 РАЗВЁРНУТЫХ постов по рубрикам:

1. УТРЕННЯЯ ОТКРЫТКА — доброе утро, пожелание на день. Упомяни дату, день недели, что-то про сезон/погоду. Если есть праздник — поздравь.
2. ЛАЙФХАК / ЗДОРОВЬЕ — конкретный полезный совет с пошаговой инструкцией (минимум 4-5 шагов). Домашние хитрости, здоровье, огород, быт.
3. НОСТАЛЬГИЯ — живые воспоминания о прошлом с деталями (названия, цены, вещи, звуки, запахи). СССР, 60-80-е.
4. РЕЦЕПТ — простое домашнее блюдо. ПОЛНЫЙ рецепт: все ингредиенты с точными количествами + пошаговое приготовление.
5. ВЕЧЕРНЯЯ ОТКРЫТКА — пожелание спокойной ночи, тёплые слова на вечер.
{previous_block}
ОБЯЗАТЕЛЬНО:
- Учитывай ближайшие праздники и события! (например: День смеха 1 апреля, Пасха, День Победы, 8 Марта, масленица и т.д.)
- Каждый пост ОБЯЗАТЕЛЬНО заканчивай:
  1) Призывом переслать пост друзьям/близким
  2) Подписью канала с ССЫЛКОЙ. Формат подписи (примеры, чередуй):
     — Ваш уютный вечер с нами: «Серебряный возраст» https://max.ru/id540697837513_biz
     — Ваши полезные советы: Серебряный возраст 🫂 https://max.ru/id540697837513_biz
     — Ваш уютный уголок: «Серебряный возраст» https://max.ru/id540697837513_biz
     — Рецепты с душой: «Серебряный возраст» https://max.ru/id540697837513_biz
  Ссылка https://max.ru/id540697837513_biz ОБЯЗАТЕЛЬНА в каждом посте!
- Для каждого поста — ДЕТАЛЬНЫЙ промпт на английском для генерации картинки (минимум 30 слов в промпте).

ФОРМАТ (строго!):

===================================================
СЕРЕБРЯНЫЙ ВОЗРАСТ — Контент на {date_str}
===================================================

---
📌 ПОСТ 1 — УТРЕННЯЯ ОТКРЫТКА
Тема: [конкретная тема]
---

[РАЗВЁРНУТЫЙ текст поста, минимум 150 слов]

[призыв переслать]

Ваш уютный канал: «Серебряный возраст» https://max.ru/id540697837513_biz

📸 Картинка: post1_morning_{date_tag}.png

🖼 Промпт для генерации картинки:
[детальный промпт на английском, минимум 30 слов --ar 4:3 --v 6.0]

---
📌 ПОСТ 2 — ЛАЙФХАК / ЗДОРОВЬЕ
Тема: [конкретная тема]
---

[РАЗВЁРНУТЫЙ текст]

[призыв переслать]

Ваши полезные советы: «Серебряный возраст» 🫂 https://max.ru/id540697837513_biz

📸 Картинка: post2_health_{date_tag}.png

🖼 Промпт для генерации картинки:
[детальный промпт --ar 4:3 --v 6.0]

---
📌 ПОСТ 3 — НОСТАЛЬГИЯ
Тема: [конкретная тема]
---

[РАЗВЁРНУТЫЙ текст]

[призыв переслать]

Ваш уютный уголок: «Серебряный возраст» https://max.ru/id540697837513_biz

📸 Картинка: post3_nostalgia_{date_tag}.png

🖼 Промпт для генерации картинки:
[детальный промпт --ar 4:3 --v 6.0]

---
📌 ПОСТ 4 — РЕЦЕПТ
Тема: [конкретная тема]
---

[ПОЛНЫЙ рецепт с ингредиентами и пошаговой инструкцией]

[призыв переслать]

Рецепты с душой: «Серебряный возраст» https://max.ru/id540697837513_biz

📸 Картинка: post4_recipe_{date_tag}.png

🖼 Промпт для генерации картинки:
[детальный промпт --ar 4:3 --v 6.0]

---
📌 ПОСТ 5 — ВЕЧЕРНЯЯ ОТКРЫТКА
Тема: [конкретная тема]
---

[РАЗВЁРНУТЫЙ текст]

[призыв переслать]

Ваш уютный вечер с нами: «Серебряный возраст» 🌙 https://max.ru/id540697837513_biz

📸 Картинка: post5_evening_{date_tag}.png

🖼 Промпт для генерации картинки:
[детальный промпт --ar 4:3 --v 6.0]

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


# ── 2. Генерация картинок (WaveSpeed AI) ─────────────────

def generate_image(prompt_text, filename):
    """Генерирует картинку через WaveSpeed AI (FLUX Schnell)."""
    clean_prompt = re.sub(r'--\w+\s+[\d.:]+', '', prompt_text).strip()
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

        # Промпт для картинки
        prompt_match = re.search(r'Промпт для (?:генерации картинки|generating picture):\s*\n(.+?)(?:\n\n|\n---)', block, re.DOTALL)
        if not prompt_match:
            prompt_match = re.search(r'Промпт для (?:генерации картинки|generating picture):\s*\n(.+)', block)
        img_prompt = prompt_match.group(1).strip() if prompt_match else ""

        # Имя файла картинки
        img_match = re.search(r'Картинка:\s*(\S+\.png)', block)
        img_filename = img_match.group(1) if img_match else f"post{post_num}.png"

        # Текст поста (от "---\n\n" до "📸 Картинка:")
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
    body = {"text": text}

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
        log.info(f"Генерация картинок через WaveSpeed AI...")
        for i, post in enumerate(posts):
            if post["img_prompt"]:
                filepath = generate_image(post["img_prompt"], post["img_filename"])
                post["img_path"] = filepath
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
