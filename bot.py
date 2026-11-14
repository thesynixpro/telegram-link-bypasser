"""
bot.py
======

Telegram bot entry point.

It accepts:
  * /start  -> usage information and legal disclaimer
  * /bypass <url> -> explicit bypass command
  * any plain message containing an arolinks.com or vplink.in URL

The bot uses long polling, which is the simplest and most reliable option for
a Railway worker service.  The token is read from TELEGRAM_BOT_TOKEN.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bypasser import (
    BypassError,
    bypass,
    get_supported_domain,
    normalize_url,
)

# Load .env for local development.  Railway injects real environment variables.
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
# httpx logs every polling request at INFO; that is too noisy.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Matches full URLs and bare domains such as "arolinks.com/abc".
_URL_RE = re.compile(
    r"""(?xi)
    \bhttps?://[^\s<>"']+
    |
    (?<![\w@.-])
    (?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+
    (?:[a-z]{2,63})
    (?::\d{2,5})?
    (?:/[^\s<>"']*)?
    """
)

START_TEXT = (
    "Hi! I can bypass ad-gateway links from:\n"
    "  • arolinks.com\n"
    "  • vplink.in\n\n"
    "How to use:\n"
    "  1. Send me a link directly, or\n"
    "  2. Use /bypass <url>\n\n"
    "Example:\n"
    "/bypass https://arolinks.com/abc123\n\n"
    "⚠️ Legal notice: bypassing a URL shortener may violate that service's "
    "Terms of Service. Use this bot only for links you are authorized to "
    "access. The bot is provided as-is, without warranty. Shortener sites "
    "change often, so some links may fail."
)

USAGE_TEXT = (
    "Usage: /bypass <url>\n"
    "Example: /bypass https://vplink.in/abc123"
)


def _extract_supported_url(text: str) -> str | None:
    """Find the first supported shortener link in a free-form message."""
    if not text:
        return None

    for match in _URL_RE.finditer(text):
        raw = match.group(0).rstrip(".,;:!?)]}\"'")
        try:
            url = normalize_url(raw)
        except BypassError:
            continue
        if get_supported_domain(url):
            return url
    return None


def _looks_like_url(text: str) -> bool:
    return bool(_URL_RE.search(text or ""))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start."""
    message = update.effective_message
    if message:
        await message.reply_text(START_TEXT)


async def bypass_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /bypass <url>."""
    message = update.effective_message
    if not message:
        return

    if not context.args:
        await message.reply_text(USAGE_TEXT)
        return

    # context.args splits on whitespace, so a URL containing spaces would be
    # unusual, but joining makes the message forgiving.
    await _handle_bypass(message, context, " ".join(context.args))


async def handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle plain messages that contain URLs."""
    message = update.effective_message
    if not message or not message.text:
        return

    url = _extract_supported_url(message.text)
    if url:
        await _handle_bypass(message, context, url)
        return

    # Do not reply to ordinary chat.  But if the user clearly sent a URL for a
    # domain we do not support, explain why nothing happened.
    if _looks_like_url(message.text):
        await message.reply_text(
            "❌ Unsupported domain. I can only bypass arolinks.com and "
            "vplink.in links."
        )


async def _handle_bypass(message, context: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    """Validate, run the bypass and report the result."""
    try:
        url = normalize_url(raw)
    except BypassError as exc:
        await message.reply_text(f"❌ {exc}")
        return

    if not get_supported_domain(url):
        await message.reply_text(
            "❌ Unsupported domain. I can only bypass arolinks.com and "
            "vplink.in links."
        )
        return

    status = await message.reply_text("⏳ Bypassing…")
    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.TYPING
        )
    except Exception:
        # A failed "typing…" indicator must not abort the actual bypass.
        logger.debug("Could not send chat action", exc_info=True)

    try:
        destination = await bypass(url)
    except BypassError as exc:
        await status.edit_text(f"❌ {exc}")
        return
    except asyncio.TimeoutError:
        logger.warning("Bypass timed out for %s", url)
        await status.edit_text("❌ The request timed out. Please try again later.")
        return
    except Exception:
        logger.exception("Unexpected error while bypassing %s", url)
        await status.edit_text(
            "❌ An unexpected error occurred. Please try again later."
        )
        return

    await status.edit_text(f"✅ Destination:\n{destination}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log exceptions that escape a handler instead of crashing polling."""
    logger.error("Unhandled exception while processing an update", exc_info=context.error)


def main() -> None:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set. Add it to .env locally or to the "
            "Railway service variables."
        )
        sys.exit(1)

    application = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("bypass", bypass_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_error_handler(on_error)

    logger.info("Bot is starting with long polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
