"""
Раз в неделю генерирует predictions.json для WebApp.

Логика:
1. Выбирает 3 случайные карты из cards.json (с равной вероятностью прямого/перевёрнутого положения).
2. Запрашивает у Groq API (бесплатная Llama 3.3 70B) три предсказания на неделю.
3. Перезаписывает webapp/predictions.json со свежим week_id.

ENV:
    GROQ_API_KEY      — ключ Groq (https://console.groq.com)

Запускать раз в неделю в воскресенье поздно вечером, чтобы понедельник
уже встретил подписчиков с готовым раскладом.
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
OUT_PATH = ROOT / "webapp" / "predictions.json"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = 60

# Сколько раз пробуем при ошибках сети/API
MAX_RETRIES = 3

# Доля шанса перевёрнутой карты (классически — 0.5, но я снижаю до 0.3
# чтобы посты были позитивнее в среднем)
REVERSED_CHANCE = 0.3


def env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        print(f"ERROR: переменная окружения {name} не задана", file=sys.stderr)
        sys.exit(1)
    return value


def msk_now():
    return datetime.now(timezone(timedelta(hours=3)))


def week_id_for(dt):
    """Идентификатор недели в формате YYYY-WNN (ISO 8601 неделя)."""
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def load_cards():
    with CARDS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def pick_three_cards(all_cards):
    """Выбирает 3 разные карты + бросает монету для перевёрнутости.

    Старшие арканы получают повышенный вес — они интереснее младших,
    и пост с тремя «важными» картами цепляет лучше, чем «Двойка пентаклей».
    """
    weights = []
    for c in all_cards:
        if c["arcana"] == "major":
            weights.append(2.0)  # старшие в 2 раза вероятнее
        else:
            weights.append(1.0)

    chosen = []
    pool = list(zip(all_cards, weights))
    for _ in range(3):
        # Взвешенный выбор без возврата
        total = sum(w for _, w in pool)
        r = random.uniform(0, total)
        cumulative = 0
        for i, (card, w) in enumerate(pool):
            cumulative += w
            if r <= cumulative:
                chosen.append(card)
                pool.pop(i)
                break

    # Случайно решаем — прямая или перевёрнутая
    result = []
    for card in chosen:
        result.append({
            "card": card,
            "is_reversed": random.random() < REVERSED_CHANCE,
        })
    return result


def build_prompt(week_label, picked):
    """Системный + пользовательский промпт для Groq."""
    cards_block = []
    for i, p in enumerate(picked, start=1):
        c = p["card"]
        keywords = c["keywords_reversed"] if p["is_reversed"] else c["keywords_upright"]
        position = "перевёрнутая" if p["is_reversed"] else "прямая"
        cards_block.append(
            f"Карта {i}: {c['name_ru']} ({position})\n"
            f"  Суть: {c['essence']}\n"
            f"  Ключевые слова: {', '.join(keywords)}"
        )
    cards_text = "\n\n".join(cards_block)

    system = (
        "Ты — таролог с тонким чувством слова. Пишешь атмосферно, "
        "но без пафоса и эзотерических клише типа «звёзды шепчут». "
        "Используешь живой современный русский язык. "
        "Обращение к читателю — на «вы». "
        "Каждое предсказание — это короткое, но плотное по смыслу послание "
        "на 3-4 предложения, около 280-380 знаков. "
        "Не повторяешь название карты в тексте предсказания. "
        "Не используешь markdown, эмодзи или форматирование — только чистый текст."
    )

    user = (
        f"Неделя: {week_label}.\n\n"
        f"Я разложил три карты для подписчиков канала. "
        f"Напиши для каждой карты предсказание на эту неделю.\n\n"
        f"{cards_text}\n\n"
        "Также придумай короткую загадочную фразу-приглашение (intro) — "
        "одно предложение, 60-90 знаков, в духе «На пороге новой недели — три двери».\n\n"
        "Верни ответ строго в формате JSON, без markdown-обёрток, без пояснений:\n"
        "{\n"
        '  "intro": "...",\n'
        '  "predictions": ["текст_для_карты_1", "текст_для_карты_2", "текст_для_карты_3"]\n'
        "}"
    )
    return system, user


def call_groq(api_key, system, user):
    """Вызов Groq API. Возвращает {intro, predictions}."""
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.85,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=GROQ_TIMEOUT)
            if r.status_code != 200:
                last_err = f"Groq API {r.status_code}: {r.text[:300]}"
                print(f"Попытка {attempt}: {last_err}", file=sys.stderr)
                continue
            data = r.json()
            content = data["choices"][0]["message"]["content"].strip()
            return json.loads(content)
        except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"Попытка {attempt}: {last_err}", file=sys.stderr)

    raise RuntimeError(f"Groq API не ответил после {MAX_RETRIES} попыток. Последняя ошибка: {last_err}")


def validate_response(resp):
    """Базовая проверка структуры ответа от Groq."""
    if not isinstance(resp, dict):
        raise ValueError("ответ не объект JSON")
    if "intro" not in resp or "predictions" not in resp:
        raise ValueError("в ответе нет полей intro/predictions")
    if not isinstance(resp["predictions"], list) or len(resp["predictions"]) != 3:
        raise ValueError("predictions должен быть массивом из 3 элементов")
    for i, text in enumerate(resp["predictions"]):
        if not isinstance(text, str) or len(text.strip()) < 50:
            raise ValueError(f"predictions[{i}] слишком короткий или пустой")
    if not isinstance(resp["intro"], str) or len(resp["intro"].strip()) < 20:
        raise ValueError("intro слишком короткий")
    return True


def assemble_predictions_json(picked, ai_response, week_label, now_iso):
    cards_out = []
    for i, p in enumerate(picked):
        c = p["card"]
        cards_out.append({
            "position": i + 1,
            "card_id": c["id"],
            "card_name_ru": c["name_ru"],
            "image_url": c["image_url"],
            "is_reversed": p["is_reversed"],
            "prediction": ai_response["predictions"][i].strip(),
        })

    return {
        "week_id": week_label,
        "generated_at": now_iso,
        "intro": ai_response["intro"].strip(),
        "cards": cards_out,
    }


def main():
    api_key = env("GROQ_API_KEY")

    cards = load_cards()
    if len(cards) != 78:
        print(f"ERROR: ожидалось 78 карт, найдено {len(cards)}", file=sys.stderr)
        sys.exit(1)

    now = msk_now()
    week_label = week_id_for(now)

    picked = pick_three_cards(cards)
    print(f"Выбрано на неделю {week_label}:", file=sys.stderr)
    for i, p in enumerate(picked, start=1):
        marker = "↓ перевёрнутая" if p["is_reversed"] else "↑ прямая"
        print(f"  {i}. {p['card']['name_ru']} ({marker})", file=sys.stderr)

    system, user = build_prompt(week_label, picked)
    ai_response = call_groq(api_key, system, user)
    validate_response(ai_response)

    result = assemble_predictions_json(picked, ai_response, week_label, now.isoformat(timespec="seconds"))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\nЗаписано в {OUT_PATH}")
    print(f"Intro: {result['intro']}")
    for c in result["cards"]:
        preview = c["prediction"][:80].replace("\n", " ")
        print(f"  [{c['position']}] {c['card_name_ru']}: {preview}…")


if __name__ == "__main__":
    main()
