#!/usr/bin/env python3
"""End-to-end pipeline test — runs ON the GitHub runner (has network access).

Resolves a real link, downloads a tagged MP3, and sends it to the admin chat.
Proves the whole path works from the runner's own network.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
import httpx  # noqa: E402

from engine import downloader, resolver  # noqa: E402

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN = int(os.getenv("ADMIN_CHAT_ID", "8874504954"))
URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=xFYQQPAOz7Y"

print(f"[1/4] resolve_any({URL})")
col = resolver.resolve_any(URL)
track = col.tracks[0]
print(f"      → {track.title} | {', '.join(track.artists)} | cover={'yes' if track.cover_url else 'no'}")

print("[2/4] download_track()")
tmp = Path(tempfile.mkdtemp())
try:
    audio = downloader.download_track(
        track=track, out_dir=tmp, quality="320", embed_lyrics=True
    )
    if not audio or not audio.exists():
        print("FAIL: no audio file produced")
        sys.exit(1)
    size_mb = audio.stat().st_size / (1024 * 1024)
    print(f"      → {audio.name} ({size_mb:.1f} MB)")
finally:
    pass

print("[3/4] sendAudio to admin")
data = {
    "chat_id": str(ADMIN),
    "title": track.title[:64],
    "performer": ", ".join(track.artists)[:64],
    "caption": f"🧪 <b>E2E pipeline test</b>\n🎵 {track.title}\n👤 {', '.join(track.artists)}\n📦 {size_mb:.1f}MB\nsource: {URL}",
    "parse_mode": "HTML",
}
with httpx.Client(timeout=180.0) as client:
    with open(audio, "rb") as f:
        resp = client.post(
            f"https://api.telegram.org/bot{TOKEN}/sendAudio",
            data=data,
            files={"audio": (audio.name, f, "audio/mpeg")},
        )
result = resp.json()
print(f"      → ok={result.get('ok')}")
if not result.get("ok"):
    print(f"      error: {result.get('description')}")
    sys.exit(1)

print("[4/4] cleanup")
shutil.rmtree(tmp, ignore_errors=True)
print("ALL STEPS PASSED")
