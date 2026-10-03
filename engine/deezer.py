"""Search and resolve via Deezer's public API — no key, no account.

Deezer exposes its catalog (search, tracks, albums, playlists) at
api.deezer.com without any authentication, which makes it a perfect
metadata source for in-app search. The downloader only needs
artist + title + duration, so where the metadata comes from doesn't
matter — the audio is found on YouTube either way.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Collection, ProviderError, SearchResult, Track

_API = "https://api.deezer.com"
_URL_RE = re.compile(
    r"deezer\.com/(?:[a-z]{2}/)?(track|album|playlist)/(\d+)"
)


def is_deezer_url(url: str) -> bool:
    return _URL_RE.search(url) is not None


def _get(path: str, **params) -> dict:
    url = f"{_API}{path}"
    if params:
        url += "?" + urlencode(params)
    try:
        with urlopen(Request(url), timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        raise ProviderError(f"Could not reach Deezer: {exc.reason}") from exc
    except OSError as exc:
        # A timeout or reset while the body is read arrives unwrapped.
        raise ProviderError(f"Could not reach Deezer: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError("Deezer returned an unreadable response.") from exc
    if isinstance(data, dict) and data.get("error"):
        message = data["error"].get("message", "unknown error")
        raise ProviderError(f"Deezer API error: {message}")
    return data


def _track_result(item: dict) -> SearchResult:
    return SearchResult(
        kind="track",
        id=str(item["id"]),
        url=item.get("link", f"https://www.deezer.com/track/{item['id']}"),
        name=item.get("title", ""),
        subtitle=(item.get("artist") or {}).get("name", ""),
        cover_url=(item.get("album") or {}).get("cover_medium"),
    )


def _album_result(item: dict, artist: str = "") -> SearchResult:
    year = (item.get("release_date") or "")[:4]
    record_type = (item.get("record_type") or "").upper()
    artist = (item.get("artist") or {}).get("name") or artist
    subtitle = " · ".join(p for p in (artist, year, record_type if record_type not in ("", "ALBUM") else "") if p)
    return SearchResult(
        kind="album",
        id=str(item["id"]),
        url=item.get("link", f"https://www.deezer.com/album/{item['id']}"),
        name=item.get("title", ""),
        subtitle=subtitle,
        cover_url=item.get("cover_medium"),
    )


def _artist_result(item: dict) -> SearchResult:
    count = item.get("nb_album")
    return SearchResult(
        kind="artist",
        id=str(item["id"]),
        url=item.get("link", f"https://www.deezer.com/artist/{item['id']}"),
        name=item.get("name", ""),
        subtitle=f"{count} releases" if count else "Artist",
        cover_url=item.get("picture_medium"),
    )


def _playlist_result(item: dict) -> SearchResult:
    owner = (item.get("user") or {}).get("name", "")
    count = item.get("nb_tracks")
    subtitle = " · ".join(
        part
        for part in (f"by {owner}" if owner else "", f"{count} tracks" if count else "")
        if part
    )
    return SearchResult(
        kind="playlist",
        id=str(item["id"]),
        url=item.get("link", f"https://www.deezer.com/playlist/{item['id']}"),
        name=item.get("title", ""),
        subtitle=subtitle,
        cover_url=item.get("picture_medium"),
    )


# Per-page quota for each of the four sub-searches. Deezer pages with an
# absolute `index`, so page N asks for index = N * quota.
PAGE_QUOTA = {"track": 30, "album": 20, "artist": 12, "playlist": 12}


def search(query: str, page: int = 0) -> list[SearchResult]:
    """Tracks, albums, artists and playlists — the four calls run in parallel."""
    index = lambda kind: page * PAGE_QUOTA[kind]  # noqa: E731
    with ThreadPoolExecutor(max_workers=4) as pool:
        tracks = pool.submit(
            _get, "/search/track", q=query,
            limit=PAGE_QUOTA["track"], index=index("track"),
        )
        albums = pool.submit(
            _get, "/search/album", q=query,
            limit=PAGE_QUOTA["album"], index=index("album"),
        )
        artists = pool.submit(
            _get, "/search/artist", q=query,
            limit=PAGE_QUOTA["artist"], index=index("artist"),
        )
        playlists = pool.submit(
            _get, "/search/playlist", q=query,
            limit=PAGE_QUOTA["playlist"], index=index("playlist"),
        )

    results: list[SearchResult] = []
    results += [_track_result(i) for i in tracks.result().get("data") or []]
    results += [_album_result(i) for i in albums.result().get("data") or []]
    results += [_artist_result(i) for i in artists.result().get("data") or []]
    results += [_playlist_result(i) for i in playlists.result().get("data") or []]
    return results


def artist(artist_id: str) -> dict:
    """Full artist page: profile, top tracks, and complete discography."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        profile_f = pool.submit(_get, f"/artist/{artist_id}")
        top_f = pool.submit(_get, f"/artist/{artist_id}/top", limit=10)
        albums_f = pool.submit(_get, f"/artist/{artist_id}/albums", limit=100)

    profile = profile_f.result()
    name = profile.get("name", "")

    top = [_track_result(item) for item in top_f.result().get("data") or []]

    # Discography is paginated via an absolute "next" URL, like playlists.
    page = albums_f.result()
    items = list(page.get("data") or [])
    next_url = page.get("next")
    while next_url:
        page = json.loads(urlopen(Request(next_url), timeout=15).read().decode())
        items.extend(page.get("data") or [])
        next_url = page.get("next")
    items.sort(key=lambda i: i.get("release_date") or "", reverse=True)
    albums = [_album_result(item, artist=name) for item in items]

    return {
        "id": str(profile.get("id", artist_id)),
        "name": name,
        "picture_url": profile.get("picture_big") or profile.get("picture_medium"),
        "fan_count": profile.get("nb_fan"),
        "top_tracks": top,
        "albums": albums,
    }


