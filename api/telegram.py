"""Telegram QR bot as a Vercel serverless function (webhook at /api/telegram).

Flow: send a link or any text -> (two lines that look like a Wi-Fi name and password:
"Wi-Fi or plain text?") -> format [JPG] [PNG] [SVG] -> dot style -> color -> for JPG also
background color -> the QR code arrives as a file. PNG and SVG have a transparent background.
Every step after the first has a "Back" button.

Custom colors: "🎨 Свой цвет (HEX)" sends a prompt with ForceReply, so the user's next message is
a reply to it. The prompt carries everything needed: the choices so far in a link's #fragment and
the user's text in a quote. A reply to it with "#ff8800" continues the flow.

No storage: the bot's menu message is a reply to the user's text, so every button press
finds the text in callback_query.message.reply_to_message. The choices so far travel in
callback_data as "q:<kind>:<fmt>:<dots>:<fg>:<bg>", empty fields not chosen yet.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import quote, unquote, urlparse

import qrcode
import requests
from PIL import Image, ImageOps
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
HEX = re.compile(r"[0-9a-f]{6}")  # a custom color in callback_data: 6 hex digits, no "#"


def parse_hex(value: str) -> str | None:
    """'#FF8800', 'ff8800', '#f80' -> 'ff8800'; None if it is not a color."""
    v = value.strip().lower().removeprefix("#")
    if re.fullmatch(r"[0-9a-f]{3}", v):
        v = "".join(ch * 2 for ch in v)
    return v if HEX.fullmatch(v) else None


def hex_of(color: str) -> str:
    return COLOR[color][2] if color in COLOR else f"#{color}"


def color_name(color: str) -> str:
    return COLOR[color][1].lower() if color in COLOR else f"#{color}"


def on_background(color: str) -> str:
    return f"на {COLOR[color][4]} фоне" if color in COLOR else f"на фоне #{color}"


# Dot styles: (button, summary, drawer for data dots, drawer for the three corner squares).
# Round dots keep rounded corner squares: corner squares made of dots fail on some scanners.
DOTS = {
    "sq": ("⬛ Квадратные", "квадратные точки", SquareModuleDrawer, SquareModuleDrawer),
    "rd": ("🔲 Скруглённые", "скруглённые точки", RoundedModuleDrawer, RoundedModuleDrawer),
    "ci": ("⚫ Круглые", "круглые точки", CircleModuleDrawer, RoundedModuleDrawer),
}
FORMATS = {"jpg": "image/jpeg", "png": "image/png", "svg": "image/svg+xml"}
BOX, BORDER = 12, 4  # pixels per module, quiet zone in modules
DENSE_VERSION = 25  # from here on some scanners (OpenCV) stop reading the code

START_TEXT = (
    "Привет! Пришли ссылку или любой текст, и я сделаю из него QR-код: JPG, PNG или "
    "SVG, нужного цвета, с квадратными, скруглёнными или круглыми точками.\n\n"
    "Для Wi-Fi пришли двумя строками название сети и пароль, например:\n"
    "MyHome\nsecret123\n"
    "По такому коду телефон сам предложит подключиться к сети."
)
NO_TEXT = "Не вижу текст: пришли его ещё раз"
WIFI_HINT = (
    "Если это Wi-Fi, пришли название сети и пароль двумя строками, например:\n"
    "MyHome\nsecret123\n"
    "Тогда телефон по коду сам подключится к сети."
)
TOO_LONG = "Слишком длинный текст для QR-кода: влезает около 2300 латинских букв или 1100 русских"


def is_valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


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


def make_qr(data: str, fmt: str, fg: str, bg: str | None = None, dots: str = "sq") -> bytes:
    """PNG and SVG: transparent background. JPG: background `bg`."""
    qr = build_qr(data)
    if fmt == "svg":
        return make_svg(qr, hex_of(fg), dots)
    _, _, drawer, eye = DOTS[dots]
    # Draw black on white, then use it as a mask to paint the colors (also gives a clean transparent PNG).
    mask = ImageOps.invert(
        qr.make_image(image_factory=StyledPilImage, module_drawer=drawer(), eye_drawer=eye()).get_image().convert("L")
    )
    size = mask.size
    buf = io.BytesIO()
    if fmt == "png":
        img = Image.composite(Image.new("RGBA", size, hex_of(fg)), Image.new("RGBA", size, (0, 0, 0, 0)), mask)
        img.save(buf, format="PNG")
    else:
        img = Image.composite(Image.new("RGB", size, hex_of(fg)), Image.new("RGB", size, hex_of(bg)), mask)
        img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _is_eye(r: int, c: int, n: int) -> bool:
    """One of the three big corner squares (7x7 modules)."""
    return (r < 7 and c < 7) or (r < 7 and c >= n - 7) or (r >= n - 7 and c < 7)


def make_svg(qr: qrcode.QRCode, color: str, dots: str) -> bytes:
    """Vector QR with the same shapes as the raster one: one path, 1 unit = 1 module.
    Square: plain squares. Rounded: a corner is rounded when both neighbours at that corner are empty
    (same rule as qrcode's RoundedModuleDrawer). Round: circles, corner squares stay rounded."""
    m = qr.modules
    n = len(m)
    on = lambda r, c: 0 <= r < n and 0 <= c < n and bool(m[r][c])  # noqa: E731
    parts = []
    for r in range(n):
        for c in range(n):
            if not m[r][c]:
                continue
            x, y = c + BORDER, r + BORDER
            style = dots if dots != "ci" or not _is_eye(r, c, n) else "rd"
            if style == "ci":
                parts.append(f"M{x},{y + 0.5}a.5,.5 0 1,0 1,0a.5,.5 0 1,0 -1,0z")
            elif style == "sq":
                parts.append(f"M{x},{y}h1v1h-1z")
            else:
                up, down, left, right = on(r - 1, c), on(r + 1, c), on(r, c - 1), on(r, c + 1)
                tl, tr = not (up or left), not (up or right)
                br, bl = not (down or right), not (down or left)
                q = 0.5
                d = f"M{x + (q if tl else 0)},{y}"
                d += f"H{x + 1 - (q if tr else 0)}" + (f"a{q},{q} 0 0,1 {q},{q}" if tr else "")
                d += f"V{y + 1 - (q if br else 0)}" + (f"a{q},{q} 0 0,1 -{q},{q}" if br else "")
                d += f"H{x + (q if bl else 0)}" + (f"a{q},{q} 0 0,1 -{q},-{q}" if bl else "")
                d += f"V{y + (q if tl else 0)}" + (f"a{q},{q} 0 0,1 {q},-{q}" if tl else "")
                parts.append(d + "z")
    side = n + 2 * BORDER
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" '
        f'width="{side * BOX}" height="{side * BOX}">'
        f'<path fill="{color}" d="{"".join(parts)}"/></svg>'
    )
    return svg.encode()


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
    lf = _luminance(hex_of(fg))
    if bg is None:  # PNG or SVG, transparent background
        return "Светлый код на прозрачном фоне виден только на тёмном фоне" if lf > 0.5 else None
    if lf > _luminance(hex_of(bg)):
        return "Светлый код на тёмном фоне читают не все сканеры. Надёжнее тёмный код на светлом фоне"
    if _gray(hex_of(bg)) - _gray(hex_of(fg)) < MIN_GRAY_DIFF:
        return "Цвета плохо различаются: код может не считаться"
    return None


