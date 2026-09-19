"""
bot.py — Telegram Bot for AutoRepost Pipeline

Receives video links via Telegram, triggers download -> caption -> upload pipeline,
and reports success/failure back to user.

Uses python-telegram-bot v20+ async style.
"""

import re
import os
import atexit
import asyncio
import logging
import traceback
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Import our pipeline modules
from downloader import download_video
from caption import get_caption, load_config, save_config
from uploader import (
    upload_video,
    list_accounts,
    normalize_api_base_url,
    ZernioError,
    PLACEHOLDER_API_KEYS,
)

# Display names for the per-platform status lines in Telegram replies
PLATFORM_LABELS = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "facebook": "Facebook",
    "youtube": "YouTube",
}

# ------------------------------------------------------------------------------
# Setup Logging — both console and file
# ------------------------------------------------------------------------------

# Ensure logs directory exists
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "pipeline.log")

# Configure logging
logger = logging.getLogger("AutoRepost")
logger.setLevel(logging.INFO)

# Formatter
formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

# File handler
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

# Add handlers if not already added (avoid duplicate logs on reload)
if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

# Also set up telegram library logging to avoid spam
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ------------------------------------------------------------------------------
# URL Detection Regex
# ------------------------------------------------------------------------------

URL_REGEX = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def extract_urls(text: str) -> list:
    """
    Extract all URLs from a text message.

    Args:
        text: Message text

    Returns:
        list: List of URLs found
    """
    if not text:
        return []
    return URL_REGEX.findall(text)


# ------------------------------------------------------------------------------
# Config Loading
# ------------------------------------------------------------------------------

def get_telegram_token() -> str:
    """
    Load telegram_token from config.json.

    Returns:
        str: Telegram bot token

    Raises:
        ValueError: If token missing or placeholder
    """
    try:
        config = load_config()
        token = config.get("telegram_token", "")
        if not token or token == "YOUR_TELEGRAM_BOT_TOKEN" or token.strip() == "":
            raise ValueError(
                "Telegram token not configured. Set it in config.json or via dashboard."
            )
        return token
    except FileNotFoundError:
        raise ValueError(
            "config.json not found. Copy config.example.json to config.json and fill tokens."
        )


