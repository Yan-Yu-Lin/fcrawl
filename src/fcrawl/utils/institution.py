"""Institution (school library) proxy: paywalled papers through the user's own subscription.

Off by default (`fcrawl auth enable <preset>` turns it on). The user logs in to the
library proxy in their own browser, fcrawl takes over the session cookies that login
left behind (`fcrawl auth login --from-browser …`, `--cookie-file`, `--paste`), keeps
them in a local file only this user can read, and sends them to the proxy host alone.
Nothing here goes through the gateway or Firecrawl, and fcrawl never sees a password:
there is no username/password login and no captcha solving, on purpose.

A preset describes one library proxy: how a publisher URL is rewritten into a proxy
URL, where the login page is, and how the proxy answers when the session is gone.
Two styles cover nearly every university:

- hostname: EZproxy "proxy by hostname" and Hyweb (凌網) proxies.
  https://ieeexplore.ieee.org/document/1 → https://ieeexplore-ieee-org.<proxy_host>/document/1
- prefix: classic EZproxy. https://<proxy_host>/login?url=https://ieeexplore.ieee.org/document/1
  (the proxy then redirects to its own rewritten host).

Built-in rate limits keep a session from looking like a bulk downloader, which is
what gets a whole school cut off by a publisher: at most DAILY_LIMIT papers a day,
PUBLISHER_DAILY_LIMIT per publisher, and MIN_SPACING seconds between two papers.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urljoin, urlsplit

import requests

from .config import load_config, save_config

DAILY_LIMIT = 20
PUBLISHER_DAILY_LIMIT = 10
MIN_SPACING = 30  # seconds between two proxied paper downloads
MAX_REDIRECTS = 8
FETCH_TIMEOUT = 60
PROBE_TIMEOUT = 20
MAX_PDF_BYTES = 200 * 1024 * 1024
# Publishers answer python-requests' default agent with a block page
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")


class InstitutionError(RuntimeError):
    """One readable line for the user; no traceback."""


class NotEnabled(InstitutionError):
    pass


class NotLoggedIn(InstitutionError):
    """No cookies on file, or the proxy answered with its login page (session expired)."""


class RateLimited(InstitutionError):
    pass


class NeedsBrowser(InstitutionError):
    """The publisher hands out its PDF only after a JavaScript challenge (ScienceDirect's
    "preparing your download" page): no HTTP client gets past it, a browser does."""


# --- presets ---------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    proxy_host: str
    style: str  # "hostname" | "prefix"
    login_url: str  # {url} = where to land after login (URL-encoded by us)
    login_marker: str  # a redirect to a URL starting with this means "not logged in"
    probe_url: str = "https://ieeexplore.ieee.org/Xplore/home.jsp"
    notes: str = ""

    def rewrite(self, url: str) -> str:
        """The proxy URL for a publisher URL. A URL already under the proxy is returned as is."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in ("http", "https") or not host:
            raise InstitutionError(f"not an http(s) URL: {url[:80]}")
        if self.under_proxy(host):
            return url
        if self.style == "prefix":
            return self.login_url.format(url=quote(url, safe=""))
        if parts.port and parts.port not in (80, 443):
            raise InstitutionError(f"cannot proxy a URL with a non-default port: {url[:80]}")
        new_host = host.replace(".", "-") + "." + self.proxy_host
        path = parts.path or "/"
        query = f"?{parts.query}" if parts.query else ""
        return f"https://{new_host}{path}{query}"

    def under_proxy(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return host == self.proxy_host or host.endswith("." + self.proxy_host)

    def cookie_domain_ok(self, domain: str) -> bool:
        """A cookie domain worth keeping: the proxy host, a host under it, or a parent of it
        (the proxy rewrites a publisher's Domain=.springer.com into a parent of its own
        host, .lib.ntut.edu.tw for ntut). Sending is restricted separately, see for_host."""
        d = domain.lower().lstrip(".").rstrip(".")
        if not d or "." not in d:
            return False
        return self.under_proxy(d) or self.proxy_host.endswith("." + d)

    def login_page(self, landing: Optional[str] = None) -> str:
        return self.login_url.format(url=quote(landing or self.probe_url, safe=""))

    def is_login_redirect(self, location: str) -> bool:
        return location.startswith(self.login_marker)


BUILTIN_PRESETS: dict[str, Preset] = {
    "ntut": Preset(
        id="ntut",
        name="National Taipei University of Technology (北科大)",
        proxy_host="search.lib.ntut.edu.tw",
        style="hostname",
        login_url="https://search.lib.ntut.edu.tw/ntut/oauth.jsp?login=true&url={url}",
        login_marker="https://search.lib.ntut.edu.tw/login?url=",
        notes="Hyweb 電子資源整合查詢系統; login is the campus portal (nportal) OAuth page with a captcha, "
              "so log in in your own browser, then hand the cookies to fcrawl.",
    ),
}

_PRESET_FIELDS = ("name", "proxy_host", "style", "login_url", "login_marker", "probe_url", "notes")


def presets(config: Optional[dict] = None) -> dict[str, Preset]:
    """Built-in presets plus the user's own from .fcrawlrc (institution.presets.<id>)."""
    out = dict(BUILTIN_PRESETS)
    config = config if config is not None else load_config()
    custom = (config.get("institution") or {}).get("presets") or {}
    if not isinstance(custom, dict):
        return out
    for pid, raw in custom.items():
        if not isinstance(raw, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", str(pid)):
            continue
        try:
            p = Preset(id=str(pid), **{k: str(raw[k]) for k in _PRESET_FIELDS if k in raw})
        except TypeError:
            continue  # a required field is missing
        if p.style not in ("hostname", "prefix") or not p.proxy_host or "{url}" not in p.login_url:
            continue
        out[p.id] = p
    return out


def settings(config: Optional[dict] = None) -> dict:
    """{'enabled': bool, 'preset': str|None} from .fcrawlrc; off unless the user turned it on."""
    config = config if config is not None else load_config()
    raw = config.get("institution")
    raw = raw if isinstance(raw, dict) else {}
    return {"enabled": bool(raw.get("enabled")), "preset": raw.get("preset") or None}


def active_preset(config: Optional[dict] = None, override: Optional[str] = None) -> Preset:
    """The preset to use, or NotEnabled. `override` (a preset id) skips the enabled check
    only for status-style commands; downloads must go through the configured one."""
    config = config if config is not None else load_config()
    table = presets(config)
    pid = override or settings(config)["preset"]
    if not override and not settings(config)["enabled"]:
        raise NotEnabled("institution proxy is off; turn it on with: fcrawl auth enable <preset>  "
                         "(fcrawl auth presets lists them)")
    if not pid:
        raise NotEnabled("no institution preset configured; run: fcrawl auth enable <preset>")
    if pid not in table:
        raise InstitutionError(f"unknown institution preset {pid!r}; fcrawl auth presets lists them")
    return table[pid]


def set_enabled(preset_id: Optional[str], enabled: bool) -> dict:
    """Persist institution.enabled/preset in the config file. Returns what was written."""
    config_path = _config_file()
    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
        if not isinstance(data, dict):
            data = {}
    inst = data.get("institution") if isinstance(data.get("institution"), dict) else {}
    if preset_id:
        inst["preset"] = preset_id
    inst["enabled"] = bool(enabled)
    data["institution"] = inst
    save_config(data, config_path)
    return {"enabled": inst["enabled"], "preset": inst.get("preset")}


def _config_file() -> Path:
    from .config import get_config_path

    return get_config_path()


# --- local state: cookies and usage, 0600 -------------------------------------------


def state_dir() -> Path:
    base = os.environ.get("FCRAWL_STATE_DIR")
    root = Path(base) if base else Path.home() / ".fcrawl"
    d = root / "institution"
    d.mkdir(parents=True, exist_ok=True)
    _private(d, directory=True)
    return d


def _private(path: Path, directory: bool = False) -> None:
    """Owner-only permissions. On Windows, chmod only knows the read-only bit; the
    files sit under the user's profile, which other accounts cannot read anyway."""
    try:
        os.chmod(path, stat.S_IRWXU if directory else stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    _private(path)


@dataclass
class Cookie:
    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = True
    expires: Optional[int] = None  # epoch seconds; None = session cookie

    def matches(self, host: str) -> bool:
        """Netscape semantics: a leading dot covers subdomains, no dot means this host only."""
        host = host.lower().rstrip(".")
        d = self.domain.lower().rstrip(".")
        if d.startswith("."):
            d = d[1:]
            return host == d or host.endswith("." + d)
        return host == d

    def expired(self, now: Optional[float] = None) -> bool:
        return self.expires is not None and self.expires <= (now if now is not None else time.time())


@dataclass
class CookieStore:
    preset: str
    cookies: list[Cookie] = field(default_factory=list)
    captured: Optional[str] = None  # ISO time
    source: Optional[str] = None  # firefox / file / paste …
    dirty: bool = False  # cookies added by the proxy since load(); save() clears it

    @staticmethod
    def path_for(preset_id: str) -> Path:
        return state_dir() / f"{preset_id}.cookies.json"

    @classmethod
    def load(cls, preset_id: str) -> "CookieStore":
        p = cls.path_for(preset_id)
        if not p.exists():
            return cls(preset=preset_id)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return cls(preset=preset_id)
        cookies = []
        for c in raw.get("cookies") or []:
            if isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("value"), str):
                exp = c.get("expires")
                cookies.append(Cookie(c["name"], c["value"], str(c.get("domain") or ""), str(c.get("path") or "/"),
                                      bool(c.get("secure", True)), int(exp) if isinstance(exp, (int, float)) else None))
        return cls(preset=preset_id, cookies=cookies, captured=raw.get("captured"), source=raw.get("source"))

    def save(self) -> Path:
        p = self.path_for(self.preset)
        data = {"preset": self.preset, "captured": self.captured, "source": self.source,
                "cookies": [c.__dict__ for c in self.cookies]}
        _write_private(p, json.dumps(data, indent=2))
        self.dirty = False
        return p

    def delete(self) -> bool:
        p = self.path_for(self.preset)
        if p.exists():
            p.unlink()
            return True
        return False

    def for_host(self, preset: Preset, host: str, now: Optional[float] = None) -> dict[str, str]:
        """Cookies to send to `host`: only when host is the proxy (or under it), and only
        cookies whose domain covers it. Anything else gets nothing, whatever the jar holds."""
        if not preset.under_proxy(host):
            return {}
        out: dict[str, str] = {}
        for c in self.cookies:
            if c.matches(host) and not c.expired(now):
                out[c.name] = c.value
        return out

    def soonest_expiry(self) -> Optional[int]:
        times = [c.expires for c in self.cookies if c.expires]
        return min(times) if times else None


def _keep(preset: Preset, cookies: list[Cookie]) -> list[Cookie]:
    kept = [c for c in cookies if preset.cookie_domain_ok(c.domain) and not c.expired()]
    if not kept:
        raise NotLoggedIn(f"no cookies for {preset.proxy_host} found; log in at {preset.login_page()} first")
    return kept


def cookies_from_browser(preset: Preset, browser: str, profile: Optional[str] = None) -> list[Cookie]:
    """The proxy's cookies out of a desktop browser's cookie database (yt-dlp's extractor,
    which fcrawl already ships). Chrome/Edge on Windows encrypt cookies so that only the
    browser can read them (app-bound encryption): Firefox works everywhere."""
    from yt_dlp.cookies import extract_cookies_from_browser  # lazy: imports sqlite/keyring helpers

    class _Quiet:
        def debug(self, *_: Any) -> None: ...
        def info(self, *_: Any) -> None: ...
        def warning(self, *_: Any) -> None: ...
        def error(self, msg: str) -> None:
            print(msg, file=sys.stderr)

    try:
        jar = extract_cookies_from_browser(browser, profile, logger=_Quiet())
    except Exception as exc:  # yt-dlp raises DownloadError/CookieLoadError with a readable text
        raise InstitutionError(f"could not read {browser} cookies: {str(exc).splitlines()[0][:200]}") from None
    return _keep(preset, [_from_cookiejar(c) for c in jar])


def cookies_from_file(preset: Preset, path: str) -> list[Cookie]:
    """A Netscape cookies.txt (what the 'Get cookies.txt' browser extensions export)."""
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(path, ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise InstitutionError(f"cannot read cookie file {path}: {exc}") from None
    return _keep(preset, [_from_cookiejar(c) for c in jar])


def cookies_from_header(preset: Preset, header: str) -> list[Cookie]:
    """A pasted `Cookie:` header value: name=value; name2=value2. Domain = the proxy host."""
    text = header.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    cookies = []
    for part in text.split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            cookies.append(Cookie(name.strip(), value.strip(), "." + preset.proxy_host))
    return _keep(preset, cookies)


def _from_cookiejar(c: http.cookiejar.Cookie) -> Cookie:
    return Cookie(c.name, c.value or "", c.domain or "", c.path or "/", bool(c.secure),
                  int(c.expires) if c.expires else None)


# --- usage: the built-in rate limits ---------------------------------------------------


_CC_SECOND_LEVEL = {"co", "com", "ac", "edu", "org", "net", "gov", "ne", "or"}


def publisher_of(url: str) -> str:
    """The registrable domain of a publisher URL: ieee.org, sciencedirect.com, nature.com,
    cambridge.org, ntut.edu.tw. Good enough to count per publisher; not a full PSL."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _CC_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


@dataclass
class Usage:
    preset: str
    day: str = ""
    total: int = 0
    publishers: dict[str, int] = field(default_factory=dict)
    last: float = 0.0  # epoch of the last proxied paper request

    @staticmethod
    def path_for(preset_id: str) -> Path:
        return state_dir() / f"{preset_id}.usage.json"

    @classmethod
    def load(cls, preset_id: str) -> "Usage":
        p = cls.path_for(preset_id)
        if not p.exists():
            return cls(preset=preset_id)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            pubs = raw.get("publishers") if isinstance(raw.get("publishers"), dict) else {}
            return cls(preset=preset_id, day=str(raw.get("day") or ""), total=int(raw.get("total") or 0),
                       publishers={str(k): int(v) for k, v in pubs.items()}, last=float(raw.get("last") or 0))
        except (ValueError, OSError, TypeError):
            return cls(preset=preset_id)

    def save(self) -> None:
        _write_private(self.path_for(self.preset), json.dumps(self.__dict__, indent=2))

    def _roll(self, today: str) -> None:
        if self.day != today:
            self.day, self.total, self.publishers = today, 0, {}

    def check(self, publisher: str, now: float, today: str) -> float:
        """RateLimited if today's quota is used up; else the seconds to wait before the next
        request (0 when none)."""
        self._roll(today)
        if self.total >= DAILY_LIMIT:
            raise RateLimited(f"daily limit reached ({DAILY_LIMIT} papers through the institution proxy per day); "
                              "try again tomorrow")
        if self.publishers.get(publisher, 0) >= PUBLISHER_DAILY_LIMIT:
            raise RateLimited(f"daily limit for {publisher} reached ({PUBLISHER_DAILY_LIMIT} papers per publisher "
                              "per day); try again tomorrow")
        return max(0.0, self.last + MIN_SPACING - now)

    def record(self, publisher: str, now: float, today: str) -> None:
        self._roll(today)
        self.total += 1
        self.publishers[publisher] = self.publishers.get(publisher, 0) + 1
        self.last = now
        self.save()


# --- HTTP through the proxy ---------------------------------------------------------------


def http_get(url: str, cookies: dict[str, str], timeout: int, stream: bool = False) -> requests.Response:
    """One request, no automatic redirects (we attach cookies per hop ourselves)."""
    return requests.get(url, cookies=cookies or None, headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                        allow_redirects=False, timeout=timeout, stream=stream)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


_EXPIRED = ("the library proxy asked for a login: the session has expired or was never captured; "
            "run: fcrawl auth login")


def _is_login_url(preset: Preset, url: str) -> bool:
    return preset.is_login_redirect(url) or (preset.under_proxy(_host(url)) and "/login?url=" in url)


def follow(preset: Preset, store: CookieStore, url: str, timeout: int, stream: bool = False,
           get: Optional[Callable[..., requests.Response]] = None) -> requests.Response:
    """GET through redirects; cookies only ever go to the proxy host, and cookies the proxy
    sets on the way (a per-publisher-host session, say) are added to the store.

    A hop to the proxy's login endpoint is followed once: with a live session it bounces
    straight back to the proxied page. NotLoggedIn when that endpoint answers with a page
    instead (its login form, a 404), sends us to a third host (the campus portal), or is
    reached twice."""
    get = get or http_get
    current = url
    login_hops = 0
    bounced: set[str] = set()  # publisher hosts the proxy sent us out to (it does not proxy them)
    for _ in range(MAX_REDIRECTS + 1):
        resp = get(current, store.for_host(preset, _host(current)), timeout, stream)
        resp.request_url = current  # type: ignore[attr-defined]
        _absorb_cookies(preset, store, resp, _host(current))
        at_login = _is_login_url(preset, current)
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            location = urljoin(current, resp.headers["Location"])
            if _is_login_url(preset, location):
                login_hops += 1
                if login_hops > 1:
                    raise NotLoggedIn(_EXPIRED)
            elif at_login and not preset.under_proxy(_host(location)):
                raise NotLoggedIn(_EXPIRED)  # the login endpoint handed us to the campus portal
            elif not preset.under_proxy(_host(location)) and preset.style == "hostname":
                # the proxy bounced us out: it does not rewrite this host (doi.org, say). Take
                # the publisher URL it resolved to back in through the proxy, once per host;
                # a second bounce for the same host means the library does not proxy it
                host = _host(location)
                if host not in bounced and location.startswith(("http://", "https://")):
                    bounced.add(host)
                    try:
                        location = preset.rewrite(location)
                    except InstitutionError:
                        pass
            try:
                resp.close()
            except Exception:
                pass
            current = location
            continue
        if at_login:
            raise NotLoggedIn(_EXPIRED)
        return resp
    raise InstitutionError(f"too many redirects from {url[:80]}")


def _absorb_cookies(preset: Preset, store: CookieStore, resp: Any, host: str) -> None:
    """Keep cookies the proxy sets (requests exposes them as a jar of http.cookiejar.Cookie)."""
    jar = getattr(resp, "cookies", None)
    if not jar or not preset.under_proxy(host):
        return
    for c in jar:
        name, value = getattr(c, "name", None), getattr(c, "value", None)
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        domain = getattr(c, "domain", None) or host
        if not preset.cookie_domain_ok(domain):
            continue
        exp = getattr(c, "expires", None)
        new = Cookie(name, value, domain, getattr(c, "path", None) or "/", bool(getattr(c, "secure", True)),
                     int(exp) if isinstance(exp, (int, float)) else None)
        store.cookies = [k for k in store.cookies if not (k.name == name and k.domain == domain)] + [new]
        store.dirty = True


# --- finding the PDF behind a landing page ------------------------------------------------


_META_PDF = re.compile(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']', re.I)
_META_PDF_REV = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']', re.I)
_IEEE_DOC = re.compile(r"^/(?:abstract/)?document/(\d+)")
_SD_PII = re.compile(r"/science/article/(?:abs/)?pii/([A-Z0-9]+)", re.I)
_DOI_PATH = re.compile(r"/(?:doi|article|content/pdf)/(?:abs/|full/|pdf/)?(10\.\d{4,9}/[^\s?#]+)", re.I)


def pdf_candidates(landing_url: str, html: str = "") -> list[str]:
    """Likely PDF URLs for a publisher landing page, best first: the page's own
    citation_pdf_url, then the publisher's known pattern. Absolute publisher URLs (the
    caller rewrites them for the proxy)."""
    out: list[str] = []

    def add(u: str) -> None:
        u = u.strip()
        if u and u not in out:
            out.append(u)

    for rx in (_META_PDF, _META_PDF_REV):
        m = rx.search(html or "")
        if m:
            add(urljoin(landing_url, m.group(1).replace("&amp;", "&")))
    parts = urlsplit(landing_url)
    host, path = (parts.hostname or "").lower(), parts.path
    origin = f"{parts.scheme}://{parts.netloc}"
    if host.endswith("ieee.org"):
        m = _IEEE_DOC.match(path)
        if m:
            add(f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={m.group(1)}")
    elif host.endswith("sciencedirect.com"):
        m = _SD_PII.search(path)
        if m:
            add(f"{origin}/science/article/pii/{m.group(1)}/pdfft?isDTMRedir=true&download=true")
    elif host.endswith("springer.com") or host.endswith("springeropen.com"):
        m = _DOI_PATH.search(path)
        if m:
            add(f"{origin}/content/pdf/{m.group(1)}.pdf")
    elif host.endswith("acm.org"):
        m = _DOI_PATH.search(path)
        if m:
            add(f"{origin}/doi/pdf/{m.group(1)}")
    elif host.endswith("wiley.com"):
        m = _DOI_PATH.search(path)
        if m:
            add(f"{origin}/doi/pdfdirect/{m.group(1)}")
    elif host.endswith("tandfonline.com"):
        m = _DOI_PATH.search(path)
        if m:
            add(f"{origin}/doi/pdf/{m.group(1)}")
    return out


_JS_CHALLENGE_MARKS = ("cra_js_challenge", "Preparing your download", "crasolve=1")


def _js_challenge(html: str) -> bool:
    return any(mark in html for mark in _JS_CHALLENGE_MARKS)


_JS_LOCATION = re.compile(r"""(?:window\.|document\.|top\.)?location(?:\.href)?\s*=\s*["']([^"'\s]{4,600})["']""", re.I)
_META_REFRESH = re.compile(r"""http-equiv=["']?refresh["']?[^>]*?url=([^"'>\s]+)""", re.I)
MAX_INTERSTITIALS = 3


def interstitial_targets(page_url: str, html: str) -> list[str]:
    """Where a 'preparing your download' page sends the browser next: JavaScript
    `location = "…"` assignments and meta refreshes that point at a PDF on the same host.
    ScienceDirect's pdfft answers with such a page (its JS challenge), whose real link
    carries a one-time token. Only PDF-looking links are returned, best first."""
    if len(html) > 200_000:
        return []
    host = _host(page_url)
    out: list[str] = []
    for m in list(_JS_LOCATION.finditer(html)) + list(_META_REFRESH.finditer(html)):
        target = urljoin(page_url, m.group(1).replace("&amp;", "&"))
        if _host(target) != host or target == page_url or target in out:
            continue
        if "pdf" in target.lower():
            out.append(target)
    return out


def _is_pdf(resp: requests.Response, head: bytes) -> bool:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    return head.startswith(b"%PDF") or ("application/pdf" in ctype and not head.startswith(b"<"))


def _read_body(chunks_iter: Any, limit: int = MAX_PDF_BYTES, already: int = 0) -> bytes:
    """The rest of a response body from the iterator that already produced its first chunk
    (`already` bytes long, counted against the limit)."""
    chunks, size = [], already
    for chunk in chunks_iter:
        if not chunk:
            continue
        size += len(chunk)
        if size > limit:
            raise InstitutionError(f"response larger than {limit // (1024 * 1024)} MB; refusing")
        chunks.append(chunk)
    return b"".join(chunks)


DOI_HOSTS = ("doi.org", "dx.doi.org")


def resolve_doi(url: str, get: Optional[Callable[..., requests.Response]] = None) -> str:
    """The publisher URL a doi.org link redirects to (doi.org is public: no proxy, no
    cookies). The link itself when it does not resolve."""
    get = get or http_get
    current = url
    for _ in range(5):
        if _host(current) not in DOI_HOSTS:
            return current
        resp = get(current, {}, PROBE_TIMEOUT, False)
        location = resp.headers.get("Location") if resp.status_code in (301, 302, 303, 307, 308) else None
        try:
            resp.close()
        except Exception:
            pass
        if not location:
            return url
        current = urljoin(current, location)
    return current


@dataclass
class Fetched:
    path: Path
    size: int
    url: str  # the publisher URL asked for
    pdf_url: str  # the (proxied) URL the bytes came from
    hops: list[str]


def fetch_pdf(preset: Preset, store: CookieStore, url: str, out_path: Path, usage: Optional[Usage] = None,
              get: Optional[Callable[..., requests.Response]] = None, clock: Optional[Callable[[], float]] = None,
              sleep: Optional[Callable[[float], None]] = None, note: Callable[[str], None] = lambda _m: None) -> Fetched:
    """Download the PDF of a publisher URL (landing page, DOI link or direct PDF) through
    the proxy to out_path. Counts against the rate limits before the first byte."""
    if not store.cookies:
        raise NotLoggedIn("no institution session on file; run: fcrawl auth login")
    get, clock, sleep = get or http_get, clock or time.time, sleep or time.sleep
    usage = usage or Usage.load(preset.id)
    target_url = url
    if _host(url) in DOI_HOSTS:
        target_url = resolve_doi(url, get)
        if target_url != url:
            note(f"DOI resolves to {target_url[:100]}")
    publisher = publisher_of(target_url)
    now = clock()
    today = date.fromtimestamp(now).isoformat()
    wait = usage.check(publisher, now, today)
    if wait > 0:
        note(f"waiting {wait:.0f}s (at least {MIN_SPACING}s between two papers through the proxy)")
        sleep(wait)
        now = clock()
    usage.record(publisher, now, date.fromtimestamp(now).isoformat())

    hops: list[str] = []
    tried: set[str] = set()
    queue = [target_url]
    html_seen: Optional[tuple[str, str]] = None
    interstitials = 0
    challenged = False
    while queue:
        target = queue.pop(0)
        proxied = preset.rewrite(target)
        if proxied in tried:
            continue
        tried.add(proxied)
        hops.append(proxied)
        resp = follow(preset, store, proxied, FETCH_TIMEOUT, stream=True, get=get)
        final = getattr(resp, "request_url", proxied)
        if resp.status_code >= 400:
            resp.close()
            if len(tried) == 1:
                if resp.status_code in (401, 403):
                    raise NotLoggedIn(f"the proxy answered {resp.status_code} for {final[:80]}: the session may "
                                      "have expired (fcrawl auth status), or the library has no subscription to "
                                      "this title")
                # the landing page is gone or broken: the publisher's PDF pattern may still work
                queue = [c for c in pdf_candidates(target, "") if c not in queue] + queue
            continue
        it = resp.iter_content(chunk_size=64 * 1024)
        head = next(it, b"")
        if _is_pdf(resp, head):
            body = head + _read_body(it, already=len(head))
            resp.close()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(body)
            if store.dirty:
                store.save()
            return Fetched(out_path, len(body), url, final, hops)
        body = (head + _read_body(it, 5 * 1024 * 1024, len(head))).decode("utf-8", "replace")
        resp.close()
        challenged = challenged or _js_challenge(body)
        # a download page that moves on by script (ScienceDirect's "preparing your download")
        if interstitials < MAX_INTERSTITIALS:
            page = _unproxy(preset, final) if preset.under_proxy(_host(final)) else final
            nxt = [t for t in interstitial_targets(page, body) if preset.rewrite(t) not in tried]
            if nxt:
                interstitials += 1
                queue = nxt[:1] + queue
        if html_seen is None:
            html_seen = (final, body)
            # where the redirects ended is the real landing page (a DOI link resolves to the
            # publisher): its citation_pdf_url first, then the publisher's known pattern for
            # that page, then the pattern for the URL as given
            landing = _unproxy(preset, final) if preset.under_proxy(_host(final)) else final
            for cand in pdf_candidates(landing, body) + pdf_candidates(target, ""):
                if cand not in queue:
                    queue.append(cand)
    where = html_seen[0][:80] if html_seen else url[:80]
    if challenged:
        raise NeedsBrowser(f"{publisher} serves its PDFs only after a JavaScript challenge, which fcrawl cannot run; "
                           f"open the paper in your browser through the library instead: {preset.rewrite(target_url)}")
    raise InstitutionError(f"no PDF found behind {where}: the page has no citation_pdf_url and no known publisher "
                           "pattern matched, or the library has no subscription to it")


def fetch_free_pdf(url: str, out_path: Path, get: Optional[Callable[..., requests.Response]] = None) -> Fetched:
    """A PDF from an open repository (arXiv, PMC): plain download, no proxy, no cookies,
    not counted against any limit."""
    get = get or http_get
    current, hops = url, [url]
    for _ in range(MAX_REDIRECTS + 1):
        resp = get(current, {}, FETCH_TIMEOUT, True)
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            current = urljoin(current, resp.headers["Location"])
            hops.append(current)
            resp.close()
            continue
        if resp.status_code >= 400:
            resp.close()
            raise InstitutionError(f"{resp.status_code} from {_host(current)} for {url[:80]}")
        it = resp.iter_content(chunk_size=64 * 1024)
        head = next(it, b"")
        if not _is_pdf(resp, head):
            resp.close()
            raise InstitutionError(f"{url[:80]} did not answer with a PDF (got {resp.headers.get('Content-Type')})")
        body = head + _read_body(it, already=len(head))
        resp.close()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(body)
        return Fetched(out_path, len(body), url, current, hops)
    raise InstitutionError(f"too many redirects from {url[:80]}")


def _unproxy(preset: Preset, url: str) -> str:
    """Best-effort inverse of hostname rewriting (publisher hosts rarely contain '-')."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    suffix = "." + preset.proxy_host
    if not host.endswith(suffix):
        return url
    real = host[: -len(suffix)].replace("-", ".")
    return f"https://{real}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")


