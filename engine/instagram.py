"""Instagram post / reel metadata without any API credentials.

Instagram's public web pages are fully client-side rendered, so scraping the
HTML yields nothing. But the page itself fires a public GraphQL query against
`/graphql/query/` — the same one yt-dlp's Instagram extractor uses
(InstagramIE._real_extract, doc_id 8845758582119845) — which returns the whole
post without a session, a login or an app registration.

We resolve the caption into a title + artist guess, then hand it to the music
catalogs for the real download. If the post is a video we also record the CDN
url so the downloader can skip the search step entirely.

Trade-off: this is an undocumented endpoint, so Instagram can change it at any
time. When that happens the bot falls back to a catalog search on whatever
caption text we could still read, so an Instagram link never hard-fails.
"""

import html as htmlmod
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from .models import Collection, ProviderError, SearchResult, Track

# The public web-app GraphQL query the Instagram page itself fires. yt-dlp
# keeps this doc_id current, so a yt-dlp upgrade silently keeps us working.
_GRAPHQL_DOC_ID = "8845758582119845"
_GRAPHQL_URL = "https://www.instagram.com/graphql/query/"
_API_BASE_URL = "https://i.instagram.com/api/v1"
_APP_ID = "936619743392459"

# Instagram hands a working csrf token to any anonymous client that asks for a
# content ruling first — same warm-up yt-dlp does before the GraphQL call.
_RULES_URL = f"{_API_BASE_URL}/web/get_ruling_for_content/"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# instagram.com/<user>/p/<id>/, /reel/<id>/, /reels/<id>/, /tv/<id>/
# instagram.com/share/<id>/, and ?igsh=... tracking junk after it.
_URL_RE = re.compile(
    r"(?:www\.|m\.)?instagram\.com/"
    r"(?:[A-Za-z0-9._\-]+/)?"
    r"(?:p|reel|reels|tv)/"
    r"(?P<id>[A-Za-z0-9_\-]+)"
)

# Instagram shortcodes are a base64 alphabet (with - and _) of 11 chars, but
# share/v1 links and some legacy posts run longer, so we stay permissive.
_MIN_ID_LEN = 6

# Characters never legal in an Instagram shortcode — lets us stop early on a
# malformed link instead of asking the API about something it will 400 on.
_BAD_ID_RE = re.compile(r"[^A-Za-z0-9_\-]")


def parse_url(url: str) -> str | None:
    """Extract the shortcode id from an Instagram post/reel URL, else None."""
    match = _URL_RE.search(url.strip())
    if not match:
        return None
    shortcode = match.group("id")
    if len(shortcode) < _MIN_ID_LEN or _BAD_ID_RE.search(shortcode):
        return None
    return shortcode


def is_instagram_url(url: str) -> bool:
    return parse_url(url) is not None


def _id_to_pk(shortcode: str) -> int:
    """Convert a shortcode to the numeric media id the v1 API speaks.

    Instagram's shortcodes are a base64 encoding (custom alphabet, LSB-first)
    of the media's numeric primary key. The GraphQL route takes the shortcode
    directly, but the warm-up endpoint wants the pk.
    """
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    pk = 0
    for char in shortcode:
        pk = (pk * 64) + alphabet.index(char)
    return pk


def _api_headers() -> dict:
    return {
        "User-Agent": _USER_AGENT,
        "x-ig-app-id": _APP_ID,
        "X-ASBD-ID": "198387",
        "X-IG-WWW-Claim": "0",
        "Origin": "https://www.instagram.com",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "Sec-Fetch-Site": "same-origin",
    }


def _get_json(url: str, headers: dict, timeout: int = 20) -> dict:
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        if exc.code in (404, 410):
            raise ProviderError("این پست اینستاگرام حذف شده یا خصوصی است.")
        if exc.code in (403, 429):
            raise ProviderError(
                "اینستاگرام الان درخواست‌های زیاد را مسدود می‌کند — "
                "کمی صبر کن و دوباره امتحان کن."
            )
        raise ProviderError(f"اینستاگرام خطای HTTP {exc.code} داد.")
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ProviderError(f"اتصال به اینستاگرام برقرار نشد: {exc}")


