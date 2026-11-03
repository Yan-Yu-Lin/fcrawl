"""`fcrawl auth login`: a browser window of fcrawl's own, so the user logs in to the
library there and fcrawl reads the session cookies straight from the browser.

The browser is a Chromium the machine already has (Edge on Windows, Chrome/Edge/Brave/
Chromium elsewhere), started with a fresh temporary profile and the DevTools protocol
(CDP) on a local port. fcrawl talks to it over a websocket (`websockets`, no Playwright)
and polls `Storage.getCookies`, which hands out cookies unencrypted, HttpOnly and session
cookies included. That sidesteps everything that makes reading a browser's cookie
database fragile: Windows app-bound encryption, macOS Keychain prompts, Linux keyrings,
locked databases, session cookies that never reach the disk.

Login is done when the preset's session cookie (ntut: HYSID) exists, no window is on a
login step any more (the campus portal, the LoginCheck hop whose URL carries the student
id and a one-time token) and the proxy accepts those cookies (one probe request). URLs
from the browser are never printed or logged. The temporary profile (it holds the
cookies) is deleted afterwards, whatever happened.

Chrome 136+ ignores --remote-debugging-port on the user's default profile; a separate
--user-data-dir, which we always use, is exactly what it wants.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .institution import Cookie, InstitutionError, Preset

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
    snap_name: str = ""


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
        if not Path(which).exists():
            return []
        snap = snap_name(which) if sys.platform.startswith("linux") else None
        return [Browser("custom", which, snap=bool(snap), snap_name=snap or "")]
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
        snap = snap_name(path) if sys.platform.startswith("linux") else None
        found.append(Browser(name, path, snap=bool(snap), snap_name=snap or ""))
    return found


_SNAP_IN_SCRIPT = re.compile(rb"/snap/bin/([a-z0-9][a-z0-9-]*)")


def snap_name(path: str) -> Optional[str]:
    """The snap a browser command runs, or None: /snap/bin/<name> (or a link to it), or a
    small wrapper script that execs one (Ubuntu's /usr/bin/chromium-browser)."""
    real = os.path.realpath(path)
    for p in (path, real):
        if p.startswith("/snap/bin/"):
            return Path(p).name
        if p.startswith("/snap/"):  # /snap/<name>/<rev>/...
            parts = Path(p).parts
            if len(parts) > 2:
                return parts[2]
    try:
        if os.path.getsize(real) < 16 * 1024:
            m = _SNAP_IN_SCRIPT.search(Path(real).read_bytes())
            if m:
                return m.group(1).decode()
    except OSError:
        pass
    return None


# --- the CDP session ----------------------------------------------------------------------


def profile_parent(browser: Browser) -> Path:
    """Where the temporary profile goes: the system temp dir, except for a snap browser.
    Strict snap confinement gives the browser a private /tmp and hides dot-directories in
    $HOME, so its profile goes where the snap itself may write, ~/snap/<name>/common
    (independent of FCRAWL_STATE_DIR). Untested on Linux so far."""
    if browser.snap:
        d = Path.home() / "snap" / (browser.snap_name or "chromium") / "common" / "fcrawl-login"
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
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


def login_candidate(preset: Preset, cookies: list[dict], urls: list[str]) -> bool:
    """Worth checking with the proxy: a session cookie (the preset's session_cookie, ntut
    HYSID) on a domain fcrawl keeps, and no window on a login step any more. Only a
    candidate: a pre-login cookie of the same name looks the same, and a proxy may turn
    an existing cookie into a logged-in one without changing it, so verify() decides.
    Where the window ends up does not matter: after the login the ntut proxy forwards to
    the publisher's own, unproxied site."""
    if not _session_cookies(preset, cookies):
        return False
    pages = [u for u in urls if urlsplit(u).scheme in ("http", "https")]
    return bool(pages) and not any(_login_hop(preset, u) for u in pages)


def verify(preset: Preset, cookies: list[Cookie]) -> bool:
    """One authenticated request to the proxy (its probe page) with the candidate cookies,
    nothing written to disk: True when the proxy serves it instead of its login page."""
    from .institution import CookieStore, probe

    candidate = CookieStore(preset.id, list(cookies))
    return bool(probe(preset, candidate, save=False).get("ok"))


VERIFY_EVERY = 5.0  # seconds between two checks of the same unchanged candidate


def browser_login(preset: Preset, browser: Optional[str] = None, timeout: float = LOGIN_TIMEOUT,
                  note: Callable[[str], None] = lambda _m: None) -> tuple[list[Cookie], str]:
    """Open the login page in a fresh browser window, wait until the user has logged in,
    return (the proxy's cookies, browser name). Logged in = a candidate (login_candidate)
    that the proxy accepts (verify); until then the window stays open and the session on
    file is untouched. BrowserNotFound when no Chromium browser is installed; LoginTimeout
    when the user does not finish within `timeout`. The temporary profile is removed
    whatever happens; InstitutionError if it cannot be."""
    from .institution import _keep

    found = find_browsers(browser)
    if not found:
        raise BrowserNotFound("no Chrome, Edge, Brave or Chromium found" + (f" (asked for {browser})" if browser else "")
                              + "; use --cookie-file, --paste or --from-clipboard")
    b = found[0]
    profile = Path(tempfile.mkdtemp(prefix="fcrawl-login-", dir=profile_parent(b)))
    proc: Optional[subprocess.Popen] = None
    cdp: Optional[CDP] = None
    result: Optional[tuple[list[Cookie], str]] = None
    try:
        try:
            os.chmod(profile, 0o700)
        except OSError:
            pass
        try:
            proc = launch(b, preset.login_page(), profile)
        except OSError as exc:
            raise InstitutionError(f"cannot start {b.name} ({exc.strerror or type(exc).__name__})") from None
        port, path = wait_devtools_port(profile, proc)
        cdp = CDP(f"ws://127.0.0.1:{port}{path}")
        note(f"a {b.name} window is open: log in to the library there (fcrawl never sees your password); "
             f"waiting up to {max(1, int(timeout // 60))} min, Ctrl+C to give up")
        deadline = time.monotonic() + timeout
        checked: dict[frozenset, float] = {}  # candidate -> when the proxy last refused it
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
            if login_candidate(preset, cookies, urls):
                key = frozenset(_session_cookies(preset, cookies))
                if time.monotonic() - checked.get(key, -VERIFY_EVERY) >= VERIFY_EVERY:
                    try:
                        kept = _keep(preset, [to_cookie(c) for c in cookies])
                        if verify(preset, kept):
                            result = (kept, b.name)
                            break
                    except InstitutionError:
                        pass
                    checked[key] = time.monotonic()
            time.sleep(POLL_SECONDS)
        if result is None:
            raise LoginTimeout(f"the login did not finish within {max(1, int(timeout // 60))} min "
                               "(or the library did not accept the session); run fcrawl auth login again")
    finally:
        try:
            if cdp is not None:
                try:
                    cdp.call("Browser.close", timeout=5)
                except Exception:
                    pass
                cdp.close()
        finally:
            try:
                if proc is not None:
                    _stop(proc)
            finally:
                _remove_profile(profile)
    return result


def _stop(proc: subprocess.Popen) -> None:
    """End the browser: it usually exits by itself after Browser.close. Never raises (a
    process that already exited makes terminate/kill fail on some platforms)."""
    for step in (None, "terminate", "kill"):
        try:
            if step:
                getattr(proc, step)()
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            if proc.poll() is not None:
                return
            continue


def _remove_profile(profile: Path) -> None:
    """The temporary profile holds the session cookies: remove it (Windows may hold a file
    open for a moment after the browser exits, so retry). InstitutionError when it stays."""
    for _ in range(10):
        shutil.rmtree(profile, ignore_errors=True)
        if not profile.exists():
            return
        time.sleep(0.3)
    raise InstitutionError(f"could not delete the temporary browser profile {profile}; it holds the library "
                           "cookies: delete it yourself")
