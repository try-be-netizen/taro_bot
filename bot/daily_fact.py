"""
Ежедневный пост: интересный факт о Таро или конкретной карте.

Логика:
1. Случайно выбираем карту из cards.json (с равной вероятностью).
2. Через Groq API генерируем короткий интересный факт о карте,
   её символике, истории создания, культурных отсылках и т.п.
3. Публикуем в канал с картинкой карты + кнопкой WebApp.
4. История публикаций — в bot/history.json (последние 60 дней не повторяем).

ENV:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    WEBAPP_URL
    GROQ_API_KEY
"""
from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
CARDS_PATH = ROOT / "webapp" / "cards.json"
HISTORY_PATH = Path(__file__).parent / "history.json"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
TG_API = "https://api.telegram.org/bot{token}/{method}"

# Не повторяем последние 60 опубликованных карт
HISTORY_KEEP = 60
MAX_RETRIES = 3
MAX_CAPTION = 1024  # лимит Telegram для sendPhoto


def env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        print(f"ERROR: переменная окружения {name} не задана", file=sys.stderr)
        sys.exit(1)
    return value


def msk_now_iso():
    return datetime.now(timezone(timedelta(hours=3))).isoformat(timespec="seconds")


def load_cards():
    with CARDS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_history():
    if not HISTORY_PATH.exists():
        return {"published_ids": [], "last_run": None}
    try:
        with HISTORY_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"published_ids": [], "last_run": None}


def save_history(history):
    history["published_ids"] = history.get("published_ids", [])[-HISTORY_KEEP:]
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def pick_card(cards, history):
    """Случайная карта, которой не было в последних HISTORY_KEEP постах."""
    used = set(history.get("published_ids", []))
    available = [c for c in cards if c["id"] not in used]
    if not available:
        # все опубликованы, начинаем заново
        available = cards
        history["published_ids"] = []
    return random.choice(available)


def call_groq(api_key, system, user):
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.8,
        "max_tokens": 700,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60)
            if r.status_code != 200:
                last_err = f"Groq {r.status_code}: {r.text[:300]}"
                print(f"Попытка {attempt}: {last_err}", file=sys.stderr)
                continue
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
        except (requests.RequestException, KeyError) as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"Попытка {attempt}: {last_err}", file=sys.stderr)

    raise RuntimeError(f"Groq не ответил после {MAX_RETRIES} попыток: {last_err}")


def generate_fact(api_key, card):
    system = (
        "Ты — историк и культуролог, специализирующийся на Таро. "
        "Пишешь короткие, точные, интересные факты о картах Райдера-Уэйта. "
        "Опираешься на проверенные факты: символика на картинке, история колоды, "
        "культурные отсылки в кино/литературе, мифологические корни. "
        "Не выдумываешь и не используешь эзотерические штампы. "
        "Стиль: живой, увлекательный, без пафоса. "
        "Пишешь на русском языке. "
        "Длина: 3-5 предложений, около 350-500 знаков. "
        "Не используешь markdown или эмодзи. "
        "Не упоминаешь, что это «факт» или «знаете ли вы» — сразу к сути."
    )
    user = (
        f"Расскажи интересный факт о карте Таро «{card['name_ru']}» "
        f"({card['name_en']}) из колоды Райдера-Уэйта. "
        f"Это может быть про символику, историю создания, культурные отсылки, "
        f"или связь с мифологией. Только сам факт, без вступлений."
    )
    return call_groq(api_key, system, user)


def html_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_caption(card, fact):
    name = html_escape(card["name_ru"])
    fact_safe = html_escape(fact)

    caption = (
        f"🌙 <b>Факт о Таро</b>\n\n"
        f"<b>{name}</b>\n\n"
        f"{fact_safe}"
    )
    if len(caption) > MAX_CAPTION:
        # Обрезаем факт, не заголовки
        overflow = len(caption) - MAX_CAPTION + 3
        cut = max(0, len(fact_safe) - overflow)
        fact_safe = fact_safe[:cut].rstrip() + "…"
        caption = (
            f"🌙 <b>Факт о Таро</b>\n\n"
            f"<b>{name}</b>\n\n"
            f"{fact_safe}"
        )
    return caption


def build_keyboard(webapp_url):
    return {
        "inline_keyboard": [
            [{"text": "🔮 Получить расклад", "url": webapp_url}]
        ]
    }


def send_photo(token, chat_id, photo_url, caption, reply_markup):
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(reply_markup, ensure_ascii=False),
    }
    url = TG_API.format(token=token, method="sendPhoto")
    try:
        r = requests.post(url, data=payload, timeout=60)
    except requests.RequestException as e:
        print(f"Telegram network error: {e}", file=sys.stderr)
        return False
    if r.status_code != 200:
        print(f"Telegram {r.status_code}: {r.text}", file=sys.stderr)
        return False
    data = r.json()
    if not data.get("ok"):
        print(f"Telegram не ок: {data}", file=sys.stderr)
        return False
    return True


def main():
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    webapp_url = env("WEBAPP_URL")
    api_key = env("GROQ_API_KEY")

    cards = load_cards()
    history = load_history()

    # До 5 попыток: вдруг конкретная карта в Telegram не загрузится
    MAX_SEND = 5
    for attempt in range(1, MAX_SEND + 1):
        card = pick_card(cards, history)

        # Помечаем как попробованную, чтобы при ошибке взять следующую
        history.setdefault("published_ids", []).append(card["id"])

        try:
            fact = generate_fact(api_key, card)
        except RuntimeError as e:
            print(f"Попытка {attempt}: {e}", file=sys.stderr)
            continue

        caption = build_caption(card, fact)
        keyboard = build_keyboard(webapp_url)

        ok = send_photo(token, chat_id, card["image_url"], caption, keyboard)
        if ok:
            history["last_run"] = msk_now_iso()
            save_history(history)
            print(f"Опубликовано: {card['name_ru']}")
            return

        print(f"Попытка {attempt} не удалась для «{card['name_ru']}»", file=sys.stderr)

    save_history(history)
    print("Все попытки провалились", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
