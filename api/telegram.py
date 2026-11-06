"""Telegram QR bot as a Vercel serverless function (webhook at /api/telegram).

Flow: send a link or any text -> (two lines that look like a Wi-Fi name and password:
"Wi-Fi or plain text?") -> format [JPG] [PNG] -> dot style -> color -> for JPG also
background color and background corners -> the QR code arrives as a file.
PNG has a transparent background.

No storage: the bot's menu message is a reply to the user's text, so every button press
finds the text in callback_query.message.reply_to_message. The choices so far travel in
callback_data as "q:<kind>:<fmt>:<dots>:<fg>:<bg>:<corners>", empty fields not chosen yet.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from http.server import BaseHTTPRequestHandler

import qrcode
import requests
from PIL import Image, ImageDraw, ImageOps
from qrcode.exceptions import DataOverflowError
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers.pil import CircleModuleDrawer, RoundedModuleDrawer, SquareModuleDrawer

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

# Dot styles: (button, summary, drawer for data dots, drawer for the three corner squares).
# Round dots keep rounded corner squares: corner squares made of dots fail on some scanners.
DOTS = {
    "sq": ("⬛ Квадратные", "квадратные точки", SquareModuleDrawer, SquareModuleDrawer),
    "rd": ("🔲 Скруглённые", "скруглённые точки", RoundedModuleDrawer, RoundedModuleDrawer),
    "ci": ("⚫ Круглые", "круглые точки", CircleModuleDrawer, RoundedModuleDrawer),
}
CORNERS = {"sq": ("⬜ Прямые", "прямые углы"), "rd": ("▢ Скруглённые", "скруглённые углы")}
CORNER_FILL = "#ffffff"  # JPG has no transparency: the cut-off corners are white
BOX, BORDER = 12, 4  # pixels per module, quiet zone in modules
DENSE_VERSION = 25  # from here on some scanners (OpenCV) stop reading the code

START_TEXT = (
    "Привет! Пришли ссылку или любой текст, и я сделаю из него QR-код: JPG или PNG, "
    "нужного цвета, с квадратными, скруглёнными или круглыми точками.\n\n"
    "Для Wi-Fi пришли двумя строками название сети и пароль, например:\n"
    "MyHome\nsecret123\n"
    "По такому коду телефон сам предложит подключиться к сети."
)
NO_TEXT = "Не вижу текст: пришли его ещё раз"
TOO_LONG = "Слишком длинный текст для QR-кода: влезает около 2300 латинских букв или 1100 русских"


def webhook_secret(token: str) -> str:
    """Secret for Telegram's X-Telegram-Bot-Api-Secret-Token header, derived from the bot token."""
    return hashlib.sha256(token.encode()).hexdigest()[:32]


# --- what goes into the code ---------------------------------------------------------------


def parse_wifi(text: str) -> tuple[str, str] | None:
    """Two lines, a network name (up to 32 bytes) and a WPA password (8-63 chars), or None."""
    lines = [line.strip() for line in text.strip().splitlines()]
    if len(lines) != 2 or not lines[0] or not 8 <= len(lines[1]) <= 63:
        return None
    if len(lines[0].encode()) > 32:
        return None
    return lines[0], lines[1]


def _escape(value: str) -> str:
    for ch in "\\;,:\"":
        value = value.replace(ch, "\\" + ch)
    return value


def payload(text: str, kind: str) -> str:
    """What the QR code holds: the text as is, or a Wi-Fi network in the format phones understand."""
    if kind == "w":
        ssid, password = parse_wifi(text)
        return f"WIFI:T:WPA;S:{_escape(ssid)};P:{_escape(password)};;"
    return text


