import io
import logging
import os
from urllib.parse import urlparse

import qrcode
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def is_valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Отправь мне ссылку (http/https), и я пришлю QR-код."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Просто отправь ссылку, например: https://example.com"
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    if not is_valid_url(text):
        await update.message.reply_text(
            "Это не похоже на ссылку. Отправь URL, начинающийся с http:// или https://"
        )
        return

    image = qrcode.make(text)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)

    await update.message.reply_photo(
        photo=buffer,
        caption="Готово! Вот твой QR-код.",
    )


def main() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Впиши токен в .env: "
            "TELEGRAM_BOT_TOKEN='token_from_botfather'"
        )

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()