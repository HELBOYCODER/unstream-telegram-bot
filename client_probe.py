#!/usr/bin/env python3
"""Diagnostic script to test which player_client works on YouTube from GitHub runner."""
import sys
import yt_dlp

URL = "https://www.youtube.com/watch?v=xFYQQPAOz7Y"

CLIENTS_TO_TEST = [
    ["android"],
    ["ios"],
    ["tv"],
    ["tv_embedded"],
    ["android_vr"],
    ["web_embedded"],
    ["mweb"],
    ["web"],
    ["web_safari"],
    ["web_creator"],
]

print("=== TESTING YOUTUBE PLAYER CLIENTS ON THIS RUNNER ===")
for client in CLIENTS_TO_TEST:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "extractor_args": {
            "youtube": {
                "player_client": client,
            }
        },
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(URL, download=False)
            title = info.get("title") if info else None
            print(f"✅ CLIENT {client}: SUCCESS -> title={title!r}")
    except Exception as exc:
        err = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        print(f"❌ CLIENT {client}: {err[:80]}")

print("=== DIAGNOSTIC FINISHED ===")
