"""
Гороскоп дня: один пост со всеми 12 знаками.

Что делает:
1. Один запрос к Groq, который генерирует 12 предсказаний — по одному на знак.
2. Каждый знак: 1-2 коротких предложения (~80-150 знаков), мягко и тепло,
   без оккультного штампа.
3. Постит в канал общим списком + кнопка «🔮 Получить расклад».

ENV:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    GROQ_API_KEY
    WEBAPP_URL
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent

TG_API = "https://api.telegram.org/bot{token}/{method}"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Основная модель — 70b, у неё лучшая образность. Если упрётся в дневной
# лимит токенов (TPD), фоллбекнемся на 8b — её лимит отдельный.
GROQ_MODEL_PRIMARY = "llama-3.3-70b-versatile"
GROQ_MODEL_FALLBACK = "llama-3.1-8b-instant"

USER_AGENT = "AstroBot/2.0"
GROQ_TIMEOUT = 60
MAX_RETRIES = 3

# Лимит сообщения в Telegram — 4096 знаков. У нас 12 блоков по ~120 знаков
# плюс заголовок и эмодзи — итого ~1800-2000 знаков. С запасом.
MAX_MESSAGE = 4096

ZODIAC = [
    {"id": "aries",       "ru": "Овен",       "emoji": "♈",
     "traits": "импульсивный, прямолинейный, лидер, любит вызовы и конкуренцию"},
    {"id": "taurus",      "ru": "Телец",      "emoji": "♉",
     "traits": "обстоятельный, чувственный, любит уют и стабильность, упрямый"},
    {"id": "gemini",      "ru": "Близнецы",   "emoji": "♊",
     "traits": "любопытный, общительный, подвижный, скачет с темы на тему"},
    {"id": "cancer",      "ru": "Рак",        "emoji": "♋",
     "traits": "эмоциональный, заботливый, домашний, чувствительный"},
    {"id": "leo",         "ru": "Лев",        "emoji": "♌",
     "traits": "яркий, гордый, щедрый, любит внимание и драматичные жесты"},
    {"id": "virgo",       "ru": "Дева",       "emoji": "♍",
     "traits": "перфекционист, аналитик, замечает детали, тревожный"},
    {"id": "libra",       "ru": "Весы",       "emoji": "♎",
     "traits": "ищет гармонию, эстет, нерешительный, дипломат"},
    {"id": "scorpio",     "ru": "Скорпион",   "emoji": "♏",
     "traits": "интенсивный, проницательный, страстный, помнит всё"},
    {"id": "sagittarius", "ru": "Стрелец",    "emoji": "♐",
     "traits": "оптимист, любит свободу и горизонты, болтает правду в лицо"},
    {"id": "capricorn",   "ru": "Козерог",    "emoji": "♑",
     "traits": "трудоголик, серьёзный, амбициозный, играет по правилам"},
    {"id": "aquarius",    "ru": "Водолей",    "emoji": "♒",
     "traits": "оригинальный, отстранённый, идеалист, любит свободу"},
    {"id": "pisces",      "ru": "Рыбы",       "emoji": "♓",
     "traits": "мечтательный, чувствительный, эмпат, теряется в реальности"},
]


def env(name, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        print(f"ERROR: {name} не задана", file=sys.stderr)
        sys.exit(1)
    return v


def msk_now():
    return datetime.now(timezone(timedelta(hours=3)))


# Месяцы для красивого заголовка («15 мая»)
MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]
WEEKDAYS_RU = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье"
]


def format_date_ru(dt):
    """«Среда, 15 мая»"""
    return f"{WEEKDAYS_RU[dt.weekday()]}, {dt.day} {MONTHS_RU[dt.month - 1]}"


def html_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_prompt():
    """Один запрос — 12 текстов в JSON. Стиль: серьёзный астрологический
    наставник (Тамара Глоба), но БЕЗ мистических предсказаний событий.
    Совет на день, выверенным выдержанным тоном."""

    system = (
        "Ты опытный астролог. Пишешь короткие гороскопы на день для "
        "Telegram-канала. Тон: серьёзный, наставнический, выверенный — "
        "как у Тамары Глобы. Обращение на «вы». Без шуток, без иронии. "
        "Только русский язык, без markdown и эмодзи.\n\n"
        "ГЛАВНОЕ ПРАВИЛО (важнее всего остального):\n"
        "ЗАПРЕЩЕНО начинать текст с названия знака. Никаких "
        "«Овен, постарайтесь...», «Телец, будьте...», «Близнецы, "
        "сосредоточьтесь...», «Дорогой Скорпион», «Рыбы, ваш день...». "
        "Имя знака УЖЕ В ЗАГОЛОВКЕ ПОСТА. Текст начинается СРАЗУ с глагола, "
        "наблюдения или совета — без обращения.\n\n"
        "ЗАПРЕЩЕНО ТАКЖЕ:\n"
        "— Имя знака где-либо в тексте (даже в середине)\n"
        "— Слово «день» в первом слове текста («День располагает», "
        "«Сегодняшний день») — найди другой вход\n"
        "— Предсказывать события («ожидает встреча», «придёт известие»)\n"
        "— Слова «звёзды», «энергии», «карма», «вселенная», «вибрации»\n"
        "— Английские слова\n\n"
        "СТРУКТУРА ТЕКСТА:\n"
        "Каждый блок — 100-160 знаков. Один знак = один блок. "
        "12 блоков должны начинаться РАЗНЫМИ способами. Возможные начала:\n"
        "1. Прямой императив: «Сохраните...», «Доверьтесь...», "
        "«Прислушайтесь...», «Воздержитесь...»\n"
        "2. Условие: «Если возникнет соблазн...», «Когда придёт желание...»\n"
        "3. Утверждение-наблюдение: «Решения, принятые в спешке...», "
        "«Разговоры сегодня требуют...»\n"
        "4. Совет с предостережением: «Не торопитесь...», «Не спешите...»\n"
        "5. Указание на сферу: «В работе сегодня...», "
        "«В отношениях стоит...», «В финансовых вопросах...»\n"
        "6. Тема дня: «Удачный момент для...», «Время уместно для...»\n\n"
        "Используй каждое начало максимум 2 раза за все 12 знаков. "
        "Тексты должны попадать в характер знака — упрямого Тельца "
        "не путать с импульсивным Овном."
    )

    zodiac_lines = []
    for z in ZODIAC:
        zodiac_lines.append(f"- {z['id']} ({z['ru']}) — {z['traits']}")

    examples = (
        "ПРАВИЛЬНЫЕ ПРИМЕРЫ (заметь: ни одно слово в начале не повторяется, "
        "и НИГДЕ нет имени знака внутри текста):\n\n"
        "✅ «Сохраните сдержанность в разговорах. Сильное слово сегодня "
        "прозвучит громче, чем вы рассчитываете.»\n\n"
        "✅ «Доверьтесь привычному ритму. Нет нужды менять то, что и без "
        "того устроено разумно.»\n\n"
        "✅ «Сосредоточьтесь на одном деле и доведите до конца. Распыление "
        "сил обернётся усталостью без результата.»\n\n"
        "✅ «В работе уместно вернуться к отложенному вопросу — он "
        "решается легче, чем казалось ранее.»\n\n"
        "✅ «Не торопитесь с категоричными решениями. Особенно там, где "
        "затронуты интересы близких людей.»\n\n"
        "✅ «Обратите внимание на детали в финансовых вопросах. Не каждое "
        "предложение стоит рассматривать всерьёз.»\n\n"
        "✅ «Удачное время для планирования поездки или обучения. Крупных "
        "трат до конца недели лучше избегать.»\n\n"
        "✅ «Если возникнет соблазн ответить резко — выдержите паузу. "
        "Резкость сегодня вернётся к вам втройне.»\n\n"
        "✅ «Прислушайтесь к собственной усталости. Стоит посвятить время "
        "восстановлению, а не новым обязательствам.»\n\n"
        "НЕПРАВИЛЬНЫЕ ПРИМЕРЫ — ТАК НЕ ПИШИ НИКОГДА:\n\n"
        "❌ «Овен, день сегодня призван для действий...» — обращение "
        "по знаку запрещено\n"
        "❌ «Телец, будьте осторожны...» — обращение по знаку запрещено\n"
        "❌ «Дорогой Скорпион...» — обращение по знаку запрещено\n"
        "❌ «День Рака сегодня лучше посвятить...» — имя знака запрещено\n"
        "❌ «Ваш день сегодня лучше посвятить...» — слово «день» в начале"
    )

    user = (
        "Напиши гороскоп на сегодня для 12 знаков. Серьёзный, наставнический "
        "тон. Совет, не предсказание событий. Каждый блок — 100-160 знаков, "
        "разная грамматическая структура.\n\n"
        f"{examples}\n\n"
        "Знаки и их характеры:\n"
        + "\n".join(zodiac_lines) + "\n\n"
        "Перед тем как писать каждый блок, сверься: "
        "1) текст НЕ начинается с имени знака; "
        "2) текст НЕ содержит имя знака; "
        "3) первое слово ≠ «День»; "
        "4) начало отличается от других блоков.\n\n"
        "Верни JSON, без markdown:\n"
        "{\n"
        '  "aries": "...",\n'
        '  "taurus": "...",\n'
        '  "gemini": "...",\n'
        '  "cancer": "...",\n'
        '  "leo": "...",\n'
        '  "virgo": "...",\n'
        '  "libra": "...",\n'
        '  "scorpio": "...",\n'
        '  "sagittarius": "...",\n'
        '  "capricorn": "...",\n'
        '  "aquarius": "...",\n'
        '  "pisces": "..."\n'
        "}"
    )
    return system, user


def call_groq(api_key, system, user, model):
    """Вызывает Groq с указанной моделью.

    Возвращает либо распарсенный JSON ответа, либо None если упёрлись
    в дневной лимит (429). При других ошибках поднимает RuntimeError.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.95,
        "max_tokens": 2500,
        "response_format": {"type": "json_object"},
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
            if r.status_code == 429:
                # Дневной лимит токенов на эту модель — не пытаемся retry
                last_err = f"Groq 429 (rate limit / TPD): {r.text[:300]}"
                print(f"  {model}: упёрлись в лимит токенов",
                      file=sys.stderr)
                return None
            if r.status_code != 200:
                last_err = f"Groq {r.status_code}: {r.text[:200]}"
                print(f"  попытка {attempt} ({model}): {last_err}",
                      file=sys.stderr)
                continue
            content = r.json()["choices"][0]["message"]["content"].strip()
            return json.loads(content)
        except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"  попытка {attempt} ({model}): {last_err}",
                  file=sys.stderr)
    raise RuntimeError(f"Groq не ответил после {MAX_RETRIES} попыток: {last_err}")