def _fetch_media(shortcode: str) -> dict:
    """Fetch the xdt_shortcode_media node for a shortcode via public GraphQL.

    Mirrors InstagramIE._real_extract exactly: warm up with a content-ruling
    request to obtain a csrf token, then fire the query.
    """
    try:
        pk = _id_to_pk(shortcode)
        warmup = _get_json(
            f"{_RULES_URL}?content_type=MEDIA&target_id={pk}",
            _api_headers(),
            timeout=15,
        )
    except ProviderError:
        warmup = {}

    headers = dict(_api_headers())
    headers["X-CSRFToken"] = ""
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Referer"] = f"https://www.instagram.com/p/{shortcode}/"
    if warmup.get("status") == "ok":
        headers["X-CSRFToken"] = "instagram"

    variables = json.dumps(
        {
            "shortcode": shortcode,
            "child_comment_count": 3,
            "fetch_comment_count": 40,
            "parent_comment_count": 24,
            "has_threaded_comments": True,
        },
        separators=(",", ":"),
    )
    url = (
        _GRAPHQL_URL
        + "?"
        + urlencode({"doc_id": _GRAPHQL_DOC_ID, "variables": variables})
    )
    data = _get_json(url, headers, timeout=25)
    media = _dig(data, ("data", "xdt_shortcode_media"))
    if not media:
        raise ProviderError(
            "اینستاگرام این پست را بدون ورود نشان نمی‌دهد — ممکن است خصوصی باشد."
        )
    return media

def _scrape_embed(shortcode: str) -> dict | None:
    """Fallback: parse whatever metadata the public embed page carries.

    The embed page is server-rendered with basic og: tags for public posts.
    It does not include the video CDN url, but caption/username/thumbnail
    sometimes leak through — good enough for a catalog search.
    """
    try:
        url = f"https://www.instagram.com/p/{shortcode}/embed/"
        req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
        with urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError):
        return None

    media: dict = {}

    og_title = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    og_desc = re.search(r'<meta property="og:description" content="([^"]*)"', html)
    og_image = re.search(r'<meta property="og:image" content="([^"]*)"', html)
    if og_image:
        media["display_url"] = htmlmod.unescape(og_image.group(1))

    # Caption sometimes appears in a JS-escaped blob — look for text patterns
    caption = None
    m = re.search(r'"caption":"((?:[^"\\]|\\.)*)"', html)
    if m:
        try:
            caption = json.loads(f'"{m.group(1)}"')
        except json.JSONDecodeError:
            caption = m.group(1)
    if not caption and og_desc:
        caption = htmlmod.unescape(og_desc.group(1))

    if caption:
        # Embed pages prefix with "Username on Instagram: ..." — strip that
        caption = re.sub(r"^[^:]{0,60}on Instagram:\s*", "", caption)
        media["caption"] = {"text": caption}

    owner = None
    m = re.search(r'"username":"([^"]+)"', html)
    if m:
        owner = m.group(1)
    elif og_title:
        title = htmlmod.unescape(og_title.group(1))
        m2 = re.match(r"(.+?)\s+on Instagram", title)
        if m2:
            owner = m2.group(1).strip()
    if owner:
        media["owner"] = {"username": owner}

    m = re.search(r'"video_duration":([\d.]+)', html)
    if m:
        media["video_duration"] = float(m.group(1))

    return media or None


