"""`fcrawl auth login`: a browser window of fcrawl's own, so the user logs in to the
library there and fcrawl reads the session cookies straight from the browser.

The browser is a Chromium the machine already has (Edge on Windows, Chrome/Edge/Brave/
Chromium elsewhere), started with a fresh temporary profile and the DevTools protocol
(CDP) on a local port. fcrawl talks to it over a websocket (`websockets`, no Playwright)
and polls `Storage.getCookies`, which hands out cookies unencrypted, HttpOnly and session
cookies included. That sidesteps everything that makes reading a browser's cookie
database fragile: Windows app-bound encryption, macOS Keychain prompts, Linux keyrings,
locked databases, session cookies that never reach the disk.

Login is done when the preset's session cookie (ntut: HYSID) exists and the window is
back on the proxy (not on the campus portal or the LoginCheck hop, whose URL carries the
student id and a one-time token). URLs from the browser are never printed or logged.
The temporary profile (it holds the cookies) is deleted afterwards.

Chrome 136+ ignores --remote-debugging-port on the user's default profile; a separate
--user-data-dir, which we always use, is exactly what it wants.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .institution import Cookie, InstitutionError, Preset, state_dir

LOGIN_TIMEOUT = 600  # seconds the user has to finish logging in
POLL_SECONDS = 1.0
STARTUP_TIMEOUT = 30


class BrowserNotFound(InstitutionError):
    pass


class LoginTimeout(InstitutionError):
    pass


@dataclass(frozen=True)
class Browser:
    name: str
    path: str
    snap: bool = False  # snap confinement: the profile cannot live in /tmp


# --- finding a browser ------------------------------------------------------------------


def _windows_candidates() -> list[tuple[str, str]]:
    roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    rel = [("edge", r"Microsoft\Edge\Application\msedge.exe"),
           ("chrome", r"Google\Chrome\Application\chrome.exe"),
           ("brave", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
           ("chromium", r"Chromium\Application\chrome.exe")]
    out = []
    for name, sub in rel:  # Edge first: it ships with Windows 10/11
        for root in roots:
            if root:
                out.append((name, str(Path(root) / sub)))
    return out


MAC_APPS = [("chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
            ("edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ("brave", "Brave Browser.app/Contents/MacOS/Brave Browser"),
            ("chromium", "Chromium.app/Contents/MacOS/Chromium"),
            ("vivaldi", "Vivaldi.app/Contents/MacOS/Vivaldi")]
# Arc is Chromium too but ignores --remote-debugging-port / --user-data-dir (tested
# 2026-11-03: it opens the user's own Arc window and no DevTools port), so it is not listed.

LINUX_NAMES = [("chrome", "google-chrome"), ("chrome", "google-chrome-stable"), ("chromium", "chromium"),
               ("chromium", "chromium-browser"), ("edge", "microsoft-edge"), ("edge", "microsoft-edge-stable"),
               ("brave", "brave-browser"), ("brave", "brave")]


def find_browsers(which: Optional[str] = None) -> list[Browser]:
    """Installed Chromium browsers, best first. `which`: only that one (name or a path)."""
    if which and (os.sep in which or "/" in which):
        return [Browser("custom", which, snap="/snap/" in which)] if Path(which).exists() else []
    found: list[Browser] = []
    if sys.platform == "win32":
        cands = _windows_candidates()
    elif sys.platform == "darwin":
        cands = [(n, str(Path(root) / rel)) for n, rel in MAC_APPS
                 for root in ("/Applications", str(Path.home() / "Applications"))]
    else:
        cands = []
        for n, exe in LINUX_NAMES:
            p = shutil.which(exe)
            if p:
                cands.append((n, p))
    seen: set[str] = set()
    for name, path in cands:
        if which and name != which.lower():
            continue
        if path in seen or not Path(path).exists():
            continue
        seen.add(path)
        real = os.path.realpath(path)
        found.append(Browser(name, path, snap=sys.platform.startswith("linux") and
                             ("/snap/" in real or real.endswith("/usr/bin/snap"))))
    return found


# --- the CDP session ----------------------------------------------------------------------


def profile_parent(browser: Browser) -> Path:
    """Where the temporary profile goes: the system temp dir, except for a snap browser,
    which cannot see /tmp (then ~/.fcrawl/browser-profiles)."""
    if browser.snap:
        d = state_dir().parent / "browser-profiles"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(tempfile.gettempdir())


def launch(browser: Browser, url: str, profile: Path) -> subprocess.Popen:
    args = [browser.path, f"--user-data-dir={profile}", "--remote-debugging-port=0",
            "--remote-allow-origins=http://127.0.0.1", "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate", "--new-window", url]
    kw: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(args, **kw)


def wait_devtools_port(profile: Path, proc: subprocess.Popen, timeout: float = STARTUP_TIMEOUT) -> tuple[int, str]:
    """(port, browser websocket path) from the DevToolsActivePort file Chromium writes."""
    f = profile / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if f.exists():
            lines = f.read_text(encoding="utf-8", errors="replace").split()
            if len(lines) >= 2 and lines[0].isdigit():
                return int(lines[0]), lines[1]
        if proc.poll() is not None:
            raise InstitutionError("the browser exited before it opened its DevTools port (is another copy "
                                   "using this profile, or does this browser ignore --remote-debugging-port?)")
        time.sleep(0.2)
    raise InstitutionError("the browser did not open its DevTools port in time")


class CDP:
    """A minimal DevTools protocol client over one browser-level websocket."""

    def __init__(self, ws_url: str):
        from websockets.sync.client import connect

        # loopback only, never through a system proxy; big messages (cookie lists)
        self._ws = connect(ws_url, max_size=16 * 1024 * 1024, open_timeout=10, proxy=None)
        self._ids = itertools.count(1)

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 10) -> dict:
        msg_id = next(self._ids)
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise InstitutionError(f"the browser did not answer {method}")
            data = json.loads(self._ws.recv(timeout=left))
            if data.get("id") == msg_id:  # events (they carry URLs) are dropped unread
                if "error" in data:
                    raise InstitutionError(f"{method} failed: {str(data['error'].get('message', ''))[:100]}")
                return data.get("result") or {}

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def page_urls(cdp: CDP) -> list[str]:
    return [t.get("url", "") for t in cdp.call("Target.getTargets").get("targetInfos", [])
            if t.get("type") == "page"]


def all_cookies(cdp: CDP) -> list[dict]:
    return cdp.call("Storage.getCookies").get("cookies", [])


def to_cookie(c: dict) -> Cookie:
    exp = c.get("expires")
    session = c.get("session") or not isinstance(exp, (int, float)) or exp <= 0
    return Cookie(str(c.get("name", "")), str(c.get("value", "")), str(c.get("domain", "")),
                  str(c.get("path") or "/"), bool(c.get("secure", False)), None if session else int(exp))


_HOP_MARKS = ("logincheck", "oauth", "/login", "sso", "authorize", "/idp/", "saml", "shibboleth", "cas/")


def _login_hop(preset: Preset, url: str) -> bool:
    """A page that is still part of the login: the proxy's login marker, the campus portal's
    OAuth form, the LoginCheck hop (it carries the student id and a token), SSO/SAML."""
    if preset.is_login_redirect(url):
        return True
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False  # about:blank, chrome:// pages
    return any(m in (parts.path + "?" + parts.query).lower() or m in (parts.hostname or "") for m in _HOP_MARKS)


def _session_cookies(preset: Preset, cookies: list[dict]) -> set[tuple[str, str, str]]:
    want = preset.session_cookie
    return {(str(c.get("name")), str(c.get("domain")), str(c.get("value"))) for c in cookies
            if c.get("value") and preset.cookie_domain_ok(str(c.get("domain", "")))
            and (c.get("name") == want if want else True)}


def login_done(preset: Preset, cookies: list[dict], urls: list[str],
               before: frozenset = frozenset()) -> bool:
    """Login is finished when a session cookie exists that was not there when the window
    opened (`before`: some proxies hand out a pre-login cookie of the same name), on a
    domain fcrawl keeps, and no window is on a login step any more. Where the window ends
    up does not matter: after the login the ntut proxy forwards to the publisher's own,
    unproxied site."""
    if not (_session_cookies(preset, cookies) - before):
        return False
    pages = [u for u in urls if urlsplit(u).scheme in ("http", "https")]
    return bool(pages) and not any(_login_hop(preset, u) for u in pages)


def browser_login(preset: Preset, browser: Optional[str] = None, timeout: float = LOGIN_TIMEOUT,
                  note: Callable[[str], None] = lambda _m: None, keep_open: bool = False,
                  start_url: Optional[str] = None) -> tuple[list[Cookie], str]:
    """Open the login page in a fresh browser window, wait until the user has logged in,
    return (the proxy's cookies, browser name). BrowserNotFound when no Chromium browser
    is installed; LoginTimeout when the user does not finish within `timeout`."""
    from .institution import _keep

    found = find_browsers(browser)
    if not found:
        raise BrowserNotFound("no Chrome, Edge, Brave or Chromium found" + (f" (asked for {browser})" if browser else "")
                              + "; use --cookie-file, --paste or --from-clipboard")
    b = found[0]
    profile = Path(tempfile.mkdtemp(prefix="fcrawl-login-", dir=profile_parent(b)))
    try:
        os.chmod(profile, 0o700)
    except OSError:
        pass
    proc = launch(b, start_url or preset.login_page(), profile)
    cdp: Optional[CDP] = None
    try:
        port, path = wait_devtools_port(profile, proc)
        cdp = CDP(f"ws://127.0.0.1:{port}{path}")
        note(f"a {b.name} window is open: log in to the library there (fcrawl never sees your password); "
             f"waiting up to {int(timeout // 60)} min, Ctrl+C to give up")
        deadline = time.monotonic() + timeout
        before: Optional[frozenset] = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise InstitutionError("the browser window was closed before the login finished")
            try:
                cookies = all_cookies(cdp)
                urls = page_urls(cdp)
            except Exception as exc:  # the browser went away mid-call
                if proc.poll() is not None:
                    raise InstitutionError("the browser window was closed before the login finished") from None
                raise InstitutionError(f"lost the connection to the browser ({type(exc).__name__})") from None
            if before is None:  # the first look: whatever is set now predates the login
                before = frozenset(_session_cookies(preset, cookies))
            elif login_done(preset, cookies, urls, before):
                kept = _keep(preset, [to_cookie(c) for c in cookies])
                return kept, b.name
            time.sleep(POLL_SECONDS)
        raise LoginTimeout(f"the login did not finish within {int(timeout // 60)} min; run fcrawl auth login again")
    finally:
        if cdp is not None:
            if not keep_open:
                try:
                    cdp.call("Browser.close", timeout=5)
                except Exception:
                    pass
            cdp.close()
        if not keep_open:
            _stop(proc)
            _remove_profile(profile)


def _stop(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _remove_profile(profile: Path) -> None:
    """The temporary profile holds the session cookies: remove it (Windows may hold a file
    open for a moment after the browser exits, so retry)."""
    for _ in range(10):
        shutil.rmtree(profile, ignore_errors=True)
        if not profile.exists():
            return
        time.sleep(0.3)