# --- the choice, step by step --------------------------------------------------------------

FIELDS = ("kind", "fmt", "dots", "fg", "bg")


def encode(state: dict, prefix: str = "q") -> str:
    """prefix "q": a choice; "x": ask for a custom HEX color for the next color step."""
    return f"{prefix}:" + ":".join(state.get(f) or "" for f in FIELDS)


def decode(data: str, prefix: str = "q") -> dict | None:
    """Parse callback_data; None if it is malformed or holds values we don't know."""
    parts = data.split(":")
    if len(parts) != len(FIELDS) + 1 or parts[0] != prefix:
        return None
    s = {f: v or None for f, v in zip(FIELDS, parts[1:])}
    allowed = {
        "kind": {"t", "w"}, "fmt": set(FORMATS), "dots": set(DOTS), "fg": set(COLOR), "bg": set(COLOR),
    }
    is_color = lambda f, v: f in ("fg", "bg") and HEX.fullmatch(v)  # noqa: E731
    if any(v is not None and v not in allowed[f] and not is_color(f, v) for f, v in s.items()):
        return None
    if s["fg"] and s["bg"] and hex_of(s["fg"]) == hex_of(s["bg"]):
        return None
    return s


def _buttons(state: dict, field: str, options) -> list[dict]:
    return [{"text": text, "callback_data": encode({**state, field: value})} for value, text in options]


def _rows(buttons: list, first: int, per_row: int = 3) -> list[list]:
    return [buttons[:first]] + [buttons[i : i + per_row] for i in range(first, len(buttons), per_row)]


def _back(state: dict, text: str) -> list[list]:
    """A row with "Back": undo the last choice. None on the first step (nothing to go back to)."""
    chosen = [f for f in FIELDS if state[f]]
    if chosen == ["kind"] and not parse_wifi(text):  # kind was set by the bot, not asked
        chosen = []
    if not chosen:
        return []
    return [[{"text": "← Назад", "callback_data": encode({**state, chosen[-1]: None})}]]


