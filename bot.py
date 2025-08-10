
#!/usr/bin/env python3
"""
Telegram downloader bot using yt-dlp.

Features:
- Accepts a URL and attempts to download the best video/audio.
- Progress updates to the user (periodic).
- Concurrent downloads limited by a semaphore.
- Temporary files cleaned up after sending.
- Uses environment variables for config (BOT_TOKEN, YTDLP_COOKIES).
- Includes a Flask web server to keep the service alive on Render.
"""

import os
import asyncio
import tempfile
import shutil
import logging
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from flask import Flask

import yt_dlp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------------
# Configuration
# -------------------------
# BOT_TOKEN is read from environment variables in main()
YTDLP_COOKIES = os.environ.get("YTDLP_COOKIES")  # optional cookies.txt path
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 2))
PROGRESS_UPDATE_INTERVAL = float(os.environ.get("PROGRESS_UPDATE_INTERVAL", 2.5))
LOCAL_MAX_BYTES = None  # e.g. 50 * 1024 * 1024 for 50MB

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------
# Globals
# -------------------------
_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
_thread_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)

# -------------------------
# Utility: yt-dlp progress -> update message
# -------------------------
class DownloadContext:
    def __init__(self, update, context, temp_dir):
        self.update = update
        self.context = context
        self.temp_dir = temp_dir
        self.last_update_ts = 0.0
        self.message_id = None
        self.chat_id = update.effective_chat.id
        self.status = "starting"
        self.info = {}
        self.err = None

    async def send_initial(self, text="Starting download..."):
        sent = await self.update.message.reply_text(text)
        self.message_id = sent.message_id

    async def periodic_update(self):
        now = time.time()
        if now - self.last_update_ts < PROGRESS_UPDATE_INTERVAL:
            return
        self.last_update_ts = now
        text = self._build_status_text()
        try:
            await self.context.bot.edit_message_text(
                text=text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    def _build_status_text(self):
        s = f"*Status:* {self.status}\n"
        title = self.info.get("title")
        if title:
            s += f"*Title:* {title}\n"
        downloaded = self.info.get("_downloaded_bytes")
        total = self.info.get("filesize") or self.info.get("filesize_approx") or self.info.get("total_bytes")
        if downloaded is not None and total:
            try:
                pct = downloaded / total * 100
                s += f"*Progress:* {pct:4.1f}% ({downloaded/1024/1024:.2f} MiB of {total/1024/1024:.2f} MiB)\n"
            except Exception:
                pass
        elif downloaded is not None:
            s += f"*Downloaded:* {downloaded/1024/1024:.2f} MiB\n"
        else:
            s += "Downloading...\n"
        if self.err:
            s += f"\n⚠️ Error: {self.err}\n"
        return s

def make_progress_hook(ctx: DownloadContext, loop: asyncio.AbstractEventLoop):
    def hook(d):
        try:
            if d.get("status") == "downloading":
                ctx.status = "downloading"
                ctx.info["_downloaded_bytes"] = d.get("downloaded_bytes", d.get("fragment_index"))
                ctx.info["total_bytes"] = d.get("total_bytes") or d.get("total_bytes_estimate")
                ctx.info["speed"] = d.get("speed")
                asyncio.run_coroutine_threadsafe(ctx.periodic_update(), loop)
            elif d.get("status") == "finished":
                ctx.status = "merging" if d.get("filename", "").endswith(".part") else "finished"
                ctx.info["title"] = d.get("filename") or ctx.info.get("title")
                asyncio.run_coroutine_threadsafe(ctx.periodic_update(), loop)
            elif d.get("status") == "error":
                ctx.err = d.get("error")
                asyncio.run_coroutine_threadsafe(ctx.periodic_update(), loop)
        except Exception as e:
            logger.exception("Error in progress hook: %s", e)
    return hook

# -------------------------
# Download logic (blocking calls inside threadpool)
# -------------------------
def run_yt_dlp_blocking(url: str, outtmpl: str, ctx: DownloadContext, loop: asyncio.AbstractEventLoop, extra_opts=None):
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "progress_hooks": [make_progress_hook(ctx, loop)],
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": shutil.which("ffmpeg") or None,
        "nocheckcertificate": True,
    }
    if extra_opts:
        ydl_opts.update(extra_opts)

    if YTDLP_COOKIES and Path(YTDLP_COOKIES).exists():
        ydl_opts["cookiefile"] = YTDLP_COOKIES

    logger.info("Starting yt-dlp with options: %s", {k: v for k, v in ydl_opts.items() if k != "progress_hooks"})
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return info, filename

