#!/usr/bin/env python3
"""
Мультиканальный бот автопостинга в MAX мессенджер.

Каналы настраиваются в channels.py. Каждый канал — своя тематика,
расписание, рубрики, chat_id.

CLI:
  python3 bot.py                              # демон (все каналы)
  python3 bot.py --now                        # все каналы сразу
  python3 bot.py --now --channel auto         # один канал
  python3 bot.py --generate-only              # только генерация
  python3 bot.py --channel auto --post 3      # один пост
  python3 bot.py --date 2026-04-01            # на дату

Переменные окружения:
  GROQ_API_KEY, MAX_BOT_TOKEN, WAVESPEED_API_KEY — обязательные
  MAX_CHAT_ID — ID канала «Серебряный возраст»
  MAX_CHAT_ID_AUTO — ID канала «Автоканал»
  PEXELS_API_KEY — для фото открыток (опционально)
"""

import os
import sys
import re
import time
import json
import signal
import logging
import datetime
import requests

from channels import CHANNELS

# ── Логирование ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

# ── Настройки ────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "moonshotai/kimi-k2-instruct-0905")

MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN", "")
MAX_API_BASE = "https://platform-api.max.ru"

WAVESPEED_API_KEY = os.environ.get("WAVESPEED_API_KEY", "")
WAVESPEED_MODEL = os.environ.get("WAVESPEED_MODEL", "wavespeed-ai/flux-schnell")
WAVESPEED_API_BASE = "https://api.wavespeed.ai/api/v3"

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

TZ_NAME = os.environ.get("TZ", "Europe/Moscow")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ── Хранилище постов в памяти ────────────────────────────
# Ключ: "{channel_id}_{date}"
today_posts = {}

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
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(TZ_NAME))
    except ImportError:
        return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))


def today_msk():
    return now_msk().date()


def get_chat_id(channel):
    """Получает chat_id канала из env или дефолта."""
    return os.environ.get(channel["chat_id_env"], channel.get("chat_id_default", ""))


def get_channel_dir(channel_id):
    """Директория данных канала."""
    d = os.path.join(DATA_DIR, channel_id)
    os.makedirs(d, exist_ok=True)
    return d


def get_images_dir(channel_id):
    d = os.path.join(get_channel_dir(channel_id), "images")
    os.makedirs(d, exist_ok=True)
    return d


# ── История тем ──────────────────────────────────────────

def load_history(channel_id):
    path = os.path.join(get_channel_dir(channel_id), "history.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"topics": []}


def save_history(channel_id, history):
    path = os.path.join(get_channel_dir(channel_id), "history.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_previous_topics(channel_id):
    history = load_history(channel_id)
    topics = history.get("topics", [])[-15:]
    return ", ".join(topics)


def add_topics_to_history(channel_id, topics_list):
    history = load_history(channel_id)
    short = [" ".join(t.split()[:5]) for t in topics_list]
    history["topics"].extend(short)
    history["topics"] = history["topics"][-100:]
    save_history(channel_id, history)


# ── 1. Генерация текста ──────────────────────────────────

def generate_posts(target_date, channel_id):
    """Генерирует посты через Groq API для конкретного канала."""
    channel = CHANNELS[channel_id]
    date_str = format_date(target_date)
    date_tag = target_date.strftime('%d%b').lower()

    previous_topics = get_previous_topics(channel_id)
    previous_block = f"\nНЕ повторяй темы: {previous_topics}\n" if previous_topics else ""

    link = channel["link"]
    channel_name = channel["name"]

    user_prompt = channel["user_prompt_template"].format(
        date_str=date_str,
        date_tag=date_tag,
        previous_block=previous_block,
        link=link,
        channel_name=channel_name,
        date_str_upper=date_str.upper(),
    )

    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY)
    log.info(f"[{channel_name}] Генерирую контент на {date_str}...")

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": channel["system_prompt"]},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.9,
        max_tokens=8000,
    )

    content = response.choices[0].message.content
    log.info(f"[{channel_name}] Контент получен ({len(content)} символов)")
    return content


def save_posts_to_file(channel_id, content):
    path = os.path.join(get_channel_dir(channel_id), "posts.txt")
    separator = "\n\n" if os.path.exists(path) and os.path.getsize(path) > 0 else ""
    with open(path, "a", encoding="utf-8") as f:
        f.write(separator + content + "\n")


# ── 2. Картинки ──────────────────────────────────────────

