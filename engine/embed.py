"""Resolve Spotify URLs without any API credentials.

Spotify's embed player (the widget any website can iframe) is served as a
public page at open.spotify.com/embed/<kind>/<id>. That page ships its
metadata — name, artists, cover art and the track list — as JSON inside
a <script id="__NEXT_DATA__"> tag. No account, no token, no API key.

The embed page only renders the first 100 rows of a playlist, so a playlist
longer than that tops its tail up from the same GraphQL endpoint the web
player itself pages with (api-partner.spotify.com/pathfinder), authorised by
the anonymous token the embed widget fetches (open.spotify.com/embed/api/token).
That token is what keeps this credential-free: no account, no app registration
— a public client token Spotify hands any embed page. Note it is *only* good
for pathfinder; api.spotify.com/v1 rejects it with 429 QUOTA_EXCEEDED on every
endpoint, since that API is for registered apps.

This is how most "Spotify downloader" websites work. Trade-off: it's an
undocumented page structure, so Spotify can change it at any time.
"""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Collection, ProviderError, Track

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.+?)</script>',
    re.DOTALL,
)
_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}(?:-[A-Z]{2})?/)?(track|album|playlist)/([A-Za-z0-9]+)"
)


def parse_url(url: str) -> tuple[str, str] | None:
    """Extract (kind, id) from a Spotify track/album/playlist URL, else None."""
    match = _URL_RE.search(url.strip())
    return (match.group(1), match.group(2)) if match else None


