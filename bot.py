#!/usr/bin/env python3
"""Unstream Music Telegram Bot.

Runs on GitHub Actions 24/7 unmetered runners.
Downloads Spotify, Deezer, Apple Music, YouTube and SoundCloud tracks, albums and playlists
as tagged, artwork-embedded MP3s and delivers them straight to Telegram.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from engine import downloader, resolver
from engine.models import Collection, ProviderError, SearchResult, Track

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("unstream-bot")

# Configuration
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "8874504954").strip())
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "").strip()
ALLOWED_USERS = {ADMIN_CHAT_ID}
if ALLOWED_USERS_RAW:
    for u in ALLOWED_USERS_RAW.split(","):
        u = u.strip()
        if u.isdigit():
            ALLOWED_USERS.add(int(u))

DEFAULT_QUALITY = os.getenv("DEFAULT_QUALITY", "320").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/unstream_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# User state storage (in-memory)
USER_QUALITY = {}
SEARCH_CACHE = {}  # query_hash -> list[SearchResult]
INSTAGRAM_PENDING = {}  # chat_id -> instagram shortcode awaiting a song name
START_TIME = time.time()


def is_authorized(user_id: int) -> bool:
    """Strict admin-only guard per project security standards."""
    return user_id in ALLOWED_USERS


def format_duration(ms: int) -> str:
    secs = max(0, ms // 1000)
    mins = secs // 60
    rem_secs = secs % 60
    return f"{mins}:{rem_secs:02d}"


# --- Telegram API Helpers ---

def tg_request(method: str, data: dict = None, files: dict = None, timeout: float = 30.0) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    with httpx.Client(timeout=timeout) as client:
        if files:
            resp = client.post(url, data=data, files=files)
        else:
            resp = client.post(url, json=data)
        return resp.json()


def send_message(chat_id: int, text: str, reply_markup: dict = None, reply_to_message_id: int = None) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    return tg_request("sendMessage", data=payload)


def edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict = None) -> dict:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_request("editMessageText", data=payload)


def delete_message(chat_id: int, message_id: int):
    try:
        tg_request("deleteMessage", data={"chat_id": chat_id, "message_id": message_id}, timeout=10.0)
    except Exception:
        pass


def answer_callback_query(callback_query_id: str, text: str = "", show_alert: bool = False):
    try:
        tg_request("answerCallbackQuery", data={
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }, timeout=10.0)
    except Exception:
        pass


def send_audio_file(
    chat_id: int,
    audio_path: Path,
    track: Track,
    quality: str,
    thumb_path: Path = None,
    caption_extra: str = "",
) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio"
    artists_str = ", ".join(track.artists)
    duration_secs = track.duration_ms // 1000 if track.duration_ms else 0

    caption = (
        f"🎵 <b>{track.title}</b>\n"
        f"👤 <b>{artists_str}</b>\n"
        f"💿 <i>{track.album or 'Single'}</i>\n"
        f"⚡ <b>کیفیت:</b> {quality}kbps • MP3 تگ‌خورده\n"
    )
    if caption_extra:
        caption += f"{caption_extra}\n"
    caption += "✨ <i>Unstream Music Bot • GitHub 10Gbps Runner</i>"

    data = {
        "chat_id": str(chat_id),
        "title": track.title[:64],
        "performer": artists_str[:64],
        "duration": str(duration_secs),
        "caption": caption,
        "parse_mode": "HTML",
    }

    files = {
        "audio": (audio_path.name, open(audio_path, "rb"), "audio/mpeg"),
    }
    thumb_file = None
    if thumb_path and thumb_path.exists():
        thumb_file = open(thumb_path, "rb")
        files["thumbnail"] = (thumb_path.name, thumb_file, "image/jpeg")

    try:
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(url, data=data, files=files)
            return resp.json()
    finally:
        files["audio"][1].close()
        if thumb_file:
            thumb_file.close()


def download_thumbnail(url: str, dest_dir: Path) -> Path | None:
    if not url:
        return None
    try:
        dest = dest_dir / f"thumb_{int(time.time() * 1000)}.jpg"
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            r = client.get(url)
            if r.status_code == 200:
                dest.write_bytes(r.content)
                return dest
    except Exception as e:
        logger.warning(f"Could not fetch thumbnail: {e}")
    return None


# --- Core Action Handlers ---

_SEARCH_HINT_RE = re.compile(
    r"(?:https?://\S+|\n+#\S+|\n+@\S+)",  # drop urls, hashtags, mentions
)


def _looks_like_url(text: str) -> bool:
    """Guard the URL-detection path so plain search text isn't mistaken for a link.

    Requires a real hostname in a public TLD plus a path segment. That keeps
    "adele hello" in the search path while still catching a bare
    "youtube.com/watch?v=abc" pasted without a scheme.
    """
    try:
        host = urlparse(text).netloc.lower()
    except Exception:
        return False
    if not host or " " in host:
        return False
    # A bare domain without a path is ambiguous — treat as search.
    path = urlparse(text).path.strip("/")
    if not path:
        return False
    return "." in host and host.split(".")[-1].isalpha()


def _search_fallback_for_url(url: str, error: ProviderError) -> str | None:
    """When direct metadata fetch fails (Instagram login wall), the caller may
    have included a caption next to the link in the same message. We scan the
    raw error and surrounding context for a searchable phrase.

    Returns a search query string, or None when nothing usable was found.
    """
    # Right now we only have the URL itself — no user-supplied caption — so we
    # cannot search meaningfully. The bot asks the user instead.
    return None


def handle_start(chat_id: int):
    text = (
        "🔥 <b>به ربات موزیک Unstream خوش آمدید!</b>\n\n"
        "این ربات از منابع پرسرعت گیت‌هاب اکشن (پورت ۱۰ گیگابیت بدون فیلتر) استفاده می‌کند "
        "تا موزیک‌های دلخواه شما را با تگ‌های رسمی ID3، کاور باکیفیت و متن ترانه (Lyrics) دانلود و ارسال کند.\n\n"
        "🔗 <b>سرویس‌های پشتیبانی‌شده:</b>\n"
        "• <b>Spotify:</b> لینک آهنگ، آلبوم یا پلی‌لیست\n"
        "• <b>Apple Music:</b> آهنگ، آلبوم، پلی‌لیست\n"
        "• <b>Deezer:</b> آهنگ، آلبوم، پلی‌لیست\n"
        "• <b>YouTube & YT Music:</b> ویدیو یا پلی‌لیست\n"
        "• <b>SoundCloud:</b> آهنگ یا سِت\n"
        "• <b>Instagram:</b> لینک ریلز یا پست — آهنگش رو پیدا و دانلود می‌کنه 🆕\n\n"
        "🔍 <b>جستجوی هوشمند:</b>\n"
        "کافیست نام آهنگ یا خواننده را بفرستید تا در تمام پلتفرم‌ها همزمان جستجو کند.\n\n"
        "⚙️ <b>دستورات کاربردی:</b>\n"
        "/quality 320 - تنظیم کیفیت پیش‌فرض (320, 192, 128, original)\n"
        "/status - وضعیت سرور و زمان فعالیت رانر\n"
        "/help - راهنمای استفاده"
    )
    send_message(chat_id, text)


def handle_status(chat_id: int):
    uptime_sec = int(time.time() - START_TIME)
    uptime_str = f"{uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m {uptime_sec % 60}s"
    quality = USER_QUALITY.get(chat_id, DEFAULT_QUALITY)

    text = (
        "⚡ <b>وضعیت ربات Unstream:</b>\n\n"
        f"⏱ <b>مدت فعالیت:</b> {uptime_str}\n"
        f"🎧 <b>کیفیت فعال:</b> {quality}kbps\n"
        f"🚀 <b>محیط اجرا:</b> GitHub Actions Runner (Ubuntu)\n"
        f"🌐 <b>سرعت شبکه:</b> 10Gbps Unmetered\n"
        f"🔒 <b>سطح دسترسی:</b> Admin Only ({ADMIN_CHAT_ID})\n"
        "🔄 <b>حالت رانر:</b> 24/7 Auto-Retrigger Loop"
    )
    send_message(chat_id, text)


def handle_quality(chat_id: int, args: list[str]):
    if not args:
        current = USER_QUALITY.get(chat_id, DEFAULT_QUALITY)
        text = (
            f"🎧 کیفیت فعلی شما: <b>{current}kbps</b>\n\n"
            "برای تغییر از دستورات زیر استفاده کنید:\n"
            "• <code>/quality 320</code> (بیشترین کیفیت MP3 - پیشنهادی)\n"
            "• <code>/quality 192</code> (کیفیت استاندارد)\n"
            "• <code>/quality 128</code> (حجم سبک)\n"
            "• <code>/quality original</code> (فرمت استریم دست‌نخورده)"
        )
        send_message(chat_id, text)
        return

    val = args[0].strip().lower()
    if val in ("320", "192", "128", "original"):
        USER_QUALITY[chat_id] = val
        send_message(chat_id, f"✅ کیفیت دانلود با موفقیت روی <b>{val}</b> تنظیم شد.")
    else:
        send_message(chat_id, "❌ مقدار نامعتبر است! یکی از گزینه‌های 320, 192, 128 یا original را بفرستید.")


def process_download(chat_id: int, track: Track, quality: str, reply_to_id: int = None, index_info: str = ""):
    prog_msg = send_message(
        chat_id,
        f"⏳ <b>در حال پردازش آهنگ {index_info}</b>\n"
        f"🎵 <b>{track.title}</b>\n"
        f"👤 <b>{', '.join(track.artists)}</b>\n"
        f"💿 <i>{track.album or 'Single'}</i>\n"
        f"⚡ کیفیت: {quality}kbps\n"
        f"<i>در حال استخراج صوت و تزریق تگ‌ها...</i>",
        reply_to_message_id=reply_to_id,
    )
    msg_id = prog_msg.get("result", {}).get("message_id")

    task_dir = DOWNLOAD_DIR / f"job_{int(time.time() * 1000)}"
    task_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = None

    try:
        if track.cover_url:
            thumb_path = download_thumbnail(track.cover_url, task_dir)

        def on_prog(stage, frac):
            logger.info(f"[{track.title}] Stage: {stage} ({frac:.1f})")

        audio_path = downloader.download_track(
            track=track,
            out_dir=task_dir,
            on_progress=on_prog,
            quality=quality,
            embed_lyrics=True,
        )

        if not audio_path or not audio_path.exists():
            raise Exception("فایل صوتی یافت نشد.")

        file_size_mb = audio_path.stat().st_size / (1024 * 1024)
        if file_size_mb > 49.5:
            send_message(
                chat_id,
                f"⚠️ حجم فایل ({file_size_mb:.1f} MB) از محدودیت ۵۰ مگابایت تلگرام بیشتر است!",
                reply_to_message_id=reply_to_id,
            )
            return

        caption_extra = f"📦 حجم: {file_size_mb:.1f}MB • مدت: {format_duration(track.duration_ms)}"
        res = send_audio_file(
            chat_id=chat_id,
            audio_path=audio_path,
            track=track,
            quality=quality,
            thumb_path=thumb_path,
            caption_extra=caption_extra,
        )

        if not res.get("ok"):
            logger.error(f"Telegram upload failed: {res}")
            send_message(chat_id, f"❌ خطا در ارسال فایل به تلگرام: {res.get('description')}")
        else:
            if msg_id:
                delete_message(chat_id, msg_id)

    except Exception as e:
        logger.exception("Download pipeline error")
        send_message(chat_id, f"❌ خطا در دریافت قطعه «{track.title}»:\n<code>{str(e)[:200]}</code>")
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


def handle_url(chat_id: int, url: str, reply_to_id: int = None):
    quality = USER_QUALITY.get(chat_id, DEFAULT_QUALITY)
    init_msg = send_message(chat_id, "🔍 <i>در حال واکشی اطلاعات از منبع موزیک...</i>", reply_to_message_id=reply_to_id)
    msg_id = init_msg.get("result", {}).get("message_id")

    try:
        col: Collection = resolver.resolve_any(url)
    except ProviderError as pe:
        err_text = str(pe)
        # Instagram login wall → remember the chat and ask for a song name.
        # The user's next plain-text message is then treated as a search.
        if "اینستاگرام" in err_text:
            shortcode = resolver.instagram.parse_url(url) or ""
            INSTAGRAM_PENDING[chat_id] = shortcode
            edit_message(
                chat_id,
                msg_id,
                "ℹ️ " + err_text + "\n\n💡 <i>الان اسم آهنگ و خواننده را همین‌جا بنویس (مثلاً: <code>Adele Hello</code>) تا برایت پیدا و دانلود کنم.</i>",
            )
            return
        edit_message(chat_id, msg_id, f"❌ {err_text}")
        return
    except Exception as e:
        logger.exception("Resolver error")
        edit_message(chat_id, msg_id, f"❌ خطا در شناسایی لینک:\n<code>{str(e)[:200]}</code>")
        return

    if col.kind == "track" and col.tracks:
        track = col.tracks[0]
        delete_message(chat_id, msg_id)
        process_download(chat_id, track, quality, reply_to_id)
    elif col.kind in ("album", "playlist") and col.tracks:
        total = len(col.tracks)
        edit_message(
            chat_id,
            msg_id,
            f"📂 <b>مجموعه {col.kind.upper()}: «{col.name}»</b>\n"
            f"👤 سازنده: <b>{col.owner}</b>\n"
            f"🔢 تعداد قطعات: <b>{total} آهنگ</b>\n\n"
            f"⏳ <i>در حال شروع دانلود و ارسال تک‌تک قطعات به ترتیب...</i>",
        )
        for idx, tr in enumerate(col.tracks, start=1):
            process_download(chat_id, tr, quality, reply_to_id, index_info=f"[{idx}/{total}]")
            time.sleep(1.5)  # Telegram flood protection
        send_message(chat_id, f"✅ <b>تمام {total} قطعه از «{col.name}» با موفقیت ارسال شدند!</b>")
    else:
        edit_message(chat_id, msg_id, "❌ هیچ قطعه‌ای در این لینک یافت نشد.")


def handle_search(chat_id: int, query: str, reply_to_id: int = None):
    wait_msg = send_message(chat_id, f"🔎 <i>در حال جستجوی «{query}» در تمام سرویس‌ها...</i>", reply_to_message_id=reply_to_id)
    msg_id = wait_msg.get("result", {}).get("message_id")

    results = resolver.search_any(query)
    if not results:
        edit_message(chat_id, msg_id, f"❌ هیچ نتیجه‌ای برای «{query}» پیدا نشد.")
        return

    top_hits = results[:5]
    q_hash = hashlib.md5(query.encode("utf-8")).hexdigest()[:8]
    SEARCH_CACHE[q_hash] = top_hits

    buttons = []
    text_lines = [f"🎵 <b>نتایج جستجو برای:</b> <i>{query}</i>\n"]
    for i, item in enumerate(top_hits):
        src_icon = {
            "spotify": "🟢",
            "deezer": "🟣",
            "itunes": "🍎",
            "youtube": "🔴",
            "soundcloud": "🟠",
        }.get(item.source, "🎧")
        text_lines.append(f"{i+1}️⃣ {src_icon} <b>{item.name}</b> — <i>{item.subtitle}</i>")
        buttons.append([{
            "text": f"⬇️ دانلود {i+1}: {item.name[:25]}",
            "callback_data": f"dl:{q_hash}:{i}",
        }])

    text = "\n".join(text_lines) + "\n\n<i>روی دکمه آهنگ مورد نظر کلیک کنید:</i>"
    reply_markup = {"inline_keyboard": buttons}
    edit_message(chat_id, msg_id, text, reply_markup=reply_markup)


def handle_callback_query(cq: dict):
    cq_id = cq["id"]
    from_user = cq["from"]["id"]
    if not is_authorized(from_user):
        answer_callback_query(cq_id, "⛔ شما اجازه دسترسی به این ربات را ندارید!", show_alert=True)
        return

    data = cq.get("data", "")
    if not data.startswith("dl:"):
        answer_callback_query(cq_id)
        return

    parts = data.split(":")
    if len(parts) != 3:
        answer_callback_query(cq_id, "داده نامعتبر!")
        return

    q_hash, idx_str = parts[1], parts[2]
    idx = int(idx_str)

    hits = SEARCH_CACHE.get(q_hash)
    if not hits or idx >= len(hits):
        answer_callback_query(cq_id, "⏳ این جستجو منقضی شده است. لطفاً مجدداً جستجو کنید.", show_alert=True)
        return

    target: SearchResult = hits[idx]
    answer_callback_query(cq_id, f"در حال شروع دانلود {target.name}...")

    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]

    edit_message(chat_id, message_id, f"⬇️ <b>در حال دانلود:</b> <i>{target.name} — {target.subtitle}</i>")
    handle_url(chat_id, target.url)


# --- Notification on Bot Start ---

def notify_admin_live():
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    text = (
        "🚀 <b>ربات موزیک Unstream روی گیت‌هاب اکشن فعال شد!</b>\n\n"
        "⚡ <b>وضعیت اتصال:</b> آنلاین و آماده دریافت لینک\n"
        "🌐 <b>شبکه:</b> 10Gbps بدون محدودیت / بدون فیلتر\n"
        "⏱ <b>تایمر رانر:</b> فعال (تمدید خودکار ۲۴/۷)\n"
        "🔒 <b>امنیت:</b> قفل اختصاصی روی اکانت مدیر\n\n"
        "💡 <i>کافیست یک لینک از Spotify، Apple Music، Deezer، YouTube یا SoundCloud بفرستید یا اسم آهنگ را سرچ کنید!</i>"
    )
    try:
        send_message(ADMIN_CHAT_ID, text)
        logger.info(f"Startup notification sent to admin {ADMIN_CHAT_ID}")
    except Exception as e:
        logger.warning(f"Failed to notify admin on start: {e}")


# --- Main Long Polling Loop ---

def main():
    if not BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable is missing!")
        sys.exit(1)

    logger.info("Starting Unstream Telegram Bot...")
    notify_admin_live()

    offset = 0
    poll_client = httpx.Client(timeout=45.0)

    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            params = {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            }
            resp = poll_client.get(url, params=params)
            if resp.status_code != 200:
                logger.error(f"getUpdates HTTP error {resp.status_code}: {resp.text}")
                time.sleep(5)
                continue

            data = resp.json()
            if not data.get("ok"):
                logger.error(f"getUpdates Telegram error: {data}")
                time.sleep(5)
                continue

            updates = data.get("result", [])
            for upd in updates:
                offset = max(offset, upd["update_id"] + 1)

                if "callback_query" in upd:
                    handle_callback_query(upd["callback_query"])
                    continue

                msg = upd.get("message")
                if not msg:
                    continue

                user = msg.get("from", {})
                user_id = user.get("id")
                chat_id = msg["chat"]["id"]
                msg_id = msg.get("message_id")
                text = (msg.get("text") or "").strip()

                if not is_authorized(user_id):
                    logger.warning(f"Unauthorized access attempt by user {user_id} (@{user.get('username')})")
                    send_message(
                        chat_id,
                        f"⛔ <b>دسترسی غیرمجاز!</b>\nاین ربات شخصی است و فقط مدیر (ID: {ADMIN_CHAT_ID}) مجاز به استفاده است.",
                        reply_to_message_id=msg_id,
                    )
                    continue

                if not text:
                    continue

                if text.startswith("/start"):
                    INSTAGRAM_PENDING.pop(chat_id, None)
                    handle_start(chat_id)
                elif text.startswith("/help"):
                    handle_start(chat_id)
                elif text.startswith("/status"):
                    handle_status(chat_id)
                elif text.startswith("/quality"):
                    parts = text.split()
                    handle_quality(chat_id, parts[1:])
                else:
                    # Pull a bare URL out of the middle of a message (people
                    # paste "check this https://..." with text around it).
                    # Requires a real hostname + path, so ordinary search text
                    # like "adele hello" is never mistaken for a link.
                    url_match = re.search(
                        r"https?://[^\s<>\"']+/[^\s<>\"']*", text
                    )
                    if url_match and _looks_like_url(url_match.group(0)):
                        handle_url(chat_id, url_match.group(0), reply_to_id=msg_id)
                    else:
                        # If we're waiting for a song name after an Instagram
                        # login-wall, treat this text as that search.
                        if chat_id in INSTAGRAM_PENDING:
                            shortcode = INSTAGRAM_PENDING.pop(chat_id)
                            send_message(
                                chat_id,
                                f"🎯 <i>جستجو برای آهنگ پست اینستاگرام <code>{shortcode}</code>...</i>",
                                reply_to_message_id=msg_id,
                            )
                        # Plain text or /search query
                        query = text
                        if text.startswith("/search "):
                            query = text[8:].strip()
                        handle_search(chat_id, query, reply_to_id=msg_id)

        except httpx.ReadTimeout:
            continue
        except Exception as e:
            logger.exception(f"Unhandled error in main polling loop: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