def call_groq_with_fallback(api_key, system, user):
    """Пробует основную модель, при 429 — фоллбекается на запасную."""
    print(f"  Пробую {GROQ_MODEL_PRIMARY}...", file=sys.stderr)
    result = call_groq(api_key, system, user, GROQ_MODEL_PRIMARY)
    if result is not None:
        return result, GROQ_MODEL_PRIMARY

    print(f"  Фоллбек на {GROQ_MODEL_FALLBACK}...", file=sys.stderr)
    result = call_groq(api_key, system, user, GROQ_MODEL_FALLBACK)
    if result is not None:
        return result, GROQ_MODEL_FALLBACK

    raise RuntimeError("Обе модели упёрлись в лимит. Подождите до завтра.")



def validate(resp):
    """Проверяет что есть текст для каждого из 12 знаков.
    Если для какого-то знака текста нет / он слишком короткий —
    подставляет запасной общий совет, а не падает с ошибкой.
    Канал важнее идеала: лучше 12 блоков с одним общим, чем тишина."""
    if not isinstance(resp, dict):
        raise ValueError("ответ не объект")

    # Запасные тексты в Глоба-стиле — общие советы, по одному на знак.
    # Используются если модель не вернула или вернула слишком короткий текст.
    FALLBACKS = {
        "aries":       "Доверьтесь решению, которое примете утром — оно "
                       "окажется верным, даже если днём его захочется пересмотреть.",
        "taurus":      "Сохраните привычный ритм. Сегодня нет нужды менять "
                       "то, что и без того устроено разумно.",
        "gemini":      "Сосредоточьтесь на одном деле и доведите до конца. "
                       "Распыление сил обернётся усталостью без результата.",
        "cancer":      "Прислушайтесь к близким. Сегодня не время "
                       "отстраняться — общение принесёт спокойствие.",
        "leo":         "Сохраните сдержанность в проявлениях. Тёплое "
                       "слово сегодня действует сильнее громкого.",
        "virgo":       "Не спешите указывать на чужие недочёты. "
                       "Доверьтесь людям — каждый разберётся сам.",
        "libra":       "Решение, которое долго откладывали, лучше принять "
                       "сегодня. Оно проще, чем казалось.",
        "scorpio":     "Воздержитесь от резких оценок. Сильное слово "
                       "сегодня прозвучит громче, чем вы рассчитываете.",
        "sagittarius": "Удачное время для планирования и обучения. "
                       "Крупных трат до конца недели лучше избегать.",
        "capricorn":   "В работе уместно вернуться к отложенному вопросу. "
                       "Он решится легче, чем казалось ранее.",
        "aquarius":    "Не пренебрегайте обычными делами. Сегодня они "
                       "принесут больше удовлетворения, чем кажется.",
        "pisces":      "Прислушайтесь к собственной усталости. Стоит "
                       "посвятить день восстановлению, а не новым обязательствам.",
    }

    fixed = 0
    for z in ZODIAC:
        z_id = z["id"]
        text = resp.get(z_id)
        if not isinstance(text, str) or len(text.strip()) < 30:
            # Подставляем запасной текст, не падаем
            resp[z_id] = FALLBACKS[z_id]
            fixed += 1
            print(f"  ⚠️ для {z_id} подставлен запасной текст",
                  file=sys.stderr)

    if fixed > 0:
        print(f"  ⚠️ всего восстановлено: {fixed}/12", file=sys.stderr)

    return True