# --- status -------------------------------------------------------------------------------


def probe(preset: Preset, store: CookieStore, get: Optional[Callable[..., requests.Response]] = None) -> dict:
    """Is the session alive? {'ok': bool, 'detail': str}. One request to the proxy, not
    counted as a paper."""
    if not store.cookies:
        return {"ok": False, "detail": "no cookies on file (fcrawl auth login)"}
    try:
        resp = follow(preset, store, preset.rewrite(preset.probe_url), PROBE_TIMEOUT, get=get)
        if store.dirty:
            store.save()
    except NotLoggedIn as exc:
        return {"ok": False, "detail": str(exc)}
    except InstitutionError as exc:
        return {"ok": False, "detail": str(exc)}
    except requests.RequestException as exc:
        return {"ok": False, "detail": f"proxy unreachable ({type(exc).__name__})"}
    final = getattr(resp, "request_url", "")
    if resp.status_code < 400 and preset.under_proxy(_host(final)):
        return {"ok": True, "detail": f"{resp.status_code} from {_host(final)}"}
    if resp.status_code < 400:
        return {"ok": False, "detail": f"the proxy sent the request out to {_host(final)} instead of proxying it"}
    return {"ok": False, "detail": f"{resp.status_code} from {_host(final) or 'the proxy'}"}


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
