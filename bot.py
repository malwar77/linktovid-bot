#!/usr/bin/env python3
"""
Universal Social Media Downloader Bot for Telegram.
Send it any social media link; it downloads the media and sends it back.
Powered by yt-dlp (supports YouTube, TikTok, Instagram, X/Twitter, Facebook,
Reddit, Pinterest, Twitch, SoundCloud, and 1000+ other sites).

Files up to ~50 MB go out through the Bot API; bigger files (up to 2 GB)
are uploaded via MTProto (mtp.py).
"""

import os
import threading
import time
import re
import shutil
import tempfile
import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction, ReactionEmoji
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram import ReactionTypeEmoji

# yt-dlp runs in its own thread pool inside handlers
from yt_dlp import YoutubeDL

import mtp
import music

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MAX_SEND = 49 * 1024 * 1024        # Bot API path (50 MB limit)
MAX_TOTAL = 1900 * 1024 * 1024    # MTProto path: stay safely under 2 GB

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

URL_RE = re.compile(r"https?://[^\s]+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Files yt-dlp may leave behind that are never the final media
SKIP_SUFFIXES = (".part", ".ytdl", ".description", ".json", ".info.json")

VIDEO_EXTS = (".mp4", ".mkv", ".webm")
AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".ogg", ".wav")

INTRO = (
    "🎬 *LinkToVid* — your all-in-one downloader\n\n"
    "📥 Just paste any social media link and I'll fetch the video, audio or "
    "image for you.\n\n"
    "✅ *Supported:* YouTube, TikTok, Instagram, Facebook, X/Twitter, Reddit "
    "and 1000+ more sites\n\n"
    "\U0001F3B5 *Music:* send a Spotify or Shazam track link and I'll "
    "return the song as an mp3\n\n"
    "👀 I react to your link while downloading\n"
    "🎉 Your download is ready\n\n"
    "Max file size: 2 GB per file 🚀\n\n"
    "Paste a link to get started 🚀"
)


def clean_error(msg: str) -> str:
    """Strip ANSI color codes and the 'ERROR: ' prefix yt-dlp adds."""
    msg = ANSI_RE.sub("", msg)
    msg = re.sub(r"^ERROR:\s*", "", msg.strip())
    return msg


