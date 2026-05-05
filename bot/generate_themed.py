"""
Генератор банка тематических предсказаний.

Запускается раз в ~4 недели. Делает запрос к Groq на каждую карту и для
каждой комбинации тема × положение генерирует ДВА варианта предсказания.

Структура банка в cards.json:
{
    "themed_predictions": {
        "love":   {"upright": ["вар1", "вар2"], "reversed": ["вар1", "вар2"]},
        "work":   {"upright": [...], "reversed": [...]},
        "path":   {"upright": [...], "reversed": [...]},
        "custom": {"upright": [...], "reversed": [...]}
    }
}

Темы:
  love   — любовь и отношения
  work   — работа, карьера, деньги (объединено)
  path   — я и мой путь, самопознание, важные решения
  custom — ответ на неизвестный личный вопрос (для «Свой вопрос»)

Итого: 78 карт × 4 темы × 2 положения × 2 варианта = 1248 текстов.

ENV:
    GROQ_API_KEY
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

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = 90
USER_AGENT = "ThemedTarotBank/1.0"

MAX_RETRIES = 3
PAUSE_BETWEEN_CARDS = 0.5

THEMES = {
    "love":   "любовь и отношения",
    "work":   "работа, карьера и деньги — профессиональная и финансовая сфера",
    "path":   "я и мой путь — самопознание, призвание, важные жизненные решения",
    "custom": "ответ на неизвестный личный вопрос",
}

POSITIONS = ["upright", "reversed"]
VARIANTS_PER_CELL = 2  # сколько вариантов на карту+тему+положение


def env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        print(f"ERROR: переменная {name} не задана", file=sys.stderr)
        sys.exit(1)
    return value


def load_cards():
    with CARDS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def save_cards(cards):
    with CARDS_PATH.open("w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)


def build_prompt(card):
    """Промпт на одну карту: 4 темы × 2 положения × 2 варианта = 16 текстов."""
    keywords_up = ", ".join(card["keywords_upright"][:5])
    keywords_rv = ", ".join(card["keywords_reversed"][:5])

    system = (
        "Ты опытный таролог. Пишешь короткие, атмосферные предсказания на русском. "
        "Стиль: благородный, мягкий, без оккультного штампа. Обращение на «вы». "
        "Каждое предсказание — 2-3 предложения, около 200-280 знаков. "
        "Никаких приветствий, подписей, упоминаний слов «карта» или «таро» — "
        "пиши о ситуации напрямую. Без markdown и эмодзи. Без вводных «в любви...» "
        "или «на работе...» — сразу к делу."
    )

    # Список нужных ключей для подсказки модели
    key_lines = []
    for theme_id, theme_desc in THEMES.items():
        for pos in POSITIONS:
            pos_ru = "прямая" if pos == "upright" else "перевёрнутая"
            key = f"{theme_id}_{pos}"
            key_lines.append(f"- {key} — {theme_desc}, {pos_ru}, два разных варианта")

    user = (
        f"Карта: «{card['name_ru']}». Суть: {card['essence']}\n"
        f"Прямое положение — ключевые темы: {keywords_up}\n"
        f"Перевёрнутое положение — ключевые темы: {keywords_rv}\n\n"
        "Напиши предсказания для всех комбинаций темы и положения. "
        f"На каждую комбинацию нужно ДВА разных варианта — они должны "
        "отличаться по образности и акценту, но оставаться по сути той же карты:\n"
        + "\n".join(key_lines) + "\n\n"
        "Верни ответ строго в JSON, без markdown:\n"
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


def call_groq(api_key, system, user):
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.9,
        "max_tokens": 4500,
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
            if r.status_code != 200:
                last_err = f"Groq {r.status_code}: {r.text[:200]}"
                print(f"  попытка {attempt}: {last_err}", file=sys.stderr)
                continue
            content = r.json()["choices"][0]["message"]["content"].strip()
            return json.loads(content)
        except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"  попытка {attempt}: {last_err}", file=sys.stderr)

    raise RuntimeError(f"Groq не ответил после {MAX_RETRIES} попыток: {last_err}")


def validate(resp):
    """Проверяет что в ответе все 8 ключей по 2 варианта длиной 30+ символов."""
    if not isinstance(resp, dict):
        raise ValueError("ответ не объект")
    for theme in THEMES:
        for pos in POSITIONS:
            key = f"{theme}_{pos}"
            if key not in resp:
                raise ValueError(f"нет поля {key}")
            value = resp[key]
            if not isinstance(value, list) or len(value) < VARIANTS_PER_CELL:
                raise ValueError(f"{key}: нужен список из {VARIANTS_PER_CELL} вариантов")
            for j, txt in enumerate(value[:VARIANTS_PER_CELL]):
                if not isinstance(txt, str) or len(txt.strip()) < 30:
                    raise ValueError(f"{key}[{j}] слишком короткий")
    return True


def reshape_response(resp):
    """Превращает плоские ключи в вложенную структуру."""
    out = {}
    for theme in THEMES:
        out[theme] = {}
        for pos in POSITIONS:
            key = f"{theme}_{pos}"
            variants = [v.strip() for v in resp[key][:VARIANTS_PER_CELL]]
            out[theme][pos] = variants
    return out


def main():
    api_key = env("GROQ_API_KEY")
    cards = load_cards()
    total = len(cards)
    print(f"Загружено карт: {total}", file=sys.stderr)
    print(f"Каждая карта = 8 ячеек × 2 варианта = 16 текстов", file=sys.stderr)
    print(f"Итого банк: {total * 16} текстов\n", file=sys.stderr)

    success = 0
    failed = []
    start = time.time()

    for i, card in enumerate(cards, start=1):
        print(f"[{i}/{total}] {card['name_ru']}...", file=sys.stderr)
        system, user = build_prompt(card)
        try:
            raw = call_groq(api_key, system, user)
            validate(raw)
            card["themed_predictions"] = reshape_response(raw)
            success += 1
            print(f"  ✓", file=sys.stderr)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            failed.append(card["id"])

        # Сохраняем cards.json после каждой карты — на случай если упадём
        # посередине, не потеряем уже обработанные
        if i % 10 == 0:
            save_cards(cards)
            print(f"  (промежуточное сохранение, {i}/{total})", file=sys.stderr)

        time.sleep(PAUSE_BETWEEN_CARDS)

    save_cards(cards)

    elapsed = time.time() - start
    print(f"\n✓ Успешно: {success}/{total} карт", file=sys.stderr)
    print(f"  Время: {elapsed:.0f} сек", file=sys.stderr)
    if failed:
        print(f"✗ Не удалось: {len(failed)} карт — {failed}", file=sys.stderr)
        # Не выходим с ошибкой если хоть что-то получилось
        if success == 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
