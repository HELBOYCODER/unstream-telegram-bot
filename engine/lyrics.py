"""Fetch lyrics from LRCLIB, falling back to Genius, and cache them on disk.

Both sources are keyless — no account, no API key, nothing that can be
revoked or start charging per call — which is the same constraint every other
metadata provider in this app keeps.

Why two sources: LRCLIB is the better one (structured API, plain *and*
time-synced lyrics) but its Persian songs are keyed by romanized titles, so a
catalog that hands us Persian script can never match them. Genius's internal
search API matches Persian queries directly and its song pages carry the
lyrics, so it is the Farsi fallback. English songs are almost always answered
by LRCLIB and never reach Genius.

A track's lyrics can be looked up twice in the worst case — once for the
on-demand view in the UI and once while tagging its download — and a long
album asks for every track in it. So results and misses go into a small
SQLite file next to analytics.db. Misses are cached too: a track with no
lyrics is unlikely to gain them within the hour, and there is no point paying
a third party twice for the same nothing.
"""

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

LRCLIB_API = "https://lrclib.net/api"
USER_AGENT = "Unstream/0.0.1 (self-hosted music downloader)"
# Genius's song pages and internal API sit behind a JS app; it answers a real
# browser agent and ignores the bare-library one, so it gets its own.
GENIUS_API = "https://genius.com/api"
GENIUS_PAGE = "https://genius.com"
GENIUS_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Last resort: keyless, plain text only, and unverifiable. See _fetch_lyrics_ovh.
LYRICS_OVH_API = "https://api.lyrics.ovh/v1"
REQUEST_TIMEOUT = 5.0