def fetch_pexels_photo(query, filepath):
    """Скачивает фото с Pexels."""
    log.info(f"Pexels: '{query}'")
    if not PEXELS_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "per_page": 15, "orientation": "landscape", "size": "medium"},
            timeout=15,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if not photos:
            return None
        import random
        photo = random.choice(photos)
        img_resp = requests.get(photo["src"]["large"], timeout=30)
        img_resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(img_resp.content)
        log.info(f"Фото: {os.path.basename(filepath)} ({os.path.getsize(filepath)//1024} KB)")
        return filepath
    except Exception as e:
        log.error(f"Pexels ошибка: {str(e)[:150]}")
        return None


def generate_image(prompt_text, filepath):
    """Генерирует картинку через WaveSpeed AI."""
    clean_prompt = re.sub(r'--\w+\s+[\d.:]+', '', prompt_text).strip()
    if "no people" not in clean_prompt.lower():
        clean_prompt += ", no people in foreground, vibrant warm colors"

    log.info(f"WaveSpeed: {os.path.basename(filepath)}")
    if not WAVESPEED_API_KEY:
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
                "prompt": clean_prompt, "size": "1024*768",
                "num_images": 1, "seed": -1,
                "output_format": "jpeg", "enable_sync_mode": True,
            },
            timeout=120,
        )
        resp.raise_for_status()
        task = resp.json()
        status = task.get("data", {}).get("status", "")

        if status == "completed":
            outputs = task["data"].get("outputs", [])
            if outputs:
                img_resp = requests.get(outputs[0], timeout=60)
                img_resp.raise_for_status()
                with open(filepath, "wb") as f:
                    f.write(img_resp.content)
                log.info(f"Картинка: {os.path.basename(filepath)} ({os.path.getsize(filepath)//1024} KB)")
                return filepath

        # Polling fallback
        task_id = task.get("data", {}).get("id")
        if not task_id:
            return None
        for _ in range(30):
            time.sleep(2)
            r = requests.get(f"{WAVESPEED_API_BASE}/predictions/{task_id}/result", headers=headers, timeout=30)
            result = r.json()
            st = result.get("data", {}).get("status", "")
            if st == "completed":
                outputs = result["data"].get("outputs", [])
                if outputs:
                    img_resp = requests.get(outputs[0], timeout=60)
                    with open(filepath, "wb") as f:
                        f.write(img_resp.content)
                    return filepath
            elif st == "failed":
                return None
        return None
    except Exception as e:
        log.error(f"WaveSpeed ошибка: {str(e)[:150]}")
        return None


