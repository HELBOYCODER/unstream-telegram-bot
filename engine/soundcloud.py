"""SoundCloud search + resolve via its public web API — no key, no account.

The SoundCloud web player talks to api-v2.soundcloud.com using a client_id
that ships inside soundcloud.com's public JS bundles. We scrape that id once
and cache it (the same trick yt-dlp uses), which buys full search parity
with the site: tracks, people, albums and playlists — far more than the
tracks-only `scsearch` that yt-dlp exposes.

Resolve also goes through this API (not yt-dlp flat extraction) so artist
profiles and sets come back with real artwork and durations.

If the id can't be scraped (markup change, network hiccup) the caller falls
back to yt-dlp's track search / resolve, so SoundCloud never disappears entirely.
"""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .models import Collection, ProviderError, SearchResult, Track

_API = "https://api-v2.soundcloud.com"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_URL_RE = re.compile(r"(?:www\.|m\.|on\.)?soundcloud\.com/")
_SCRIPT_RE = re.compile(
    r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"'
)
_CLIENT_ID_RE = re.compile(r'client_id\s*:\s*"([a-zA-Z0-9]{32})"')

_client_id: str | None = None
_client_id_at = 0.0
_client_id_lock = threading.Lock()

# A 401 on an id scraped this recently is not an expired id. SoundCloud
# answers every API call from some addresses (VPN exits, mostly) with 401, and
# re-scraping ten pages of JS per call made each search pay for it four times.
_CLIENT_ID_FRESH_SECONDS = 300


def is_soundcloud_url(url: str) -> bool:
    return _URL_RE.search(url) is not None


def _fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _scrape_client_id() -> str:
    html = _fetch("https://soundcloud.com/")
    scripts = _SCRIPT_RE.findall(html)
    # The id usually sits in one of the last bundles — search backwards.
    for src in reversed(scripts):
        try:
            match = _CLIENT_ID_RE.search(_fetch(src))
        except (URLError, OSError):
            continue
        if match:
            return match.group(1)
    raise ProviderError("Could not extract a SoundCloud client id.")


def _get_client_id(force_refresh: bool = False) -> str:
    global _client_id, _client_id_at
    with _client_id_lock:
        if _client_id is None or force_refresh:
            try:
                _client_id = _scrape_client_id()
            except (URLError, OSError, HTTPError) as exc:
                raise ProviderError(f"Could not reach SoundCloud: {exc}") from exc
            _client_id_at = time.monotonic()
        return _client_id


def _client_id_is_fresh() -> bool:
    return time.monotonic() - _client_id_at < _CLIENT_ID_FRESH_SECONDS


