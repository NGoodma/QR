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
    assert buttons(c["reply_markup"]) == [["JPG", "PNG"]]
    assert c["reply_parameters"] == {"message_id": 10}
    assert c["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "q:t:jpg::::"


def test_any_text_is_accepted():
    assert run(link_message("ты пидр")).calls[0]["text"] == "Выберите формат"
    assert run(link_message("/start")).calls[0]["text"] == bot.START_TEXT


def test_too_long_text_is_refused():
    assert run(link_message("я" * 1300)).calls[0]["text"] == bot.TOO_LONG
    assert run(link_message("я" * 1000)).calls[0]["text"] == "Выберите формат"


def test_wifi_looking_text_asks_what_to_do():
    [c] = run(link_message(WIFI)).calls
    assert buttons(c["reply_markup"]) == [["📶 Код для подключения к Wi-Fi"], ["📝 Просто текст"]]
    assert [r[0]["callback_data"] for r in c["reply_markup"]["inline_keyboard"]] == ["q:w:::::", "q:t:::::"]
    assert run(tap("q:w:::::", WIFI)).calls[-1]["text"] == "Выберите формат"


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


def test_steps_in_order():
    s = {"kind": "t"}
    assert run(tap(q(s))).calls[-1]["text"] == "Выберите формат"
    edit = run(tap(q({**s, "fmt": "png"}))).calls[-1]
    assert edit["text"] == "Выберите стиль точек"
    assert buttons(edit["reply_markup"]) == [["⬛ Квадратные", "🔲 Скруглённые", "⚫ Круглые"]]
    edit = run(tap(q({**s, "fmt": "png", "dots": "ci"}))).calls[-1]
    assert edit["text"] == "Выберите цвет"
    rows = buttons(edit["reply_markup"])
    assert rows[0] == ["⚫ Чёрный", "⚪ Белый"]
    assert sum(len(r) for r in rows) == len(bot.COLORS)


def test_png_is_sent_right_after_color_with_transparent_background():
    tg = run(tap({"kind": "t", "fmt": "png", "dots": "rd", "fg": "red"}))
    assert tg.methods() == ["answerCallbackQuery", "editMessageText", "sendDocument"]
    doc = tg.calls[-1]
    name, data, mime = doc["files"]["document"]
    assert (name, mime) == ("qr.png", "image/png")
    assert doc["reply_parameters"] == {"message_id": 10}
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG" and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0  # corner is background: transparent
    assert tg.calls[1]["text"] == "Готово: PNG, скруглённые точки, красный"


def test_jpg_asks_for_background_without_the_same_color():
    edit = run(tap({"kind": "t", "fmt": "jpg", "dots": "sq", "fg": "black"})).calls[-1]
    assert edit["text"] == "Выберите цвет фона"
    rows = buttons(edit["reply_markup"])
    assert rows[0] == ["⚪ Белый"]
    assert "⚫ Чёрный" not in sum(rows, [])
    data = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
    assert all(d.startswith("q:t:jpg:sq:black:") for d in data)


def test_jpg_asks_about_corners_then_sends():
    s = {"kind": "t", "fmt": "jpg", "dots": "sq", "fg": "blue", "bg": "yellow"}
    edit = run(tap(s)).calls[-1]
    assert edit["text"] == "Углы фона" and buttons(edit["reply_markup"]) == [["⬜ Прямые", "▢ Скруглённые"]]

    tg = run(tap({**s, "corners": "sq"}))
    name, data, mime = tg.calls[-1]["files"]["document"]
    assert (name, mime) == ("qr.jpg", "image/jpeg")
    img = Image.open(io.BytesIO(data))
    r, g, b = img.getpixel((2, 2))
    assert r > 230 and g > 200 and b < 90  # yellow up to the very corner
    assert tg.calls[1]["text"] == "Готово: JPG, квадратные точки, синий на жёлтом фоне"

    tg = run(tap({**s, "corners": "rd"}))
    img = Image.open(io.BytesIO(tg.calls[-1]["files"]["document"][1]))
    assert min(img.getpixel((2, 2))) > 240  # cut-off corner is white
    r, g, b = img.getpixel((img.width // 2, 5))
    assert r > 230 and g > 200 and b < 90  # the edge between corners is still yellow
    assert tg.calls[1]["text"] == "Готово: JPG, квадратные точки, синий на жёлтом фоне, скруглённые углы"


def test_white_background_skips_the_corner_question():
    tg = run(tap({"kind": "t", "fmt": "jpg", "dots": "ci", "fg": "black", "bg": "white"}))
    assert tg.methods()[-1] == "sendDocument"


def test_wifi_code_and_caption():
    tg = run(tap({"kind": "w", "fmt": "png", "dots": "sq", "fg": "black"}, WIFI))
    assert tg.calls[-1]["caption"] == "Вот QR-код для подключения к сети «MyHome»"


@pytest.mark.parametrize("data", [
    "q:t:jpg:sq:red:red:",    # same color twice
    "q:t:gif::::", "q:t:jpg:star:::", "q:x:::::", "q:t:jpg:sq:pink::",
    "q:t:jpg", "zzz", "f:jpg", "c:png:red", "b:black:white",  # old buttons from the previous version
])
def test_bad_callbacks_send_nothing(data):
    tg = run(tap(data))
    assert "sendDocument" not in tg.methods() and "editMessageText" not in tg.methods()


def test_lost_text_shows_alert():
    tg = run(tap({"kind": "t", "fmt": "png", "dots": "sq", "fg": "red"}, source_text=""))
    [c] = tg.calls
    assert c["method"] == "answerCallbackQuery" and c["show_alert"] and c["text"] == bot.NO_TEXT
    # a Wi-Fi choice on a text that is not Wi-Fi
    assert run(tap({"kind": "w", "fmt": "png", "dots": "sq", "fg": "red"}, "просто текст")).calls[0]["show_alert"]


def test_callback_data_fits_telegram_limit():
    longest = {"kind": "w", "fmt": "jpg", "dots": "ci", "fg": "purple", "bg": "orange", "corners": "rd"}
    assert len(bot.encode(longest).encode()) <= 64


def test_dense_code_gets_a_warning():
    s = {"kind": "t", "fmt": "png", "dots": "sq", "fg": "black"}
    assert "плотным" in run(tap(s, "я" * 900)).calls[-1]["caption"]
    assert "⚠️" not in run(tap(s, URL)).calls[-1]["caption"]


def _scan(png_or_jpg: bytes):
    cv2 = pytest.importorskip("cv2")
    zxingcpp = pytest.importorskip("zxingcpp")
    np = pytest.importorskip("numpy")
    img = Image.open(io.BytesIO(png_or_jpg))
    if img.mode == "RGBA":  # what the transparent PNG looks like on a white page
        page = Image.new("RGB", img.size, "white")
        page.paste(img, mask=img)
        img = page
    pixels = np.array(img.convert("RGB"))
    return cv2.QRCodeDetector().detectAndDecode(pixels)[0], [r.text for r in zxingcpp.read_barcodes(pixels)]


TEXTS = [URL, "ты пидр", "WIFI:T:WPA;S:MyHome;P:secret123;;", "Длинный текст " * 20]


@pytest.mark.parametrize("dots", list(bot.DOTS))
@pytest.mark.parametrize("fmt, corners", [("png", "sq"), ("jpg", "sq"), ("jpg", "rd")])
@pytest.mark.parametrize("text", TEXTS)
def test_every_style_scans(dots, fmt, corners, text):
    """Decode the real image with two different scanners: OpenCV and ZXing (used in many phone apps)."""
    image = bot.make_qr(text, fmt, "blue", "white" if fmt == "jpg" else None, dots, corners)
    assert _scan(image) == (text, [text])


def test_every_unwarned_color_pair_scans():
    """ZXing reads every pair the bot does not warn about, in every style.
    (OpenCV alone misses blue on yellow on the tiniest codes, so it is checked on blue on white above.)"""
    for fg, *_ in bot.COLORS:
        for bg, *_ in bot.COLORS:
            if fg == bg or bot.readability_note(fg, bg):
                continue
            for text in TEXTS:
                for dots in bot.DOTS:
                    for corners in bot.CORNERS:
                        image = bot.make_qr(text, "jpg", fg, bg, dots, corners)
                        assert _scan(image)[1] == [text], (fg, bg, text, dots, corners)


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
    caption = run(tap({"kind": "t", "fmt": "jpg", "dots": "sq", "fg": fg, "bg": bg, "corners": "sq"})).calls[-1]["caption"]
    assert ("⚠️" in caption) is warned


def test_readability_warning_for_light_png():
    s = {"kind": "t", "fmt": "png", "dots": "sq"}
    assert "⚠️" in run(tap({**s, "fg": "white"})).calls[-1]["caption"]
    assert "⚠️" not in run(tap({**s, "fg": "black"})).calls[-1]["caption"]


def test_every_unwarned_combination_is_dark_on_light():
    for fg, *_ in bot.COLORS:
        for bg, *_ in bot.COLORS:
            if fg != bg and bot.readability_note(fg, bg) is None:
                assert bot._luminance(bot.COLOR[fg][2]) < bot._luminance(bot.COLOR[bg][2])
