"""Unified resolver and multi-catalog search for Unstream Telegram Bot.

Resolves Spotify, Deezer, Apple Music, YouTube and SoundCloud URLs without API keys.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor, wait
from difflib import SequenceMatcher

from . import deezer, embed, instagram, itunes, soundcloud, ytdlp
from .models import Collection, ProviderError, SearchResult

SEARCH_TIMEOUT_SECONDS = 15


def is_supported_url(url: str) -> bool:
    """Check if the URL belongs to any supported music provider."""
    url = url.strip()
    return bool(
        deezer.is_deezer_url(url)
        or itunes.is_itunes_url(url)
        or instagram.is_instagram_url(url)
        or soundcloud.is_soundcloud_url(url)
        or ytdlp.is_supported_url(url)
        or embed.parse_url(url)
    )


def resolve_any(url: str) -> Collection:
    """Route a URL to the right metadata provider and return Collection."""
    url = url.strip()
    if deezer.is_deezer_url(url):
        return deezer.resolve(url)
    if itunes.is_itunes_url(url):
        return itunes.resolve(url)
    if instagram.is_instagram_url(url):
        return instagram.resolve(url)
    if soundcloud.is_soundcloud_url(url):
        try:
            return soundcloud.resolve(url)
        except ProviderError:
            return ytdlp.resolve(url)
    if ytdlp.is_supported_url(url):
        return ytdlp.resolve(url)
    spotify_ref = embed.parse_url(url)
    if spotify_ref:
        return embed.resolve(*spotify_ref)
    raise ProviderError(
        "Unsupported link — please send a Spotify, Deezer, Apple Music, "
        "YouTube, SoundCloud or Instagram URL."
    )


def _squash(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def relevance(result: SearchResult, query: str) -> float:
    """Score search result relevance against query."""
    q = _squash(query)
    name = _squash(result.name)
    both = f"{name} {_squash(result.subtitle)}".strip()

    score = max(
        SequenceMatcher(None, q, name).ratio(),
        SequenceMatcher(None, q, both).ratio(),
    )
    if name == q:
        score = 1.0
    elif name.startswith(q):
        score = max(score, 0.93)
    elif q in name:
        score = max(score, 0.86)

    tokens = set(q.split())
    if tokens and tokens <= set(both.split()):
        score = max(score, 0.88)

    source_weights = {"deezer": 1.0, "itunes": 0.97, "soundcloud": 0.9, "youtube": 0.87}
    return score * source_weights.get(result.source, 0.8)


def search_any(query: str, page: int = 0) -> list[SearchResult]:
    """Search across multiple catalogs in parallel and return ranked hits."""
    query = query.strip()
    if not query:
        return []

    def soundcloud_search(q: str, p: int) -> list[SearchResult]:
        try:
            return soundcloud.search(q, p)
        except Exception:
            return ytdlp.search_soundcloud(q, p)

    providers = [deezer.search, itunes.search, soundcloud_search]
    disable_yt = os.getenv("UNSTREAM_YOUTUBE_DISABLED", "").lower() in ("1", "true")
    if not disable_yt:
        providers.append(ytdlp.search_youtube)

    pool = ThreadPoolExecutor(max_workers=len(providers))
    futures = [pool.submit(p, query, page) for p in providers]
    wait(futures, timeout=SEARCH_TIMEOUT_SECONDS)
    pool.shutdown(wait=False, cancel_futures=True)

    merged: list[SearchResult] = []
    seen: set[str] = set()
    for future in futures:
        if not future.done() or future.exception():
            continue
        try:
            for item in future.result():
                key = f"{item.kind}:{_squash(item.name)}:{_squash(item.subtitle)}"
                if key not in seen:
                    seen.add(key)
                    merged.append(item)
        except Exception:
            continue

    merged.sort(key=lambda r: relevance(r, query), reverse=True)
    return merged