DB_PATH = Path(
    os.getenv("LYRICS_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "lyrics.db")
)

# How long a cached miss stays trusted before LRCLIB is asked again.
MISS_TTL_HOURS = 24 * 7

# Arabic-script codepoints (Arabic, Persian, Urdu…). Anything mostly in here
# is Arabic-script text and wants a `fas` ID3 language tag on its lyrics.
_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")

# An LRC timestamp: [mm:ss], [mm:ss.xx], or [hh:mm:ss.xx]. Metadata lines
# like [ar:Artist] are not touched — the `:` there is not between two digits.
_LRC_TIME_RE = re.compile(r"\[(?:\d{1,2}:)?\d{1,2}:\d{1,2}(?:\.\d{1,3})?\]")
_LRC_META_RE = re.compile(r"^\[[a-z]+:")

# How far (in seconds) a search hit may drift from the catalog duration before
# it stops being "this song". Looser than the downloader's 20 s because lyrics
# catalogs key on title, and a remastered or live take can run long.
MAX_DURATION_DRIFT = 60

# The same bound for a hit whose name and artist both match exactly. Wider,
# because an exact name is real evidence and remasters legitimately run a few
# seconds long.
MAX_EXACT_DRIFT = 90

# Shortest answer worth trusting from a source with nothing to check it
# against. Only applied to lyrics.ovh — LRCLIB hits are verified by duration,
# and a genuinely short song there is fine.
MIN_PLAUSIBLE_CHARS = 120


@dataclass
class Lyrics:
    plain: str
    synced: str
    source: str  # "lrclib-get" | "lrclib-search" | "genius"


class LyricsUnavailable(Exception):
    """Network or server failure — transient, so never cached as a miss."""


def strip_lrc(synced: str) -> str:
    """Turn synced LRC into plain text: drop timestamps and metadata lines.

    Blank source lines survive as blank lines, so verse breaks in the LRC
    stay visible in the plain text (LRCLIB separates stanzas with them).
    """
    lines: list[str] = []
    for raw in synced.splitlines():
        if not raw.strip():
            lines.append("")
            continue
        line = _LRC_TIME_RE.sub("", raw).strip()
        if not line or _LRC_META_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def detect_lang(text: str) -> str:
    """ISO 639-2 language code for lyrics: 'fas' for Arabic-script, else 'eng'.

    Only meaningful when a whole lyric is mostly one script; the >10% bar
    keeps a stray English line in a Persian song from flipping the tag.
    """
    if not text:
        return "eng"
    letters = sum(1 for c in text if c.isalpha())
    if letters and len(_ARABIC_RE.findall(text)) / letters > 0.1:
        return "fas"
    return "eng"


# Arabic-script combining marks and tatweel: they split a letter in "گُلِ یَخ"
# from the same letter in "گل یخ", and comparing across catalogs must not
# care. Also stripped from the cache key so the two forms share one row.
_ARABIC_MARKS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")

# Several Persian letters have an Arabic twin that looks near-identical on
# screen but carries a different codepoint, and catalogs mix them constantly:
# "\u06A9\u06CC" typed with Arabic kaf and yeh is a different string from the Persian
# spelling, with nothing to see. Folding the twins is what lets one song have
# one cache key instead of two that each half-miss.
#
# ZWNJ is deleted rather than spaced. Persian writes compounds with it
# ("\u0645\u06CC\u200C\u062E\u0648\u0627\u0645") and catalogs routinely omit it ("\u0645\u06CC\u062E\u0648\u0627\u0645"); turning it into a
# space leaves those two disagreeing, while removing it makes them identical.
# The cost is that an explicit space ("\u0628\u0686\u0647 \u0647\u0627") no longer matches \u2014 a rarer
# spelling, and a misspelling, so it is the right side to lose.
_ARABIC_FOLD = str.maketrans(
    {
        "\u064A": "\u06CC",  # Arabic yeh        -> Farsi yeh
        "\u0649": "\u06CC",  # alef maksura      -> Farsi yeh
        "\u0643": "\u06A9",  # Arabic kaf        -> keheh
        "\u0622": "\u0627",  # alef madda        -> alef
        "\u0623": "\u0627",  # alef + hamza above
        "\u0625": "\u0627",  # alef + hamza below
        "\u0629": "\u0647",  # teh marbuta       -> heh
        "\u200C": "",  # ZWNJ
        "\u200D": "",  # ZWJ
        "\u200E": "",  # LTR mark
        "\u200F": "",  # RTL mark
        # Both digit families read as the ASCII digit they mean, so "\u06F7" and
        # "\u0667" and "7" are one number.
        **{chr(0x0660 + n): str(n) for n in range(10)},  # Arabic-Indic
        **{chr(0x06F0 + n): str(n) for n in range(10)},  # Persian
    }
)


def _normalize(artist: str, title: str) -> str:
    """Cache key: the pair lowercased, punctuation collapsed."""
    text = f"{artist} - {title}".lower()
    return _squash(text)


def _squash(text: str) -> str:
    """Lowercase and collapse punctuation, keeping Arabic-script letters.

    Used for comparing names across catalogs, where a Persian song appears as
    "گل یخ" in one place and "Gole Yakh - گُلِ یَخ" in another — stripping
    non-ASCII would turn both into empty strings and match anything.

    Arabic-script letters are folded to their Persian twins first, so the same
    word spelled either way compares equal \u2014 see `_ARABIC_FOLD`.
    """
    text = _ARABIC_MARKS_RE.sub("", text).translate(_ARABIC_FOLD)
    return re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", text.lower()).strip()


# Decorations a catalog hangs off a title that no lyrics database keys on:
# a parenthesised qualifier, or a trailing " - Remastered 2013" style suffix.
# Only suffixes are stripped, never a leading segment, because "(What's the
# Story) Morning Glory" is the title.
#
# The optional year is not decoration: catalogs write both "- Remastered 2011"
# and "- 2013 Remaster", and without it the second shape sails straight past.
_DECORATION = (
    r"(?:\d{4}\s+)?(?:feat|ft|with|remaster|remastered|live|acoustic|radio edit|"
    r"single version|album version|bonus track|deluxe|explicit|mono|stereo)\b"
)
_TITLE_PAREN_RE = re.compile(rf"\s*[\(\[]\s*{_DECORATION}[^\)\]]*[\)\]]", re.IGNORECASE)
_TITLE_SUFFIX_RE = re.compile(rf"\s+-\s+{_DECORATION}.*$", re.IGNORECASE)

# A run of Arabic-script text, used to split a mixed-script string. iTunes
# hands back titles like "Gole Yakh  گل یخ" — one field, both scripts.
_ARABIC_RUN_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]+")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _strip_decorations(title: str) -> str:
    """The title without "(feat. X)" / " - 2013 Remaster" style trimmings."""
    stripped = _TITLE_SUFFIX_RE.sub("", _TITLE_PAREN_RE.sub("", title))
    return stripped.strip(" -–—") or title