# ------------------------------------------------------------------------------
# Telegram Command Handlers
# ------------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    message = (
        "👋 Send me any video link and I'll repost it to Instagram & TikTok automatically!\n\n"
        "Supported platforms: YouTube, Instagram, TikTok, Facebook Reels, and 1000+ sites via yt-dlp.\n\n"
        "Use /help to see all commands."
    )
    await update.message.reply_text(message)
    logger.info(f"/start command from user {update.effective_user.id}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    await update.message.reply_text("✅ AutoRepost Pipeline is running!")
    logger.info(f"/status command from user {update.effective_user.id}")


async def captions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /captions command — list all captions numbered."""
    try:
        config = load_config()
        captions = config.get("captions", [])

        if not captions:
            await update.message.reply_text("📝 No captions in pool. Use /addcaption to add one.")
            return

        # Build numbered list
        lines = ["📝 Captions in pool:\n"]
        for idx, cap in enumerate(captions, start=1):
            # Truncate long captions for display
            display_cap = cap if len(cap) <= 100 else cap[:97] + "..."
            lines.append(f"{idx}. {display_cap}")

        full_message = "\n".join(lines)

        # Telegram message limit is 4096 chars, split if needed
        if len(full_message) > 4000:
            # Send in chunks
            chunk = ""
            for line in lines:
                if len(chunk) + len(line) + 1 > 4000:
                    await update.message.reply_text(chunk)
                    chunk = line + "\n"
                else:
                    chunk += line + "\n"
            if chunk:
                await update.message.reply_text(chunk)
        else:
            await update.message.reply_text(full_message)

    except Exception as e:
        logger.error(f"Error in /captions: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(f"❌ Error loading captions: {e}")


async def addcaption_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /addcaption [text] — add caption to config.json."""
    try:
        # Get caption text from args
        if context.args:
            new_caption = " ".join(context.args).strip()
        else:
            # Try to get from message text after command
            text = update.message.text or ""
            # Remove "/addcaption" prefix
            parts = text.split(" ", 1)
            if len(parts) > 1:
                new_caption = parts[1].strip()
            else:
                new_caption = ""

        if not new_caption:
            await update.message.reply_text(
                "Usage: /addcaption [your caption text]\nExample: /addcaption Follow for more 🔥"
            )
            return

        # Load config, append, save
        config = load_config()
        if "captions" not in config or not isinstance(config["captions"], list):
            config["captions"] = []

        config["captions"].append(new_caption)
        save_config(config)

        await update.message.reply_text(f"✅ Caption added: \"{new_caption}\"")
        logger.info(f"Caption added via Telegram: {new_caption}")

    except Exception as e:
        logger.error(f"Error in /addcaption: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(f"❌ Error adding caption: {e}")


async def thumbnail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /thumbnail — show filename of current default thumbnail."""
    try:
        config = load_config()
        thumb = config.get("default_thumbnail", "Not set")
        await update.message.reply_text(f"🖼️ Current default thumbnail: {thumb}")
    except Exception as e:
        logger.error(f"Error in /thumbnail: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(f"❌ Error: {e}")


def _describe_zernio_accounts() -> str:
    """Blocking helper for /accounts: list connected Zernio accounts (runs in a thread)."""
    config = load_config()
    api_key = str(config.get("zernio_api_key") or "").strip()
    if api_key in PLACEHOLDER_API_KEYS:
        return "❌ Zernio API key is not configured. Set it in the dashboard → Settings."

    base_url = normalize_api_base_url(config.get("zernio_api_base_url"))
    accounts = list_accounts(api_key, base_url)

    post_to = [str(p).lower().strip() for p in (config.get("post_to") or ["instagram", "tiktok"])]
    overrides = config.get("zernio_account_ids") or {}
    if not isinstance(overrides, dict):
        overrides = {}

    lines = ["🔗 Zernio accounts:"]
    if not accounts:
        lines.append("  (none connected — connect Instagram & TikTok at zernio.com)")
    for account in accounts:
        platform = str(account.get("platform") or "?").lower()
        username = account.get("username") or "?"
        state = "active" if account.get("isActive", True) else "INACTIVE — reconnect"
        lines.append(f"  • {PLATFORM_LABELS.get(platform, platform.title())} @{username} — {state}\n    id: {account.get('_id', '?')}")

    lines.append("")
    lines.append("📤 post_to check:")
    for platform in post_to:
        override = str(overrides.get(platform) or "").strip()
        active = [a for a in accounts if str(a.get("platform", "")).lower() == platform and a.get("isActive", True)]
        if override:
            lines.append(f"  {PLATFORM_LABELS.get(platform, platform.title())}: pinned to {override}")
        elif active:
            lines.append(f"  {PLATFORM_LABELS.get(platform, platform.title())}: ✅ @{active[0].get('username', '?')}")
        else:
            lines.append(f"  {PLATFORM_LABELS.get(platform, platform.title())}: ❌ not connected")
    lines.append("")
    lines.append(f"API: {base_url}")
    return "\n".join(lines)


async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /accounts — show the social accounts connected to Zernio."""
    try:
        text = await asyncio.to_thread(_describe_zernio_accounts)
    except ZernioError as e:
        text = f"❌ Zernio error: {e}"
    except Exception as e:
        logger.error(f"Error in /accounts: {e}\n{traceback.format_exc()}")
        text = f"❌ Error: {e}"
    await update.message.reply_text(text[:4000], disable_web_page_preview=True)
    logger.info(f"/accounts command from user {update.effective_user.id}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — list all commands."""
    help_text = (
        "📖 *AutoRepost Pipeline — Commands:*\n\n"
        "/start — Welcome message\n"
        "/status — Check if pipeline is running\n"
        "/accounts — Show Instagram/TikTok accounts connected to Zernio\n"
        "/captions — List all captions in pool\n"
        "/addcaption [text] — Add new caption to pool\n"
        "/thumbnail — Show current default thumbnail\n"
        "/help — Show this help message\n\n"
        "📎 *How to use:*\n"
        "Just send any video link (YouTube, TikTok, Instagram, Facebook, etc.) and I'll download and repost it automatically!"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


# ------------------------------------------------------------------------------
# Main Pipeline Handler — triggered on any message containing URL
# ------------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle incoming messages, detect URLs, and trigger pipeline.
    """
    message_text = update.message.text or ""
    urls = extract_urls(message_text)

    if not urls:
        # No URL found, ignore (or optionally reply)
        return

    # Use first URL found
    url = urls[0]
    user_id = update.effective_user.id
    username = update.effective_user.username or "unknown"

    logger.info(f"Received URL from user {user_id} (@{username}): {url}")

    # Send initial acknowledgment
    try:
        status_msg = await update.message.reply_text("⏳ Downloading your video...")
    except Exception as e:
        logger.error(f"Failed to send initial reply: {e}")
        return

    video_path = None
    final_caption = None

    try:
        # Step 1: Download video
        try:
            await status_msg.edit_text("⏳ Downloading your video...")
        except:
            pass

        logger.info(f"Downloading video: {url}")
        download_result = await asyncio.to_thread(download_video, url)

        video_path = download_result.get("video_path")
        original_caption = download_result.get("original_caption")
        title = download_result.get("title", "Unknown")

        logger.info(f"Downloaded: {video_path}, title: {title}, has_caption: {bool(original_caption)}")

        # Step 2: Get caption
        try:
            await status_msg.edit_text("📝 Processing caption...")
        except:
            pass

        final_caption = get_caption(original_caption)
        logger.info(f"Caption selected: {final_caption[:100]}...")

        # Step 3: Get thumbnail path + target platforms from config
        target_names = "Instagram & TikTok"
        try:
            config = load_config()
            thumbnail_path = config.get("default_thumbnail", "thumbnails/default.jpg")
            post_to = config.get("post_to") or []
            if isinstance(post_to, list) and post_to:
                target_names = " & ".join(PLATFORM_LABELS.get(str(p).lower(), str(p).title()) for p in post_to)
        except Exception as e:
            logger.warning(f"Could not load thumbnail from config: {e}, using default")
            thumbnail_path = "thumbnails/default.jpg"

        # Step 4: Upload video (presign -> PUT -> POST /v1/posts, see uploader.py)
        try:
            await status_msg.edit_text(f"🚀 Publishing to {target_names} via Zernio...")
        except:
            pass

        logger.info(f"Uploading video {video_path} with caption")
        upload_result = await asyncio.to_thread(upload_video, video_path, final_caption, thumbnail_path)

        # Step 5: Delete downloaded file to save space
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
                logger.info(f"Deleted temp file: {video_path}")
            except Exception as e:
                logger.warning(f"Failed to delete temp file {video_path}: {e}")

        # Step 6: Report result
        success = upload_result.get("success", False)
        message_text_result = upload_result.get("message", "Unknown result")

        # Per-platform lines (the uploader always includes instagram + tiktok,
        # plus anything else listed in post_to).
        platforms = upload_result.get("platforms") or ["instagram", "tiktok"]
        platform_lines = "\n".join(
            f"{PLATFORM_LABELS.get(p, p.title())}: {upload_result.get(p, 'unknown')}" for p in platforms
        )
        posted_to = [
            PLATFORM_LABELS.get(p, p.title())
            for p in platforms
            if str(upload_result.get(p, "")).startswith("posted")
        ]

        # Log entry: timestamp | url | caption_used | success/fail
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Escape pipe in caption for log parsing
        safe_caption = final_caption.replace("|", "/").replace("\n", " ")[:200]
        log_entry = f"{timestamp} | {url} | {safe_caption} | {'SUCCESS' if success else 'FAIL'} | {message_text_result}"
        logger.info(log_entry)

        if success:
            headline = "✅ Posted to " + (" & ".join(posted_to) if posted_to else "Zernio") + "!"
            if len(posted_to) < len(platforms):
                headline = "⚠️ Partially posted (" + ", ".join(posted_to) + ")"
            reply_text = (
                f"{headline}\n"
                f"📝 Caption: {final_caption}\n\n"
                f"📊 {message_text_result}\n"
                f"{platform_lines}"
            )
        else:
            reply_text = (
                f"❌ Upload failed:\n"
                f"{message_text_result}\n"
                f"{platform_lines}\n\n"
                f"📝 Caption was: {final_caption}"
            )
        # Telegram caps messages at 4096 characters.
        if len(reply_text) > 4000:
            reply_text = reply_text[:3990] + "…"
        try:
            await status_msg.edit_text(reply_text, disable_web_page_preview=True)
        except Exception:
            await update.message.reply_text(reply_text, disable_web_page_preview=True)

    except ValueError as ve:
        # Known errors (invalid URL, too large, unavailable)
        error_msg = str(ve)
        logger.error(f"ValueError for URL {url}: {error_msg}")

        # Clean up file if exists
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except:
                pass

        # Log failure
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} | {url} | N/A | FAIL | {error_msg}"
        logger.info(log_entry)

        try:
            await status_msg.edit_text(f"❌ Error: {error_msg}")
        except:
            await update.message.reply_text(f"❌ Error: {error_msg}")

    except Exception as e:
        # Unexpected errors
        error_msg = str(e)[:500]
        logger.error(f"Unexpected error for URL {url}: {e}\n{traceback.format_exc()}")

        # Clean up
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except:
                pass

        # Log failure
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} | {url} | {final_caption or 'N/A'} | FAIL | {error_msg}"
        logger.info(log_entry)

        try:
            await status_msg.edit_text(f"❌ Error: {error_msg}")
        except:
            await update.message.reply_text(f"❌ Error: {error_msg}")


# ------------------------------------------------------------------------------
# Main Bot Runner
# ------------------------------------------------------------------------------

PID_FILE = os.path.join(LOG_DIR, "bot.pid")


def write_pid_file() -> None:
    """Record our PID so the dashboard can show a real 'bot running' status."""
    try:
        with open(PID_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except Exception as e:
        logger.warning(f"Could not write PID file {PID_FILE}: {e}")


def remove_pid_file() -> None:
    """Remove the PID file on clean shutdown (only if it is ours)."""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r", encoding="utf-8") as fh:
                if fh.read().strip() == str(os.getpid()):
                    os.remove(PID_FILE)
    except Exception:
        pass


def main() -> None:
    """Start the Telegram bot."""
    print("🚀 Starting AutoRepost Telegram Bot...")

    # Load token
    try:
        token = get_telegram_token()
    except ValueError as e:
        print(f"❌ {e}")
        print("Run: cp config.example.json config.json and fill in your tokens")
        return

    # Create Application
    application = Application.builder().token(token).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("accounts", accounts_command))
    application.add_handler(CommandHandler("captions", captions_command))
    application.add_handler(CommandHandler("addcaption", addcaption_command))
    application.add_handler(CommandHandler("thumbnail", thumbnail_command))
    application.add_handler(CommandHandler("help", help_command))

    # Register message handler for URLs (any text message)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot is running! Press Ctrl+C to stop.")
    logger.info("Bot started")

    # Track our PID for the dashboard's health check
    write_pid_file()
    atexit.register(remove_pid_file)

    # Run polling (blocking)
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        remove_pid_file()


if __name__ == "__main__":
    main()
