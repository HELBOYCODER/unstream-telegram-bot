"""Provider-agnostic data models.

Metadata can come from Spotify's public embed pages, Deezer, iTunes, the
SoundCloud web API, or yt-dlp (YouTube / SoundCloud) — everything
downstream (downloader, jobs, API responses) only sees these shapes.
"""

from dataclasses import dataclass, field


class ProviderError(Exception):
    """User-facing error from a metadata provider."""


@dataclass
class Track:
    id: str
    title: str
    artists: list[str]
    album: str
    duration_ms: int
    cover_url: str | None
    track_number: int = 0
    release_date: str = ""
    # Set when the track already lives on a downloadable page (YouTube or
    # SoundCloud URL) — the downloader then skips the search step.
    source_url: str | None = None
    # 30-second audio preview (Deezer / iTunes / Spotify embed), if the
    # provider exposes one — lets the UI play a clip before downloading.
    preview_url: str | None = None

    @property
    def query(self) -> str:
        """Search query used to find this track on YouTube."""
        return f"{', '.join(self.artists)} - {self.title}"


@dataclass
class Collection:
    kind: str  # "track" | "album" | "playlist"
    name: str
    owner: str
    cover_url: str | None
    tracks: list[Track] = field(default_factory=list)


@dataclass
class SearchResult:
    kind: str  # "track" | "album" | "artist" | "playlist"
    id: str
    name: str
    subtitle: str
    cover_url: str | None
    url: str
    source: str = "deezer"  # deezer | itunes | youtube | soundcloud