def _primary_artist(artist: str) -> str:
    """Just the first credited artist. Lyrics catalogs file a song under one."""
    for sep in (",", " & ", " feat. ", " ft. ", " featuring ", " x ", " with "):
        if sep in artist:
            artist = artist.split(sep)[0]
    return artist.strip()


def _latin_part(text: str) -> str:
    """The Latin half of a mixed-script string, or "" if there isn't one.

    Measured: LRCLIB stores "Gole Yakh", and asking it for the whole of
    "Gole Yakh  گل یخ" returns nothing. The Latin half is the half it knows.
    """
    if not _ARABIC_RUN_RE.search(text) or not _LATIN_RE.search(text):
        return ""
    return re.sub(r"\s{2,}", " ", _ARABIC_RUN_RE.sub(" ", text)).strip(" -–—,")


# How many distinct LRCLIB searches one lookup may cost. A miss pays the full
# bill, and an album download pays it per track, so this is deliberately
# small — the variants are ordered most-likely-first and cut off here.
MAX_VARIANTS = 4


def _query_variants(artist: str, title: str, duration_s: float = 0) -> list[tuple[str, str]]:
    """(artist, title) pairs to try against LRCLIB, most likely first.

    Each is a real shape our own providers return, not a guess:
      - as given — Latin catalogs answer here and never go further
      - decorations stripped — "Levitating (feat. DaBaby)" -> "Levitating"
      - primary artist only — "Drake, 21 Savage" -> "Drake"
      - the Latin half of a mixed-script field — iTunes' "Gole Yakh  گل یخ"
      - title alone, when the artist is the part LRCLIB cannot read

    The last one needs the duration. Deezer files songs under a Persian-script
    artist with a Latin title ("محسن چاوشی" / "Be Khoda") and LRCLIB has the
    title but not the pair, so dropping the artist is the only way in — and it
    is also the variant most able to return a different artist's song of the
    same name, which a duration is the only remaining evidence against.

    Transliterating Persian into Latin looks like the obvious fix here and does
    not work; see docs/DESIGN.md before trying it.
    """
    out: list[tuple[str, str]] = []

    def add(a: str, t: str) -> None:
        pair = (a.strip(), t.strip())
        if pair[1] and pair not in out:
            out.append(pair)

    bare = _strip_decorations(title)
    primary = _primary_artist(artist)
    latin_title = _latin_part(title)
    latin_artist = _latin_part(artist)

    add(artist, title)
    add(artist, bare)
    add(primary, bare)
    if latin_title:
        add(latin_artist or primary, latin_title)
    elif latin_artist:
        add(latin_artist, bare)
    # Unreadable artist, readable title, and a duration to check the answer.
    if duration_s and _ARABIC_RUN_RE.search(artist) and not _ARABIC_RUN_RE.search(bare):
        add("", bare)
    return out[:MAX_VARIANTS]


# Genius prefixes Persian songs with an auto header like
# [متن آهنگ «گل یخ» از کوروش یغمایی] — bookkeeping, not a lyric.
_GENIUS_SONG_HEADER_RE = re.compile(r"^\[متن آهنگ[^\]]*\]\s*$")


# --------------------------------------------------------------------------
# circuit breaker
#
# A source that is refusing us will not change its mind within one album
# download, and asking anyway is not free: Genius answers 403 in ~1.7 s and
# lyrics.ovh does not answer Persian queries at all, it burns the whole
# REQUEST_TIMEOUT. Per track, per album. So after BREAKER_THRESHOLD consecutive
# failures a source is skipped until BREAKER_COOLDOWN passes, then one request
# is let through to see whether it is back — recovery needs no restart and no
# configuration.
#
# Skipping still counts as degraded. A source we declined to ask is one that
# might have had the song, so the answer stays "unavailable" rather than
# hardening into "this song has no lyrics".

BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN = 900  # seconds


@dataclass
class _Breaker:
    failures: int = 0
    opened_at: float = 0.0


_breakers: dict[str, _Breaker] = {}
_breaker_lock = threading.Lock()


def _breaker_allows(name: str) -> bool:
    """False while `name` is being rested after repeated failures."""
    with _breaker_lock:
        breaker = _breakers.get(name)
        if breaker is None or breaker.failures < BREAKER_THRESHOLD:
            return True
        if time.time() - breaker.opened_at >= BREAKER_COOLDOWN:
            # Half-open: let one request decide whether the source is back.
            breaker.failures = BREAKER_THRESHOLD - 1
            return True
        return False


def _breaker_record(name: str, ok: bool) -> None:
    with _breaker_lock:
        breaker = _breakers.setdefault(name, _Breaker())
        if ok:
            breaker.failures = 0
            breaker.opened_at = 0.0
            return
        breaker.failures += 1
        if breaker.failures >= BREAKER_THRESHOLD:
            breaker.opened_at = time.time()


def reset_breakers() -> None:
    """Forget every source's failure history — a person retrying asks for this."""
    with _breaker_lock:
        _breakers.clear()


# --------------------------------------------------------------------------
# disk cache


_lock = threading.Lock()
_schema_ready = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lyrics (
    key        TEXT PRIMARY KEY,          -- normalized artist + title
    plain      TEXT NOT NULL DEFAULT '',
    synced     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    found      INTEGER NOT NULL DEFAULT 0, -- 0 = cached miss
    fetched_at INTEGER NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _lock:
        if _schema_ready:
            return
        try:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _connect() as conn:
                conn.executescript(_SCHEMA)
            _schema_ready = True
        except sqlite3.Error:
            pass  # an unwritable volume costs us the cache, not the service


# Distinguishes "nothing cached" from "cached as a miss", which is the whole
# point of caching misses: None already means "no lyrics" to callers.
_NOT_CACHED = object()
# And a third: cached as "we could not reach the sources". Short-lived, so an
# album download pays one lookup instead of one per track, without an outage
# hardening into a week of "this song has no lyrics".
_UNAVAILABLE = object()

# The `found` column carries three states rather than a boolean. Reusing the
# existing INTEGER is what lets this ship without a migration: rows written by
# the old code are already 1 or 0 and still mean exactly what they meant.
_FOUND, _ABSENT, _UNREACHABLE = 1, 0, -1

# How long a cached "unreachable" stays trusted. Minutes, not the week a real
# miss gets: the point is to survive one album, not to remember an outage.
UNAVAILABLE_TTL_MINUTES = 15


def _read(key: str):
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT plain, synced, source, found, fetched_at FROM lyrics WHERE key = ?",
                (key,),
            ).fetchone()
    except sqlite3.Error:
        return _NOT_CACHED
    if row is None:
        return _NOT_CACHED
    plain, synced, source, state, fetched_at = row
    age = time.time() - fetched_at
    if state == _FOUND:
        return Lyrics(plain=plain, synced=synced, source=source)
    if state == _UNREACHABLE:
        return _UNAVAILABLE if age < UNAVAILABLE_TTL_MINUTES * 60 else _NOT_CACHED
    if age < MISS_TTL_HOURS * 3600:
        return None
    return _NOT_CACHED


def _write(key: str, found: Lyrics | None, state: int | None = None) -> None:
    if state is None:
        state = _FOUND if found else _ABSENT
    try:
        with _lock, _connect() as conn:
            conn.execute(
                """INSERT INTO lyrics (key, plain, synced, source, found, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     plain = excluded.plain,
                     synced = excluded.synced,
                     source = excluded.source,
                     found = excluded.found,
                     fetched_at = excluded.fetched_at""",
                (
                    key,
                    found.plain if found else "",
                    found.synced if found else "",
                    found.source if found else "",
                    state,
                    int(time.time()),
                ),
            )
    except sqlite3.Error:
        pass  # same deal: a cache that cannot write must not break anything


