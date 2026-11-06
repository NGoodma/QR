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


def test_not_a_link_and_start():
    assert run(link_message("привет")).calls[0]["text"] == bot.NOT_A_LINK
    assert run(link_message("/start")).calls[0]["text"] == bot.START_TEXT


def test_format_then_color_buttons_black_and_white_first():
    tg = run(tap("f:png"))
    edit = tg.calls[-1]
    assert edit["method"] == "editMessageText" and edit["text"] == "Выберите цвет"
    rows = buttons(edit["reply_markup"])
    assert rows[0] == ["⚫ Чёрный", "⚪ Белый"]
    assert sum(len(r) for r in rows) == len(bot.COLORS)


def test_png_is_sent_right_after_color_with_transparent_background():
    tg = run(tap("c:png:red"))
    assert tg.methods() == ["answerCallbackQuery", "editMessageText", "sendDocument"]
    doc = tg.calls[-1]
    name, data, mime = doc["files"]["document"]
    assert (name, mime) == ("qr.png", "image/png")
    assert doc["reply_parameters"] == {"message_id": 10}
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG" and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0  # corner is background: transparent
    assert tg.calls[1]["text"] == "Готово: PNG, красный"


def test_jpg_asks_for_background_without_the_same_color():
    tg = run(tap("c:jpg:black"))
    edit = tg.calls[-1]
    assert edit["text"] == "Выберите цвет фона"
    rows = buttons(edit["reply_markup"])
    assert rows[0] == ["⚪ Белый"]
    assert "⚫ Чёрный" not in sum(rows, [])
    assert all(b["callback_data"].startswith("b:black:") for row in edit["reply_markup"]["inline_keyboard"] for b in row)


def test_jpg_is_sent_after_background():
    tg = run(tap("b:blue:yellow"))
    name, data, mime = tg.calls[-1]["files"]["document"]
    assert (name, mime) == ("qr.jpg", "image/jpeg")
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    r, g, b = img.getpixel((2, 2))
    assert r > 230 and g > 200 and b < 90  # yellow background
    assert tg.calls[1]["text"] == "Готово: JPG, синий на жёлтом фоне"


@pytest.mark.parametrize("data", ["b:red:red", "c:gif:red", "f:bmp", "zzz", "b:red:pink"])
def test_bad_callbacks_send_nothing(data):
    assert "sendDocument" not in run(tap(data)).methods()


def test_lost_link_shows_alert():
    tg = run(tap("c:png:red", source_text="не ссылка"))
    [c] = tg.calls
    assert c["method"] == "answerCallbackQuery" and c["show_alert"] and c["text"] == bot.NO_LINK


def test_callback_data_fits_telegram_limit():
    for prefix in ["c:jpg", "c:png", "b:purple"]:
        for row in bot.color_keyboard(prefix)["inline_keyboard"]:
            for b in row:
                assert len(b["callback_data"].encode()) <= 64


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
])
def test_readability_warning_for_jpg(fg, bg, warned):
    caption = run(tap(f"b:{fg}:{bg}")).calls[-1]["caption"]
    assert ("⚠️" in caption) is warned


def test_readability_warning_for_light_png():
    assert "⚠️" in run(tap("c:png:white")).calls[-1]["caption"]
    assert "⚠️" not in run(tap("c:png:black")).calls[-1]["caption"]


def test_every_unwarned_combination_is_dark_on_light():
    for fg, *_ in bot.COLORS:
        for bg, *_ in bot.COLORS:
            if fg != bg and bot.readability_note(fg, bg) is None:
                assert bot._luminance(bot.COLOR[fg][2]) < bot._luminance(bot.COLOR[bg][2])