def build_qr(data: str) -> qrcode.QRCode:
    """Raises DataOverflowError when the text does not fit."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=BOX, border=BORDER)
    qr.add_data(data)
    qr.make(fit=True)
    return qr


def fits(data: str) -> bool:
    try:
        build_qr(data)
        return True
    except (DataOverflowError, ValueError):  # qrcode raises ValueError past version 40
        return False


# --- images ------------------------------------------------------------------------------


def make_qr(data: str, fmt: str, fg: str, bg: str | None = None, dots: str = "sq", corners: str = "sq") -> bytes:
    """PNG: transparent background. JPG: background `bg`, optionally with rounded corners."""
    qr = build_qr(data)
    _, _, drawer, eye = DOTS[dots]
    # Draw black on white, then use it as a mask to paint the colors (also gives a clean transparent PNG).
    mask = ImageOps.invert(
        qr.make_image(image_factory=StyledPilImage, module_drawer=drawer(), eye_drawer=eye()).get_image().convert("L")
    )
    size = mask.size
    buf = io.BytesIO()
    if fmt == "png":
        img = Image.composite(Image.new("RGBA", size, COLOR[fg][2]), Image.new("RGBA", size, (0, 0, 0, 0)), mask)
        img.save(buf, format="PNG")
    else:
        back = Image.new("RGB", size, COLOR[bg][2])
        if corners == "rd":  # radius = the quiet zone, so the code itself is never cut
            shape = Image.new("L", size, 0)
            ImageDraw.Draw(shape).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=BORDER * BOX, fill=255)
            back = Image.composite(back, Image.new("RGB", size, CORNER_FILL), shape)
        img = Image.composite(Image.new("RGB", size, COLOR[fg][2]), back, mask)
        img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _luminance(hex_color: str) -> float:
    c = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _gray(hex_color: str) -> float:
    """Brightness the way scanners see it after converting to grayscale (BT.601)."""
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b


MIN_GRAY_DIFF = 80  # below this some pairs fail to scan (measured on every pair with OpenCV and ZXing)


def readability_note(fg: str, bg: str | None) -> str | None:
    """Warn about combinations scanners struggle with."""
    lf = _luminance(COLOR[fg][2])
    if bg is None:  # PNG, transparent background
        return "Светлый код на прозрачном фоне виден только на тёмном фоне" if lf > 0.5 else None
    if lf > _luminance(COLOR[bg][2]):
        return "Светлый код на тёмном фоне читают не все сканеры. Надёжнее тёмный код на светлом фоне"
    if _gray(COLOR[bg][2]) - _gray(COLOR[fg][2]) < MIN_GRAY_DIFF:
        return "Цвета плохо различаются: код может не считаться"
    return None


# --- the choice, step by step --------------------------------------------------------------

FIELDS = ("kind", "fmt", "dots", "fg", "bg", "corners")


def encode(state: dict) -> str:
    return "q:" + ":".join(state.get(f) or "" for f in FIELDS)


def decode(data: str) -> dict | None:
    """Parse callback_data; None if it is malformed or holds values we don't know."""
    parts = data.split(":")
    if len(parts) != len(FIELDS) + 1 or parts[0] != "q":
        return None
    s = {f: v or None for f, v in zip(FIELDS, parts[1:])}
    allowed = {
        "kind": {"t", "w"}, "fmt": {"jpg", "png"}, "dots": set(DOTS),
        "fg": set(COLOR), "bg": set(COLOR), "corners": set(CORNERS),
    }
    if any(v is not None and v not in allowed[f] for f, v in s.items()):
        return None
    if s["fg"] and s["fg"] == s["bg"]:
        return None
    return s


def _buttons(state: dict, field: str, options) -> list[dict]:
    return [{"text": text, "callback_data": encode({**state, field: value})} for value, text in options]


def _rows(buttons: list, first: int, per_row: int = 3) -> list[list]:
    return [buttons[:first]] + [buttons[i : i + per_row] for i in range(first, len(buttons), per_row)]