def _fetch_entity(kind: str, spotify_id: str) -> dict:
    url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code == 404:
            raise ProviderError(
                f"Spotify says that {kind} doesn't exist (or it's private)."
            ) from exc
        raise ProviderError(f"Spotify embed page returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise ProviderError(f"Could not reach Spotify: {exc.reason}") from exc

    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise ProviderError(
            "Could not read the Spotify embed page — its layout may have changed."
        )
    try:
        data = json.loads(match.group(1))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProviderError(
            "Spotify embed data was not in the expected format."
        ) from exc
    if not entity:
        raise ProviderError("Spotify embed page had no metadata for this item.")
    return entity


def _cover(entity: dict) -> str | None:
    # Album/playlist embeds use coverArt.sources; track embeds moved the
    # artwork under visualIdentity.image.
    sources = (entity.get("coverArt") or {}).get("sources") or []
    if sources:
        return max(sources, key=lambda s: s.get("width") or 0).get("url")
    images = (entity.get("visualIdentity") or {}).get("image") or []
    if images:
        return max(images, key=lambda s: s.get("maxWidth") or 0).get("url")
    return None


def _id_from_uri(uri: str) -> str:
    # "spotify:track:4uLU6..." -> "4uLU6..."
    return uri.rsplit(":", 1)[-1] if uri else ""


def _release_year(entity: dict) -> str:
    iso = (entity.get("releaseDate") or {}).get("isoString") or ""
    return iso[:4]


def _preview_url(entity: dict) -> str | None:
    # 30-second clip Spotify ships for embed players, when available.
    return (entity.get("audioPreview") or {}).get("url") or None


def _tracklist_track(
    item: dict, position: int, album_name: str, cover_url: str | None, year: str
) -> Track:
    return Track(
        id=_id_from_uri(item.get("uri", "")) or f"pos{position}",
        title=item.get("title", "Unknown"),
        artists=[a.strip() for a in (item.get("subtitle") or "").split(",") if a.strip()],
        album=album_name,
        duration_ms=int(item.get("duration") or 0),
        cover_url=cover_url,
        track_number=position,
        release_date=year,
        preview_url=_preview_url(item),
    )


_EMBED_CAP = 100  # how many rows the embed page renders before it stops
_PAGE = 100  # rows per pathfinder request
_PATHFINDER = "https://api-partner.spotify.com/pathfinder/v1/query"
# Pathfinder only accepts persisted queries: the document itself lives on
# Spotify's side and callers name it by hash. This is the hash the web player
# sends for a playlist's contents. Spotify rotates it on bundle releases; when
# that happens the request fails and _extend_playlist falls back to the embed's
# first 100 rows. To refresh it, grep open.spotify.com's web-player JS bundle
# for "fetchPlaylistContents" — the hash is the third argument beside it.
_PLAYLIST_QUERY_HASH = (
    "86dde7b9d9356e2369414647cf6950cfed96e778e129cfdfc99aea6c1613b3b0"
)
# Token endpoints on open.spotify.com itself, so a deployment that can reach
# the embed pages (i.e. any one that works today) can reach these too. The
# first is what the embed widget currently uses; the second is its classic
# equivalent, kept because Spotify has been known to block one but not the
# other for some networks.
_TOKEN_URLS = (
    "https://open.spotify.com/embed/api/token",
    "https://open.spotify.com/get_access_token?reason=transport&productType=web_player",
)


def _anonymous_token() -> str:
    """The anonymous access token the embed widget uses against the API.

    No account, no app registration: Spotify mints these for any public embed
    page. If neither endpoint will answer, the playlist simply stays at the
    embed's first 100 rows rather than turning into an error.
    """
    for url in _TOKEN_URLS:
        try:
            req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            token = data.get("accessToken")
            if token:
                return token
        except (HTTPError, URLError, json.JSONDecodeError, TypeError):
            continue
    raise ProviderError("Spotify would not issue an anonymous token.")


def _playlist_page(spotify_id: str, token: str, offset: int) -> dict:
    """One page of playlist contents from pathfinder."""
    query = urlencode(
        {
            "operationName": "fetchPlaylistContents",
            "variables": json.dumps(
                {
                    "uri": f"spotify:playlist:{spotify_id}",
                    "offset": offset,
                    "limit": _PAGE,
                    "enableWatchFeedEntrypoint": False,
                    "includeEpisodeContentRatingsV2": False,
                }
            ),
            "extensions": json.dumps(
                {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": _PLAYLIST_QUERY_HASH,
                    }
                }
            ),
        }
    )
    req = Request(
        f"{_PATHFINDER}?{query}",
        headers={
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise ProviderError(f"Spotify's playlist API returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise ProviderError(f"Could not reach Spotify's playlist API: {exc.reason}") from exc
    # GraphQL reports a rejected or stale query as 200 with an errors array.
    if data.get("errors"):
        raise ProviderError("Spotify's playlist API rejected the query.")
    try:
        return data["data"]["playlistV2"]["content"]
    except (KeyError, TypeError) as exc:
        raise ProviderError("Spotify's playlist API answered in an unexpected shape.") from exc


def _api_playlist_tracks(spotify_id: str, token: str) -> list[dict]:
    """Every playlist row past the embed's cap, paginated a page at a time."""
    items: list[dict] = []
    offset = _EMBED_CAP
    while True:
        content = _playlist_page(spotify_id, token, offset)
        batch = content.get("items") or []
        items.extend(batch)
        total = int(content.get("totalCount") or 0)
        if not batch or offset + len(batch) >= total:
            break
        offset += len(batch)
    return items


def _api_track(item: dict, position: int) -> Track | None:
    """A pathfinder row into our model, or None if it isn't a downloadable track.

    Episodes and local files sit in the same list under a different wrapper
    type, and neither is something we can go and find the audio for.
    """
    wrapper = item.get("itemV2") or {}
    if wrapper.get("__typename") != "TrackResponseWrapper":
        return None
    data = wrapper.get("data") or {}
    album = data.get("albumOfTrack") or {}
    sources = (album.get("coverArt") or {}).get("sources") or []
    artists = [
        (a.get("profile") or {}).get("name", "")
        for a in (data.get("artists") or {}).get("items") or []
    ]
    return Track(
        id=_id_from_uri(data.get("uri", "")) or f"pos{position}",
        title=data.get("name", "Unknown"),
        artists=[a for a in artists if a] or ["Unknown"],
        album=album.get("name", ""),
        duration_ms=int((data.get("trackDuration") or {}).get("totalMilliseconds") or 0),
        cover_url=(
            max(sources, key=lambda s: s.get("width") or 0).get("url")
            if sources
            else None
        ),
        track_number=position,
        release_date=((album.get("date") or {}).get("isoString") or "")[:4],
        # Pathfinder carries no 30-second preview, so rows past the cap have
        # no in-app preview. It costs nothing at download time.
        preview_url=None,
    )


def _extend_playlist(tracks: list[Track], spotify_id: str) -> list[Track]:
    """Append the rows the embed page capped away, best-effort.

    A playlist at exactly the embed's cap is the only way to tell it *could*
    be longer, so that is the trigger: a 100-row playlist asks for row 101,
    hears "no more", and keeps exactly what it had. Failure is silent by
    design — an unreachable API must never turn a playlist that already
    resolved into an error, so the worst outcome is the first 100 rows, which
    is exactly today's behaviour.
    """
    if len(tracks) < _EMBED_CAP:
        return tracks
    try:
        token = _anonymous_token()
        items = _api_playlist_tracks(spotify_id, token)
    except (ProviderError, OSError, ValueError):
        return tracks
    extra: list[Track] = []
    for item in items:
        track = _api_track(item, len(tracks) + len(extra) + 1)
        if track is not None:
            extra.append(track)
    return tracks + extra


def resolve(kind: str, spotify_id: str) -> Collection:
    entity = _fetch_entity(kind, spotify_id)
    name = entity.get("name") or entity.get("title") or "Unknown"
    cover = _cover(entity)

    if kind == "track":
        artists = [a.get("name", "") for a in entity.get("artists") or []]
        track = Track(
            id=spotify_id,
            title=name,
            artists=[a for a in artists if a] or ["Unknown"],
            album=(entity.get("albumOfTrack") or {}).get("name", ""),
            duration_ms=int(entity.get("duration") or 0),
            cover_url=cover,
            release_date=_release_year(entity),
            preview_url=_preview_url(entity),
        )
        return Collection(
            kind="track",
            name=track.title,
            owner=", ".join(track.artists),
            cover_url=cover,
            tracks=[track],
        )

    items = entity.get("trackList") or []
    if not items:
        raise ProviderError(f"The {kind} embed page listed no tracks.")
    year = _release_year(entity)

    if kind == "album":
        # Album embeds carry the artist in "subtitle", not an artists array.
        owner = entity.get("subtitle") or ", ".join(
            a.get("name", "") for a in entity.get("artists") or [] if a.get("name")
        )
        # Album embeds don't repeat the artist on every row; fall back to it.
        tracks = []
        for i, item in enumerate(items, start=1):
            track = _tracklist_track(item, i, album_name=name, cover_url=cover, year=year)
            if not track.artists and owner:
                track.artists = [owner]
            tracks.append(track)
        return Collection(
            kind="album", name=name, owner=owner, cover_url=cover, tracks=tracks
        )

    # playlist — rows carry "title" and "subtitle" (artist names). The embed
    # caps these at _EMBED_CAP rows; _extend_playlist fetches the rest.
    tracks = [
        _tracklist_track(item, i, album_name="", cover_url=cover, year="")
        for i, item in enumerate(items, start=1)
    ]
    tracks = _extend_playlist(tracks, spotify_id)
    return Collection(
        kind="playlist",
        name=name,
        owner=entity.get("subtitle", ""),
        cover_url=cover,
        tracks=tracks,
    )
