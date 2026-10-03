"""Outbound networking every provider shares: certificates, the proxy, and a
connection check the desktop settings page can run.

Every metadata provider in this package calls plain `urlopen`, and yt-dlp
opens its own connections. Both are routed from here, so "use my VPN's
proxy" is one setting instead of a flag threaded through each module.

Three modes, configured by UNSTREAM_PROXY at start and by /api/desktop/config
at runtime:

  system  follow the OS proxy settings, re-read on every request (default)
  off     connect directly, ignoring any system proxy
  <url>   http://host:port or socks5://host:port, for everything
"""

import functools
import http.client
import os
import shutil
import socket
import ssl
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit

SYSTEM = "system"
OFF = "off"


def _ensure_ca_bundle() -> None:
    """Hand OpenSSL a CA bundle it can actually find.

    The frozen desktop sidecar carries an OpenSSL whose default certificate
    path was baked in on the build machine. For the Intel macOS build that is
    python.org's framework directory, which does not exist on a user's Mac, so
    every HTTPS request failed certificate verification — search and download
    both, VPN or not. certifi ships the bundle inside the app instead. Set
    only when unset, so an operator's own SSL_CERT_FILE still wins; OpenSSL
    keeps loading the system store alongside it on Windows and Linux.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except ImportError:
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()


_ensure_ca_bundle()


def normalize(value: str) -> str:
    """Canonical form of a proxy setting: "system", "off", or a proxy URL.

    Raises ValueError for anything that is none of those.
    """
    raw = (value or "").strip()
    lowered = raw.lower()
    if lowered in ("", SYSTEM, "auto", "automatic"):
        return SYSTEM
    if lowered in (OFF, "none", "direct"):
        return OFF
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    # A VPN client's proxy is where names have to be resolved too. Resolving
    # locally is exactly what DNS filtering intercepts, so plain socks5:// would
    # connect to the filter's answer instead of the site.
    scheme = {"socks5": "socks5h", "socks4": "socks4a", "socks": "socks5h"}.get(scheme, scheme)
    if scheme not in ("http", "socks4a", "socks5h"):
        raise ValueError("Proxy must be http:// or socks5://")
    try:
        port = parts.port
    except ValueError:
        port = None
    if not parts.hostname or not port:
        raise ValueError("Proxy needs a host and a port, like 127.0.0.1:10809")
    auth = parts.netloc.rpartition("@")[0]
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{scheme}://{auth + '@' if auth else ''}{host}:{port}"


_lock = threading.Lock()
_setting = SYSTEM


def configure(value: str) -> str:
    """Switch the proxy for every request from now on. Returns the setting."""
    global _setting
    cleaned = normalize(value)
    with _lock:
        _setting = cleaned
    return cleaned


def setting() -> str:
    return _setting


try:
    configure(os.getenv("UNSTREAM_PROXY", ""))
except ValueError:
    pass  # a malformed saved value must not keep the sidecar from starting


def _system_proxy_for(url: str) -> str | None:
    """What the OS proxy settings say to use for `url`, read fresh.

    Fresh on purpose: urllib's own handler snapshots the settings the first
    time anything is fetched, so a VPN switched on after launch was never used
    until the app restarted — which reads exactly like the VPN not working.
    """
    parts = urlsplit(url)
    proxies = urllib.request.getproxies()
    if parts.hostname and urllib.request.proxy_bypass(parts.hostname):
        return None
    return proxies.get(parts.scheme) or proxies.get("all") or None


def proxy_for(url: str) -> str | None:
    current = _setting
    if current == OFF:
        return None
    if current == SYSTEM:
        return _system_proxy_for(url)
    return current


def ytdlp_proxy() -> str | None:
    """The `proxy` option for yt-dlp. None leaves it to read the system
    settings itself, which it does per YoutubeDL instance."""
    current = _setting
    if current == SYSTEM:
        return None
    if current == OFF:
        return ""
    return current


# --------------------------------------------------------------------------
# urllib routing


def _socks_socket(proxy: str, host: str, port: int, timeout) -> socket.socket:
    from yt_dlp.socks import ProxyType, sockssocket

    parts = urlsplit(proxy)
    scheme = parts.scheme.lower()
    kind = {
        "socks4": ProxyType.SOCKS4,
        "socks4a": ProxyType.SOCKS4A,
        "socks5": ProxyType.SOCKS5,
        "socks5h": ProxyType.SOCKS5,
    }[scheme]
    sock = sockssocket()
    sock.setproxy(
        kind,
        parts.hostname,
        parts.port or 1080,
        rdns=scheme in ("socks4a", "socks5h"),
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
    )
    if isinstance(timeout, (int, float)):
        sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except BaseException:
        sock.close()
        raise
    return sock


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, proxy: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._socks_proxy = proxy

    def connect(self):
        self.sock = _socks_socket(self._socks_proxy, self.host, self.port, self.timeout)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, proxy: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._socks_proxy = proxy

    def connect(self):
        sock = _socks_socket(self._socks_proxy, self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _Router(urllib.request.ProxyHandler):
    """Picks the proxy per request from the live setting.

    A ProxyHandler subclass so build_opener() leaves the stock one out — that
    one would apply the startup snapshot of the system proxy on top of ours.
    """

    handler_order = 100

    def __init__(self):
        super().__init__({})
        self._http = urllib.request.HTTPHandler()
        self._https = urllib.request.HTTPSHandler()

    def _route(self, req):
        proxy = proxy_for(req.full_url)
        if not proxy:
            return None
        scheme = urlsplit(proxy).scheme.lower()
        if scheme.startswith("socks"):
            if req.type == "https":
                conn = functools.partial(_SocksHTTPSConnection, proxy=proxy)
                return self._https.do_open(conn, req)
            conn = functools.partial(_SocksHTTPConnection, proxy=proxy)
            return self._http.do_open(conn, req)
        return self.proxy_open(req, proxy, req.type)

    def http_open(self, req):
        return self._route(req)

    def https_open(self, req):
        return self._route(req)


urllib.request.install_opener(urllib.request.build_opener(_Router()))


# --------------------------------------------------------------------------
# Finding the VPN's proxy


# Local ports the common desktop VPN clients listen on out of the box:
# v2rayN, Clash / Clash Verge, NekoRay, Hiddify, V2RayU, v2rayA, Surge, Privoxy.
COMMON_PORTS = (
    10809, 10808, 7890, 7897, 7891, 2080, 2081, 12334, 1087, 1086,
    20171, 20170, 6152, 6153, 8118, 1080, 8889,
)


def _speaks_http_proxy(port: int) -> bool:
    # CONNECT rather than GET: an ordinary local web server answers any GET,
    # and only a proxy answers CONNECT with a 200.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as sock:
            sock.settimeout(4)
            sock.sendall(b"CONNECT www.gstatic.com:443 HTTP/1.1\r\nHost: www.gstatic.com:443\r\n\r\n")
            head = sock.recv(64)
    except OSError:
        return False
    return head.startswith(b"HTTP/") and head[9:12] == b"200"


def _speaks_socks5(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as sock:
            sock.settimeout(2)
            sock.sendall(b"\x05\x01\x00")
            return sock.recv(2) == b"\x05\x00"
    except OSError:
        return False


def _probe_port(port: int) -> str | None:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
    except OSError:
        return None
    if _speaks_http_proxy(port):
        return f"http://127.0.0.1:{port}"
    if _speaks_socks5(port):
        return f"socks5h://127.0.0.1:{port}"
    return None


def detect_system() -> str | None:
    """The proxy the OS settings name for HTTPS right now, if any."""
    return _system_proxy_for("https://www.youtube.com/")


def detect() -> dict:
    """Proxies a VPN client is serving on this machine, plus what the OS says."""
    with ThreadPoolExecutor(max_workers=len(COMMON_PORTS)) as pool:
        found = [url for url in pool.map(_probe_port, COMMON_PORTS) if url]
    return {"found": found, "system": detect_system()}


# --------------------------------------------------------------------------
# Connection check

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

def _soundcloud_api() -> None:
    # The site itself loads fine from addresses whose API calls come back 401
    # (VPN exits, mostly), so a check of soundcloud.com would pass while every
    # SoundCloud search and link failed. The API is what the app uses.
    from . import soundcloud

    soundcloud._api("/search/tracks", q="music", limit=1)


SERVICES = (
    ("deezer", "https://api.deezer.com/genre/0"),
    ("itunes", "https://itunes.apple.com/search?term=music&limit=1"),
    ("soundcloud", _soundcloud_api),
    ("youtube", "https://www.youtube.com/generate_204"),
)


def failure_kind(exc: BaseException, url: str) -> str:
    """Bucket a connection failure into something a person can act on."""
    from yt_dlp.socks import ProxyError as SocksError

    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, str):
        kind = error_kind(reason)
        return kind if kind not in (None, "other") else "network"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "tls"
    if isinstance(reason, SocksError):
        return "proxy"
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ConnectionRefusedError):
        # With a proxy in the path, the refusal is the proxy's: the VPN app
        # is closed, or listening on another port.
        return "proxy" if proxy_for(url) else "refused"
    # A reset in the middle of the TLS handshake is what SNI filtering looks
    # like from this side, so it reads as "blocked", not as a flaky network.
    if isinstance(reason, (ConnectionResetError, ssl.SSLError, http.client.RemoteDisconnected)):
        return "blocked"
    return "network"


def _check_one(name: str, target) -> dict:
    started = time.monotonic()
    result = {"id": name, "ok": False, "ms": None, "error": None, "status": None}
    url = target if isinstance(target, str) else f"https://{name}.invalid/"
    try:
        if callable(target):
            target()
        else:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read(1024)
                result["status"] = resp.status
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 — every failure is a finding here
        # Providers wrap what urllib raised; the cause is the useful part.
        cause = exc if isinstance(exc, URLError) else (exc.__cause__ or exc)
        if isinstance(cause, HTTPError):
            # Reached the service, which then said no — a region or VPN
            # address block, usually.
            result["status"] = cause.code
            result["error"] = "refused_by_service"
        else:
            result["error"] = failure_kind(cause, url)
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


def check_services() -> list[dict]:
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as pool:
        return list(pool.map(lambda pair: _check_one(*pair), SERVICES))


def tools() -> dict:
    from . import ytdlp

    return {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "js_runtime": ytdlp._js_runtime(),
    }


# --------------------------------------------------------------------------
# Reading a failure message

_NETWORK_NEEDLES = (
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "network is unreachable",
    "no route to host",
    "getaddrinfo failed",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "unable to download webpage",
    "unable to download api page",
    "remote end closed",
    "tunnel connection failed",
    "eof occurred in violation of protocol",
    "unexpected_eof",
    "could not reach",
    "proxyerror",
    "socks",
)


def error_kind(message: str | None) -> str | None:
    """What kind of failure a download or search error message describes.

    The UI turns this into a sentence, and into a pointer at the setting that
    fixes it; the raw message stays in `error` for anyone who needs it.
    """
    if not message:
        return None
    text = message.lower()
    if "not a bot" in text or "sign in to confirm" in text or "login_required" in text:
        return "bot_check"
    if "certificate verify failed" in text or "certificate_verify_failed" in text:
        return "tls"
    if "cookie" in text:
        return "cookies"
    if any(needle in text for needle in _NETWORK_NEEDLES):
        return "network"
    if "ffmpeg" in text:
        return "encoding"
    if "no results found" in text or "no audio file" in text or "no video formats" in text:
        return "not_found"
    return "other"