def _with_client_id(url: str, client_id: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["client_id"] = client_id
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _get_json(url: str) -> dict | list:
    for attempt in (0, 1):
        client_id = _get_client_id(force_refresh=attempt > 0)
        try:
            return json.loads(_fetch(_with_client_id(url, client_id)))
        except HTTPError as exc:
            # An expired client id answers 401/403 — re-scrape once.
            if exc.code in (401, 403) and attempt == 0 and not _client_id_is_fresh():
                continue
            raise ProviderError(f"SoundCloud API returned HTTP {exc.code}") from exc
        except (URLError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Could not reach SoundCloud: {exc}") from exc
    raise ProviderError("SoundCloud rejected the request.")


def _api(path: str, **params) -> dict:
    return _get_json(f"{_API}{path}?{urlencode(params)}")  # type: ignore[return-value]


def _paginate(path: str, **params) -> list[dict]:
    """Follow SoundCloud's linked_partitioning pages until exhausted.

    SoundCloud often leaves a stale next_href after the last real page —
    stop as soon as a page comes back empty so we don't hang on it.
    """
    params = {**params, "limit": params.get("limit", 200), "linked_partitioning": 1}
    data = _api(path, **params)
    items = list(data.get("collection") or [])
    next_href = data.get("next_href")
    while next_href:
        page = _get_json(next_href)
        if isinstance(page, list):
            items.extend(page)
            break
        batch = page.get("collection") or []
        if not batch:
            break
        items.extend(batch)
        next_href = page.get("next_href")
    return items


def _artwork(item: dict) -> str | None:
    url = item.get("artwork_url") or (item.get("user") or {}).get("avatar_url")
    # Default artwork is 100x100 ("-large"); the site swaps the suffix.
    return url.replace("-large.", "-t300x300.") if url else None


def _avatar(user: dict) -> str | None:
    url = user.get("avatar_url")
    return url.replace("-large.", "-t300x300.") if url else None


def _count(n: int | None, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}" if n else ""


def _track_from_api(
    item: dict, album: str = "", position: int = 0
) -> Track | None:
    # SNIP/BLOCK tracks are Go-only (DRM) — a download would fail anyway.
    if item.get("policy") in ("SNIP", "BLOCK"):
        return None
    # Large playlists return stub rows with only an id — skip until hydrated.
    if not item.get("title") and not item.get("permalink_url"):
        return None
    permalink = item.get("permalink_url")
    if not permalink:
        return None
    user = item.get("user") or {}
    # SoundCloud durations are already milliseconds.
    return Track(
        id=str(item.get("id", "")),
        title=item.get("title") or "Unknown",
        artists=[user.get("username") or "Unknown"],
        album=album,
        duration_ms=int(item.get("duration") or 0),
        cover_url=_artwork(item),
        track_number=position,
        release_date=str(item.get("created_at") or "")[:4],
        source_url=permalink,
    )


def _tracks_from_items(
    items: list[dict], album: str = ""
) -> list[Track]:
    tracks: list[Track] = []
    for i, item in enumerate(items, start=1):
        if track := _track_from_api(item, album=album, position=i):
            tracks.append(track)
    return tracks


def _hydrate_playlist_tracks(stubs: list[dict]) -> list[dict]:
    """Fill in stub track rows that playlists return without metadata.

    Playlists only inline full data for the first handful of tracks; the rest
    are `{id, policy, ...}` stubs. Batch-fetch those through /tracks?ids=
    (the same call the web player and yt-dlp make) and keep playlist order.
    """
    stub_ids = [
        item["id"]
        for item in stubs
        if item.get("id") and not (item.get("title") or item.get("permalink_url"))
    ]
    if not stub_ids:
        return stubs
    hydrated: dict[int, dict] = {}
    for i in range(0, len(stub_ids), 50):  # the API caps ids= at 50 per call
        batch = ",".join(str(track_id) for track_id in stub_ids[i : i + 50])
        page = _api("/tracks", ids=batch)
        for item in page if isinstance(page, list) else []:
            hydrated[item["id"]] = item
    return [hydrated.get(item.get("id"), item) for item in stubs]


def resolve(url: str) -> Collection:
    """Resolve a SoundCloud track / set / profile URL into a Collection."""
    resource = _api("/resolve", url=url)
    kind = resource.get("kind")

    if kind == "track":
        track = _track_from_api(resource)
        if not track:
            raise ProviderError("That SoundCloud track can't be downloaded.")
        return Collection(
            kind="track",
            name=track.title,
            owner=", ".join(track.artists),
            cover_url=track.cover_url,
            tracks=[track],
        )

    if kind == "playlist":
        name = resource.get("title") or "Playlist"
        owner = (resource.get("user") or {}).get("username", "")
        cover = _artwork(resource)
        stubs = list(resource.get("tracks") or [])
        items = _hydrate_playlist_tracks(stubs)
        tracks = _tracks_from_items(items, album=name)
        if not tracks:
            raise ProviderError("That playlist appears to be empty or private.")
        if not cover:
            cover = next((t.cover_url for t in tracks if t.cover_url), None)
        return Collection(
            kind="album" if resource.get("is_album") else "playlist",
            name=name,
            owner=owner,
            cover_url=cover,
            tracks=tracks,
        )

    if kind == "user":
        name = resource.get("username") or "Artist"
        cover = _avatar(resource)
        items = _paginate(f"/users/{resource['id']}/tracks")
        tracks = _tracks_from_items(items, album=name)
        if not tracks:
            raise ProviderError("That artist has no public tracks.")
        if not cover:
            cover = next((t.cover_url for t in tracks if t.cover_url), None)
        return Collection(
            kind="playlist",
            name=name,
            owner=name,
            cover_url=cover,
            tracks=tracks,
        )

    raise ProviderError(f"Unsupported SoundCloud page ({kind or 'unknown'}).")


def _track_result(item: dict) -> SearchResult | None:
    # SNIP/BLOCK tracks are Go-only (DRM) — a download would fail anyway.
    if item.get("policy") in ("SNIP", "BLOCK"):
        return None
    if not item.get("permalink_url"):
        return None
    return SearchResult(
        kind="track",
        id=str(item.get("id", "")),
        name=item.get("title", ""),
        subtitle=(item.get("user") or {}).get("username", ""),
        cover_url=_artwork(item),
        url=item["permalink_url"],
        source="soundcloud",
    )


def _user_result(item: dict) -> SearchResult | None:
    if not item.get("permalink_url"):
        return None
    followers = _count(item.get("followers_count"), "follower")
    return SearchResult(
        kind="artist",
        id=str(item.get("id", "")),
        name=item.get("username", ""),
        subtitle=followers or "On SoundCloud",
        cover_url=(item.get("avatar_url") or "").replace("-large.", "-t300x300.")
        or None,
        url=item["permalink_url"],
        source="soundcloud",
    )


def _set_result(item: dict, kind: str) -> SearchResult | None:
    if not item.get("permalink_url"):
        return None
    owner = (item.get("user") or {}).get("username", "")
    tracks = _count(item.get("track_count"), "track")
    return SearchResult(
        kind=kind,
        id=str(item.get("id", "")),
        name=item.get("title", ""),
        subtitle=" · ".join(p for p in (owner, tracks) if p),
        cover_url=_artwork(item),
        url=item["permalink_url"],
        source="soundcloud",
    )


# Per-page quota for each of the four sub-searches. api-v2 takes a real
# `offset`, so page N simply asks for offset = N * quota.
PAGE_QUOTA = {"track": 30, "artist": 10, "album": 10, "playlist": 10}


def search(query: str, page: int = 0) -> list[SearchResult]:
    """Tracks, people, albums and playlists — the four calls run in parallel."""
    offset = lambda kind: page * PAGE_QUOTA[kind]  # noqa: E731
    with ThreadPoolExecutor(max_workers=4) as pool:
        tracks = pool.submit(
            _api, "/search/tracks", q=query,
            limit=PAGE_QUOTA["track"], offset=offset("track"),
        )
        users = pool.submit(
            _api, "/search/users", q=query,
            limit=PAGE_QUOTA["artist"], offset=offset("artist"),
        )
        albums = pool.submit(
            _api, "/search/albums", q=query,
            limit=PAGE_QUOTA["album"], offset=offset("album"),
        )
        playlists = pool.submit(
            _api, "/search/playlists_without_albums", q=query,
            limit=PAGE_QUOTA["playlist"], offset=offset("playlist"),
        )

    results: list[SearchResult] = []
    for item in tracks.result().get("collection") or []:
        if r := _track_result(item):
            results.append(r)
    for item in users.result().get("collection") or []:
        if r := _user_result(item):
            results.append(r)
    for item in albums.result().get("collection") or []:
        if r := _set_result(item, "album"):
            results.append(r)
    for item in playlists.result().get("collection") or []:
        if r := _set_result(item, "playlist"):
            results.append(r)
    return results
