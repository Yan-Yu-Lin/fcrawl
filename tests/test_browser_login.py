"""`fcrawl auth login`'s browser window: the CDP client, login detection, cleanup, browser
discovery, the CLI default path, and the test-only http preset against a local server.
Offline (fake CDP, local servers); the real-browser check is a manual script."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from fcrawl.utils import browser_login as B
from fcrawl.utils import institution as I
from test_institution import PDF, R, enable, run
from test_institution import env as _env_fixture

env = _env_fixture


# --- B: browser-window login ------------------------------------------------------------------


NTUT = I.BUILTIN_PRESETS["ntut"]


def cdp_cookie(name, value, domain, session=True):
    return {"name": name, "value": value, "domain": domain, "path": "/", "secure": True,
            "session": session, "expires": -1 if session else 4102444800}


HYSID = cdp_cookie("HYSID", "v", ".search.lib.ntut.edu.tw")
PRE = frozenset({("HYSID", "search.lib.ntut.edu.tw", "pre")})


@pytest.mark.parametrize("cookies,urls,before,done", [
    ([], ["https://search.lib.ntut.edu.tw/ntut/oauth.jsp?login=true"], frozenset(), False),
    ([HYSID], ["https://search.lib.ntut.edu.tw/ntut/LoginCheck.jsp?uid=1&pass=2"], frozenset(), False),  # token hop
    ([HYSID], ["https://nportal.ntut.edu.tw/oauth2Server.do"], frozenset(), False),
    ([HYSID], ["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/Xplore/home.jsp"], frozenset(), True),
    # after LoginCheck the ntut proxy forwards to the unproxied publisher: still done
    ([HYSID], ["https://ieeexplore.ieee.org/Xplore/home.jsp"], frozenset(), True),
    ([HYSID], ["about:blank"], frozenset(), False),
    # the pre-login cookie of the same name (host-only, set before the portal) is not a login
    ([cdp_cookie("HYSID", "pre", "search.lib.ntut.edu.tw")], ["https://ieeexplore.ieee.org/x"], PRE, False),
    ([cdp_cookie("HYSID", "pre", "search.lib.ntut.edu.tw"), HYSID], ["https://ieeexplore.ieee.org/x"], PRE, True),
    ([cdp_cookie("HYSID", "v", ".evil.example")], ["https://ieeexplore.ieee.org/x"], frozenset(), False),
    ([cdp_cookie("JSESSIONID", "v", "search.lib.ntut.edu.tw")], ["https://ieeexplore.ieee.org/x"], frozenset(), False),
])
def test_login_done(cookies, urls, before, done):
    assert B.login_done(NTUT, cookies, urls, before) is done


def test_cdp_cookie_conversion():
    c = B.to_cookie(cdp_cookie("HYSID", "v", ".search.lib.ntut.edu.tw"))
    assert c.expires is None and c.secure
    assert B.to_cookie(cdp_cookie("x", "v", "h", session=False)).expires == 4102444800


def test_find_browsers_by_path(tmp_path):
    exe = tmp_path / "chrome"
    exe.write_text("")
    assert B.find_browsers(str(exe))[0].path == str(exe)
    assert B.find_browsers(str(tmp_path / "missing")) == []


class FakeCDP:
    """Answers like a browser: after `ready_after` polls the user has logged in."""

    def __init__(self, ready_after=2):
        self.polls = 0
        self.ready_after = ready_after
        self.closed = False
        self.calls: list[str] = []

    def call(self, method, params=None, timeout=10):
        self.calls.append(method)
        if method == "Storage.getCookies":
            self.polls += 1
            if self.polls < self.ready_after:
                return {"cookies": [cdp_cookie("HYSID", "pre-login", ".search.lib.ntut.edu.tw"),
                                    cdp_cookie("NPORTAL", "p", "nportal.ntut.edu.tw")]}
            return {"cookies": [cdp_cookie("HYSID", "after-login", ".search.lib.ntut.edu.tw"),
                                cdp_cookie("NPORTAL", "p", "nportal.ntut.edu.tw"),
                                cdp_cookie("GA", "g", ".ntut.edu.tw")]}
        if method == "Target.getTargets":
            url = ("https://nportal.ntut.edu.tw/oauth2Server.do" if self.polls < self.ready_after
                   else "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/Xplore/home.jsp")
            return {"targetInfos": [{"type": "page", "url": url}]}
        return {}

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self):
        self.done = False

    def poll(self):
        return 0 if self.done else None

    def wait(self, timeout=None):
        self.done = True
        return 0

    def terminate(self):
        self.done = True

    kill = terminate


def fake_browser(monkeypatch, tmp_path, cdp):
    exe = tmp_path / "chrome"
    exe.write_text("")
    launched = {}

    def launch(browser, url, profile):
        launched["url"], launched["profile"] = url, profile
        (profile / "DevToolsActivePort").write_text("12345\n/devtools/browser/abc\n")
        (profile / "Cookies").write_text("secret browser state")
        return FakeProc()

    monkeypatch.setattr(B, "launch", launch)
    monkeypatch.setattr(B, "CDP", lambda url: cdp)
    monkeypatch.setattr(B, "POLL_SECONDS", 0)
    monkeypatch.setattr(B, "profile_parent", lambda b: tmp_path)
    return str(exe), launched


def test_browser_login_waits_keeps_only_proxy_cookies_and_cleans_up(env, monkeypatch, tmp_path):
    cdp = FakeCDP(ready_after=3)
    exe, launched = fake_browser(monkeypatch, tmp_path, cdp)
    notes: list[str] = []
    cookies, name = B.browser_login(NTUT, exe, timeout=30, note=notes.append)
    assert {c.name: c.value for c in cookies} == {"HYSID": "after-login"}  # portal/GA cookies dropped
    assert cdp.polls == 3 and "Browser.close" in cdp.calls and cdp.closed
    assert launched["url"].startswith("https://search.lib.ntut.edu.tw/ntut/oauth.jsp")
    assert not launched["profile"].exists()  # the temp profile (with the cookies) is gone
    assert not any("nportal" in n or "ieeexplore" in n for n in notes)  # no browser URL is ever echoed


def test_browser_login_timeout_and_closed_window(env, monkeypatch, tmp_path):
    cdp = FakeCDP(ready_after=10**9)
    exe, launched = fake_browser(monkeypatch, tmp_path, cdp)
    monkeypatch.setattr(B, "POLL_SECONDS", 0.01)
    with pytest.raises(B.LoginTimeout):
        B.browser_login(NTUT, exe, timeout=0.3)
    assert not launched["profile"].exists()
    # the user closes the window: a clear error, profile removed all the same
    closing = FakeCDP(ready_after=10**9)
    exe, launched = fake_browser(monkeypatch, tmp_path, closing)
    real_launch = B.launch

    def launch_and_close(browser, url, profile):
        proc = real_launch(browser, url, profile)
        proc.done = True
        return proc

    monkeypatch.setattr(B, "launch", launch_and_close)
    with pytest.raises(I.InstitutionError, match="closed"):
        B.browser_login(NTUT, exe, timeout=30)
    assert not launched["profile"].exists()


def test_browser_not_found_falls_back_to_instructions(env, monkeypatch):
    enable()
    monkeypatch.setattr(B, "find_browsers", lambda which=None: [])
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    res = run("auth", "login")
    assert res.exit_code == 0
    assert "no Chrome, Edge, Brave or Chromium found" in res.output and "--cookie-file" in res.output


def test_auth_login_default_uses_the_browser_window(env, monkeypatch):
    web, _ = env
    enable()
    seen = {}

    def fake_login(preset, browser, timeout, note):
        seen["args"] = (preset.id, browser, timeout)
        return [I.Cookie("HYSID", "from-window", ".search.lib.ntut.edu.tw")], "edge"

    monkeypatch.setattr(B, "browser_login", fake_login)
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/Xplore/home.jsp"] = R(200, "<html>IEEE</html>")
    res = run("auth", "login", "--browser", "edge", "--timeout", "120")
    assert res.exit_code == 0, res.output
    assert seen["args"] == ("ntut", "edge", 120) and "session OK" in res.output and "from-window" not in res.output
    assert I.CookieStore.load("ntut").source == "browser-window:edge"


# a real CDP client against a local websocket server speaking the protocol


def test_cdp_client_ignores_events_and_matches_ids():
    from websockets.sync.server import serve

    def handler(ws):
        for raw in ws:
            msg = json.loads(raw)
            ws.send(json.dumps({"method": "Target.targetInfoChanged", "params": {"url": "https://x/LoginCheck?pass=t"}}))
            if msg["method"] == "Storage.getCookies":
                ws.send(json.dumps({"id": msg["id"], "result": {"cookies": [cdp_cookie("HYSID", "v", "h")]}}))
            else:
                ws.send(json.dumps({"id": msg["id"], "error": {"message": "nope"}}))

    with serve(handler, "127.0.0.1", 0) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.socket.getsockname()[1]
        cdp = B.CDP(f"ws://127.0.0.1:{port}/devtools/browser/x")
        assert B.all_cookies(cdp)[0]["name"] == "HYSID"
        with pytest.raises(I.InstitutionError, match="nope"):
            cdp.call("Bogus.method")
        cdp.close()
        server.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="the fake browser is a shell script")
def test_devtools_port_file_is_read(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("4567\n/devtools/browser/uuid\n")
    assert B.wait_devtools_port(tmp_path, FakeProc(), timeout=1) == (4567, "/devtools/browser/uuid")
    proc = FakeProc()
    proc.done = True
    with pytest.raises(I.InstitutionError, match="exited"):
        B.wait_devtools_port(tmp_path / "nothing", proc, timeout=1)


# the probe and fetch paths with a test-only http preset against a local server


def test_local_http_preset_end_to_end(env, monkeypatch):
    """A preset on a local http server (scheme http is for tests only; .fcrawlrc presets
    are https): login cookie accepted, PDF fetched with the cookie, no cookie elsewhere."""
    seen: list[tuple[str, str]] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append((self.headers.get("Host", "").split(":")[0], self.headers.get("Cookie") or ""))
            if "HYSID=ok" not in (self.headers.get("Cookie") or ""):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/login?url=x")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.end_headers()
            self.wfile.write(PDF)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    monkeypatch.setattr(I, "http_get", lambda url, cookies, timeout, stream=False: requests.get(
        url, cookies=cookies or None, allow_redirects=False, timeout=timeout, stream=stream))
    preset = I.Preset(id="local", name="local", proxy_host="127.0.0.1", style="hostname",
                      login_url=f"http://127.0.0.1:{port}/start?url={{url}}", login_marker=f"http://127.0.0.1:{port}/login?url=",
                      session_cookie="HYSID", scheme="http", port=port)
    store = I.CookieStore("local", [I.Cookie("HYSID", "ok", "127.0.0.1", secure=False)])
    got = I.fetch_pdf(preset, store, f"http://127.0.0.1:{port}/paper.pdf", env[1] / "p.pdf",
                      lim=I.Limits(20, 10, 10), sleep=lambda s: None)
    assert got.size == len(PDF) and seen[-1] == ("127.0.0.1", "HYSID=ok")
    with pytest.raises(I.NotLoggedIn):
        I.fetch_pdf(preset, I.CookieStore("local", [I.Cookie("HYSID", "bad", "127.0.0.1")]),
                    f"http://127.0.0.1:{port}/paper.pdf", env[1] / "q.pdf", lim=I.Limits(20, 10, 10),
                    sleep=lambda s: None)
    srv.shutdown()