def next_step(state: dict) -> tuple[str, dict] | None:
    """The next question and its keyboard, or None when everything is chosen.
    Fills in choices that need no question (e.g. corners on a white background)."""
    if not state["kind"]:
        return "Похоже на сеть Wi-Fi и пароль. Что сделать?", {"inline_keyboard": [
            _buttons(state, "kind", [("w", "📶 Код для подключения к Wi-Fi")]),
            _buttons(state, "kind", [("t", "📝 Просто текст")]),
        ]}
    if not state["fmt"]:
        return "Выберите формат", {"inline_keyboard": [_buttons(state, "fmt", [("jpg", "JPG"), ("png", "PNG")])]}
    if not state["dots"]:
        return "Выберите стиль точек", {"inline_keyboard": [_buttons(state, "dots", [(k, v[0]) for k, v in DOTS.items()])]}
    if not state["fg"]:
        b = _buttons(state, "fg", [(cid, f"{e} {name}") for cid, name, _, e, _ in COLORS])
        return "Выберите цвет", {"inline_keyboard": _rows(b, 2)}
    if state["fmt"] == "png":
        return None
    if not state["bg"]:
        colors = [(cid, f"{e} {name}") for cid, name, _, e, _ in COLORS if cid != state["fg"]]
        first = 2 if state["fg"] not in ("black", "white") else 1  # black and white on the first row
        return "Выберите цвет фона", {"inline_keyboard": _rows(_buttons(state, "bg", colors), first)}
    if not state["corners"]:
        if COLOR[state["bg"]][2] == CORNER_FILL:  # white corners on a white background: nothing to round
            state["corners"] = "sq"
            return None
        return "Углы фона", {"inline_keyboard": [_buttons(state, "corners", [(k, v[0]) for k, v in CORNERS.items()])]}
    return None


def summary(state: dict) -> str:
    fmt, fg, bg = state["fmt"], state["fg"], state["bg"]
    parts = [fmt.upper(), DOTS[state["dots"]][1], COLOR[fg][1].lower() + (f" на {COLOR[bg][4]} фоне" if bg else "")]
    if state.get("corners") == "rd":
        parts.append(CORNERS["rd"][1])
    return ", ".join(parts)


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
        return
    if not fits(text):
        tg.call("sendMessage", chat_id=chat_id, text=TOO_LONG)
        return
    state = dict.fromkeys(FIELDS)
    if not parse_wifi(text):
        state["kind"] = "t"  # only Wi-Fi-looking text needs the extra question
    question, markup = next_step(state)
    tg.call(
        "sendMessage",
        chat_id=chat_id,
        text=question,
        reply_markup=markup,
        reply_parameters={"message_id": msg["message_id"]},
    )


def handle_callback(tg, cq: dict) -> None:
    menu = cq.get("message") or {}
    chat_id = menu.get("chat", {}).get("id")
    source = menu.get("reply_to_message") or {}
    text = (source.get("text") or "").strip()
    state = decode(cq.get("data") or "")

    if not chat_id or not text or state is None or (state["kind"] == "w" and not parse_wifi(text)):
        tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=NO_TEXT, show_alert=True)
        return

    def edit(message, markup=None):
        tg.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=menu["message_id"],
            text=message,
            **({"reply_markup": markup} if markup else {}),
        )

    step = next_step(state)
    if step:
        tg.call("answerCallbackQuery", callback_query_id=cq["id"])
        edit(*step)
        return

    data = payload(text, state["kind"])
    tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="Рисую QR-код…")
    edit(f"Готово: {summary(state)}")
    fmt = state["fmt"]
    caption = (
        f"Вот QR-код для подключения к сети «{parse_wifi(text)[0]}»" if state["kind"] == "w" else "Вот твой QR-код"
    )
    notes = [readability_note(state["fg"], state["bg"])]
    if build_qr(data).version >= DENSE_VERSION:
        notes.append("Текст длинный, код получился плотным: с экрана его прочитают не все камеры. Лучше распечатать покрупнее")
    caption += "".join(f"\n\n⚠️ {n}" for n in notes if n)
    image = make_qr(data, fmt, state["fg"], state["bg"], state["dots"], state["corners"] or "sq")
    tg.call(
        "sendDocument",
        chat_id=chat_id,
        caption=caption,
        reply_parameters={"message_id": source["message_id"]},
        files={"document": (f"qr.{fmt}", image, f"image/{'png' if fmt == 'png' else 'jpeg'}")},
    )


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