def _hex_button(state: dict) -> list[dict]:
    return [{"text": "🎨 Свой цвет (HEX)", "callback_data": encode(state, "x")}]


def next_step(state: dict, text: str) -> tuple[str, dict] | None:
    """The next question and its keyboard, or None when everything is chosen."""
    if not state["kind"]:
        question, rows = "Похоже на сеть Wi-Fi и пароль. Что сделать?", [
            _buttons(state, "kind", [("w", "📶 Код для подключения к Wi-Fi")]),
            _buttons(state, "kind", [("t", "📝 Просто текст")]),
        ]
    elif not state["fmt"]:
        question = "Выберите формат"
        if state["kind"] == "t" and not is_valid_url(text) and not parse_wifi(text):
            question += "\n\n" + WIFI_HINT
        rows = [_buttons(state, "fmt", [(k, k.upper()) for k in FORMATS])]
    elif not state["dots"]:
        question, rows = "Выберите стиль точек", [_buttons(state, "dots", [(k, v[0]) for k, v in DOTS.items()])]
    elif not state["fg"]:
        b = _buttons(state, "fg", [(cid, f"{e} {name}") for cid, name, _, e, _ in COLORS])
        question, rows = "Выберите цвет", _rows(b, 2) + [_hex_button(state)]
    elif state["fmt"] == "jpg" and not state["bg"]:
        fg = hex_of(state["fg"])
        colors = [(cid, f"{e} {name}") for cid, name, hx, e, _ in COLORS if hx != fg]
        first = 1 if fg in (COLOR["black"][2], COLOR["white"][2]) else 2  # black and white on the first row
        question, rows = "Выберите цвет фона", _rows(_buttons(state, "bg", colors), first) + [_hex_button(state)]
    else:
        return None
    return question, {"inline_keyboard": rows + _back(state, text)}


def summary(state: dict) -> str:
    fmt, fg, bg = state["fmt"], state["fg"], state["bg"]
    return ", ".join([fmt.upper(), DOTS[state["dots"]][1], color_name(fg) + (f" {on_background(bg)}" if bg else "")])


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


# --- custom HEX colors: a prompt that carries the state -------------------------------------

PICKER = "https://htmlcolorcodes.com/color-picker/"  # the "pick a color" link; our state rides in its #fragment
HEX_HINT = "Не понял цвет. Пришли HEX-код вида #ff8800"
HEX_WITHOUT_PROMPT = "Похоже на цвет. Чтобы задать свой цвет, нажми «🎨 Свой цвет (HEX)» под вопросом про цвет"