def _track_from_api(item: dict, album_name: str = "", cover_url: str | None = None) -> Track:
    album = item.get("album") or {}
    return Track(
        id=str(item["id"]),
        title=item.get("title", "Unknown"),
        artists=[(item.get("artist") or {}).get("name", "Unknown")],
        album=album.get("title", album_name),
        duration_ms=int(item.get("duration") or 0) * 1000,
        cover_url=album.get("cover_big") or cover_url,
        track_number=item.get("track_position", 0),
        release_date=(item.get("release_date") or "")[:4],
        preview_url=item.get("preview") or None,
    )


def resolve(url: str) -> Collection:
    match = _URL_RE.search(url)
    if not match:
        raise ProviderError("That doesn't look like a Deezer URL.")
    kind, deezer_id = match.group(1), match.group(2)

    if kind == "track":
        item = _get(f"/track/{deezer_id}")
        track = _track_from_api(item)
        return Collection(
            kind="track",
            name=track.title,
            owner=", ".join(track.artists),
            cover_url=track.cover_url,
            tracks=[track],
        )

    if kind == "album":
        album = _get(f"/album/{deezer_id}")
        cover = album.get("cover_big")
        name = album.get("title", "")
        year = (album.get("release_date") or "")[:4]
        artist = (album.get("artist") or {}).get("name", "")
        tracks = []
        for i, item in enumerate((album.get("tracks") or {}).get("data") or [], 1):
            track = _track_from_api(item, album_name=name, cover_url=cover)
            track.cover_url = track.cover_url or cover
            track.track_number = track.track_number or i
            track.release_date = track.release_date or year
            tracks.append(track)
        return Collection(
            kind="album", name=name, owner=artist, cover_url=cover, tracks=tracks
        )

    # playlist — track list is paginated via an absolute "next" URL
    playlist = _get(f"/playlist/{deezer_id}")
    cover = playlist.get("picture_big")
    tracks_page = playlist.get("tracks") or {}
    items = list(tracks_page.get("data") or [])
    next_url = tracks_page.get("next")
    while next_url:
        page = json.loads(urlopen(Request(next_url), timeout=15).read().decode())
        items.extend(page.get("data") or [])
        next_url = page.get("next")
    tracks = [_track_from_api(item, cover_url=cover) for item in items]
    return Collection(
        kind="playlist",
        name=playlist.get("title", ""),
        owner=(playlist.get("creator") or {}).get("name", ""),
        cover_url=cover,
        tracks=tracks,
    )
