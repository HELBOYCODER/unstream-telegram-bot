#!/usr/bin/env python3
"""Verify resolver no longer hard-errors on unknown URLs.

Before the fix, a link that no detector recognised raised ProviderError
immediately. Now it falls through to yt-dlp's generic extractor, which knows
1000+ sites. We can't reach most of those hosts from this sandbox (DNS is
filtered), so we verify the routing decision rather than the download itself:
the generic path must be attempted (i.e. we must NOT get the hard
"unsupported" error instantly).
"""
import sys
import time

sys.path.insert(0, ".")
from engine import resolver
from engine.models import ProviderError

# A URL none of the native detectors claim.
URL = "https://example.com/some/song"

# Native detection should still say "not one of mine" — that's what makes it
# route to the generic fallback rather than a native provider.
print(f"is_supported_url({URL}) = {resolver.is_supported_url(URL)}")

start = time.time()
try:
    col = resolver.resolve_any(URL)
    print(f"resolve_any returned: kind={col.kind} tracks={len(col.tracks)}")
except ProviderError as exc:
    elapsed = time.time() - start
    print(f"ProviderError after {elapsed:.1f}s: {exc}")
    # The fix's contract: the generic extractor must have been *tried*, which
    # takes real network time. An instant raise means the old hard-error path.
    if elapsed < 2.0:
        print("FAIL — raised instantly, generic fallback never ran")
        sys.exit(1)
    print("PASS — generic path was attempted before giving up")
except Exception as exc:
    print(f"Unexpected exception type: {type(exc).__name__}: {exc}")
    sys.exit(1)
