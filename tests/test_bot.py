import importlib.util
import io
import json
from pathlib import Path

import pytest
from PIL import Image

spec = importlib.util.spec_from_file_location("qrbot", Path(__file__).parent.parent / "api" / "telegram.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

URL = "https://example.com/some/path?x=1"
WIFI = "MyHome\nsecret123"


def q(state):
    """callback_data for a partly filled choice, e.g. q(kind="t", fmt="png")."""
    return bot.encode({f: state.get(f) for f in bot.FIELDS})


class FakeTelegram:
    def __init__(self):
        self.calls = []

    def call(self, method, files=None, **params):
        self.calls.append({"method": method, "files": files, **params})
        return {}

    def methods(self):
        return [c["method"] for c in self.calls]


def link_message(text=URL):
    return {"message": {"message_id": 10, "chat": {"id": 5, "type": "private"}, "text": text}}


def tap(data, source_text=URL):
    data = q(data) if isinstance(data, dict) else data
    return {
        "callback_query": {
            "id": "cq1",
            "data": data,
            "message": {
                "message_id": 11,
                "chat": {"id": 5},
                "text": "…",
                "reply_to_message": {"message_id": 10, "text": source_text},
            },
        }
    }


def run(*updates):
    tg = FakeTelegram()
    for u in updates:
        bot.handle_update(tg, u)
    return tg


def buttons(markup):
    return [[b["text"] for b in row] for row in markup["inline_keyboard"]]


def test_link_asks_for_format_as_a_reply():
    tg = run(link_message())
    [c] = tg.calls
    assert c["text"] == "Выберите формат"
    assert buttons(c["reply_markup"]) == [["JPG", "PNG", "SVG"]]  # first step: no "Back"
    assert c["reply_parameters"] == {"message_id": 10}
    assert c["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "q:t:jpg:::"


def test_any_text_is_accepted_with_a_wifi_hint():
    assert run(link_message("ты пидр")).calls[0]["text"] == "Выберите формат\n\n" + bot.WIFI_HINT
    assert bot.WIFI_HINT in run(tap({"kind": "t"}, "ты пидр")).calls[-1]["text"]  # also after "Back"
    assert run(link_message("/start")).calls[0]["text"] == bot.START_TEXT


def test_too_long_text_is_refused():
    assert run(link_message("я" * 1300)).calls[0]["text"] == bot.TOO_LONG
    assert run(link_message("я" * 1000)).calls[0]["text"].startswith("Выберите формат")


def test_wifi_looking_text_asks_what_to_do():
    [c] = run(link_message(WIFI)).calls
    assert buttons(c["reply_markup"]) == [["📶 Код для подключения к Wi-Fi"], ["📝 Просто текст"]]
    assert [r[0]["callback_data"] for r in c["reply_markup"]["inline_keyboard"]] == ["q:w::::", "q:t::::"]
    edit = run(tap("q:w::::", WIFI)).calls[-1]
    assert edit["text"] == "Выберите формат"  # no hint: the user already chose
    assert edit["reply_markup"]["inline_keyboard"][-1] == [{"text": "← Назад", "callback_data": "q:::::"}]
    assert run(tap("q:t::::", WIFI)).calls[-1]["text"] == "Выберите формат"


@pytest.mark.parametrize("text, wifi", [
    ("MyHome\nsecret123", ("MyHome", "secret123")),
    ("  Кафе Ромашка \n пароль с пробелом ", ("Кафе Ромашка", "пароль с пробелом")),
    ("MyHome\nshort", None),            # WPA passwords are at least 8 characters
    ("одна строка подлиннее", None),
    ("a\nb\nc", None),
    ("N" * 33 + "\nsecret123", None),   # network names are at most 32 bytes
])
def test_parse_wifi(text, wifi):
    assert bot.parse_wifi(text) == wifi


def test_wifi_payload_escapes_special_characters():
    assert bot.payload("Home;Net\npa:ss\"wo,rd\\", "w") == 'WIFI:T:WPA;S:Home\\;Net;P:pa\\:ss\\"wo\\,rd\\\\;;'
    assert bot.payload(WIFI, "t") == WIFI


def test_steps_in_order_with_back_buttons():
    s = {"kind": "t"}
    edit = run(tap(q({**s, "fmt": "png"}))).calls[-1]
    assert edit["text"] == "Выберите стиль точек"
    assert buttons(edit["reply_markup"]) == [["⬛ Квадратные", "🔲 Скруглённые", "⚫ Круглые"], ["← Назад"]]
    assert edit["reply_markup"]["inline_keyboard"][-1][0]["callback_data"] == q(s)
    edit = run(tap(q({**s, "fmt": "png", "dots": "ci"}))).calls[-1]
    assert edit["text"] == "Выберите цвет"
    rows = buttons(edit["reply_markup"])
    assert rows[0] == ["⚫ Чёрный", "⚪ Белый"] and rows[-2:] == [["🎨 Свой цвет (HEX)"], ["← Назад"]]
    assert sum(len(r) for r in rows[:-2]) == len(bot.COLORS)
    assert edit["reply_markup"]["inline_keyboard"][-1][0]["callback_data"] == q({**s, "fmt": "png"})


def test_back_from_background_goes_to_color():
    s = {"kind": "t", "fmt": "jpg", "dots": "sq", "fg": "black"}
    back = run(tap(s)).calls[-1]["reply_markup"]["inline_keyboard"][-1][0]
    assert back == {"text": "← Назад", "callback_data": q({**s, "fg": None})}


@pytest.mark.parametrize("fmt, mime, dots", [("png", "image/png", "rd"), ("svg", "image/svg+xml", "ci")])
def test_png_and_svg_are_sent_right_after_color(fmt, mime, dots):
    tg = run(tap({"kind": "t", "fmt": fmt, "dots": dots, "fg": "red"}))
    assert tg.methods() == ["answerCallbackQuery", "editMessageText", "sendDocument"]
    doc = tg.calls[-1]
    name, data, sent_mime = doc["files"]["document"]
    assert (name, sent_mime) == (f"qr.{fmt}", mime)
    assert doc["reply_parameters"]["message_id"] == 10
    style = bot.DOTS[dots][1]
    assert tg.calls[1]["text"] == f"Готово: {fmt.upper()}, {style}, красный"
    if fmt == "png":
        img = Image.open(io.BytesIO(data))
        assert img.format == "PNG" and img.mode == "RGBA"
        assert img.getpixel((0, 0))[3] == 0  # corner is background: transparent
    else:
        assert data.startswith(b"<svg") and b'fill="#e53935"' in data and b"<rect" not in data  # no background


def test_jpg_asks_for_background_without_the_same_color():
    edit = run(tap({"kind": "t", "fmt": "jpg", "dots": "sq", "fg": "black"})).calls[-1]
    assert edit["text"] == "Выберите цвет фона"
    rows = buttons(edit["reply_markup"])
    assert rows[0] == ["⚪ Белый"]
    assert "⚫ Чёрный" not in sum(rows, [])
    assert rows[-2:] == [["🎨 Свой цвет (HEX)"], ["← Назад"]]
    data = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"][:-2] for b in row]
    assert all(d.startswith("q:t:jpg:sq:black:") for d in data)


def test_jpg_is_sent_after_background():
    tg = run(tap({"kind": "t", "fmt": "jpg", "dots": "sq", "fg": "blue", "bg": "yellow"}))
    name, data, mime = tg.calls[-1]["files"]["document"]
    assert (name, mime) == ("qr.jpg", "image/jpeg")
    img = Image.open(io.BytesIO(data))
    r, g, b = img.getpixel((2, 2))
    assert r > 230 and g > 200 and b < 90  # yellow up to the very corner
    assert tg.calls[1]["text"] == "Готово: JPG, квадратные точки, синий на жёлтом фоне"


def test_wifi_code_and_caption():
    tg = run(tap({"kind": "w", "fmt": "png", "dots": "sq", "fg": "black"}, WIFI))
    assert tg.calls[-1]["caption"] == "Вот QR-код для подключения к сети «MyHome»"


@pytest.mark.parametrize("data", [
    "q:t:jpg:sq:red:red",     # same color twice
    "q:t:png:sq:red:white",   # PNG has no background
    "q:t:gif:::", "q:t:jpg:star::", "q:x::::", "q:t:jpg:sq:pink:",
    "q:t:jpg", "zzz", "q:t:jpg:sq:black:white:rd",  # the version with corners
    "q:t:jpg:sq:000000:black",                     # custom code color equal to the background
    "q:t:png:sq:ff88:", "x:t:jpg:sq:black:white",  # bad hex; HEX asked when nothing is left to choose
    "f:jpg", "c:png:red", "b:black:white",           # the first version
])
def test_bad_callbacks_send_nothing(data):
    tg = run(tap(data))
    assert "sendDocument" not in tg.methods() and "editMessageText" not in tg.methods()


# --- custom HEX colors ----------------------------------------------------------------------


@pytest.mark.parametrize("value, color", [
    ("#FF8800", "ff8800"), ("ff8800", "ff8800"), (" #f80 ", "ff8800"), ("#ff880", None), ("оранжевый", None), ("#gg0000", None),
])
def test_parse_hex(value, color):
    assert bot.parse_hex(value) == color


def reply_to(prompt_call, text, message_id=20):
    """The user's reply to the bot's HEX prompt, as Telegram delivers it (the prompt comes back as reply_to_message)."""
    prompt = {k: prompt_call[k] for k in ("text", "entities")}
    return {"message": {
        "message_id": message_id, "chat": {"id": 5, "type": "private"}, "text": text,
        "reply_to_message": {"message_id": 12, "from": {"is_bot": True}, **prompt},
    }}


def ask_hex(state, source=URL):
    """Press "🎨 Свой цвет (HEX)"; returns the prompt the bot sends."""
    tg = run(tap(bot.encode({f: state.get(f) for f in bot.FIELDS}, "x"), source))
    assert tg.methods() == ["answerCallbackQuery", "sendMessage"]
    return tg.calls[-1]


def test_hex_prompt_uses_force_reply_and_carries_the_state():
    p = ask_hex({"kind": "t", "fmt": "png", "dots": "ci"})
    assert p["reply_markup"] == {"force_reply": True, "input_field_placeholder": "#ff8800"}
    assert p["text"].startswith("🎨 Пришли HEX-код цвета кода") and p["text"].endswith(URL)
    assert bot.read_prompt({"text": p["text"], "entities": p["entities"]}) == (
        {"kind": "t", "fmt": "png", "dots": "ci", "fg": None, "bg": None}, URL, 11, 10)


def test_hex_reply_for_png_sends_the_code_right_away():
    p = ask_hex({"kind": "t", "fmt": "png", "dots": "sq"})
    tg = run(reply_to(p, "#FF8800"))
    assert tg.methods() == ["deleteMessage", "editMessageText", "sendDocument"]
    assert tg.calls[0]["message_id"] == 12  # the prompt is removed
    assert tg.calls[1] == {"method": "editMessageText", "files": None, "chat_id": 5, "message_id": 11,
                           "text": "Готово: PNG, квадратные точки, #ff8800"}
    doc = tg.calls[-1]
    assert doc["reply_parameters"]["message_id"] == 10
    img = Image.open(io.BytesIO(doc["files"]["document"][1])).convert("RGBA")
    colors = {px[:3] for px in img.get_flattened_data() if px[3] == 255}
    assert (255, 136, 0) in colors


def test_hex_for_jpg_code_then_background():
    s = {"kind": "t", "fmt": "jpg", "dots": "rd"}
    tg = run(reply_to(ask_hex(s), "#123456"))
    assert tg.methods() == ["deleteMessage", "editMessageText", "sendMessage"]
    # the old menu (above the user's reply) keeps only the choice, without buttons
    assert tg.calls[1] == {"method": "editMessageText", "files": None, "chat_id": 5, "message_id": 11,
                           "text": "Цвет кода: #123456"}
    # the background question comes as a new message, under the user's reply, still tied to the text
    ask = tg.calls[-1]
    assert ask["text"] == "Выберите цвет фона" and ask["reply_parameters"] == {"message_id": 10}
    rows = buttons(ask["reply_markup"])
    assert rows[0] == ["⚫ Чёрный", "⚪ Белый"]  # a custom code color hides no palette color
    assert rows[-2:] == [["🎨 Свой цвет (HEX)"], ["← Назад"]]
    assert ask["reply_markup"]["inline_keyboard"][0][1]["callback_data"] == "q:t:jpg:rd:123456:white"
    # and a custom background too
    p = ask_hex({**s, "fg": "123456"})
    assert "цвета фона" in p["text"]
    tg = run(reply_to(p, "fafafa"))
    assert tg.methods()[-1] == "sendDocument"
    assert tg.calls[-2]["text"] == "Готово: JPG, скруглённые точки, #123456 на фоне #fafafa"
    img = Image.open(io.BytesIO(tg.calls[-1]["files"]["document"][1]))
    assert all(abs(a - b) < 6 for a, b in zip(img.getpixel((2, 2)), (250, 250, 250)))


def test_custom_colors_work_through_buttons_too():
    tg = run(tap({"kind": "t", "fmt": "jpg", "dots": "sq", "fg": "123456", "bg": "white"}))
    assert tg.methods()[-1] == "sendDocument"
    assert tg.calls[1]["text"] == "Готово: JPG, квадратные точки, #123456 на белом фоне"


def test_bad_hex_reply_asks_again():
    p = ask_hex({"kind": "t", "fmt": "png", "dots": "sq"})
    tg = run(reply_to(p, "оранжевенький"))
    [again] = tg.calls
    assert again["method"] == "sendMessage" and again["text"].startswith(bot.HEX_HINT)
    assert again["reply_markup"]["force_reply"]
    assert bot.read_prompt(again)[0] == bot.read_prompt(p)[0]  # same state, the user can just try again


def test_same_background_as_code_is_refused():
    p = ask_hex({"kind": "t", "fmt": "jpg", "dots": "sq", "fg": "black"})
    tg = run(reply_to(p, "#000"))
    assert tg.methods() == ["sendMessage"] and "тот же цвет" in tg.calls[0]["text"]


def test_hex_prompt_keeps_wifi_and_unicode_text_exact():
    text = "Кафе 🌼 Ромашка\nпароль123"
    p = ask_hex({"kind": "w", "fmt": "svg", "dots": "sq"}, source=text)
    assert bot.read_prompt(p)[1] == text
    tg = run(reply_to(p, "#204080"))
    assert tg.calls[-1]["caption"] == "Вот QR-код для подключения к сети «Кафе 🌼 Ромашка»"


def test_hex_without_the_prompt_gets_a_hint():
    assert run(link_message("#ff8800")).calls[0]["text"] == bot.HEX_WITHOUT_PROMPT
    # a reply to some other bot message is just a new text
    other = link_message("#ff8800")
    other["message"]["reply_to_message"] = {"message_id": 3, "text": "Выберите цвет"}
    assert run(other).calls[0]["text"] == bot.HEX_WITHOUT_PROMPT


def test_hex_prompt_fits_telegram_limits():
    text = "я" * 1150  # about the longest text a QR code takes
    assert bot.fits(text)
    p = ask_hex({"kind": "t", "fmt": "jpg", "dots": "ci", "fg": "ffffff"}, source=text)
    assert bot._utf16(p["text"]) <= 4096


def test_lost_text_shows_alert():
    tg = run(tap({"kind": "t", "fmt": "png", "dots": "sq", "fg": "red"}, source_text=""))
    [c] = tg.calls
    assert c["method"] == "answerCallbackQuery" and c["show_alert"] and c["text"] == bot.NO_TEXT
    # a Wi-Fi choice on a text that is not Wi-Fi
    assert run(tap({"kind": "w", "fmt": "png", "dots": "sq", "fg": "red"}, "просто текст")).calls[0]["show_alert"]


def test_callback_data_fits_telegram_limit():
    longest = {"kind": "w", "fmt": "jpg", "dots": "ci", "fg": "purple", "bg": "orange"}
    assert len(bot.encode(longest).encode()) <= 64


def test_dense_code_gets_a_warning():
    s = {"kind": "t", "fmt": "png", "dots": "sq", "fg": "black"}
    assert "плотным" in run(tap(s, "я" * 900)).calls[-1]["caption"]
    assert "⚠️" not in run(tap(s, URL)).calls[-1]["caption"]


def _scan(image: bytes):
    """Decode with OpenCV and ZXing (used in many phone apps). SVG is rasterized with resvg first."""
    cv2 = pytest.importorskip("cv2")
    zxingcpp = pytest.importorskip("zxingcpp")
    np = pytest.importorskip("numpy")
    if image.startswith(b"<svg"):
        image = bytes(pytest.importorskip("resvg_py").svg_to_bytes(svg_string=image.decode()))
    img = Image.open(io.BytesIO(image))
    if img.mode in ("RGBA", "LA", "P"):  # what a transparent image looks like on a white page
        img = img.convert("RGBA")
        page = Image.new("RGB", img.size, "white")
        page.paste(img, mask=img)
        img = page
    pixels = np.array(img.convert("RGB"))
    return cv2.QRCodeDetector().detectAndDecode(pixels)[0], [r.text for r in zxingcpp.read_barcodes(pixels)]


TEXTS = [URL, "ты пидр", "WIFI:T:WPA;S:MyHome;P:secret123;;", "Длинный текст " * 20]


@pytest.mark.parametrize("dots", list(bot.DOTS))
@pytest.mark.parametrize("fmt", list(bot.FORMATS))
@pytest.mark.parametrize("text", TEXTS)
def test_every_style_and_format_scans(dots, fmt, text):
    image = bot.make_qr(text, fmt, "blue", "white" if fmt == "jpg" else None, dots)
    assert _scan(image) == (text, [text])


def test_svg_has_the_same_modules_as_the_raster():
    """Square SVG and square PNG, both rasterized, match pixel for pixel (up to anti-aliasing)."""
    resvg = pytest.importorskip("resvg_py")
    svg = Image.open(io.BytesIO(bytes(resvg.svg_to_bytes(svg_string=bot.make_qr(URL, "svg", "black").decode()))))
    png = Image.open(io.BytesIO(bot.make_qr(URL, "png", "black")))
    assert svg.size == png.size
    a, b = svg.convert("RGBA").getchannel("A"), png.getchannel("A")
    differ = sum(1 for x, y in zip(a.tobytes(), b.tobytes()) if abs(x - y) > 128)
    assert differ < svg.width * svg.height * 0.001


def test_every_unwarned_color_pair_scans():
    """ZXing reads every pair the bot does not warn about, in every style.
    (OpenCV alone misses blue on yellow on the tiniest codes, so it is checked on blue on white above.)"""
    for fg, *_ in bot.COLORS:
        for bg, *_ in bot.COLORS:
            if fg == bg or bot.readability_note(fg, bg):
                continue
            for text in TEXTS:
                for dots in bot.DOTS:
                    assert _scan(bot.make_qr(text, "jpg", fg, bg, dots))[1] == [text], (fg, bg, text, dots)


def test_multipart_encodes_nested_params(monkeypatch):
    sent = {}

    class Resp:
        def json(self):
            return {"ok": True, "result": True}

    def fake_post(url, data=None, files=None, json=None, timeout=None):
        sent.update(url=url, data=data, files=files, json=json)
        return Resp()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    bot.Telegram("1:x", base="http://t").call("sendDocument", files={"document": ("a", b"b", "c")},
                                              chat_id=5, reply_parameters={"message_id": 1})
    assert sent["url"] == "http://t/bot1:x/sendDocument"
    assert json.loads(sent["data"]["reply_parameters"]) == {"message_id": 1}


@pytest.mark.parametrize("fg, bg, warned", [
    ("black", "white", False),
    ("blue", "yellow", False),
    ("white", "black", True),   # light on dark
    ("brown", "purple", True),  # almost the same
    ("purple", "brown", True),
    ("purple", "red", True),    # looks different, but the same gray for a scanner
    ("blue", "orange", True),
])
def test_readability_warning_for_jpg(fg, bg, warned):
    caption = run(tap({"kind": "t", "fmt": "jpg", "dots": "sq", "fg": fg, "bg": bg})).calls[-1]["caption"]
    assert ("⚠️" in caption) is warned


@pytest.mark.parametrize("fmt", ["png", "svg"])
def test_readability_warning_for_light_transparent(fmt):
    s = {"kind": "t", "fmt": fmt, "dots": "sq"}
    assert "⚠️" in run(tap({**s, "fg": "white"})).calls[-1]["caption"]
    assert "⚠️" not in run(tap({**s, "fg": "black"})).calls[-1]["caption"]


def test_every_unwarned_combination_is_dark_on_light():
    for fg, *_ in bot.COLORS:
        for bg, *_ in bot.COLORS:
            if fg != bg and bot.readability_note(fg, bg) is None:
                assert bot._luminance(bot.COLOR[fg][2]) < bot._luminance(bot.COLOR[bg][2])