def overlay_text_on_image(filepath, text):
    """Накладывает текст пожелания на картинку-открытку."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import unicodedata

        img = Image.open(filepath).convert("RGBA")
        w, h = img.size

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        font_dir = os.path.join(BASE_DIR, "fonts")
        font_path = os.path.join(font_dir, "GeorgiaBold.ttf")
        if not os.path.exists(font_path):
            for fp in ["/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
                        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"]:
                if os.path.exists(fp):
                    font_path = fp
                    break
            else:
                font_path = None

        font_size = int(h * 0.065)
        font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()

        # Убираем эмодзи
        clean_text = "".join(
            c for c in text
            if unicodedata.category(c) not in ("So", "Sk", "Cn") or c in ("«", "»", "—", "–", "…", "✨")
        )
        clean_text = re.sub(r'[\ufe0f\ufe0e\u200d\u200b]', '', clean_text)
        lines = [line.strip() for line in clean_text.split("\n") if line.strip()]

        line_spacing = int(font_size * 0.4)
        total_height = len(lines) * font_size + (len(lines) - 1) * line_spacing
        max_line_width = 0
        line_bboxes = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            lw = bbox[2] - bbox[0]
            line_bboxes.append(lw)
            max_line_width = max(max_line_width, lw)

        pad_x, pad_y = 40, 25
        block_x = (w - max_line_width) // 2
        block_y = (h - total_height) // 2 + int(h * 0.05)

        draw.rounded_rectangle(
            [block_x - pad_x, block_y - pad_y,
             block_x + max_line_width + pad_x, block_y + total_height + pad_y],
            radius=20, fill=(255, 255, 255, 160),
        )

        y = block_y
        for i, line in enumerate(lines):
            x = (w - line_bboxes[i]) // 2
            draw.text((x + 2, y + 2), line, font=font, fill=(80, 60, 40, 120))
            draw.text((x, y), line, font=font, fill=(60, 30, 10, 255))
            y += font_size + line_spacing

        result = Image.alpha_composite(img, overlay).convert("RGB")
        result.save(filepath, "JPEG", quality=95)
        log.info(f"Надпись наложена: {os.path.basename(filepath)}")
        return filepath
    except Exception as e:
        log.error(f"Overlay ошибка: {e}")
        return filepath


# ── 3. Парсинг ───────────────────────────────────────────

def parse_posts(content):
    """Парсит сгенерированный контент в список постов."""
    posts = []
    blocks = re.split(r'---\s*\n📌\s*ПОСТ\s+(\d+)', content)

    for i in range(1, len(blocks), 2):
        post_num = int(blocks[i])
        block = blocks[i + 1] if i + 1 < len(blocks) else ""

        topic_match = re.search(r'Тема:\s*(.+)', block)
        topic = topic_match.group(1).strip() if topic_match else f"Пост {post_num}"

        # AI-промпт
        prompt_match = re.search(r'Промпт для (?:картинки|генерации картинки|generating picture)[^:]*:\s*\n(.+?)(?:\n\n|\n---)', block, re.DOTALL)
        if not prompt_match:
            prompt_match = re.search(r'Промпт для (?:картинки|генерации картинки|generating picture)[^:]*:\s*\n(.+)', block)
        img_prompt = prompt_match.group(1).strip() if prompt_match else ""

        # Поиск фото
        search_match = re.search(r'🔍\s*Поиск фото[^:]*:\s*\n(.+?)(?:\n\n|\n---)', block, re.DOTALL)
        if not search_match:
            search_match = re.search(r'🔍\s*Поиск фото[^:]*:\s*\n(.+)', block)
        photo_query = search_match.group(1).strip() if search_match else ""

        img_match = re.search(r'Картинка:\s*(\S+\.png)', block)
        img_filename = img_match.group(1) if img_match else f"post{post_num}.png"

        # Надпись на открытке
        card_text = ""
        card_match = re.search(r'✉️\s*Надпись на открытке:\s*\n(.*?)(?:\n\n📸|\n📸)', block, re.DOTALL)
        if card_match:
            card_text = card_match.group(1).strip()

        # Текст поста
        if card_text:
            text_match = re.search(r'---\s*\n\n(.*?)(?:\n\n✉️|\n✉️)', block, re.DOTALL)
        else:
            text_match = re.search(r'---\s*\n\n(.*?)(?:\n\n📸\s*Картинка:|\n📸\s*Картинка:)', block, re.DOTALL)
            if not text_match:
                text_match = re.search(r'---\s*\n(.*?)(?:\n📸)', block, re.DOTALL)
        post_text = text_match.group(1).strip() if text_match else ""

        posts.append({
            "num": post_num, "topic": topic, "text": post_text,
            "img_prompt": img_prompt, "img_filename": img_filename,
            "img_path": None, "card_text": card_text, "photo_query": photo_query,
        })

    return posts


# ── 4. MAX API ───────────────────────────────────────────

def max_api(method, endpoint, **kwargs):
    url = f"{MAX_API_BASE}{endpoint}"
    headers = {"Authorization": MAX_BOT_TOKEN}
    if method == "GET":
        return requests.get(url, headers=headers, params=kwargs.get("params"), timeout=30)
    elif method == "POST":
        if "files" in kwargs:
            return requests.post(url, headers=headers, params=kwargs.get("params"), files=kwargs["files"], timeout=60)
        elif "json" in kwargs:
            return requests.post(url, headers=headers, params=kwargs.get("params"), json=kwargs["json"], timeout=30)
        return requests.post(url, headers=headers, params=kwargs.get("params"), timeout=30)


def max_upload_image(filepath):
    resp = max_api("POST", "/uploads", params={"type": "image"})
    if resp.status_code != 200:
        return None
    upload_url = resp.json().get("url")
    if not upload_url:
        return None
    with open(filepath, "rb") as f:
        upload_resp = requests.post(upload_url, files={"data": (os.path.basename(filepath), f, "image/png")}, timeout=60)
    if upload_resp.status_code != 200:
        return None
    photos = upload_resp.json().get("photos", {})
    for photo_id, photo_info in photos.items():
        token = photo_info.get("token")
        if token:
            return token
    return None


def max_send_post(chat_id, text, image_path=None):
    body = {"text": text, "format": "markdown"}
    if image_path and os.path.exists(image_path):
        photo_token = max_upload_image(image_path)
        if photo_token:
            body["attachments"] = [{"type": "image", "payload": {"token": photo_token}}]
    resp = max_api("POST", "/messages", params={"chat_id": chat_id}, json=body)
    if resp.status_code == 200:
        log.info(f"Опубликовано! {resp.json().get('message', {}).get('url', '')}")
        return True
    else:
        log.error(f"Ошибка: {resp.status_code} {resp.text[:200]}")
        return False


def check_max_connection():
    try:
        resp = max_api("GET", "/me")
        if resp.status_code == 200:
            info = resp.json()
            log.info(f"MAX бот: {info.get('name', '?')}")
            return True
    except Exception as e:
        log.error(f"MAX недоступен: {e}")
    return False


# ── 5. Задачи ────────────────────────────────────────────

def task_generate(channel_id, target_date=None):
    """Генерирует контент + картинки для канала."""
    channel = CHANNELS[channel_id]
    if target_date is None:
        target_date = today_msk()

    store_key = f"{channel_id}_{target_date.isoformat()}"
    chat_id = get_chat_id(channel)
    images_dir = get_images_dir(channel_id)

    log.info(f"{'='*50}")
    log.info(f"[{channel['name']}] ГЕНЕРАЦИЯ на {format_date(target_date)}")
    log.info(f"{'='*50}")

    missing = []
    if not GROQ_API_KEY: missing.append("GROQ_API_KEY")
    if not MAX_BOT_TOKEN: missing.append("MAX_BOT_TOKEN")
    if not chat_id: missing.append(channel["chat_id_env"])
    if missing:
        log.error(f"Не заданы: {', '.join(missing)}")
        return False

    try:
        content = generate_posts(target_date, channel_id)
        save_posts_to_file(channel_id, content)

        posts = parse_posts(content)
        log.info(f"Распарсено: {len(posts)} постов")

        topics = [p["topic"] for p in posts if p["topic"]]
        add_topics_to_history(channel_id, topics)

        # Картинки
        log.info("Генерация картинок...")
        for i, post in enumerate(posts):
            filepath = os.path.join(images_dir, post["img_filename"])
            result = None

            if post.get("photo_query"):
                result = fetch_pexels_photo(post["photo_query"], filepath)
                if not result and post["img_prompt"]:
                    result = generate_image(post["img_prompt"], filepath)
            elif post["img_prompt"]:
                result = generate_image(post["img_prompt"], filepath)

            post["img_path"] = result
            if result and post.get("card_text"):
                overlay_text_on_image(result, post["card_text"])
            if i < len(posts) - 1:
                time.sleep(1)

        today_posts[store_key] = posts
        log.info(f"[{channel['name']}] Готово! ({len(posts)} постов)")
        return True

    except Exception as e:
        log.error(f"[{channel['name']}] Ошибка: {e}", exc_info=True)
        return False


def task_publish_post(channel_id, post_index, target_date=None):
    """Публикует один пост канала."""
    channel = CHANNELS[channel_id]
    if target_date is None:
        target_date = today_msk()

    store_key = f"{channel_id}_{target_date.isoformat()}"
    chat_id = get_chat_id(channel)

    posts = today_posts.get(store_key)
    if not posts:
        log.warning(f"[{channel['name']}] Нет контента, генерирую...")
        if not task_generate(channel_id, target_date):
            return False
        posts = today_posts.get(store_key)

    if not posts or post_index >= len(posts):
        log.error(f"Пост {post_index+1} не найден")
        return False

    post = posts[post_index]
    names = channel.get("post_names", [])
    name = names[post_index] if post_index < len(names) else f"Пост {post_index+1}"

    log.info(f"[{channel['name']}] Публикую: {name} — {post['topic'][:50]}")

    if not post["text"]:
        log.error("Пустой текст!")
        return False

    return max_send_post(chat_id, post["text"], post.get("img_path"))


def task_now(channel_id, target_date=None):
    """Генерирует и публикует все посты канала сразу."""
    if target_date is None:
        target_date = today_msk()

    if not task_generate(channel_id, target_date):
        return False

    store_key = f"{channel_id}_{target_date.isoformat()}"
    posts = today_posts.get(store_key, [])

    for i in range(len(posts)):
        task_publish_post(channel_id, i, target_date)
        if i < len(posts) - 1:
            time.sleep(10)
    return True


# ── 6. Планировщик ───────────────────────────────────────

def run_scheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ_NAME)
    except ImportError:
        import pytz
        tz = pytz.timezone(TZ_NAME)

    scheduler = BlockingScheduler(timezone=tz)

    log.info("=" * 60)
    log.info("  МУЛЬТИКАНАЛЬНЫЙ БОТ АВТОПОСТИНГА")
    log.info("=" * 60)

    for ch_id, ch in CHANNELS.items():
        chat_id = get_chat_id(ch)
        if not chat_id:
            log.warning(f"  [{ch['name']}] ПРОПУЩЕН — нет {ch['chat_id_env']}")
            continue

        gen_h, gen_m = map(int, ch["generate_time"].split(":"))

        # Генерация
        scheduler.add_job(
            task_generate, CronTrigger(hour=gen_h, minute=gen_m, timezone=tz),
            args=[ch_id], id=f"{ch_id}_gen",
            name=f"[{ch['name']}] Генерация ({ch['generate_time']})",
            misfire_grace_time=3600,
        )

        # Публикация
        for i, time_str in enumerate(ch["post_times"][:5]):
            h, m = map(int, time_str.strip().split(":"))
            names = ch.get("post_names", [])
            pname = names[i] if i < len(names) else f"Пост {i+1}"
            scheduler.add_job(
                task_publish_post, CronTrigger(hour=h, minute=m, timezone=tz),
                args=[ch_id, i], id=f"{ch_id}_pub_{i+1}",
                name=f"[{ch['name']}] {pname} ({time_str})",
                misfire_grace_time=3600,
            )

        log.info(f"  [{ch['name']}] генерация {ch['generate_time']}, посты {', '.join(ch['post_times'][:5])}")

    log.info("=" * 60)

    if check_max_connection():
        log.info("MAX подключение ОК")

    # Догоняем пропущенное
    current = now_msk()
    for ch_id, ch in CHANNELS.items():
        chat_id = get_chat_id(ch)
        if not chat_id:
            continue
        gen_h, gen_m = map(int, ch["generate_time"].split(":"))
        gen_time = current.replace(hour=gen_h, minute=gen_m, second=0, microsecond=0)
        store_key = f"{ch_id}_{today_msk().isoformat()}"

        if store_key not in today_posts and current > gen_time:
            log.info(f"[{ch['name']}] Догоняю пропущенную генерацию...")
            task_generate(ch_id)
            for i, time_str in enumerate(ch["post_times"][:5]):
                h, m = map(int, time_str.strip().split(":"))
                post_time = current.replace(hour=h, minute=m, second=0, microsecond=0)
                if current > post_time:
                    task_publish_post(ch_id, i)
                    time.sleep(5)
            # Пауза между каналами (Groq TPM)
            time.sleep(120)

    log.info("Планировщик запущен.")

    def shutdown(signum, frame):
        log.info("Завершаю...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    scheduler.start()


# ── 7. CLI ───────────────────────────────────────────────

def main():
    target_date = None
    channel_filter = None

    for i, arg in enumerate(sys.argv):
        if arg == "--date" and i + 1 < len(sys.argv):
            target_date = datetime.date.fromisoformat(sys.argv[i + 1])
        if arg == "--channel" and i + 1 < len(sys.argv):
            channel_filter = sys.argv[i + 1]

    if target_date is None:
        target_date = today_msk()

    channels_to_run = [channel_filter] if channel_filter else list(CHANNELS.keys())

    if "--now" in sys.argv:
        check_max_connection()
        for ch_id in channels_to_run:
            task_now(ch_id, target_date)
            if ch_id != channels_to_run[-1]:
                log.info("Пауза 120 сек (Groq TPM)...")
                time.sleep(120)

    elif "--generate-only" in sys.argv:
        for ch_id in channels_to_run:
            task_generate(ch_id, target_date)
            if ch_id != channels_to_run[-1]:
                time.sleep(120)

    elif "--post" in sys.argv:
        idx = sys.argv.index("--post")
        if idx + 1 < len(sys.argv):
            post_num = int(sys.argv[idx + 1])
            ch_id = channels_to_run[0]
            task_generate(ch_id, target_date)
            task_publish_post(ch_id, post_num - 1, target_date)

    else:
        run_scheduler()


if __name__ == "__main__":
    main()
