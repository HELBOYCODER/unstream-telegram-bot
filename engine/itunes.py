"""Search and resolve via Apple's iTunes Search API — no key, no account.

itunes.apple.com/search and /lookup are public JSON endpoints (the same ones
the iTunes desktop client used). They cover songs and albums, which also
makes pasted music.apple.com links resolvable. Playlists aren't exposed.
"""

import json
import re
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from .models import Collection, ProviderError, SearchResult, Track

_API = "https://itunes.apple.com"
_URL_RE = re.compile(
    r"music\.apple\.com/(?:[a-z]{2}/)?(album|song)/[^/]+/(?:id)?(\d+)"
)


def is_itunes_url(url: str) -> bool:
    return _URL_RE.search(url) is not None


def _get(path: str, **params) -> list[dict]:
    url = f"{_API}{path}?{urlencode(params)}"
    try:
        with urlopen(Request(url), timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        raise ProviderError(f"Could not reach iTunes: {exc.reason}") from exc
    except OSError as exc:
        # A timeout or reset while the body is read arrives unwrapped.
        raise ProviderError(f"Could not reach iTunes: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError("iTunes returned an unreadable response.") from exc
    return data.get("results") or []


def _artwork(item: dict) -> str | None:
    url = item.get("artworkUrl100") or item.get("artworkUrl60")
    return url.replace("100x100", "600x600").replace("60x60", "600x600") if url else None


def _track_from_api(item: dict) -> Track:
    return Track(
        id=str(item.get("trackId") or item.get("collectionId") or ""),
        title=item.get("trackName", "Unknown"),
        artists=[item.get("artistName", "Unknown")],
        album=item.get("collectionName", ""),
        duration_ms=int(item.get("trackTimeMillis") or 0),
        cover_url=_artwork(item),
        track_number=item.get("trackNumber", 0),
        release_date=(item.get("releaseDate") or "")[:4],
        preview_url=item.get("previewUrl") or None,
    )


# iTunes has no offset parameter — only `limit`, capped at 200. Paging means
# asking for everything up to the end of the requested page and slicing the
# tail, so the deepest page we can serve is API_MAX / PAGE_QUOTA.
PAGE_QUOTA = 25
API_MAX = 200


def search(query: str, page: int = 0) -> list[SearchResult]:
    offset = page * PAGE_QUOTA
    if offset >= API_MAX:
        return []
    limit = min(API_MAX, offset + PAGE_QUOTA)

    results: list[SearchResult] = []
    songs = _get("/search", term=query, media="music", entity="song", limit=limit)
    for item in songs[offset:]:
        if not item.get("trackViewUrl"):
            continue
        results.append(
            SearchResult(
                kind="track",
                id=str(item.get("trackId", "")),
                name=item.get("trackName", ""),
                subtitle=item.get("artistName", ""),
                cover_url=_artwork(item),
                url=item["trackViewUrl"],
                source="itunes",
            )
        )
    albums = _get("/search", term=query, media="music", entity="album", limit=limit)
    for item in albums[offset:]:
        if not item.get("collectionViewUrl"):
            continue
        year = (item.get("releaseDate") or "")[:4]
        artist = item.get("artistName", "")
        results.append(
            SearchResult(
                kind="album",
                id=str(item.get("collectionId", "")),
                name=item.get("collectionName", ""),
                subtitle=f"{artist}{' · ' + year if year else ''}",
                cover_url=_artwork(item),
                url=item["collectionViewUrl"],
                source="itunes",
            )
        )
    return results


def resolve(url: str) -> Collection:
    match = _URL_RE.search(url)
    if not match:
        raise ProviderError("That doesn't look like an Apple Music URL.")
    kind, item_id = match.group(1), match.group(2)

    # An album URL with ?i=<trackId> points at one song on that album.
    query = parse_qs(urlparse(url).query)
    if kind == "album" and query.get("i"):
        kind, item_id = "song", query["i"][0]

    if kind == "song":
        items = [i for i in _get("/lookup", id=item_id) if i.get("trackId")]
        if not items:
            raise ProviderError("iTunes doesn't know that song.")
        track = _track_from_api(items[0])
        return Collection(
            kind="track",
            name=track.title,
            owner=", ".join(track.artists),
            cover_url=track.cover_url,
            tracks=[track],
        )

    items = _get("/lookup", id=item_id, entity="song", limit=200)
    album = next((i for i in items if i.get("collectionType")), {})
    tracks = [_track_from_api(i) for i in items if i.get("wrapperType") == "track"]
    if not tracks:
        raise ProviderError("iTunes listed no tracks for that album.")
    return Collection(
        kind="album",
        name=album.get("collectionName", tracks[0].album),
        owner=album.get("artistName", ""),
        cover_url=_artwork(album) or tracks[0].cover_url,
        tracks=tracks,
    )
