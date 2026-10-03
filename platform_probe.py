#!/usr/bin/env python3
"""Test SoundCloud, Deezer, Apple Music, Spotify on this runner."""
import sys
from engine import deezer, embed, itunes, soundcloud

print("=== TESTING MUSIC PLATFORMS ON GITHUB RUNNER ===")

# 1. Deezer
try:
    d = deezer.resolve("https://www.deezer.com/track/3135556")
    print(f"✅ DEEZER: SUCCESS -> {d.tracks[0].title} by {d.tracks[0].artists}")
except Exception as e:
    print(f"❌ DEEZER: {e}")

# 2. iTunes / Apple Music
try:
    a = itunes.resolve("https://music.apple.com/us/album/hello/1544494115")
    print(f"✅ ITUNES/APPLE: SUCCESS -> {a.tracks[0].title} by {a.tracks[0].artists}")
except Exception as e:
    print(f"❌ ITUNES/APPLE: {e}")

# 3. Spotify Embed
try:
    s = embed.resolve("track", "4cOdK2wGLETKBW3PvgPWqT")
    print(f"✅ SPOTIFY: SUCCESS -> {s.tracks[0].title} by {s.tracks[0].artists}")
except Exception as e:
    print(f"❌ SPOTIFY: {e}")

# 4. SoundCloud search
try:
    hits = soundcloud.search("adele hello")
    print(f"✅ SOUNDCLOUD SEARCH: SUCCESS -> found {len(hits)} hits")
except Exception as e:
    print(f"❌ SOUNDCLOUD SEARCH: {e}")

# 5. SoundCloud download via yt-dlp
import yt_dlp
try:
    opts = {"quiet": True, "skip_download": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://soundcloud.com/octobersveryown/drake-gods-plan", download=False)
        print(f"✅ SOUNDCLOUD YT-DLP: SUCCESS -> {info.get('title')}")
except Exception as e:
    print(f"❌ SOUNDCLOUD YT-DLP: {e}")

print("=== PLATFORM TEST FINISHED ===")
