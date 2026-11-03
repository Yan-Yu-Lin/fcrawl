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
`institution.limits` in .fcrawlrc (or `institution.presets.<id>.limits`) changes them,
within the hard ceiling HARD_DAILY_MAX / HARD_MIN_SPACING. The usage file is shared by
every fcrawl process on the machine and only touched under a file lock.

Cookies go out over https only, to the proxy host and hosts under it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote, urljoin, urlsplit

import requests

from .config import load_config
from .platform import exclusive_lock

DAILY_LIMIT = 20
PUBLISHER_DAILY_LIMIT = 10
MIN_SPACING = 30  # seconds between two proxied paper downloads
HARD_DAILY_MAX = 50  # a .fcrawlrc limit above this is lowered to it
HARD_MIN_SPACING = 10  # a .fcrawlrc spacing below this is raised to it
MAX_REDIRECTS = 8
FETCH_TIMEOUT = 60
PROBE_TIMEOUT = 20
MAX_PDF_BYTES = 200 * 1024 * 1024
MIN_PDF_BYTES = 1024  # a real paper is never this small; a login page served as "PDF" often is
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
    session_cookie: str = ""  # the cookie a finished login sets (browser login waits for it)
    # parents of proxy_host whose cookies are kept (the proxy may rewrite a publisher's
    # cookie domain onto one); everything else above the proxy host is dropped
    extra_cookie_domains: tuple[str, ...] = ()
    experimental: bool = False  # not yet verified against a real library
    # tests point a preset at a local http server; presets from .fcrawlrc are always
    # https on the default port (presets() never sets these two)
    scheme: str = "https"
    port: int = 0

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
        return f"{self.scheme}://{new_host}{self._port()}{path}{query}"

    def _port(self) -> str:
        return f":{self.port}" if self.port else ""

    def secure_url(self, url: str) -> bool:
        """May cookies go with a request to this URL? https only (the test-only http preset
        aside), and only to the proxy host or a host under it."""
        parts = urlsplit(url)
        return parts.scheme == self.scheme and self.under_proxy(parts.hostname or "")

    def under_proxy(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return host == self.proxy_host or host.endswith("." + self.proxy_host)

    def cookie_domain_ok(self, domain: str) -> bool:
        """A cookie domain worth keeping: the proxy host, a host under it, or one of the
        preset's extra_cookie_domains (the ntut proxy rewrites a publisher's
        Domain=.springer.com into .lib.ntut.edu.tw, a parent of its own host). Other
        parents (.ntut.edu.tw, .edu.tw) are dropped. Sending is restricted separately,
        see CookieStore.for_host."""
        d = domain.lower().lstrip(".").rstrip(".")
        if not d or "." not in d:
            return False
        extra = {x.lower().lstrip(".").rstrip(".") for x in self.extra_cookie_domains}
        return self.under_proxy(d) or d in extra

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
              "so you log in yourself, in a browser window fcrawl opens.",
        session_cookie="HYSID",
        extra_cookie_domains=(".lib.ntut.edu.tw",),
    ),
}

_PRESET_FIELDS = ("name", "proxy_host", "style", "login_url", "login_marker", "probe_url", "notes", "session_cookie")
_HOST = re.compile(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){2,}[a-z]{2,63}")


def _preset_problem(p: Preset) -> Optional[str]:
    """Why a .fcrawlrc preset is refused, or None. Every URL must be https and point at
    the proxy (a preset decides where the cookies go), the proxy host needs three labels
    or more (so it cannot claim a whole registry like edu.tw), the login marker must be
    a real URL (an empty one makes every URL look like the login page)."""
    if p.style not in ("hostname", "prefix"):
        return "style must be hostname or prefix"
    if not _HOST.fullmatch(p.proxy_host):
        return "proxy_host must be a host name with at least three labels (ezproxy.lib.example.edu)"
    if "{url}" not in p.login_url:
        return "login_url must contain {url}"
    for name in ("login_url", "login_marker"):
        value = getattr(p, name).replace("{url}", "x")
        parts = urlsplit(value)
        if parts.scheme != "https" or not p.under_proxy(parts.hostname or ""):
            return f"{name} must be an https URL on {p.proxy_host}"
    if urlsplit(p.probe_url).scheme != "https":
        return "probe_url must be https"
    for d in p.extra_cookie_domains:
        d = d.lower().strip(".")
        if d.count(".") < 2 or not p.proxy_host.endswith("." + d):
            return f"extra_cookie_domains: {d!r} must be a parent of {p.proxy_host} with at least three labels"
    return None


