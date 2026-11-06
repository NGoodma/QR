"""Telegram QR bot as a Vercel serverless function (webhook at /api/telegram).

Flow: send a link -> "Выберите формат" [JPG] [PNG] -> "Выберите цвет" -> for JPG also
"Выберите цвет фона" -> the QR code arrives as a file. PNG has a transparent background.

No storage: the bot's menu message is a reply to the user's link, so every button press
finds the link in callback_query.message.reply_to_message. The choices so far travel in
callback_data, e.g. "c:jpg:black" or "b:black:white".
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

import qrcode
import requests

log = logging.getLogger("qrbot")
logging.basicConfig(level=logging.INFO)

# (id, name, hex, emoji, "on ... background"). Black and white come first, as asked.
COLORS = [
    ("black", "Чёрный", "#000000", "⚫", "чёрном"),
    ("white", "Белый", "#ffffff", "⚪", "белом"),
    ("red", "Красный", "#e53935", "🔴", "красном"),
    ("orange", "Оранжевый", "#fb8c00", "🟠", "оранжевом"),
    ("yellow", "Жёлтый", "#fdd835", "🟡", "жёлтом"),
    ("green", "Зелёный", "#43a047", "🟢", "зелёном"),
    ("blue", "Синий", "#1e88e5", "🔵", "синем"),
    ("purple", "Фиолетовый", "#8e24aa", "🟣", "фиолетовом"),
    ("brown", "Коричневый", "#6d4c41", "🟤", "коричневом"),
]
COLOR = {c[0]: c for c in COLORS}

START_TEXT = "Привет! Отправь мне ссылку (http/https), и я сделаю QR-код в JPG или PNG нужного цвета."
NOT_A_LINK = "Это не похоже на ссылку. Отправь URL, начинающийся с http:// или https://"
NO_LINK = "Не вижу ссылку: пришли её ещё раз"


def is_valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def webhook_secret(token: str) -> str:
    """Secret for Telegram's X-Telegram-Bot-Api-Secret-Token header, derived from the bot token."""
    return hashlib.sha256(token.encode()).hexdigest()[:32]


# --- images ------------------------------------------------------------------------------