# -------------------------
# Handlers
# -------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Send me a link from YouTube, Instagram, Facebook, TikTok, Twitter/X, etc. "
        "I'll try to download the video and send it back.\n\n"
        "Commands:\n"
        "/start - show this message\n"
        "/help - usage & tips\n"
        "/about - bot info\n"
    )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "Usage tips:\n"
        "- Paste a single URL in a message.\n"
        "- If a site requires login (private IG), provide a cookies.txt file path via the YTDLP_COOKIES env var.\n"
        "- Large files might not be deliverable by Telegram due to API limits; the bot will notify you.\n"
        "- This bot is for personal/public content only. Don't use to infringe copyright."
    )
    await update.message.reply_text(txt)

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Downloader bot built with yt-dlp. Maintainer: you. Use responsibly!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text or not (text.startswith("http://") or text.startswith("https://")):
        await update.message.reply_text("Please send a valid URL (starting with http:// or https://).")
        return

    await update.message.reply_text("Queued your request — waiting for an available downloader slot...")
    async with _download_semaphore:
        await _process_download(update, context, text)

async def _process_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    chat_id = update.effective_chat.id
    temp_dir = Path(tempfile.mkdtemp(prefix="tgdl_"))
    ctx = DownloadContext(update, context, temp_dir)
    await ctx.send_initial("Queued. Preparing download...")

    loop = asyncio.get_event_loop()
    outtmpl = str(temp_dir / "%(title).200s.%(ext)s")
    try:
        ctx.status = "preparing"
        fut = loop.run_in_executor(_thread_pool, run_yt_dlp_blocking, url, outtmpl, ctx, loop, None)
        info, filename = await fut

        ctx.status = "finished"
        ctx.info["title"] = info.get("title") or Path(filename).stem
        await ctx.periodic_update()

        try:
            size = Path(filename).stat().st_size
        except Exception:
            size = None

        if LOCAL_MAX_BYTES and size and size > LOCAL_MAX_BYTES:
            await context.bot.send_message(
                chat_id=chat_id,
                text=("Downloaded file is too large to send via this bot."),
            )
            return

        caption = f"{ctx.info.get('title', '')}\nSource: {url}"
        try:
            if filename.lower().endswith((".mp4", ".mkv", ".mov", ".webm")):
                with open(filename, "rb") as f:
                    await context.bot.send_video(chat_id=chat_id, video=f, caption=caption)
            else:
                with open(filename, "rb") as f:
                    await context.bot.send_document(chat_id=chat_id, document=f, caption=caption)
        except Exception as e_send:
            logger.exception("Failed to send file: %s", e_send)
            try:
                with open(filename, "rb") as f:
                    await context.bot.send_document(chat_id=chat_id, document=f, caption=caption)
            except Exception as e2:
                logger.exception("Sending as document also failed: %s", e2)
                await context.bot.send_message(chat_id=chat_id, text=f"Failed to send file: {e2}")
    except yt_dlp.utils.DownloadError as de:
        logger.exception("Download error: %s", de)
        await context.bot.send_message(chat_id=chat_id, text=f"Download error: {de}")
    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        await context.bot.send_message(chat_id=chat_id, text=f"Unexpected error: {e}")
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass

# -------------------------
# Web Server to Keep Render Alive
# -------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive."

def run_web_server():
    # Runs the Flask app on the port Render provides
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# -------------------------
# Main
# -------------------------
def main():
    # --- Start the web server in a background thread ---
    web_thread = Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    # ---------------------------------------------------

    # Get the bot token from environment variables
    token = os.environ.get("BOT_TOKEN")
    if not token:
        logger.error("Bot token not set. Please set BOT_TOKEN environment variable.")
        raise SystemExit("BOT_TOKEN is required. Set it in your deployment environment.")

    # Set higher timeouts for uploads
    application = (
        Application.builder()
        .token(token)
        .connect_timeout(60)
        .read_timeout(120)
        .build()
    )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("about", about_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    application.run_polling()

if __name__ == "__main__":
    main()