# --------------------------------------------------------------------------
# fetching


def _get_json(path: str, params: dict, user_agent: str = USER_AGENT) -> object:
    """GET a JSON endpoint. None when the honest answer is "no lyrics";
    raises LyricsUnavailable for anything that might work on a retry."""
    request = Request(
        f"{path}?{urlencode(params)}" if params else path,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return None  # a real miss — LRCLIB knows this song, just no lyrics
        raise LyricsUnavailable(f"{path} answered {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise LyricsUnavailable(str(exc)) from exc


def _lyrics_from(item: dict, source: str) -> Lyrics | None:
    plain = (item.get("plainLyrics") or "").strip()
    synced = (item.get("syncedLyrics") or "").strip()
    if not plain and not synced:
        return None
    if not plain:
        plain = strip_lrc(synced)
    return Lyrics(plain=plain, synced=synced, source=source)


def _pick(results: list, title: str, artist: str, duration_s: float) -> Lyrics | None:
    """Best hit from a fuzzy search: exact name+artist wins, otherwise the
    closest duration within MAX_DURATION_DRIFT. Exactness dominates so an
    exact title beats a slightly-better-timed wrong cover."""
    wanted_title = _squash(title)
    wanted_artist = _squash(artist)
    best, best_score = None, None
    for item in results:
        lyrics = _lyrics_from(item, "lrclib-search")
        if lyrics is None:
            continue
        exact = _squash(item.get("name") or "") == wanted_title and _squash(
            item.get("artistName") or ""
        ) == wanted_artist
        drift = abs((item.get("duration") or 0) - duration_s) if duration_s else 0
        # Without a duration there is no drift to measure — accept any name.
        # An exact *name* match is not an exact song: "Bohemian Rhapsody - Live
        # Aid" names the same song as the studio cut and shares none of its
        # timing. A take minutes away from the one we downloaded is a different
        # take, and its words are worse than no words. Hence exact matches get
        # a wider bound here, not an exemption.
        if duration_s and drift > (MAX_EXACT_DRIFT if exact else MAX_DURATION_DRIFT):
            continue
        # Exact matches sort by duration proximity; non-exact ones by drift.
        score = (0, drift) if exact else (1, drift)
        if best_score is None or score < best_score:
            best, best_score = lyrics, score
    return best


# --------------------------------------------------------------------------
# Genius — the Farsi fallback. LRCLIB keys Persian songs by romanized titles,
# so it can never match a Persian-script catalog; Genius matches Persian
# queries directly and serves the lyrics from the song page's preloaded state.


def _score_genius_hit(hit: dict, title: str, artist: str) -> int:
    """0–12 score for how well a search hit answers the track.

    The title comparison is a two-way substring check because Persian songs
    show up as "Gole Yakh - گُلِ یَخ" — the catalog's "گل یخ" is a substring
    of it, and the romanized half is a substring of an English catalog entry.
    Artist match breaks ties between covers and translations.
    """
    result = hit.get("result") or {}
    if not result.get("path"):
        return 0
    state = result.get("lyrics_state")
    if state not in ("complete", "visible", "unreleased"):
        return 0
    name = _squash(result.get("title") or "")
    artist_name = _squash((result.get("primary_artist") or {}).get("name") or "")
    wanted = _squash(title)
    wanted_artist = _squash(artist)

    score = 0
    if wanted and name and (wanted == name or wanted in name or name in wanted):
        score += 10
    if wanted_artist and artist_name and (
        wanted_artist == artist_name
        or wanted_artist in artist_name
        or artist_name in wanted_artist
    ):
        score += 6
    return score


def _find_lyrics_data(node) -> dict | None:
    """The first `lyricsData` block anywhere in Genius's preloaded state."""
    if isinstance(node, dict):
        block = node.get("lyricsData")
        if isinstance(block, dict) and block.get("body"):
            return block
        for value in node.values():
            found = _find_lyrics_data(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_lyrics_data(value)
            if found is not None:
                return found
    return None


def _genius_lyrics_from_page(path: str) -> str | None:
    """Pull the lyric text out of a Genius song page. None = no lyrics there."""
    request = Request(
        GENIUS_PAGE + path,
        headers={"User-Agent": GENIUS_USER_AGENT, "Accept": "text/html"},
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            html = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        raise LyricsUnavailable(f"genius page answered {exc.code}") from exc
    except OSError as exc:
        raise LyricsUnavailable(str(exc)) from exc

    # The lyrics ride along in `window.__PRELOADED_STATE__ = JSON.parse('...')`
    # — a JS string literal whose content is the state JSON.
    match = re.search(r"window\.__PRELOADED_STATE__ = JSON\.parse\('", html)
    if not match:
        return None
    end = match.end()
    raw: list[str] = []
    i = end
    while i < len(html):
        char = html[i]
        if char == "\\":
            if i + 1 >= len(html):
                break  # a dangling backslash; treat the rest as literal
            raw.append(char)
            raw.append(html[i + 1])
            i += 2
            continue
        if char == "'":
            break
        raw.append(char)
        i += 1
    try:
        # Decode the JS string literal (JS uses \' where JSON would not), then
        # parse the JSON it contained.
        state = json.loads('"' + "".join(raw).replace("\\'", "'") + '"')
        state = json.loads(state)
    except (ValueError, TypeError):
        return None
    block = _find_lyrics_data(state)
    if block is None:
        return None
    text = re.sub(r"<[^>]+>", "", block["body"].get("html") or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    lines = [line for line in text.splitlines() if not _GENIUS_SONG_HEADER_RE.match(line.strip())]
    return "\n".join(lines).strip() or None


def _fetch_genius(artist: str, title: str) -> Lyrics | None:
    """Search Genius for the track and pull its lyrics from the best hit."""
    data = _get_json(
        GENIUS_API + "/search/multi",
        {"per_page": "5", "q": f"{title} {artist}".strip()},
        user_agent=GENIUS_USER_AGENT,
    )
    hits: list[dict] = []
    if isinstance(data, dict):
        for section in data.get("response", {}).get("sections", []):
            if section.get("type") == "song":
                hits.extend(section.get("hits", []) or [])
    if not hits:
        return None
    # Best first, so a bad page can fall through to the next hit.
    ranked = sorted(
        ((_score_genius_hit(h, title, artist), h) for h in hits),
        key=lambda pair: pair[0],
        reverse=True,
    )
    for score, hit in ranked:
        if score < 10:
            break  # nothing left that plausibly is this song
        path = (hit.get("result") or {}).get("path")
        plain = _genius_lyrics_from_page(path)
        if plain:
            return Lyrics(plain=plain, synced="", source="genius")
    return None


def _fetch_lyrics_ovh(artist: str, title: str) -> Lyrics | None:
    """lyrics.ovh — keyless, plain text only, no duration or album to match on.

    Third in line precisely because it cannot be verified: it answers on an
    artist/title pair and hands back a blob, so there is no way to tell a
    right answer from a confident wrong one. Only reached once the two
    catalogs that *can* be checked have both said no.
    """
    for candidate_artist in dict.fromkeys([_primary_artist(artist), artist]):
        if not candidate_artist:
            continue
        data = _get_json(
            f"{LYRICS_OVH_API}/{quote(candidate_artist, safe='')}/{quote(title, safe='')}",
            {},
        )
        if isinstance(data, dict):
            plain = (data.get("lyrics") or "").strip()
            # Its short answers are near-always junk — a truncated entry or an
            # "instrumental" note — and with nothing to verify against, length
            # is the only signal available. A real lyric clears this easily.
            if len(plain) >= MIN_PLAUSIBLE_CHARS:
                return Lyrics(plain=plain, synced="", source="lyrics-ovh")
    return None


def _fetch_lrclib(artist: str, title: str, album: str, duration_s: float) -> Lyrics | None:
    """LRCLIB, asked up to MAX_VARIANTS ways. The only source with synced LRC."""
    # The /get endpoint keys on all four fields and rejects a missing duration,
    # so without one we go straight to the fuzzy search.
    if duration_s:
        exact = _get_json(
            LRCLIB_API + "/get",
            {
                "artist_name": artist,
                "track_name": title,
                "album_name": album,
                "duration": int(duration_s),
            },
        )
        if isinstance(exact, dict):
            found = _lyrics_from(exact, "lrclib-get")
            if found is not None:
                return found

    for variant_artist, variant_title in _query_variants(artist, title, duration_s):
        params = {"track_name": variant_title}
        if variant_artist:
            # Sent only when we have one: an empty artist_name is a filter
            # LRCLIB still applies, not one it ignores.
            params["artist_name"] = variant_artist
        results = _get_json(LRCLIB_API + "/search", params)
        if isinstance(results, list):
            # Scored against the variant we asked for, not the original: a hit
            # for "Levitating" is not a bad match just because we set out
            # looking for "Levitating (feat. DaBaby)".
            found = _pick(results, variant_title, variant_artist, duration_s)
            if found is not None:
                return found
    return None


def _fetch_remote(artist: str, title: str, album: str, duration_s: float) -> Lyrics | None:
    """Every source in turn, best-verified first.

    An unreachable source does not end the search — it is noted and the next
    one is asked. Genius sits in front of lyrics.ovh and Genius is the one that
    gets itself blocked, so letting its 403 propagate would cost us the source
    behind it too.

    If any source failed to *answer*, as opposed to answering "no", the result
    is LyricsUnavailable rather than None — a bad afternoon must never be
    written into the cache as a week of "this song has no lyrics".
    """
    degraded = False
    for name, source in (
        ("lrclib", lambda: _fetch_lrclib(artist, title, album, duration_s)),
        ("genius", lambda: _fetch_genius(artist, title)),
        ("lyrics-ovh", lambda: _fetch_lyrics_ovh(artist, title)),
    ):
        if not _breaker_allows(name):
            degraded = True  # not asked is not the same as answered "no"
            continue
        try:
            found = source()
        except LyricsUnavailable:
            _breaker_record(name, ok=False)
            degraded = True
            continue
        _breaker_record(name, ok=True)
        if found is not None:
            return found
    if degraded:
        raise LyricsUnavailable("no lyrics source could be reached")
    return None


def fetch(
    artist: str, title: str, album: str, duration_s: float, force: bool = False
) -> Lyrics | None:
    """Lyrics for a track, from the cache, LRCLIB, Genius, then lyrics.ovh.

    None means the sources answered and none of them has this song.
    LyricsUnavailable means they did not answer, which is a different fact and
    must stay a different fact: callers report it separately so a source outage
    is never counted or shown as "this song has no lyrics".

    Both are cached, on very different clocks. A miss keeps MISS_TTL_HOURS
    because a song that has no lyrics today will not have them next Tuesday
    either. An outage keeps UNAVAILABLE_TTL_MINUTES, which is long enough that
    an album pays for one lookup instead of one per track, and short enough
    that a source coming back is noticed almost immediately.

    `force` skips only the cached *outage* — never a hit, never a real miss.
    It exists for the retry button: serving a person the same cached refusal
    they just asked us to reconsider would make the button a decoration. Batch
    callers leave it alone and keep the protection.
    """
    _ensure_schema()
    key = _normalize(artist, title)
    cached = _read(key)
    if cached is _UNAVAILABLE:
        if not force:
            raise LyricsUnavailable("sources were unreachable a moment ago")
        cached = _NOT_CACHED
        reset_breakers()  # the person asking is also asking us to re-test
    if cached is not _NOT_CACHED:
        return cached
    try:
        found = _fetch_remote(artist, title, album, duration_s)
    except LyricsUnavailable:
        _write(key, None, state=_UNREACHABLE)
        raise
    _write(key, found)
    return found