def _dig(node, keys):
    """Follow a path of dict keys; return None as soon as one is missing."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _caption(media: dict) -> str:
    """Caption text — the edge_media_to_caption relay node."""
    text = _dig(media, ("caption", "text"))
    if not text:
        edges = _dig(media, ("edge_media_to_caption", "edges")) or []
        if edges and isinstance(edges[0], dict):
            text = _dig(edges[0], ("node", "text"))
    return (text or "").strip()


def _owner(media: dict) -> str:
    return _dig(media, ("owner", "username")) or ""


def _thumbnail(media: dict) -> str | None:
    for key in ("thumbnail_src", "display_url", "display_resources"):
        value = media.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, list):
            for item in value:
                url = item.get("src") if isinstance(item, dict) else None
                if isinstance(url, str) and url.startswith("http"):
                    return url
    return None


def _video_url(media: dict) -> str | None:
    """Direct CDN url for a video post. None for image-only posts."""
    url = media.get("video_url")
    if isinstance(url, str) and url.startswith("http"):
        return url
    # Carousel posts carry one video_url per sidecar child.
    for edge in _dig(media, ("edge_sidecar_to_children", "edges")) or []:
        child = _dig(edge, ("node",)) if isinstance(edge, dict) else None
        url = (child or {}).get("video_url")
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None


def _clean_caption(text: str) -> str:
    """Strip hashtags, mentions and urls — what is left is usually the song."""
    text = re.sub(r"#[\w\-\.]+", " ", text)
    text = re.sub(r"@[A-Za-z0-9._\-]+", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    # Emoji and repeated punctuation make bad search queries.
    text = re.sub(r"[\U0001F000-\U0001FAFF➀-➿←-⇿⬀-⯿️⃣]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_song_line(text: str) -> tuple[str, list[str]]:
    """Split a caption line like 'Artist - Title' into (title, artists)."""
    for sep in (" - ", " – ", " — ", " ~ "):
        if sep in text:
            left, _, right = text.partition(sep)
            return right.strip(), [left.strip()]
    if " by " in text:
        left, _, right = text.partition(" by ")
        return right.strip(), [left.strip()]
    return text.strip(), []


def resolve(url: str) -> Collection:
    """Resolve an Instagram post/reel URL to a Collection.

    The track carries the caption as its title and the poster as the artist
    guess. If the post is a video, source_url points at the CDN mp4 so the
    downloader can skip the catalog search. When Instagram refuses the
    anonymous GraphQL call we raise a helpful error telling the user to send
    the song name so the bot can search catalogs instead.
    """
    shortcode = parse_url(url)
    if not shortcode:
        raise ProviderError("این لینک اینستاگرام پست یا ریلز نیست.")

    try:
        media = _fetch_media(shortcode)
    except ProviderError:
        # Anonymous GraphQL was refused (login wall / rate limit). Try the
        # embed page as a last resort for og: metadata.
        media = _scrape_embed(shortcode) or {}

    raw_caption = _caption(media)
    owner = _owner(media)
    video_url = _video_url(media)
    thumb = _thumbnail(media)
    duration = media.get("video_duration") or 0
    release = media.get("taken_at_timestamp") or media.get("taken_at") or 0

    caption = _clean_caption(raw_caption)
    title, artists = _split_song_line(caption)

    if not media:
        # Instagram refused the anonymous read (rate limit or login wall).
        # The bot turns this into a search prompt instead of a dead end.
        raise ProviderError(
            "اینستاگرام الان اجازه خواندن این پست را به ربات نمی‌دهد "
            "(دیوار ورود یا محدودیت درخواست). اسم آهنگ و خواننده را "
            "برای من بنویس تا از کاتالوگ‌های موزیک پیدایش کنم."
        )

    if not title:
        title = f"Instagram by {owner}" if owner else "Instagram post"

    duration_ms = int((duration or 0) * 1000)
    release_date = ""
    if release:
        release_date = str(release)

    track = Track(
        id=shortcode,
        title=title,
        artists=artists or ([owner] if owner else ["Instagram"]),
        album="",
        duration_ms=duration_ms,
        cover_url=thumb,
        track_number=1,
        release_date=release_date,
        source_url=video_url,
        preview_url=None,
    )
    return Collection(
        kind="track",
        name=title,
        owner=owner or "Instagram",
        cover_url=thumb,
        tracks=[track],
    )


def search(query: str, page: int = 0) -> list[SearchResult]:
    """Instagram is not a music catalog, so it does not answer searches."""
    return []