def _utf16(s: str) -> int:
    """Telegram counts entity offsets in UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


def hex_prompt(state: dict, text: str, menu_id: int, source_id: int, error: str | None = None) -> dict:
    """sendMessage params for "send me a HEX": ForceReply, the state in a link, the text in a quote."""
    which = "фона" if state["fg"] else "кода"
    head = (f"{error}\n\n" if error else "") + f"🎨 Пришли HEX-код цвета {which}, например #ff8800. "
    link = "Подобрать цвет"
    body = "\n\nQR-код для:\n"
    full = head + link + body + text
    url = f"{PICKER}#qr={quote(encode(state, 'x'))}/{menu_id}/{source_id}"
    return {
        "text": full,
        "entities": [
            {"type": "text_link", "offset": _utf16(head), "length": _utf16(link), "url": url},
            {"type": "expandable_blockquote", "offset": _utf16(head + link + body), "length": _utf16(text)},
        ],
        "link_preview_options": {"is_disabled": True},
        "reply_markup": {"force_reply": True, "input_field_placeholder": "#ff8800"},
    }


def read_prompt(msg: dict) -> tuple[dict, str, int, int] | None:
    """(state, text, menu_id, source_id) from our HEX prompt, or None if it is not one."""
    text16 = (msg.get("text") or "").encode("utf-16-le")
    piece = lambda e: text16[e["offset"] * 2 : (e["offset"] + e["length"]) * 2].decode("utf-16-le")  # noqa: E731
    url = quoted = None
    for e in msg.get("entities") or []:
        if e.get("type") == "text_link" and e.get("url", "").startswith(PICKER + "#qr="):
            url = e["url"]
        elif e.get("type") in ("blockquote", "expandable_blockquote"):
            quoted = piece(e)
    if not url or not quoted:
        return None
    try:
        data, menu_id, source_id = url.split("#qr=", 1)[1].rsplit("/", 2)
        state = decode(unquote(data), "x")
        return (state, quoted, int(menu_id), int(source_id)) if state else None
    except ValueError:
        return None


# --- bot logic -----------------------------------------------------------------------------


def handle_update(tg, update: dict) -> None:
    if "callback_query" in update:
        return handle_callback(tg, update["callback_query"])
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    if not text or msg.get("chat", {}).get("type") != "private":
        return
    chat_id = msg["chat"]["id"]
    prompt = read_prompt(msg.get("reply_to_message") or {})
    if prompt:
        return handle_hex(tg, chat_id, msg, prompt)
    if text.startswith(("/start", "/help")):
        tg.call("sendMessage", chat_id=chat_id, text=START_TEXT)
        return
    if text.startswith("#") and parse_hex(text):
        tg.call("sendMessage", chat_id=chat_id, text=HEX_WITHOUT_PROMPT)
        return
    if not fits(text):
        tg.call("sendMessage", chat_id=chat_id, text=TOO_LONG)
        return
    state = dict.fromkeys(FIELDS)
    if not parse_wifi(text):
        state["kind"] = "t"  # only Wi-Fi-looking text needs the extra question
    question, markup = next_step(state, text)
    tg.call(
        "sendMessage",
        chat_id=chat_id,
        text=question,
        reply_markup=markup,
        reply_parameters={"message_id": msg["message_id"]},
    )


def _valid(state: dict | None, text: str) -> bool:
    return bool(
        text and state is not None
        and not (state["kind"] == "w" and not parse_wifi(text))
        and not (state["bg"] and state["fmt"] != "jpg")  # only JPG has a background
    )


def _edit(tg, chat_id, message_id, text, markup=None):
    tg.call(
        "editMessageText",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        **({"reply_markup": markup} if markup else {}),
    )


def advance(tg, chat_id: int, menu_id: int, source_id: int, state: dict, text: str) -> None:
    """Show the next question in the menu, or draw and send the QR code if everything is chosen."""
    step = next_step(state, text)
    if step:
        _edit(tg, chat_id, menu_id, *step)
        return
    _edit(tg, chat_id, menu_id, f"Готово: {summary(state)}")
    data = payload(text, state["kind"])
    fmt = state["fmt"]
    caption = (
        f"Вот QR-код для подключения к сети «{parse_wifi(text)[0]}»" if state["kind"] == "w" else "Вот твой QR-код"
    )
    notes = [readability_note(state["fg"], state["bg"])]
    if build_qr(data).version >= DENSE_VERSION:
        notes.append("Текст длинный, код получился плотным: с экрана его прочитают не все камеры. Лучше распечатать покрупнее")
    caption += "".join(f"\n\n⚠️ {n}" for n in notes if n)
    image = make_qr(data, fmt, state["fg"], state["bg"], state["dots"])
    tg.call(
        "sendDocument",
        chat_id=chat_id,
        caption=caption,
        reply_parameters={"message_id": source_id, "allow_sending_without_reply": True},
        files={"document": (f"qr.{fmt}", image, FORMATS[fmt])},
    )


def handle_callback(tg, cq: dict) -> None:
    menu = cq.get("message") or {}
    chat_id = menu.get("chat", {}).get("id")
    source = menu.get("reply_to_message") or {}
    text = (source.get("text") or "").strip()
    data = cq.get("data") or ""
    ask_hex = data.startswith("x:")
    state = decode(data, "x" if ask_hex else "q")

    if not chat_id or not _valid(state, text) or (ask_hex and state["bg"]):
        tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=NO_TEXT, show_alert=True)
        return

    if ask_hex:
        tg.call("answerCallbackQuery", callback_query_id=cq["id"])
        tg.call("sendMessage", chat_id=chat_id, **hex_prompt(state, text, menu["message_id"], source["message_id"]))
        return
    final = next_step(state, text) is None
    tg.call("answerCallbackQuery", callback_query_id=cq["id"], **({"text": "Рисую QR-код…"} if final else {}))
    advance(tg, chat_id, menu["message_id"], source["message_id"], state, text)


def handle_hex(tg, chat_id: int, msg: dict, prompt: tuple) -> None:
    """A reply to our HEX prompt: take the color and carry on where the menu was."""
    state, text, menu_id, source_id = prompt
    color = parse_hex(msg.get("text") or "")
    field = "bg" if state["fg"] else "fg"
    new = {**state, field: color}
    if not color or not _valid(state, text) or (field == "bg" and hex_of(color) == hex_of(state["fg"])):
        error = "Это тот же цвет, что и у кода. Пришли другой" if color else HEX_HINT
        tg.call("sendMessage", chat_id=chat_id, **hex_prompt(state, text, menu_id, source_id, error))
        return
    try:  # the prompt has done its job
        tg.call("deleteMessage", chat_id=chat_id, message_id=msg["reply_to_message"]["message_id"])
    except RuntimeError:
        pass
    advance(tg, chat_id, menu_id, source_id, new, text)


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