def make_qr(url: str, fmt: str, fg: str, bg: str | None = None) -> bytes:
    """PNG: transparent background. JPG: background `bg`."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.BytesIO()
    if fmt == "png":
        qr.make_image(fill_color=COLOR[fg][2], back_color="transparent").get_image().save(buf, format="PNG")
    else:
        img = qr.make_image(fill_color=COLOR[fg][2], back_color=COLOR[bg][2]).get_image().convert("RGB")
        img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _luminance(hex_color: str) -> float:
    c = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def readability_note(fg: str, bg: str | None) -> str | None:
    """Warn about combinations scanners struggle with (checked with OpenCV on every pair)."""
    lf = _luminance(COLOR[fg][2])
    if bg is None:  # PNG, transparent background
        return "Светлый код на прозрачном фоне виден только на тёмном фоне" if lf > 0.5 else None
    lb = _luminance(COLOR[bg][2])
    if (max(lf, lb) + 0.05) / (min(lf, lb) + 0.05) < 1.5:
        return "Цвета почти не различаются: код может не считаться"
    if lf > lb:
        return "Светлый код на тёмном фоне читают не все сканеры. Надёжнее тёмный код на светлом фоне"
    return None


# --- keyboards -----------------------------------------------------------------------------


def format_keyboard() -> dict:
    return {"inline_keyboard": [[{"text": "JPG", "callback_data": "f:jpg"}, {"text": "PNG", "callback_data": "f:png"}]]}


def color_keyboard(prefix: str, skip: str | None = None) -> dict:
    """Black and white on the first row, then three per row. `skip` hides a color (same as the QR)."""
    buttons = [{"text": f"{e} {name}", "callback_data": f"{prefix}:{cid}"} for cid, name, _, e, _ in COLORS if cid != skip]
    rows = [buttons[:2]] if skip not in ("black", "white") else [buttons[:1]]
    rest = buttons[len(rows[0]) :]
    rows += [rest[i : i + 3] for i in range(0, len(rest), 3)]
    return {"inline_keyboard": rows}


# --- Telegram ------------------------------------------------------------------------------


class Telegram:
    def __init__(self, token: str, base: str | None = None):
        self.base = f"{base or os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org')}/bot{token}/"

    def call(self, method: str, files: dict | None = None, **params):
        if files:  # multipart: nested objects must be JSON strings
            data = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}
            resp = requests.post(self.base + method, data=data, files=files, timeout=30)
        else:
            resp = requests.post(self.base + method, json=params, timeout=30)
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"{method}: {body.get('description')}")
        return body["result"]


# --- bot logic -----------------------------------------------------------------------------


def handle_update(tg, update: dict) -> None:
    if "callback_query" in update:
        return handle_callback(tg, update["callback_query"])
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    if not text or msg.get("chat", {}).get("type") != "private":
        return
    chat_id = msg["chat"]["id"]
    if text.startswith(("/start", "/help")):
        tg.call("sendMessage", chat_id=chat_id, text=START_TEXT)
    elif is_valid_url(text):
        tg.call(
            "sendMessage",
            chat_id=chat_id,
            text="Выберите формат",
            reply_markup=format_keyboard(),
            reply_parameters={"message_id": msg["message_id"]},
        )
    else:
        tg.call("sendMessage", chat_id=chat_id, text=NOT_A_LINK)


def handle_callback(tg, cq: dict) -> None:
    menu = cq.get("message") or {}
    chat_id = menu.get("chat", {}).get("id")
    source = menu.get("reply_to_message") or {}
    url = (source.get("text") or "").strip()
    parts = (cq.get("data") or "").split(":")

    if not chat_id or not is_valid_url(url):
        tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=NO_LINK, show_alert=True)
        return

    def edit(text, markup=None):
        tg.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=menu["message_id"],
            text=text,
            **({"reply_markup": markup} if markup else {}),
        )

    def send(fmt, fg, bg=None):
        tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="Рисую QR-код…")
        what = f"{fmt.upper()}, {COLOR[fg][1].lower()}" + (f" на {COLOR[bg][4]} фоне" if bg else "")
        edit(f"Готово: {what}")
        tg.call(
            "sendDocument",
            chat_id=chat_id,
            caption="Вот твой QR-код" + (f"\n\n⚠️ {note}" if (note := readability_note(fg, bg)) else ""),
            reply_parameters={"message_id": source["message_id"]},
            files={"document": (f"qr.{fmt}", make_qr(url, fmt, fg, bg), f"image/{'png' if fmt == 'png' else 'jpeg'}")},
        )

    match parts:
        case ["f", ("jpg" | "png") as fmt]:
            tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            edit("Выберите цвет", color_keyboard(f"c:{fmt}"))
        case ["c", "png", fg] if fg in COLOR:
            send("png", fg)
        case ["c", "jpg", fg] if fg in COLOR:
            tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            edit("Выберите цвет фона", color_keyboard(f"b:{fg}", skip=fg))
        case ["b", fg, bg] if fg in COLOR and bg in COLOR and fg != bg:
            send("jpg", fg, bg)
        case _:
            tg.call("answerCallbackQuery", callback_query_id=cq["id"])


# --- Vercel entry --------------------------------------------------------------------------


class handler(BaseHTTPRequestHandler):  # noqa: N801 (Vercel looks for this exact name)
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))  # read first, even to refuse
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token or self.headers.get("X-Telegram-Bot-Api-Secret-Token") != webhook_secret(token):
            return self._reply(403, "forbidden")
        try:
            handle_update(Telegram(token), json.loads(body))
        except Exception:  # still 200, or Telegram would resend the same update forever
            log.exception("update failed")
        self._reply(200, "ok")

    def do_GET(self):
        self._reply(200, "ok")

    def _reply(self, code: int, text: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(text)))
        self.end_headers()
        self.wfile.write(text.encode())
