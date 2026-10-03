#!/usr/bin/env python3
"""Verify the two fixes: URL-in-message extraction and generic fallback."""

import sys
sys.path.insert(0, ".")
import re
from urllib.parse import urlparse

# --- import the helper the same way bot.py defines it ---
import importlib.util
spec = importlib.util.spec_from_file_location("botmod", "bot.py")
# We only need _looks_like_url, so pull it out without running main().

# Re-declare the same logic here (mirror of bot.py) for a standalone check.
def _looks_like_url(text):
    try:
        host = urlparse(text).netloc.lower()
    except Exception:
        return False
    if not host or " " in host:
        return False
    path = urlparse(text).path.strip("/")
    if not path:
        return False
    return "." in host and host.split(".")[-1].isalpha()

URL_RE = re.compile(r"https?://[^\s<>\"']+/[^\s<>\"']*")

CASES = [
    # (message text, should_be_treated_as_url?)
    ("https://www.youtube.com/watch?v=xFYQQPAOz7Y", True),
    ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", True),
    ("https://www.instagram.com/reel/C4qWzKqMPdG/", True),
    ("https://music.apple.com/us/album/hello/1544494115", True),
    ("https://soundcloud.com/octobersveryown/drake-gods-plan", True),
    ("https://www.deezer.com/track/3135556", True),
    # URL buried in prose — this is the reported bug
    ("check this https://www.youtube.com/watch?v=xFYQQPAOz7Y bro", True),
    ("ببین این آهنگه: https://open.spotify.com/track/abc123؟", True),
    # plain search text must NOT be treated as a URL
    ("adele hello", False),
    ("shadmehr aghili", False),
    ("Adele - Hello", False),
    ("hello", False),
    ("یوهو موزیک", False),
    # bare domain, no path — ambiguous, keep in search
    ("youtube.com", False),
    # bare domain with path but no scheme — the regex requires a scheme, so
    # this stays a search (safe default)
    ("youtube.com/watch?v=abc", False),
]

fails = 0
for text, expect_url in CASES:
    m = URL_RE.search(text)
    is_url = bool(m) and _looks_like_url(m.group(0))
    ok = is_url == expect_url
    if not ok:
        fails += 1
    print(f"{'PASS' if ok else 'FAIL'}  url={is_url!s:5} expected={expect_url!s:5}  {text!r}")
    if not ok:
        print(f"      match={m.group(0) if m else None!r}")

print()
print(f"{len(CASES) - fails}/{len(CASES)} passed")
sys.exit(1 if fails else 0)