def build_caption(predictions, today):
    date_str = format_date_ru(today)
    lines = [f"✦ <b>Гороскоп · {html_escape(date_str)}</b>", ""]

    for z in ZODIAC:
        text = html_escape(predictions[z["id"]].strip())
        lines.append(f"{z['emoji']} <b>{html_escape(z['ru'])}</b>")
        lines.append(text)
        lines.append("")  # пустая строка между знаками

    caption = "\n".join(lines).rstrip()

    # На всякий случай страховка от лимита 4096
    if len(caption) > MAX_MESSAGE:
        # Если каким-то чудом перебор — обрезаем последние знаки до лимита
        caption = caption[:MAX_MESSAGE - 50].rsplit("\n", 1)[0] + "\n\n…"
    return caption


def build_keyboard(webapp_url):
    """Кнопка для поста — открывает WebApp в режим меню тем."""
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

    today = msk_now()
    print(f"Гороскоп на {format_date_ru(today)}", file=sys.stderr)

    system, user = build_prompt()
    try:
        raw, model_used = call_groq_with_fallback(api_key, system, user)
        validate(raw)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  Использована модель: {model_used}", file=sys.stderr)

    caption = build_caption(raw, today)
    print(f"  Длина поста: {len(caption)} знаков", file=sys.stderr)

    keyboard = build_keyboard(webapp_url)
    send_message(token, chat_id, caption, reply_markup=keyboard)
    print(f"✓ Опубликован гороскоп дня", file=sys.stderr)


if __name__ == "__main__":
    main()
