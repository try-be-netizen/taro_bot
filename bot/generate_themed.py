"""
Генератор банка тематических предсказаний с персонализацией по знакам зодиака.

Структура банка (webapp/predictions_bank.json):
{
  "<card_id>": {
    "<theme>": {                       # love | work | path | custom
      "<position>": {                  # upright | reversed
        "general":  ["вар1", "вар2"],  # для тех, кто не выбрал знак
        "aries":    ["вар1"],          # для каждого из 12 знаков
        "taurus":   ["вар1"],
        ...
        "pisces":   ["вар1"]
      }
    }
  }
}

Объём: 78 карт × 4 темы × 2 положения × 14 ячеек (12 знаков + 2 general) ≈ 8736 текстов.

На каждую карту делаем 5 запросов к Groq:
1. «general» — все 4 темы × 2 положения × 2 варианта = 16 текстов
2-5. По одному запросу на каждую тему: 12 знаков × 2 положения = 24 текста

Итого 78 × 5 = 390 запросов. ~12 минут работы.

ENV: GROQ_API_KEY
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
CARDS_PATH = ROOT / "webapp" / "cards.json"
BANK_PATH = ROOT / "webapp" / "predictions_bank.json"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = 90
USER_AGENT = "ThemedTarotBank/2.0"

MAX_RETRIES = 5
PAUSE_BETWEEN_REQUESTS = 2.5  # пауза между запросами — Groq бесплатный
                              # имеет лимит токенов в минуту, дросселимся
RATE_LIMIT_PAUSE = 60         # если получили 429, ждём минуту

THEMES = {
    "love":   "любовь и отношения",
    "work":   "работа, карьера и деньги, профессиональная сфера",
    "path":   "самопознание, личный путь, важные жизненные решения, призвание",
    "custom": "ответ на неизвестный личный вопрос",
}

ZODIAC = [
    ("aries",       "Овен",       "лидер, прямой, импульсивный, любит вызовы, воин"),
    ("taurus",      "Телец",      "обстоятельный, чувственный, упрямый, любит уют и стабильность"),
    ("gemini",      "Близнецы",   "любопытный, изменчивый, общительный, скачет с темы на тему"),
    ("cancer",      "Рак",        "эмоциональный, заботливый, домашний, обидчивый"),
    ("leo",         "Лев",        "яркий, щедрый, гордый, любит внимание и сцену"),
    ("virgo",       "Дева",       "перфекционист, аналитик, тревожный, замечает детали"),
    ("libra",       "Весы",       "ищет гармонию, эстет, нерешительный, дипломат"),
    ("scorpio",     "Скорпион",   "интенсивный, проницательный, мстительный, страстный"),
    ("sagittarius", "Стрелец",    "оптимист, ищет смысл, любит свободу и горизонты"),
    ("capricorn",   "Козерог",    "трудоголик, серьёзный, долгосрочно мыслящий, амбициозный"),
    ("aquarius",    "Водолей",    "оригинальный, отстранённый, идеалист, любит свободу"),
    ("pisces",      "Рыбы",       "мечтательный, чувствительный, эмпат, теряется в реальности"),
]
ZODIAC_IDS = [z[0] for z in ZODIAC]
POSITIONS = ["upright", "reversed"]


def env(name, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        print(f"ERROR: {name} не задана", file=sys.stderr)
        sys.exit(1)
    return v


def load_cards():
    with CARDS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_bank():
    """Загружает существующий банк или создаёт пустой каркас."""
    if BANK_PATH.exists():
        try:
            with BANK_PATH.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_bank(bank):
    with BANK_PATH.open("w", encoding="utf-8") as f:
        json.dump(bank, f, ensure_ascii=False, indent=2)


# ============================================================
# ЗАПРОС 1: общие тексты (general) — все темы и положения вместе
# ============================================================

def build_general_prompt(card):
    keywords_up = ", ".join(card["keywords_upright"][:5])
    keywords_rv = ", ".join(card["keywords_reversed"][:5])

    system = (
        "Ты опытный таролог. Пишешь короткие, атмосферные предсказания на "
        "русском. Стиль: благородный, мягкий, без оккультного штампа. "
        "Обращение на «вы». Каждое предсказание — 2-3 предложения, около "
        "200-280 знаков. Никаких приветствий, подписей, упоминаний слов "
        "«карта» или «таро». Без markdown, без эмодзи. Без вводных «в любви...» "
        "— сразу к делу."
    )
    keys = []
    for theme_id, theme_desc in THEMES.items():
        for pos in POSITIONS:
            pos_ru = "прямая" if pos == "upright" else "перевёрнутая"
            keys.append(f"- {theme_id}_{pos} — {theme_desc}, {pos_ru}, два разных варианта")

    user = (
        f"Карта: «{card['name_ru']}». Суть: {card['essence']}\n"
        f"Прямое положение — ключевые темы: {keywords_up}\n"
        f"Перевёрнутое положение — ключевые темы: {keywords_rv}\n\n"
        "Напиши предсказания на все темы и положения. Для каждой комбинации — "
        "ДВА разных варианта (отличаются по образности, но одна суть карты):\n"
        + "\n".join(keys) + "\n\n"
        "Верни ответ в JSON, без markdown:\n"
        "{\n"
        '  "love_upright":    ["вариант 1", "вариант 2"],\n'
        '  "love_reversed":   ["вариант 1", "вариант 2"],\n'
        '  "work_upright":    ["вариант 1", "вариант 2"],\n'
        '  "work_reversed":   ["вариант 1", "вариант 2"],\n'
        '  "path_upright":    ["вариант 1", "вариант 2"],\n'
        '  "path_reversed":   ["вариант 1", "вариант 2"],\n'
        '  "custom_upright":  ["вариант 1", "вариант 2"],\n'
        '  "custom_reversed": ["вариант 1", "вариант 2"]\n'
        "}"
    )
    return system, user


def validate_general(resp):
    if not isinstance(resp, dict):
        raise ValueError("ответ не объект")
    for theme in THEMES:
        for pos in POSITIONS:
            key = f"{theme}_{pos}"
            value = resp.get(key)
            if not isinstance(value, list) or len(value) < 2:
                raise ValueError(f"{key}: нужен список из 2 вариантов")
            for j, txt in enumerate(value[:2]):
                if not isinstance(txt, str) or len(txt.strip()) < 30:
                    raise ValueError(f"{key}[{j}] слишком короткий")
    return True


# ============================================================
# ЗАПРОС 2-5: персонализация под одну тему — 12 знаков × 2 положения
# ============================================================

def build_zodiac_prompt(card, theme_id):
    keywords_up = ", ".join(card["keywords_upright"][:5])
    keywords_rv = ", ".join(card["keywords_reversed"][:5])
    theme_desc = THEMES[theme_id]

    system = (
        "Ты опытный таролог с глубоким знанием астрологии. Пишешь короткие "
        "атмосферные предсказания, учитывающие характер каждого знака зодиака. "
        "Стиль: благородный, мягкий, без оккультного штампа. Обращение на «вы». "
        "Каждое предсказание — 2-3 предложения, около 200-280 знаков. "
        "Тон должен ощущаться лично — учитывай характер знака, его слабости и "
        "сильные стороны, типичные ситуации. Никаких приветствий, подписей, "
        "упоминаний слов «карта», «таро», «зодиак», «гороскоп». "
        "Без markdown, без эмодзи. Без вводных «в любви...» — сразу к делу."
    )

    zodiac_lines = []
    for z_id, z_name, z_traits in ZODIAC:
        zodiac_lines.append(f"- {z_id} ({z_name}) — {z_traits}")

    keys_listing = []
    for z_id, z_name, _ in ZODIAC:
        for pos in POSITIONS:
            pos_ru = "прямая" if pos == "upright" else "перевёрнутая"
            keys_listing.append(f"  «{z_id}_{pos}» — {z_name}, {pos_ru}")

    json_template_lines = []
    for z_id, _, _ in ZODIAC:
        for pos in POSITIONS:
            json_template_lines.append(f'  "{z_id}_{pos}": "...",')
    # Убираем последнюю запятую
    json_template_lines[-1] = json_template_lines[-1].rstrip(",")

    user = (
        f"Карта: «{card['name_ru']}». Суть: {card['essence']}\n"
        f"Прямое положение — ключевые темы: {keywords_up}\n"
        f"Перевёрнутое положение — ключевые темы: {keywords_rv}\n\n"
        f"Тема расклада: {theme_desc}\n\n"
        "Знаки зодиака и их характеры:\n"
        + "\n".join(zodiac_lines) + "\n\n"
        "Напиши предсказание ОТДЕЛЬНО для каждого знака в каждом положении. "
        "Текст должен ЯВНО отличаться по знакам — попадать в их характер, "
        "типичные ситуации, слабые места. Не пиши обобщённо: думай как именно "
        "эта карта в этом положении проявится для конкретного человека этого "
        "знака в контексте темы.\n\n"
        "Нужны такие ключи:\n"
        + "\n".join(keys_listing) + "\n\n"
        "Верни ответ в JSON, без markdown, без вводных:\n"
        "{\n"
        + "\n".join(json_template_lines) + "\n"
        "}"
    )
    return system, user


def validate_zodiac(resp):
    if not isinstance(resp, dict):
        raise ValueError("ответ не объект")
    for z_id in ZODIAC_IDS:
        for pos in POSITIONS:
            key = f"{z_id}_{pos}"
            value = resp.get(key)
            if not isinstance(value, str) or len(value.strip()) < 30:
                raise ValueError(f"{key} слишком короткий или пустой")
    return True


# ============================================================
# Запросы к Groq
# ============================================================

def call_groq(api_key, system, user, max_tokens=4500):
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.85,
        "max_tokens": max_tokens,
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
            # Rate limit — ждём дольше и пробуем снова
            if r.status_code == 429:
                # Groq возвращает retry-after в секундах либо в заголовке,
                # либо в теле ответа. Парсим что найдём.
                retry_after = RATE_LIMIT_PAUSE
                hdr = r.headers.get("retry-after")
                if hdr:
                    try:
                        retry_after = max(int(float(hdr)), 5)
                    except ValueError:
                        pass
                # Capped, чтобы не висеть слишком долго
                retry_after = min(retry_after, 90)
                print(f"    rate limit (429), жду {retry_after} сек "
                      f"(попытка {attempt}/{MAX_RETRIES})...", file=sys.stderr)
                time.sleep(retry_after)
                last_err = "rate limit"
                continue
            if r.status_code != 200:
                last_err = f"Groq {r.status_code}: {r.text[:200]}"
                print(f"    попытка {attempt}: {last_err}", file=sys.stderr)
                time.sleep(2)
                continue
            content = r.json()["choices"][0]["message"]["content"].strip()
            return json.loads(content)
        except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"    попытка {attempt}: {last_err}", file=sys.stderr)
            time.sleep(2)
    raise RuntimeError(f"Groq не ответил после {MAX_RETRIES} попыток: {last_err}")


# ============================================================
# Сборка ячеек банка
# ============================================================

def build_card_predictions(api_key, card):
    """Возвращает структуру предсказаний для одной карты."""
    result = {
        theme: {pos: {} for pos in POSITIONS}
        for theme in THEMES
    }

    # 1) general — общие варианты
    print(f"  [1/5] general...", file=sys.stderr)
    system, user = build_general_prompt(card)
    raw_general = call_groq(api_key, system, user, max_tokens=4500)
    validate_general(raw_general)
    for theme in THEMES:
        for pos in POSITIONS:
            key = f"{theme}_{pos}"
            result[theme][pos]["general"] = [v.strip() for v in raw_general[key][:2]]
    time.sleep(PAUSE_BETWEEN_REQUESTS)

    # 2-5) для каждой темы — 12 знаков × 2 положения
    for i, theme_id in enumerate(THEMES, start=2):
        print(f"  [{i}/5] {theme_id} × 12 знаков...", file=sys.stderr)
        system, user = build_zodiac_prompt(card, theme_id)
        raw_zodiac = call_groq(api_key, system, user, max_tokens=4500)
        validate_zodiac(raw_zodiac)
        for z_id in ZODIAC_IDS:
            for pos in POSITIONS:
                key = f"{z_id}_{pos}"
                result[theme_id][pos][z_id] = [raw_zodiac[key].strip()]
        time.sleep(PAUSE_BETWEEN_REQUESTS)

    return result


# ============================================================
# Основной цикл
# ============================================================

def main():
    api_key = env("GROQ_API_KEY")
    cards = load_cards()
    bank = load_bank()
    total = len(cards)

    print(f"Карт всего: {total}", file=sys.stderr)
    print(f"Запросов к Groq на карту: 5", file=sys.stderr)
    print(f"Итого запросов: {total * 5}", file=sys.stderr)
    print(f"Текстов в банке после прогона: {total * 4 * 2 * (12 + 2)} ≈ "
          f"{total * 4 * 2 * 14}\n", file=sys.stderr)

    success = 0
    failed = []
    skipped = 0
    start = time.time()

    for i, card in enumerate(cards, start=1):
        card_id = card["id"]
        print(f"[{i}/{total}] {card['name_ru']} ({card_id})", file=sys.stderr)

        # Skip если для этой карты уже есть достаточно полный банк
        # (полезно при повторном запуске после падения)
        existing = bank.get(card_id)
        if existing and is_card_complete(existing):
            print(f"  ↷ уже есть в банке — пропускаю", file=sys.stderr)
            skipped += 1
            continue

        try:
            bank[card_id] = build_card_predictions(api_key, card)
            success += 1
            print(f"  ✓", file=sys.stderr)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            failed.append(card_id)

        # Промежуточные сохранения каждые 5 карт
        if i % 5 == 0:
            save_bank(bank)
            elapsed = time.time() - start
            print(f"  (сохранено, прошло {elapsed:.0f} сек)", file=sys.stderr)

    save_bank(bank)
    elapsed = time.time() - start
    print(f"\n✓ Успешно: {success}/{total}", file=sys.stderr)
    print(f"  Пропущено (уже было): {skipped}", file=sys.stderr)
    print(f"  Время: {elapsed:.0f} сек ({elapsed/60:.1f} мин)", file=sys.stderr)
    if failed:
        print(f"✗ Не удалось: {len(failed)} — {failed}", file=sys.stderr)
        if success == 0 and skipped == 0:
            sys.exit(1)


def is_card_complete(card_bank):
    """Проверяет что у карты заполнены все ячейки (все темы × положения × general + 12 знаков)."""
    if not isinstance(card_bank, dict):
        return False
    for theme in THEMES:
        if theme not in card_bank:
            return False
        for pos in POSITIONS:
            if pos not in card_bank[theme]:
                return False
            cell = card_bank[theme][pos]
            # Должны быть general и все 12 знаков
            if "general" not in cell or not cell["general"]:
                return False
            for z_id in ZODIAC_IDS:
                if z_id not in cell or not cell[z_id]:
                    return False
    return True


if __name__ == "__main__":
    main()