def preset_errors(config: Optional[dict] = None) -> dict[str, str]:
    """{preset id: why it was ignored} for the user's .fcrawlrc presets (shown by auth presets)."""
    errors: dict[str, str] = {}
    presets(config, errors)
    return errors


def presets(config: Optional[dict] = None, errors: Optional[dict] = None) -> dict[str, Preset]:
    """Built-in presets plus the user's own from .fcrawlrc (institution.presets.<id>).
    A user preset cannot replace a built-in one; an invalid one is skipped (reason in
    `errors` when given)."""
    out = dict(BUILTIN_PRESETS)
    errors = errors if errors is not None else {}
    config = config if config is not None else load_config()
    inst = config.get("institution") if isinstance(config.get("institution"), dict) else {}
    custom = inst.get("presets") or {}
    if not isinstance(custom, dict):
        return out
    for pid, raw in custom.items():
        pid = str(pid)
        if pid in BUILTIN_PRESETS:
            if isinstance(raw, dict) and set(raw) - {"limits"}:
                errors[pid] = "a built-in preset cannot be redefined (only its limits can be set); pick another id"
            continue
        if not isinstance(raw, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", pid):
            errors[pid] = "invalid id (a-z, 0-9, - and _) or not an object"
            continue
        extra = raw.get("extra_cookie_domains") or ()
        try:
            p = Preset(id=pid, **{k: str(raw[k]) for k in _PRESET_FIELDS if k in raw},
                       extra_cookie_domains=tuple(str(x) for x in extra) if isinstance(extra, list) else (),
                       experimental=True)
        except TypeError:
            errors[pid] = "missing a required field (name, proxy_host, style, login_url, login_marker)"
            continue
        p = Preset(**{**p.__dict__, "proxy_host": p.proxy_host.lower().strip().rstrip(".")})
        problem = _preset_problem(p)
        if problem:
            errors[pid] = problem
            continue
        out[p.id] = p
    return out


def settings(config: Optional[dict] = None) -> dict:
    """{'enabled': bool, 'preset': str|None} from .fcrawlrc; off unless the user turned it on."""
    config = config if config is not None else load_config()
    raw = config.get("institution")
    raw = raw if isinstance(raw, dict) else {}
    return {"enabled": bool(raw.get("enabled")), "preset": raw.get("preset") or None}


@dataclass(frozen=True)
class Limits:
    daily: int = DAILY_LIMIT
    per_publisher: int = PUBLISHER_DAILY_LIMIT
    spacing: int = MIN_SPACING
    notes: tuple[str, ...] = ()  # what was clamped to the hard ceiling


def limits(preset_id: str, config: Optional[dict] = None) -> Limits:
    """The rate limits in force: defaults, then `institution.limits`, then
    `institution.presets.<id>.limits` (keys daily, per_publisher, spacing_seconds),
    clamped to the hard ceiling (daily <= HARD_DAILY_MAX, spacing >= HARD_MIN_SPACING)."""
    config = config if config is not None else load_config()
    inst = config.get("institution") if isinstance(config.get("institution"), dict) else {}
    merged: dict = {}
    for layer in (inst.get("limits"), ((inst.get("presets") or {}).get(preset_id) or {}).get("limits")
                  if isinstance(inst.get("presets"), dict) else None):
        if isinstance(layer, dict):
            merged.update(layer)
    notes: list[str] = []

    def num(key: str, default: int) -> int:
        v = merged.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 1:
            if key in merged:
                notes.append(f"institution limits: {key}={v!r} is not a positive number; using {default}")
            return default
        return int(v)

    daily = num("daily", DAILY_LIMIT)
    per_pub = num("per_publisher", PUBLISHER_DAILY_LIMIT)
    spacing = num("spacing_seconds", MIN_SPACING)
    if daily > HARD_DAILY_MAX:
        notes.append(f"institution limits: daily {daily} is above the hard ceiling; using {HARD_DAILY_MAX}")
        daily = HARD_DAILY_MAX
    if per_pub > daily:
        per_pub = daily
    if spacing < HARD_MIN_SPACING:
        notes.append(f"institution limits: spacing {spacing}s is below the hard floor; using {HARD_MIN_SPACING}s")
        spacing = HARD_MIN_SPACING
    return Limits(daily, per_pub, spacing, tuple(notes))


def notify_once(text: str, emit: Callable[[str], None]) -> None:
    """Show a limits notice once per machine (not on every download): remembered in the state dir."""
    marker = state_dir() / "notices.json"
    try:
        seen = json.loads(marker.read_text(encoding="utf-8"))
        seen = seen if isinstance(seen, list) else []
    except (OSError, ValueError):
        seen = []
    if text in seen:
        return
    emit(text)
    _write_private(marker, json.dumps((seen + [text])[-50:]))


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
    """Persist institution.enabled/preset in the config file. Returns what was written.
    A config file that is not valid JSON is never overwritten (it holds the gateway
    token): InstitutionError, and the user fixes it first."""
    config_path = _config_file()
    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            reason = f"line {exc.lineno}" if isinstance(exc, json.JSONDecodeError) else type(exc).__name__
            raise InstitutionError(f"{config_path} is not valid JSON ({reason}); fix it first, "
                                   "fcrawl will not overwrite it") from None
        if not isinstance(data, dict):
            raise InstitutionError(f"{config_path} is not a JSON object; fix it first, fcrawl will not overwrite it")
    inst = data.get("institution") if isinstance(data.get("institution"), dict) else {}
    if preset_id:
        inst["preset"] = preset_id
    inst["enabled"] = bool(enabled)
    data["institution"] = inst
    _write_private(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
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
    """Atomic owner-only write: a temp file with a unique name next to the target (two
    processes never share one), then rename over it. Windows refuses the rename while
    another process has the target open for a moment: retry briefly."""
    write_private_bytes(path, text.encode("utf-8"))


def write_private_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        _private(Path(tmp))
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _private(path)


@contextmanager
def state_lock() -> Iterator[None]:
    """One lock for the cookie and usage files of every preset, across processes."""
    with open(state_dir() / "lock", "a+", encoding="utf-8") as handle, exclusive_lock(handle):
        yield


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

    def save(self, merge: bool = False) -> Path:
        """Write the store. merge=True (cookies the proxy handed out during a download):
        under the state lock, keep what another fcrawl process saved meanwhile and lay
        ours over it, so parallel downloads do not drop each other's cookies. A fresh
        login saves with merge=False and replaces the file."""
        p = self.path_for(self.preset)
        with state_lock():
            cookies = self.cookies
            if merge:
                ours = {(c.name, c.domain, c.path) for c in self.cookies}
                disk = [c for c in CookieStore.load(self.preset).cookies if (c.name, c.domain, c.path) not in ours]
                cookies = disk + self.cookies
            data = {"preset": self.preset, "captured": self.captured, "source": self.source,
                    "cookies": [c.__dict__ for c in cookies]}
            _write_private(p, json.dumps(data, indent=2))
        self.dirty = False
        return p

    def delete(self) -> bool:
        p = self.path_for(self.preset)
        with state_lock():
            if p.exists():
                p.unlink()
                return True
        return False

    def for_url(self, preset: Preset, url: str, now: Optional[float] = None) -> dict[str, str]:
        """Cookies to send with a request to `url`: only over https (never in clear text,
        whatever a redirect says), only when the host is the proxy or under it, and only
        cookies whose domain covers that host. Anything else gets nothing."""
        if not preset.secure_url(url):
            return {}
        return self.for_host(preset, urlsplit(url).hostname or "", now)

    def for_host(self, preset: Preset, host: str, now: Optional[float] = None) -> dict[str, str]:
        """The host part of for_url (the scheme check is for_url's): cookies whose domain
        covers `host`, when host is the proxy or under it."""
        if not preset.under_proxy(host):
            return {}
        out: dict[str, str] = {}
        for c in self.cookies:
            if c.matches(host) and not c.expired(now):
                out[c.name] = c.value
        return out

    def live(self, now: Optional[float] = None) -> bool:
        """Any unexpired cookie on file (a session cookie counts until the proxy says no)."""
        return any(not c.expired(now) for c in self.cookies)

    def soonest_expiry(self) -> Optional[int]:
        times = [c.expires for c in self.cookies if c.expires]
        return min(times) if times else None


def _keep(preset: Preset, cookies: list[Cookie]) -> list[Cookie]:
    kept = [c for c in cookies if preset.cookie_domain_ok(c.domain) and not c.expired()]
    if not kept:
        raise NotLoggedIn(f"no cookies for {preset.proxy_host} found; log in at {preset.login_page()} first")
    return kept


def cookies_from_browser(preset: Preset, browser: str, profile: Optional[str] = None,
                         warn: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> list[Cookie]:
    """The proxy's cookies out of a desktop browser's cookie database (yt-dlp's extractor,
    which fcrawl already ships). A fallback, not the main path (`fcrawl auth login` opens
    its own browser window): Chrome/Edge on Windows encrypt cookies so only they can read
    them, a running Chromium locks the file, and a session cookie (ntut's HYSID) may never
    reach the database at all (Firefox keeps session cookies in memory). yt-dlp's
    warnings say which of these happened, so they are passed on (they carry no values)."""
    from yt_dlp.cookies import extract_cookies_from_browser  # lazy: imports sqlite/keyring helpers

    warnings: list[str] = []

    class _Logger:
        def debug(self, *_: Any, **__: Any) -> None: ...
        def info(self, *_: Any, **__: Any) -> None: ...

        def warning(self, msg: str, *_: Any, **__: Any) -> None:
            line = str(msg).splitlines()[0][:200] if msg else ""
            if line and line not in warnings:
                warnings.append(line)

        def error(self, msg: str, *_: Any, **__: Any) -> None:
            self.warning(msg)

    try:
        jar = extract_cookies_from_browser(browser, profile, logger=_Logger())
    except Exception as exc:  # yt-dlp raises DownloadError/CookieLoadError with a readable text
        raise InstitutionError(f"could not read {browser} cookies: {str(exc).splitlines()[0][:200]}") from None
    finally:
        for line in warnings[:3]:
            warn(f"{browser}: {line}")
    try:
        return _keep(preset, [_from_cookiejar(c) for c in jar])
    except NotLoggedIn:
        hint = (f" ({preset.session_cookie} is a session cookie, which {browser} may keep in memory only; "
                "use the browser window of plain `fcrawl auth login`, --cookie-file or --paste)") \
            if preset.session_cookie else ""
        raise NotLoggedIn(f"no cookies for {preset.proxy_host} in {browser}'s cookie store{hint}") from None


def parse_netscape(text: str) -> list[Cookie]:
    """A Netscape cookies.txt, tolerantly: tab-separated domain, include-subdomains flag,
    path, secure, expiry, name, value; `#HttpOnly_` prefixes; blank lines and comments.
    The flag column is ignored (a leading dot on the domain decides, as browsers do):
    exporters disagree about it and Python's own parser refuses such files. Problems
    are reported by line number only, never with the line (it holds a cookie value)."""
    cookies: list[Cookie] = []
    bad: list[int] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r\n")
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.lstrip().startswith(("#", "$")):
            continue
        fields = line.split("\t")
        if len(fields) == 6:
            fields.append("")  # an empty value with its tab trimmed
        if len(fields) != 7 or not fields[0].strip() or not fields[5].strip():
            bad.append(n)
            continue
        domain, _flag, path, secure, expires, name, value = (f.strip() for f in fields)
        try:
            exp = int(float(expires)) if expires else 0
        except ValueError:
            bad.append(n)
            continue
        cookies.append(Cookie(name, value, domain, path or "/", secure.upper() == "TRUE", exp or None))
    if bad and not cookies:
        shown = ", ".join(str(n) for n in bad[:5]) + ("…" if len(bad) > 5 else "")
        raise InstitutionError(f"not a Netscape cookies.txt (lines {shown} do not have the 7 tab-separated "
                               "fields); export it again with a 'Get cookies.txt' extension")
    return cookies


def cookies_from_file(preset: Preset, path: str) -> list[Cookie]:
    """A Netscape cookies.txt (what the 'Get cookies.txt' browser extensions export)."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise InstitutionError(f"cannot read cookie file {path}: {exc.strerror or type(exc).__name__}") from None
    return _keep(preset, parse_netscape(text))


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


def _from_cookiejar(c: Any) -> Cookie:
    """An http.cookiejar.Cookie (what yt-dlp and requests hand out) as ours."""
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
    last: float = 0.0  # epoch of the last proxied paper request (or the reserved slot)
    limits: Limits = field(default_factory=Limits)

    @staticmethod
    def path_for(preset_id: str) -> Path:
        return state_dir() / f"{preset_id}.usage.json"

    @classmethod
    def load(cls, preset_id: str, lim: Optional[Limits] = None) -> "Usage":
        """The usage on file. A missing or unreadable file starts from zero: the limits are
        the user's own brake, not a security boundary (deleting the file resets them)."""
        lim = lim or Limits()
        p = cls.path_for(preset_id)
        if not p.exists():
            return cls(preset=preset_id, limits=lim)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            pubs = raw.get("publishers") if isinstance(raw.get("publishers"), dict) else {}
            return cls(preset=preset_id, day=str(raw.get("day") or ""), total=int(raw.get("total") or 0),
                       publishers={str(k): int(v) for k, v in pubs.items()}, last=float(raw.get("last") or 0),
                       limits=lim)
        except (ValueError, OSError, TypeError, AttributeError):
            return cls(preset=preset_id, limits=lim)

    def save(self) -> None:
        data = {"preset": self.preset, "day": self.day, "total": self.total, "publishers": self.publishers,
                "last": self.last}
        _write_private(self.path_for(self.preset), json.dumps(data, indent=2))

    def _roll(self, today: str) -> None:
        if self.day != today:
            self.day, self.total, self.publishers = today, 0, {}

    def check(self, publisher: str, now: float, today: str) -> float:
        """RateLimited if today's quota is used up; else the seconds to wait before the next
        request (0 when none). A `last` far in the future means the clock was turned back:
        then one spacing, not hours."""
        lim = self.limits
        self._roll(today)
        if self.total >= lim.daily:
            raise RateLimited(f"daily limit reached ({lim.daily} papers through the institution proxy per day); "
                              "try again tomorrow")
        if self.publishers.get(publisher, 0) >= lim.per_publisher:
            raise RateLimited(f"daily limit for {publisher} reached ({lim.per_publisher} papers per publisher "
                              "per day); try again tomorrow")
        wait = self.last + lim.spacing - now
        if wait > lim.spacing * (lim.daily + 1):
            return float(lim.spacing)
        return max(0.0, wait)

    def record(self, publisher: str, now: float, today: str) -> None:
        self._roll(today)
        self.total += 1
        self.publishers[publisher] = self.publishers.get(publisher, 0) + 1
        self.last = now
        self.save()

    @classmethod
    def reserve(cls, preset_id: str, publisher: str, now: float, lim: Optional[Limits] = None) -> float:
        """Count one paper and book its time slot, atomically across fcrawl processes:
        under the state lock, read the file, check the limits, record the paper at
        now + wait and write it back. Returns the wait; the caller sleeps it after the
        lock is released, so parallel downloads queue up 'spacing' seconds apart."""
        with state_lock():
            usage = cls.load(preset_id, lim)
            today = date.fromtimestamp(now).isoformat()
            wait = usage.check(publisher, now, today)
            usage.record(publisher, now + wait, today)
        return wait


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
        resp = get(current, store.for_url(preset, current), timeout, stream)
        resp.request_url = current  # type: ignore[attr-defined]
        if preset.secure_url(current):
            _absorb_cookies(preset, store, resp, _host(current))
        at_login = _is_login_url(preset, current)
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            location = urljoin(current, resp.headers["Location"])
            loc = urlsplit(location)
            if preset.under_proxy(loc.hostname or "") and loc.scheme != preset.scheme:
                # a downgrade to http on the proxy: go on over https (cookies never travel in
                # clear text; for_url would send none, and the page would then look logged out)
                location = loc._replace(scheme=preset.scheme).geturl()
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


def _is_pdf(head: bytes) -> bool:
    """A PDF starts with `%PDF-` within its first 1024 bytes (the spec's allowance for junk
    before the header). The Content-Type is not trusted: a login page served as
    application/pdf, with or without leading whitespace, is not a paper."""
    return b"%PDF-" in head[:1024]


def _head(chunks_iter: Any, want: int = 1024) -> bytes:
    """At least `want` bytes from the start of a body (fewer only when it is shorter)."""
    head = b""
    while len(head) < want:
        chunk = next(chunks_iter, None)
        if chunk is None:
            break
        head += chunk
    return head


def _save_pdf(out_path: Path, body: bytes, where: str) -> None:
    if len(body) < MIN_PDF_BYTES:
        raise InstitutionError(f"{where} answered with only {len(body)} bytes; that is not a paper")
    write_private_bytes(out_path, body)  # 0600: a paywalled paper is for this user only


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


def fetch_pdf(preset: Preset, store: CookieStore, url: str, out_path: Path, lim: Optional[Limits] = None,
              get: Optional[Callable[..., requests.Response]] = None, clock: Optional[Callable[[], float]] = None,
              sleep: Optional[Callable[[float], None]] = None, note: Callable[[str], None] = lambda _m: None) -> Fetched:
    """Download the PDF of a publisher URL (landing page, DOI link or direct PDF) through
    the proxy to out_path. Counts against the rate limits before the first byte (a
    failure counts too); no session on file costs nothing."""
    if not store.live():
        raise NotLoggedIn("no institution session on file; run: fcrawl auth login")
    get, clock, sleep = get or http_get, clock or time.time, sleep or time.sleep
    lim = lim or limits(preset.id)
    target_url = url
    if _host(url) in DOI_HOSTS:
        target_url = resolve_doi(url, get)
        if target_url != url:
            note(f"DOI resolves to {target_url[:100]}")
    # an already-proxied URL counts for its publisher, not for the library
    publisher = publisher_of(_unproxy(preset, target_url) if preset.under_proxy(_host(target_url)) else target_url)
    wait = Usage.reserve(preset.id, publisher, clock(), lim)
    if wait > 0:
        note(f"waiting {wait:.0f}s (at least {lim.spacing}s between two papers through the proxy)")
        sleep(wait)

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
        head = _head(it)
        if _is_pdf(head):
            body = head + _read_body(it, already=len(head))
            resp.close()
            _save_pdf(out_path, body, _host(final) or "the proxy")
            if store.dirty:
                store.save(merge=True)
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
    if store.dirty:
        store.save(merge=True)  # the per-host handshake cookies help the next attempt
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
        head = _head(it)
        if not _is_pdf(head):
            resp.close()
            raise InstitutionError(f"{url[:80]} did not answer with a PDF (got {resp.headers.get('Content-Type')})")
        body = head + _read_body(it, already=len(head))
        resp.close()
        _save_pdf(out_path, body, _host(current))
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
            store.save(merge=True)
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
