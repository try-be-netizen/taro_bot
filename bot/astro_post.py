"""
Астро-пост: юмористическое наблюдение про знак зодиака.

Что делает:
1. Случайно выбирает знак из 12, исключая последние 12 опубликованных
   (чтобы цикл был ровный — каждый знак минимум раз в 12 постов).
2. Через Groq генерирует короткое бытовое наблюдение в духе твиттер-скетча.
3. Постит текстом + кнопка «🔮 Получить расклад», ведущая в WebApp
   в режим меню тем (?startapp=daily).

ENV:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    GROQ_API_KEY
    WEBAPP_URL — direct link на Mini App, например
                 https://t.me/please_taro_bot/please_taro
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
HISTORY_PATH = ROOT / "bot" / "astro_history.json"

TG_API = "https://api.telegram.org/bot{token}/{method}"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
USER_AGENT = "AstroBot/1.0"
GROQ_TIMEOUT = 60
MAX_RETRIES = 3

HISTORY_KEEP = 12  # 12 знаков — не повторяем последние 12 публикаций

ZODIAC = [
    {"id": "aries",       "ru": "Овен",       "emoji": "♈",
     "traits": "импульсивный, прямолинейный, нетерпеливый, лидер, конкурентный"},
    {"id": "taurus",      "ru": "Телец",      "emoji": "♉",
     "traits": "упрямый, обстоятельный, любит уют и еду, медленный на подъём, гедонист"},
    {"id": "gemini",      "ru": "Близнецы",   "emoji": "♊",
     "traits": "болтливый, любопытный, скачет с темы на тему, не доделывает, обожает сплетни"},
    {"id": "cancer",      "ru": "Рак",        "emoji": "♋",
     "traits": "эмоциональный, обидчивый, любит дом и семью, прячется в раковину, переживает за всех"},
    {"id": "leo",         "ru": "Лев",        "emoji": "♌",
     "traits": "пафосный, любит внимание, драматичный, щедрый напоказ, обожает себя"},
    {"id": "virgo",       "ru": "Дева",       "emoji": "♍",
     "traits": "перфекционист, критикует, замечает все детали, чистоплотный до невроза, тревожный"},
    {"id": "libra",       "ru": "Весы",       "emoji": "♎",
     "traits": "нерешительный, ищет гармонию, эстет, не выносит конфликтов, выбирает 40 минут"},
    {"id": "scorpio",     "ru": "Скорпион",   "emoji": "♏",
     "traits": "интенсивный, мстительный, помнит всё, проницательный, не отпускает обиды"},
    {"id": "sagittarius", "ru": "Стрелец",    "emoji": "♐",
     "traits": "оптимист, любит путешествия и свободу, болтает правду в лицо, не выносит рутины"},
    {"id": "capricorn",   "ru": "Козерог",    "emoji": "♑",
     "traits": "трудоголик, серьёзный, держит лицо, расчётливый, играет по правилам"},
    {"id": "aquarius",    "ru": "Водолей",    "emoji": "♒",
     "traits": "эксцентричный, рассеянный, оригинальный, отстранённый, забывает базовые вещи"},
    {"id": "pisces",      "ru": "Рыбы",       "emoji": "♓",
     "traits": "мечтательный, чувствительный, теряется в реальности, эмпат, плачет от рекламы"},
]


def env(name, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        print(f"ERROR: {name} не задана", file=sys.stderr)
        sys.exit(1)
    return v


def msk_now_iso():
    return datetime.now(timezone(timedelta(hours=3))).isoformat(timespec="seconds")


def load_history():
    if not HISTORY_PATH.exists():
        return {"published_ids": [], "last_run": None}
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"published_ids": [], "last_run": None}


def save_history(history):
    history["published_ids"] = history.get("published_ids", [])[-HISTORY_KEEP:]
    HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def html_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pick_sign(history):
    """Случайный знак с исключением последних HISTORY_KEEP опубликованных."""
    excluded = set(history.get("published_ids", []))
    available = [s for s in ZODIAC if s["id"] not in excluded]
    if not available:
        # все 12 уже опубликованы недавно — значит цикл прошёл, начинаем заново
        available = ZODIAC
    return random.choice(available)


def build_prompt(sign):
    """Промпт под бытовой юмор-наблюдение."""
    system = (
        "Ты пишешь короткие юмористические наблюдения про знаки зодиака для "
        "Telegram-канала. Стиль: твиттер-скетч / стенд-ап. Конкретные бытовые "
        "ситуации (магазин, офис, метро, доставка, чаты, очередь, кафе, дом). "
        "Никаких эзотерических штампов, никакого «вы рождены под звездой», "
        "никаких приветствий. Не используешь слова «гороскоп», «астрология», "
        "«зодиак». Прямо, наблюдательно, с тёплым юмором — НЕ обидно. "
        "Никаких эмодзи в тексте. Без markdown. На русском. "
        "Длина: 2-4 предложения, всего 200-350 знаков.\n\n"
        "ВАЖНО про оформление списков и перечислений: если в тексте есть "
        "пронумерованные пункты или перечисление шагов — каждый пункт с НОВОЙ "
        "строки. Не клей пункты в одно предложение через запятую."
    )
    user = (
        f"Знак: {sign['ru']}. Характерные черты: {sign['traits']}.\n\n"
        "Напиши одно бытовое наблюдение в духе:\n\n"
        "«Если коллега греет рыбу в микроволновке, Козерог про себя составляет "
        "список:\n"
        "1) написать в общий чат\n"
        "2) пометить в HR-тикете\n"
        "3) ничего не делать, потому что он взрослый человек.\n\n"
        "В итоге пишет в чат в 17:58.»\n\n"
        "Это должна быть конкретная сцена из обычной жизни — на работе, в магазине, "
        "в чате, в транспорте, в кафе, дома. Точное попадание в характер знака. "
        "Без морали, без «и в этом весь Козерог». Просто сценка, заканчивающаяся "
        "на смешной детали.\n\n"
        "Если есть нумерованный список — каждый пункт с НОВОЙ строки, как в примере выше. "
        "Не упоминай знак внутри текста — он будет в заголовке. Не используй markdown."
    )
    return system, user


def call_groq(api_key, system, user):
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.95,  # выше для разнообразия и юмора
        "max_tokens": 350,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=payload,
                              timeout=GROQ_TIMEOUT)
            if r.status_code != 200:
                last_err = f"Groq {r.status_code}: {r.text[:200]}"
                print(f"  попытка {attempt}: {last_err}", file=sys.stderr)
                continue
            return r.json()["choices"][0]["message"]["content"].strip()
        except (requests.RequestException, KeyError) as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"  попытка {attempt}: {last_err}", file=sys.stderr)
    raise RuntimeError(f"Groq не ответил после {MAX_RETRIES} попыток: {last_err}")


def split_list_items(text):
    """Если в тексте есть пронумерованный список вида «1) ... 2) ... 3) ...»
    или «1. ... 2. ...», разбивает каждый пункт на свою строку.

    Срабатывает только если в тексте >=2 пунктов подряд — иначе текст не трогаем.
    """
    import re

    # Паттерн пункта: цифра + ) или . + пробел
    pattern = re.compile(r"\s+(\d+[.)]\s)")

    # Считаем сколько пунктов в тексте — должно быть хотя бы 2 чтобы это
    # действительно был список, а не случайное «работает 24/7»
    matches = pattern.findall(text)
    if len(matches) < 2:
        return text

    # Перед каждым пунктом ставим перевод строки (но не дублируем если уже есть)
    result = pattern.sub(r"\n\1", text)
    # Убираем тройные переводы строк если случайно образовались
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def build_caption(sign, observation):
    # Сначала разбиваем списки на строки, потом экранируем
    formatted = split_list_items(observation)
    obs = html_escape(formatted)
    return (
        f"{sign['emoji']} <b>{html_escape(sign['ru'])}</b>\n\n"
        f"{obs}"
    )


def build_keyboard(webapp_url):
    """Кнопка для поста — открывает WebApp в режиме меню тем (daily).

    Telegram распознаёт ссылки t.me/<bot>/<app>?startapp=<param> и
    открывает WebApp с этим параметром. WebApp читает start_param
    и переключается в нужный режим.
    """
    separator = "&" if "?" in webapp_url else "?"
    daily_url = f"{webapp_url}{separator}startapp=daily"
    return {
        "inline_keyboard": [
            [{"text": "🔮 Получить расклад", "url": daily_url}]
        ]
    }


def send_message(token, chat_id, text, reply_markup=None):
    url = TG_API.format(token=token, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    r = requests.post(url, data=payload, timeout=30,
                      headers={"User-Agent": USER_AGENT})
    if r.status_code != 200:
        print(f"Telegram {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()
    return r.json()


def main():
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    api_key = env("GROQ_API_KEY")
    webapp_url = env("WEBAPP_URL")

    history = load_history()
    sign = pick_sign(history)
    print(f"Знак: {sign['ru']} ({sign['id']})", file=sys.stderr)

    system, user = build_prompt(sign)
    try:
        observation = call_groq(api_key, system, user)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  Сгенерировано {len(observation)} символов", file=sys.stderr)

    caption = build_caption(sign, observation)
    keyboard = build_keyboard(webapp_url)
    send_message(token, chat_id, caption, reply_markup=keyboard)

    history.setdefault("published_ids", []).append(sign["id"])
    history["last_run"] = msk_now_iso()
    save_history(history)
    print(f"✓ Опубликовано: {sign['ru']}", file=sys.stderr)


if __name__ == "__main__":
    main()