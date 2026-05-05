"""
Публикует пост «Карта недели» в Telegram-канал.

Делает 2 вещи:
1. Постит фото-коллаж с 3 закрытыми обложками (генерируем на лету через PIL)
2. К посту прикрепляет одну инлайн-кнопку «🔮 Открыть карту» — ведёт в WebApp

ENV:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID            — канал, например @my_channel или -100xxxxxxxxxx
    WEBAPP_URL                  — например https://username.github.io/repo

Запускать в понедельник 9:00 МСК после generate_predictions.py.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
PREDICTIONS_PATH = ROOT / "webapp" / "predictions.json"

TG_API = "https://api.telegram.org/bot{token}/{method}"


def env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        print(f"ERROR: переменная окружения {name} не задана", file=sys.stderr)
        sys.exit(1)
    return value


def render_three_backs():
    """Рисует PNG с тремя обложками карт в ряд для поста.

    Размер 900x540 — оптимально для Telegram превью (соотношение ~1.66).
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("ERROR: установи Pillow (pip install Pillow)", file=sys.stderr)
        sys.exit(1)

    W, H = 900, 540
    CARD_W, CARD_H = 200, 343  # стандартное соотношение карт таро 1:1.71
    GAP = 60
    total_w = CARD_W * 3 + GAP * 2
    start_x = (W - total_w) // 2
    y = (H - CARD_H) // 2

    # Тёмный градиентный фон
    img = Image.new("RGB", (W, H), "#0a0420")
    draw = ImageDraw.Draw(img)
    # Грубый градиент через горизонтальные полосы
    for ny in range(H):
        ratio = ny / H
        r = int(0x0a + (0x2d - 0x0a) * ratio)
        g = int(0x04 + (0x1b - 0x04) * ratio)
        b = int(0x20 + (0x69 - 0x20) * ratio)
        draw.line([(0, ny), (W, ny)], fill=(r, g, b))

    # Звёздочки
    import random
    rng = random.Random(42)
    for _ in range(80):
        x = rng.randint(0, W - 1)
        ny = rng.randint(0, H - 1)
        size = rng.choice([1, 1, 2])
        color = rng.choice(["#d4af37", "#ffffff", "#a59cd6"])
        if size == 1:
            draw.point((x, ny), fill=color)
        else:
            draw.ellipse([x - 1, ny - 1, x + 1, ny + 1], fill=color)

    # Три обложки
    for i in range(3):
        x = start_x + i * (CARD_W + GAP)
        draw_card_back(draw, x, y, CARD_W, CARD_H)

    # Сохраняю в байты
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def draw_card_back(draw, x, y, w, h):
    """Рисует одну обложку — тот же стиль что в SVG в WebApp.

    Упрощённый вариант для превью:
    - градиент (через несколько слоёв)
    - золотая рамка
    - центральная звезда
    """
    # Фон карты
    draw.rounded_rectangle([x, y, x + w, y + h], radius=12, fill="#1a0b3d")

    # Внешняя золотая рамка
    draw.rounded_rectangle([x + 6, y + 6, x + w - 6, y + h - 6],
                           radius=8, outline="#d4af37", width=2)
    # Внутренняя
    draw.rounded_rectangle([x + 12, y + 12, x + w - 12, y + h - 12],
                           radius=6, outline="#8b7029", width=1)

    # Центральная восьмиконечная звезда
    cx, cy = x + w // 2, y + h // 2
    star_r = w // 4
    points_outer = []
    points_inner = []
    import math
    for i in range(8):
        angle = (i * 45 - 90) * math.pi / 180
        px = cx + star_r * math.cos(angle)
        py = cy + star_r * math.sin(angle)
        points_outer.append((px, py))
        angle_inner = ((i + 0.5) * 45 - 90) * math.pi / 180
        px = cx + (star_r * 0.4) * math.cos(angle_inner)
        py = cy + (star_r * 0.4) * math.sin(angle_inner)
        points_inner.append((px, py))

    star_points = []
    for o, i_pt in zip(points_outer, points_inner):
        star_points.append(o)
        star_points.append(i_pt)
    draw.polygon(star_points, fill="#d4af37")

    # Маленькая центральная точка
    draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill="#fff")

    # Луна сверху
    moon_y = y + h // 5
    draw.ellipse([cx - 14, moon_y - 14, cx + 14, moon_y + 14], fill="#d4af37")
    draw.ellipse([cx - 6, moon_y - 14, cx + 22, moon_y + 14], fill="#1a0b3d")

    # Солнце снизу
    sun_y = y + h - h // 5
    draw.ellipse([cx - 9, sun_y - 9, cx + 9, sun_y + 9], fill="#d4af37")


def load_predictions():
    with PREDICTIONS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def html_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_caption(predictions):
    intro = html_escape(predictions.get("intro", "На пороге новой недели."))
    return (
        f"🔮 <b>Карта недели</b>\n\n"
        f"<i>{intro}</i>\n\n"
        f"Перед вами три закрытые карты. Прислушайтесь к себе — "
        f"какая из них зовёт вас сильнее? Откройте её, чтобы узнать "
        f"послание на эту неделю."
    )


def build_keyboard(webapp_url):
    """Кнопка для поста в канале.

    ВАЖНО: в каналах работает только тип `url` (не `web_app`).
    Поэтому передавать сюда нужно direct link на Mini App в формате:
        https://t.me/<bot_username>/<app_short_name>
    Telegram сам распознает такую ссылку и откроет WebApp inline,
    а не во внешнем браузере.

    Если же передать обычный URL на GitHub Pages — он откроется в
    браузере. Так что обязательно сделай Mini App в @BotFather и
    привяжи к боту, а в WEBAPP_URL клади t.me/-ссылку.
    """
    return {
        "inline_keyboard": [
            [{
                "text": "🔮 Открыть карту",
                "url": webapp_url,
            }]
        ]
    }


def send_photo(token, chat_id, photo_buf, caption, reply_markup):
    files = {"photo": ("card-week.png", photo_buf, "image/png")}
    data = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(reply_markup, ensure_ascii=False),
    }
    url = TG_API.format(token=token, method="sendPhoto")
    r = requests.post(url, data=data, files=files, timeout=60)
    if r.status_code != 200:
        print(f"ERROR Telegram {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()
    resp = r.json()
    if not resp.get("ok"):
        print(f"ERROR Telegram: {resp}", file=sys.stderr)
        sys.exit(1)
    return resp


def main():
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    webapp_url = env("WEBAPP_URL")

    if not PREDICTIONS_PATH.exists():
        print(f"ERROR: {PREDICTIONS_PATH} не найден. Сначала запусти generate_predictions.py",
              file=sys.stderr)
        sys.exit(1)

    predictions = load_predictions()
    photo = render_three_backs()
    caption = build_caption(predictions)
    keyboard = build_keyboard(webapp_url)

    send_photo(token, chat_id, photo, caption, keyboard)
    print(f"Опубликовано в {chat_id}: {predictions['week_id']}")
    print(f"Карты: {[c['card_id'] for c in predictions['cards']]}")


if __name__ == "__main__":
    main()