def run_download(url: str, workdir: str) -> dict:
    """Download `url` into workdir with yt-dlp. Returns info dict of first item."""
    ydl_opts = {
        "outtmpl": os.path.join(workdir, "%(title).100s.%(ext)s"),
        # Aim for the best quality that fits under the 2 GB MTProto limit.
        # sd = pre-muxed "standard def" YouTube stream (fast, no merge);
        # then progressively smaller fallbacks so we always land a file.
        "format": (
            "sd/best[filesize<1900M]/best[height<=1080][filesize<1900M]/"
            "best[height<=720][filesize<1900M]/"
            "best[height<=480]+worstaudio/worst"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,      # single item per link, not whole playlists
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "restrictfilenames": True,
        "overwrites": True,
        # YouTube's SABR rollout breaks the default "web" client for many
        # videos (missing URLs on https formats). The android client is
        # unaffected and gives direct, mergeable streams.
        "extractor_args": {"youtube": {"player_client": ["android", "web", "ios"]}},
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # For playlists, info may have entries; use the first one only
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            info = entries[0] if entries else info

    # Don't trust prepare_filename()'s guess: merged containers, format-id
    # suffixes, and extension swaps can all make the real file differ from
    # it. Just look at what's actually sitting in the (fresh, per-request)
    # workdir and take the largest real file.
    candidates = [
        os.path.join(workdir, f)
        for f in os.listdir(workdir)
        if not f.endswith(SKIP_SUFFIXES)
    ]
    filename = max(candidates, key=os.path.getsize) if candidates else ""
    info["filepath"] = filename
    return info


async def react(update: Update, emoji: str):
    """Set an emoji reaction on the user's message; best-effort."""
    try:
        await update.message.set_reaction(
            reaction=[ReactionTypeEmoji(emoji)]
        )
    except Exception as e:
        log.warning("reaction failed: %s", e)


def _keep_alive(url: str):
    """Ping our own public URL every 5 minutes so free hosts (Render free
    spins down after ~15 min without inbound HTTP) keep the service awake."""
    def loop():
        import urllib.request
        while True:
            try:
                urllib.request.urlopen(url, timeout=20)
            except Exception:
                pass
            time.sleep(300)

    threading.Thread(target=loop, daemon=True).start()
    log.info("keep-alive pinging %s every 5 min", url)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(INTRO, parse_mode="Markdown")


async def send_file(update: Update, chat_id: int, path: str, size: int,
                    title: str):
    """Send the downloaded file via Bot API (small) or MTProto (large)."""
    ext = os.path.splitext(path)[1].lower()

    if size > MAX_SEND:
        # Large path: MTProto, up to 2 GB
        log.info("Sending via MTProto (%d MB)", size // (1024 * 1024))
        try:
            await mtp.send_large(chat_id, path, caption=title, is_video=ext in VIDEO_EXTS)
            return
        except Exception as e:
            log.error("MTProto send failed: %s", e)
            await update.message.reply_text(
                "That file is %d MB — over the 50 MB Bot API limit.\n"
                "My 2 GB upgrade is built and waiting on Telegram API "
                "credentials; it goes live as soon as they're approved. "
                "Try a shorter or lower-quality link for now."
                % (size // (1024 * 1024))
            )
            raise

    with open(path, "rb") as f:
        if ext in VIDEO_EXTS:
            await update.message.reply_video(
                video=f, caption=title[:1000], supports_streaming=True
            )
        elif ext in AUDIO_EXTS:
            await update.message.reply_audio(audio=f, title=title[:64])
        elif ext in (".jpg", ".jpeg", ".png", ".webp"):
            await update.message.reply_photo(photo=f, caption=title[:1000])
        else:
            await update.message.reply_document(
                document=f, filename=title[:60] + ext
            )


def run_music_download(query: str, workdir: str, track: dict) -> str:
    """Search YouTube for the track and produce an mp3 in workdir.

    Returns the path of the finished mp3.
    """
    ydl_opts = {
        "outtmpl": os.path.join(workdir, "audio.%(ext)s"),
        # song first, then progressively looser fallbacks
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "writethumbnail": True,   # album art for the audio player
        "postprocessors": [
            {"key": "FFmpegExtractAudio",
             "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "EmbedThumbnail"},
        ],
    }
    with YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info("ytsearch1:" + query, download=True)
    path = os.path.join(workdir, "audio.mp3")
    if not os.path.exists(path):
        raise RuntimeError("audio conversion failed")
    # tag the file so music players show the right names
    try:
        from mutagen.easyid3 import EasyID3
        tags = EasyID3(path)
        tags["title"] = track["song"]
        if track["artist"]:
            tags["artist"] = track["artist"]
        tags.save()
    except Exception as e:
        log.warning("mp3 tagging failed: %s", e)
    return path


async def handle_music(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       url: str):
    """Spotify/Shazam link -> resolved track -> mp3 back to the user."""
    chat_id = update.effective_chat.id
    await react(update, ReactionEmoji.EYES)          # 👀 fetching
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    workdir = tempfile.mkdtemp(prefix="dlbot_music_")
    loop = asyncio.get_running_loop()
    try:
        track = await loop.run_in_executor(None, music.resolve_track, url)
        log.info("music: %s -> %s - %s", url, track["artist"], track["song"])
        query = music.search_query(track)
        path = await loop.run_in_executor(
            None, run_music_download, query, workdir, track)

        with open(path, "rb") as f:
            await update.message.reply_audio(
                audio=f,
                title=track["song"][:64],
                performer=(track["artist"] or track["source"])[:64],
                filename=f'{track["song"]} - {track["artist"] or track["source"]}.mp3'.replace("/", "_")[:100],
            )
        log.info("Sent audio '%s - %s' from %s",
                 track["artist"], track["song"], url)
        await react(update, ReactionEmoji.PARTY_POPPER)   # 🎉
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode="Markdown")
        await react(update, ReactionEmoji.NEUTRAL_FACE)   # 😐
    except Exception as e:
        log.exception("music download failed")
        await update.message.reply_text(
            "Couldn't fetch that song. If the link is private or the song "
            "isn't on YouTube, I can't get it — try another track.")
        await react(update, ReactionEmoji.NEUTRAL_FACE)   # 😐
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("message from chat_id=%s user_id=%s", update.effective_chat.id, update.effective_user.id if update.effective_user else None)
    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text("That doesn't look like a link. Send me a URL.")
        return

    url = match.group(0)
    chat_id = update.effective_chat.id

    if music.is_music_url(url):
        await handle_music(update, context, url)
        return

    await react(update, ReactionEmoji.EYES)          # 👀 downloading
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

    workdir = tempfile.mkdtemp(prefix="dlbot_")
    loop = asyncio.get_running_loop()
    try:
        # run the blocking download in a worker thread
        info = await loop.run_in_executor(None, run_download, url, workdir)
        path = info.get("filepath", "")

        if not path or not os.path.exists(path):
            log.warning("No output file after download for %s (workdir=%s, listing=%s)",
                        url, workdir, os.listdir(workdir) if os.path.isdir(workdir) else "n/a")
            await update.message.reply_text(
                "Couldn't download that. The link may be private, deleted, "
                "or from an unsupported site."
            )
            await react(update, ReactionEmoji.NEUTRAL_FACE)
            return

        size = os.path.getsize(path)
        if size > MAX_TOTAL:
            await update.message.reply_text(
                f"That file is {size // (1024*1024)} MB — over my 2 GB limit. "
                "Try a shorter or lower-quality version."
            )
            await react(update, ReactionEmoji.NEUTRAL_FACE)
            return

        title = info.get("title") or os.path.basename(path)

        try:
            await send_file(update, chat_id, path, size, title)
        except Exception:
            # send_file already reported the failure to the user
            await react(update, ReactionEmoji.NEUTRAL_FACE)
            return
        log.info("Sent %s (%d bytes) from %s", title, size, url)
        await react(update, ReactionEmoji.PARTY_POPPER)  # 🎉 ready

    except Exception as e:
        log.exception("download failed")
        msg = clean_error(str(e))
        low = msg.lower()
        if "private" in low or "login" in low or "cookies" in low:
            await update.message.reply_text(
                "That link is private or requires a login, so I can't fetch it."
            )
        elif "unsupported url" in low or "no video" in low or "unable to extract" in low:
            await update.message.reply_text(
                "I couldn't find downloadable media at that link. Double-check "
                "the URL points directly to a post/video."
            )
        else:
            await update.message.reply_text(f"Download failed: {msg[:300]}")
        await react(update, ReactionEmoji.NEUTRAL_FACE)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def on_started(app: Application):
    """Start the MTProto client inside the bot's event loop."""
    try:
        await mtp.get_client()
        log.info("MTProto big-file sender is up")
    except Exception as e:
        log.error("MTProto client failed to start (large files >50MB will fail): %s", e)


def _serve_health(port: int):
    """Minimal HTTP server so hosts like Hugging Face see a listening port."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"linktovid ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("health server on :%d", port)


def main():
    # Some hosts inject env vars a beat after container start; wait for the
    # token instead of exiting immediately (up to 10 minutes).
    global BOT_TOKEN
    _waited = 0
    while not BOT_TOKEN:
        BOT_TOKEN = os.environ.get("BOT_TOKEN", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if BOT_TOKEN:
            break
        if _waited >= 600:
            raise SystemExit(
                "Set your bot token first:\n"
                "  export BOT_TOKEN=123456:ABC-your-token-from-BotFather"
            )
        log.info("waiting for BOT_TOKEN env (%ds)", _waited)
        time.sleep(10)
        _waited += 10
    hp = int(os.environ.get("PORT", "0") or 0)
    if hp:
        _serve_health(hp)
    ping_url = os.environ.get("SELF_PING_URL", "").strip()
    if ping_url:
        _keep_alive(ping_url)
        raise SystemExit(
            "Set your bot token first:\n"
            "  export BOT_TOKEN=123456:ABC-your-token-from-BotFather"
        )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(120)
        .pool_timeout(30)
        .post_init(on_started)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("Bot starting (polling + MTProto large uploads)...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
