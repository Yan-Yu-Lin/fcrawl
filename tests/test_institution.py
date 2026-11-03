"""Institution (school library) proxy: presets, cookie scoping, rate limits, the fetch
pipeline and the `fcrawl auth` / `fcrawl papers fetch` commands. All HTTP is faked;
nothing leaves the machine and no real cookie is ever involved."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import click
import pytest
from click.testing import CliRunner

from fcrawl.cli import cli
from fcrawl.utils import institution as I

NTUT = I.BUILTIN_PRESETS["ntut"]
PDF = b"%PDF-1.7\n%fake\n" + b"x" * 100
LANDING = ('<html><head><meta name="citation_title" content="T">'
           '<meta name="citation_pdf_url" content="https://link.springer.com/content/pdf/10.1007/s1.pdf">'
           '</head><body>abstract</body></html>')


class R:
    """A fake requests.Response: status, headers, body (streamed in chunks)."""

    def __init__(self, status=200, body=b"", headers=None, cookies=None):
        self.status_code = status
        self.headers = {"Content-Type": "text/html"} | (headers or {})
        self._body = body if isinstance(body, bytes) else body.encode()
        self.cookies = [mk_cookie(*c) for c in (cookies or [])]  # what requests exposes as a CookieJar
        self.closed = False

    def iter_content(self, chunk_size=8):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


def mk_cookie(name, value, domain, expires=None):
    import http.cookiejar

    return http.cookiejar.Cookie(0, name, value, None, False, domain, True, domain.startswith("."), "/", True,
                                 True, expires, False, None, None, {})


class Web:
    """Routes URLs to responses and records every request with the cookies it carried."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, cookies, timeout, stream=False):
        self.calls.append((url, dict(cookies or {})))
        hit = self.routes.get(url)
        if hit is None:
            # match on path alone for proxied hosts (query strings vary)
            for k, v in self.routes.items():
                if url.startswith(k):
                    hit = v
                    break
        if hit is None:
            return R(404, b"nope")
        return hit() if callable(hit) else hit

    def hosts(self):
        return [(urlsplit(u).hostname, sorted(c)) for u, c in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh config (feature off), private state dir, fake HTTP, frozen sleep."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "https://gw.example", "gateway_token": "fgw_t"}))
    monkeypatch.setenv("FCRAWL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FCRAWL_TMP_DIR", str(tmp_path / "tmp"))
    web = Web()
    monkeypatch.setattr(I, "http_get", web)
    monkeypatch.setattr(I.time, "sleep", lambda s: None)
    return web, tmp_path


def run(*args, **kw):
    return CliRunner().invoke(cli, list(args), **kw)


def store_with(name="HYSID", value="s3cret-value", domain=".search.lib.ntut.edu.tw", expires=None):
    s = I.CookieStore("ntut", [I.Cookie(name, value, domain, expires=expires)], captured=I.iso_now(), source="test")
    s.save()
    return s


def enable():
    I.set_enabled("ntut", True)


# --- presets and rewriting ---------------------------------------------------------


@pytest.mark.parametrize("url,want", [
    ("https://ieeexplore.ieee.org/document/9609764", "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/9609764"),
    ("https://doi.org/10.1109/TPEL.2012.2223718", "https://doi-org.search.lib.ntut.edu.tw/10.1109/TPEL.2012.2223718"),
    ("https://www.sciencedirect.com/science/article/pii/S1?via=ihub",
     "https://www-sciencedirect-com.search.lib.ntut.edu.tw/science/article/pii/S1?via=ihub"),
    ("http://link.springer.com/article/10.1007/x", "https://link-springer-com.search.lib.ntut.edu.tw/article/10.1007/x"),
    ("https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1",
     "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1"),  # already proxied: untouched
])
def test_hostname_rewrite(url, want):
    assert NTUT.rewrite(url) == want


def test_prefix_rewrite_and_login_page():
    p = I.Preset("x", "X", "ezproxy.x.edu", "prefix", "https://ezproxy.x.edu/login?url={url}",
                 "https://ezproxy.x.edu/login")
    assert p.rewrite("https://ieeexplore.ieee.org/document/1?a=b") == \
        "https://ezproxy.x.edu/login?url=https%3A%2F%2Fieeexplore.ieee.org%2Fdocument%2F1%3Fa%3Db"
    assert NTUT.login_page("https://ieeexplore.ieee.org/x") == \
        "https://search.lib.ntut.edu.tw/ntut/oauth.jsp?login=true&url=https%3A%2F%2Fieeexplore.ieee.org%2Fx"


@pytest.mark.parametrize("bad", ["ftp://x.org/a", "not a url", "https://host.org:8080/a"])
def test_rewrite_rejects(bad):
    with pytest.raises(I.InstitutionError):
        NTUT.rewrite(bad)


def test_under_proxy():
    assert NTUT.under_proxy("search.lib.ntut.edu.tw")
    assert NTUT.under_proxy("ieeexplore-ieee-org.search.lib.ntut.edu.tw")
    assert not NTUT.under_proxy("ieee.org")
    assert not NTUT.under_proxy("evilsearch.lib.ntut.edu.tw")
    assert not NTUT.under_proxy("search.lib.ntut.edu.tw.evil.com")


def test_custom_presets_from_config(env):
    _, tmp = env
    cfg = {"institution": {"presets": {
        "ntust": {"name": "台科", "proxy_host": "ezproxy.lib.ntust.edu.tw", "style": "prefix",
                  "login_url": "https://ezproxy.lib.ntust.edu.tw/login?url={url}",
                  "login_marker": "https://ezproxy.lib.ntust.edu.tw/login"},
        "broken": {"name": "no host"},
        "Bad Id!": {"name": "x", "proxy_host": "h", "style": "prefix", "login_url": "{url}", "login_marker": "m"},
        "nostyle": {"name": "x", "proxy_host": "h", "style": "magic", "login_url": "{url}", "login_marker": "m"},
    }}}
    table = I.presets(cfg)
    assert set(table) == {"ntut", "ntust"}
    assert table["ntust"].style == "prefix"
    res = run("auth", "presets")
    assert res.exit_code == 0 and "ntut:" in res.output


# --- on/off ------------------------------------------------------------------------------


def test_off_by_default(env):
    assert I.settings() == {"enabled": False, "preset": None}
    with pytest.raises(I.NotEnabled):
        I.active_preset()
    res = run("auth", "status")
    assert res.exit_code != 0 and "off" in res.output
    res = run("papers", "fetch", "doi:10.1109/TPEL.2012.2223718")
    assert res.exit_code != 0 and "fcrawl auth enable" in res.output


def test_enable_disable_round_trip(env):
    _, tmp = env
    res = run("auth", "enable", "ntut")
    assert res.exit_code == 0, res.output
    saved = json.loads((tmp / ".fcrawlrc").read_text())
    assert saved["institution"] == {"preset": "ntut", "enabled": True}
    assert saved["gateway_token"] == "fgw_t"  # the rest of the config survives
    assert I.active_preset().id == "ntut"
    res = run("auth", "disable")
    assert res.exit_code == 0
    assert I.settings() == {"enabled": False, "preset": "ntut"}
    res = run("auth", "enable", "nope")
    assert res.exit_code != 0 and "unknown preset" in res.output


# --- cookies: scoping and storage ---------------------------------------------------------


def test_cookies_only_go_to_the_proxy(env):
    s = I.CookieStore("ntut", [
        I.Cookie("HYSID", "a", ".search.lib.ntut.edu.tw"),
        I.Cookie("JSESSIONID", "b", "search.lib.ntut.edu.tw"),
        I.Cookie("leak", "c", ".ieee.org"),  # a cookie for the publisher itself: never sent anywhere
        I.Cookie("old", "d", ".search.lib.ntut.edu.tw", expires=int(time.time()) - 10),
        I.Cookie("idp_session", "e", ".lib.ntut.edu.tw"),  # the proxy's rewrite of Domain=.springer.com
    ])
    assert s.for_host(NTUT, "search.lib.ntut.edu.tw") == {"HYSID": "a", "JSESSIONID": "b", "idp_session": "e"}
    assert s.for_host(NTUT, "ieeexplore-ieee-org.search.lib.ntut.edu.tw") == {"HYSID": "a", "idp_session": "e"}
    assert s.for_host(NTUT, "ieeexplore.ieee.org") == {}
    assert s.for_host(NTUT, "ieee.org") == {}
    assert s.for_host(NTUT, "lib.ntut.edu.tw") == {}  # a parent-domain cookie still only goes to the proxy
    assert s.for_host(NTUT, "search.lib.ntut.edu.tw.evil.com") == {}
    assert NTUT.cookie_domain_ok(".lib.ntut.edu.tw") and NTUT.cookie_domain_ok("search.lib.ntut.edu.tw")
    assert not NTUT.cookie_domain_ok(".ieee.org") and not NTUT.cookie_domain_ok("tw") and not NTUT.cookie_domain_ok("")


def test_store_round_trip_is_private(env):
    _, tmp = env
    s = store_with(expires=4102444800)
    path = I.CookieStore.path_for("ntut")
    assert path.parent == tmp / "state" / "institution"
    loaded = I.CookieStore.load("ntut")
    assert [c.__dict__ for c in loaded.cookies] == [c.__dict__ for c in s.cookies]
    assert loaded.captured == s.captured and loaded.source == "test"
    assert loaded.soonest_expiry() == 4102444800
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert loaded.delete() and not path.exists()
    assert I.CookieStore.load("ntut").cookies == []


def test_cookies_from_header_and_file(env, tmp_path):
    got = I.cookies_from_header(NTUT, "Cookie: HYSID=abc; JSESSIONID=def; empty=")
    assert [(c.name, c.value, c.domain) for c in got] == [("HYSID", "abc", ".search.lib.ntut.edu.tw"),
                                                           ("JSESSIONID", "def", ".search.lib.ntut.edu.tw")]
    with pytest.raises(I.NotLoggedIn):
        I.cookies_from_header(NTUT, "")
    txt = tmp_path / "cookies.txt"
    txt.write_text("# Netscape HTTP Cookie File\n"
                   ".search.lib.ntut.edu.tw\tTRUE\t/\tTRUE\t4102444800\tHYSID\tfromfile\n"
                   ".google.com\tTRUE\t/\tTRUE\t4102444800\tNID\tnotours\n")
    got = I.cookies_from_file(NTUT, str(txt))
    assert [(c.name, c.value, c.expires) for c in got] == [("HYSID", "fromfile", 4102444800)]
    other = tmp_path / "other.txt"
    other.write_text("# Netscape HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t0\tNID\tx\n")
    with pytest.raises(I.NotLoggedIn):
        I.cookies_from_file(NTUT, str(other))


def test_cookies_from_browser_uses_ytdlp(env, monkeypatch):
    import http.cookiejar

    import yt_dlp.cookies as ytc

    jar = http.cookiejar.CookieJar()

    jar.set_cookie(mk_cookie("HYSID", "fromff", ".search.lib.ntut.edu.tw"))
    jar.set_cookie(mk_cookie("SID", "google", ".google.com"))
    seen = {}

    def fake(browser, profile=None, logger=None, *, keyring=None, container=None):
        seen["args"] = (browser, profile)
        return jar

    monkeypatch.setattr(ytc, "extract_cookies_from_browser", fake)
    got = I.cookies_from_browser(NTUT, "firefox", "work")
    assert seen["args"] == ("firefox", "work")
    assert [(c.name, c.value) for c in got] == [("HYSID", "fromff")]

    def boom(*a, **k):
        raise RuntimeError("Could not find firefox cookies database")

    monkeypatch.setattr(ytc, "extract_cookies_from_browser", boom)
    with pytest.raises(I.InstitutionError, match="firefox cookies"):
        I.cookies_from_browser(NTUT, "firefox")


# --- following redirects through the proxy ------------------------------------------------


def test_follow_attaches_cookies_per_hop_and_detects_login(env):
    web, _ = env
    s = store_with()
    doc = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1"
    login = "https://search.lib.ntut.edu.tw/login?url=https%3A%2F%2Fieeexplore.ieee.org%2Fdocument%2F1"
    outside = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/outside"
    # the proxy sends us out to the publisher: we go back in once; when it bounces the same
    # host again, the library does not proxy it and we continue outside, without cookies
    web.routes[doc] = R(302, headers={"Location": "https://ieeexplore.ieee.org/outside"})
    web.routes[outside] = R(302, headers={"Location": "https://ieeexplore.ieee.org/outside"})
    web.routes["https://ieeexplore.ieee.org/outside"] = R(200, b"<html>free</html>")
    resp = I.follow(NTUT, s, doc, 10)
    assert resp.status_code == 200
    assert web.hosts() == [("ieeexplore-ieee-org.search.lib.ntut.edu.tw", ["HYSID"]),
                           ("ieeexplore-ieee-org.search.lib.ntut.edu.tw", ["HYSID"]), ("ieeexplore.ieee.org", [])]

    # the login endpoint answers with a page (404 here, its form in real life): session gone
    web.calls.clear()
    web.routes[doc] = R(302, headers={"Location": login})
    with pytest.raises(I.NotLoggedIn, match="fcrawl auth login"):
        I.follow(NTUT, s, doc, 10)
    assert web.hosts() == [("ieeexplore-ieee-org.search.lib.ntut.edu.tw", ["HYSID"]),
                           ("search.lib.ntut.edu.tw", ["HYSID"])]
    # …or hands us to the campus portal
    web.routes[login] = R(302, headers={"Location": "https://nportal.ntut.edu.tw/oauth2Server.do?x=1"})
    with pytest.raises(I.NotLoggedIn):
        I.follow(NTUT, s, doc, 10)
    web.routes[doc] = R(302, headers={"Location": "/login?url=x"})  # relative login redirect
    with pytest.raises(I.NotLoggedIn):
        I.follow(NTUT, s, doc, 10)
    web.routes[doc] = R(302, headers={"Location": doc})
    with pytest.raises(I.InstitutionError, match="too many redirects"):
        I.follow(NTUT, s, doc, 10)


def test_follow_handshake_keeps_proxy_cookies(env):
    """A live session: the publisher-host hop has no cookie yet, the login endpoint sets one
    and bounces back. The new cookie is kept (and saved by fetch_pdf) for the next paper."""
    web, _ = env
    s = store_with()
    doc = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1"
    login = "https://search.lib.ntut.edu.tw/login?url=https%3A%2F%2Fieeexplore.ieee.org%2Fdocument%2F1"
    first = {"n": 0}

    def doc_route():
        first["n"] += 1
        if first["n"] == 1:
            return R(302, headers={"Location": login})
        return R(200, b"%PDF-ok", {"Content-Type": "application/pdf"})

    web.routes[doc] = doc_route
    web.routes[login] = R(302, headers={"Location": doc},
                          cookies=[("ezsid", "per-host-session", "ieeexplore-ieee-org.search.lib.ntut.edu.tw"),
                                   ("tracker", "no", ".ieee.org")])
    resp = I.follow(NTUT, s, doc, 10)
    assert resp.status_code == 200
    assert web.hosts() == [("ieeexplore-ieee-org.search.lib.ntut.edu.tw", ["HYSID"]),
                           ("search.lib.ntut.edu.tw", ["HYSID"]),
                           ("ieeexplore-ieee-org.search.lib.ntut.edu.tw", ["HYSID", "ezsid"])]
    assert s.dirty and [c.name for c in s.cookies] == ["HYSID", "ezsid"]  # the publisher's own cookie is dropped
    # a second login bounce for the same page means the session is not accepted after all
    first["n"] = 0
    web.routes[login] = R(302, headers={"Location": doc})
    web.routes[doc] = R(302, headers={"Location": login})
    with pytest.raises(I.NotLoggedIn):
        I.follow(NTUT, s, doc, 10)


# --- the PDF behind a landing page ----------------------------------------------------------


@pytest.mark.parametrize("landing,html,want", [
    ("https://ieeexplore.ieee.org/document/9609764", "", ["https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=9609764"]),
    ("https://ieeexplore.ieee.org/abstract/document/77/", "", ["https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=77"]),
    ("https://www.sciencedirect.com/science/article/pii/S0026269221000276", "",
     ["https://www.sciencedirect.com/science/article/pii/S0026269221000276/pdfft?isDTMRedir=true&download=true"]),
    ("https://link.springer.com/article/10.1007/s10470-014-0268-2", "",
     ["https://link.springer.com/content/pdf/10.1007/s10470-014-0268-2.pdf"]),
    ("https://dl.acm.org/doi/10.1145/3404512.3404531", "", ["https://dl.acm.org/doi/pdf/10.1145/3404512.3404531"]),
    ("https://onlinelibrary.wiley.com/doi/full/10.1002/cta.2641", "",
     ["https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/cta.2641"]),
    ("https://www.tandfonline.com/doi/abs/10.1080/00207217.2015.1087054", "",
     ["https://www.tandfonline.com/doi/pdf/10.1080/00207217.2015.1087054"]),
    ("https://example.org/paper/1", LANDING, ["https://link.springer.com/content/pdf/10.1007/s1.pdf"]),
    ("https://example.org/paper/1", '<meta content="/files/x.pdf?a=1&amp;b=2" name="citation_pdf_url">',
     ["https://example.org/files/x.pdf?a=1&b=2"]),
    ("https://example.org/paper/1", "<html>nothing</html>", []),
])
def test_pdf_candidates(landing, html, want):
    assert I.pdf_candidates(landing, html) == want


SD_INTERSTITIAL = ('<html><head><meta http-equiv="refresh" content="0; url=/science/article/pii/S1?ref=cra_js_challenge" />'
                   '</head><body>Preparing your download<script>var a=1; location="/science/article/pii/S1/pdfft?'
                   'crasolve=1&r=abc123&ts=1793678828793&rtype=";</script></body></html>')


def test_interstitial_targets():
    page = "https://www.sciencedirect.com/science/article/pii/S1/pdfft?isDTMRedir=true&download=true"
    assert I.interstitial_targets(page, SD_INTERSTITIAL) == [
        "https://www.sciencedirect.com/science/article/pii/S1/pdfft?crasolve=1&r=abc123&ts=1793678828793&rtype="]
    # other hosts, the page itself and non-PDF targets are ignored
    html = '<script>window.location.href = "https://evil.example/x.pdf"; location="/same/page.pdf"</script>'
    assert I.interstitial_targets("https://pub.org/same/page.pdf", html) == []
    assert I.interstitial_targets("https://pub.org/a", '<script>location="/b.html"</script>') == []
    assert I.interstitial_targets("https://pub.org/a", "x" * 300_000) == []


def test_fetch_sciencedirect_download_page(env):
    """ScienceDirect: article page → pdfft → a 'preparing your download' page whose script
    carries the one-time link → the PDF. The script link is followed, once."""
    web, tmp = env
    s = store_with()
    base = "https://www-sciencedirect-com.search.lib.ntut.edu.tw/science/article/pii/S1"
    web.routes[base] = R(200, "<html>abstract</html>")
    web.routes[base + "/pdfft?isDTMRedir=true&download=true"] = R(200, SD_INTERSTITIAL)
    web.routes[base + "/pdfft?crasolve=1&r=abc123&ts=1793678828793&rtype="] = R(200, PDF, {"Content-Type": "application/pdf"})
    got = I.fetch_pdf(NTUT, s, "https://www.sciencedirect.com/science/article/pii/S1", tmp / "sd.pdf")
    assert got.size == len(PDF) and "crasolve=1" in got.pdf_url
    assert [u for u, _ in web.calls] == [base, base + "/pdfft?isDTMRedir=true&download=true",
                                        base + "/pdfft?crasolve=1&r=abc123&ts=1793678828793&rtype="]
    assert I.Usage.load("ntut").publishers == {"sciencedirect.com": 1}


def test_fetch_sciencedirect_js_challenge_is_named(env):
    """The real ScienceDirect keeps answering with its JavaScript challenge (the one-time
    link is computed in the browser). fetch says so instead of a generic 'no PDF'."""
    web, tmp = env
    s = store_with()
    base = "https://www-sciencedirect-com.search.lib.ntut.edu.tw/science/article/pii/S1"
    web.routes[base + "/pdfft"] = lambda: R(200, SD_INTERSTITIAL)  # every pdfft variant: the challenge again
    web.routes[base] = R(200, "<html>abstract</html>")  # after the prefix route: Web matches in insertion order
    with pytest.raises(I.NeedsBrowser, match="JavaScript challenge") as exc:
        I.fetch_pdf(NTUT, s, "https://www.sciencedirect.com/science/article/pii/S1", tmp / "sd.pdf")
    assert "www-sciencedirect-com.search.lib.ntut.edu.tw/science/article/pii/S1" in str(exc.value)
    assert len(web.calls) <= 2 + I.MAX_INTERSTITIALS + 1


def test_publisher_of():
    assert I.publisher_of("https://ieeexplore.ieee.org/document/1") == "ieee.org"
    assert I.publisher_of("https://www.sciencedirect.com/x") == "sciencedirect.com"
    assert I.publisher_of("https://search.lib.ntut.edu.tw/x") == "ntut.edu.tw"
    assert I.publisher_of("https://www.cambridge.org/core") == "cambridge.org"
    assert I.publisher_of("https://journals.co.uk/x") == "journals.co.uk"


# --- fetch_pdf -----------------------------------------------------------------------------------


def test_fetch_landing_then_pdf(env):
    """A DOI link is resolved at doi.org itself (public, no cookies), then the publisher
    page goes through the proxy; the per-publisher count names the publisher."""
    web, tmp = env
    s = store_with()
    article = "https://link-springer-com.search.lib.ntut.edu.tw/article/10.1007/s1"
    pdf = "https://link-springer-com.search.lib.ntut.edu.tw/content/pdf/10.1007/s1.pdf"
    web.routes["https://doi.org/10.1007/s1"] = R(302, headers={"Location": "http://link.springer.com/10.1007/s1"})
    web.routes["https://link-springer-com.search.lib.ntut.edu.tw/10.1007/s1"] = R(301, headers={"Location": article})
    web.routes[article] = R(200, LANDING)
    web.routes[pdf] = R(200, PDF, {"Content-Type": "application/pdf"})
    out = tmp / "out" / "p.pdf"
    notes: list[str] = []
    got = I.fetch_pdf(NTUT, s, "https://doi.org/10.1007/s1", out, note=notes.append)
    assert got.path == out and out.read_bytes() == PDF and got.size == len(PDF)
    assert got.pdf_url == pdf and got.url == "https://doi.org/10.1007/s1"
    assert web.hosts() == [("doi.org", []),  # the public resolver, no cookies
                           ("link-springer-com.search.lib.ntut.edu.tw", ["HYSID"]),
                           ("link-springer-com.search.lib.ntut.edu.tw", ["HYSID"]),
                           ("link-springer-com.search.lib.ntut.edu.tw", ["HYSID"])]
    assert notes and notes[0].startswith("DOI resolves to http://link.springer.com/10.1007/s1")
    u = I.Usage.load("ntut")
    assert u.total == 1 and u.publishers == {"springer.com": 1}
    assert not s.dirty and [c.name for c in I.CookieStore.load("ntut").cookies] == ["HYSID"]


def test_fetch_springer_idp_handshake(env):
    """Springer bounces every article request to idp.springer.com, which sets its cookies
    with Domain=.springer.com; the proxy rewrites that to .lib.ntut.edu.tw, a parent of its
    own host. Those cookies must be kept and sent back, or the two hosts loop forever."""
    web, tmp = env
    s = store_with()
    article = "https://link-springer-com.search.lib.ntut.edu.tw/article/10.1007/s1"
    idp = "https://idp-springer-com.search.lib.ntut.edu.tw/authorize?response_type=cookie"
    pdf = "https://link-springer-com.search.lib.ntut.edu.tw/content/pdf/10.1007/s1.pdf"
    seen = {"idp": 0}

    def article_route():
        if "idp_session" in web.calls[-1][1]:
            return R(200, LANDING)
        return R(303, headers={"Location": idp}, cookies=[("trackid", "t", ".lib.ntut.edu.tw")])

    def idp_route():
        seen["idp"] += 1
        return R(302, headers={"Location": article}, cookies=[("idp_session", "sess", ".lib.ntut.edu.tw"),
                                                                ("idp_marker", "m", ".lib.ntut.edu.tw")])

    web.routes[article] = article_route
    web.routes[idp] = idp_route
    web.routes[pdf] = R(200, PDF, {"Content-Type": "application/pdf"})
    got = I.fetch_pdf(NTUT, s, "https://link.springer.com/article/10.1007/s1", tmp / "t.pdf")
    assert got.size == len(PDF) and seen["idp"] == 1
    assert web.hosts()[-1] == ("link-springer-com.search.lib.ntut.edu.tw", ["HYSID", "idp_marker", "idp_session", "trackid"])
    kept = {c.name: c.domain for c in I.CookieStore.load("ntut").cookies}
    assert kept["idp_session"] == ".lib.ntut.edu.tw" and "HYSID" in kept


def test_fetch_doi_that_does_not_resolve(env):
    web, tmp = env
    s = store_with()
    web.routes["https://doi.org/10.1016/nope"] = R(404, b"DOI not found")
    web.routes["https://doi-org.search.lib.ntut.edu.tw/10.1016/nope"] = R(404, b"nope")
    with pytest.raises(I.InstitutionError, match="no PDF found"):
        I.fetch_pdf(NTUT, s, "https://doi.org/10.1016/nope", tmp / "t.pdf")
    assert I.Usage.load("ntut").publishers == {"doi.org": 1}  # counted all the same


def test_fetch_ieee_direct_pdf(env):
    web, tmp = env
    s = store_with()
    stamp = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/stampPDF/getPDF.jsp"
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/9609764"] = R(200, "<html>abstract</html>")
    web.routes[stamp] = R(200, PDF, {"Content-Type": "application/pdf"})
    got = I.fetch_pdf(NTUT, s, "https://ieeexplore.ieee.org/document/9609764", tmp / "a.pdf")
    assert got.size == len(PDF) and got.pdf_url.startswith(stamp)
    assert I.Usage.load("ntut").publishers == {"ieee.org": 1}


def test_fetch_failures(env):
    web, tmp = env
    s = store_with()
    doc = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1"
    web.routes[doc] = R(200, "<html>no pdf here</html>")
    with pytest.raises(I.InstitutionError, match="no PDF found"):
        I.fetch_pdf(NTUT, s, "https://ieeexplore.ieee.org/document/1", tmp / "a.pdf")
    web.routes[doc] = R(403, b"denied")
    with pytest.raises(I.NotLoggedIn, match="403"):
        I.fetch_pdf(NTUT, s, "https://ieeexplore.ieee.org/document/1", tmp / "a.pdf")
    web.routes[doc] = R(302, headers={"Location": "https://search.lib.ntut.edu.tw/login?url=x"})
    with pytest.raises(I.NotLoggedIn, match="session has expired"):
        I.fetch_pdf(NTUT, s, "https://ieeexplore.ieee.org/document/1", tmp / "a.pdf")
    web.routes[doc] = R(200, b"%PDF" + b"y" * (I.MAX_PDF_BYTES + 10), {"Content-Type": "application/pdf"})
    with pytest.raises(I.InstitutionError, match="larger than"):
        I.fetch_pdf(NTUT, s, "https://ieeexplore.ieee.org/document/1", tmp / "a.pdf")
    with pytest.raises(I.NotLoggedIn, match="no institution session"):
        I.fetch_pdf(NTUT, I.CookieStore("ntut"), "https://ieeexplore.ieee.org/document/1", tmp / "a.pdf")
    assert not (tmp / "a.pdf").exists()


# --- rate limits ----------------------------------------------------------------------------


def test_usage_limits_and_rollover(env):
    u = I.Usage("ntut")
    now, today = 1_700_000_000.0, "2023-11-14"
    assert u.check("ieee.org", now, today) == 0
    for i in range(I.PUBLISHER_DAILY_LIMIT):
        u.record("ieee.org", now + i, today)
    with pytest.raises(I.RateLimited, match="ieee.org"):
        u.check("ieee.org", now + 100, today)
    assert u.check("acm.org", now + 100, today) == 0  # another publisher is fine
    for i in range(I.DAILY_LIMIT - I.PUBLISHER_DAILY_LIMIT):
        u.record(f"pub{i}.org", now + 100 + i, today)
    with pytest.raises(I.RateLimited, match="daily limit reached"):
        u.check("new.org", now + 500, today)
    assert u.check("ieee.org", now + 90000, "2023-11-15") == 0  # a new day resets everything
    assert u.total == 0 and u.publishers == {}
    loaded = I.Usage.load("ntut")
    assert loaded.day == "2023-11-14" and loaded.total == I.DAILY_LIMIT  # check() does not persist a rollover


def test_spacing_between_papers(env):
    web, tmp = env
    s = store_with()
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/stampPDF/getPDF.jsp"] = \
        lambda: R(200, PDF, {"Content-Type": "application/pdf"})
    clock = {"t": 1_700_000_000.0}
    slept: list[float] = []
    notes: list[str] = []

    def sleep(sec):
        slept.append(sec)
        clock["t"] += sec

    def fetch(n):
        return I.fetch_pdf(NTUT, s, f"https://ieeexplore.ieee.org/document/{n}", tmp / f"{n}.pdf",
                           clock=lambda: clock["t"], sleep=sleep, note=notes.append)

    fetch(1)
    assert slept == []
    clock["t"] += 5
    fetch(2)
    assert slept == [pytest.approx(I.MIN_SPACING - 5)] and "waiting" in notes[0]
    clock["t"] += I.MIN_SPACING + 1
    fetch(3)
    assert len(slept) == 1
    assert I.Usage.load("ntut").total == 3


def test_free_pdf_is_direct_and_uncounted(env):
    web, tmp = env
    web.routes["https://arxiv.org/pdf/1706.03762"] = R(302, headers={"Location": "https://arxiv.org/pdf/1706.03762v7"})
    web.routes["https://arxiv.org/pdf/1706.03762v7"] = R(200, PDF, {"Content-Type": "application/pdf"})
    got = I.fetch_free_pdf("https://arxiv.org/pdf/1706.03762", tmp / "ax.pdf")
    assert got.size == len(PDF) and all(c == {} for _, c in web.calls)
    assert not I.Usage.path_for("ntut").exists()
    web.routes["https://arxiv.org/pdf/1706.03762v7"] = R(200, b"<html>")
    with pytest.raises(I.InstitutionError, match="did not answer with a PDF"):
        I.fetch_free_pdf("https://arxiv.org/pdf/1706.03762", tmp / "ax.pdf")


# --- probe / status ------------------------------------------------------------------------


def test_probe(env):
    web, _ = env
    s = store_with()
    home = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/Xplore/home.jsp"
    web.routes[home] = R(200, "<html>IEEE</html>")
    assert I.probe(NTUT, s)["ok"] is True
    web.routes[home] = R(302, headers={"Location": "https://search.lib.ntut.edu.tw/login?url=x"})
    r = I.probe(NTUT, s)
    assert r["ok"] is False and "login" in r["detail"]
    web.routes[home] = R(302, headers={"Location": "https://ieeexplore.ieee.org/Xplore/home.jsp"})
    web.routes["https://ieeexplore.ieee.org/Xplore/home.jsp"] = R(200, "<html>")
    assert "instead of proxying" in I.probe(NTUT, s)["detail"]
    assert I.probe(NTUT, I.CookieStore("ntut"))["ok"] is False
    assert web.hosts()[-1][0] == "ieeexplore.ieee.org"


# --- the commands ---------------------------------------------------------------------------


def test_auth_login_shows_instructions_without_opening(env):
    enable()
    res = run("auth", "login", "--no-open")
    assert res.exit_code == 0, res.output
    assert "oauth.jsp?login=true" in res.output and "--from-browser" in res.output
    assert "password" in res.output


def test_auth_login_paste_then_status_hides_values(env):
    web, _ = env
    enable()
    home = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/Xplore/home.jsp"
    web.routes[home] = R(200, "<html>IEEE</html>")
    res = run("auth", "login", "--paste", input="HYSID=supersecretvalue; JSESSIONID=other\n")
    assert res.exit_code == 0, res.output
    assert "saved 2 cookie(s)" in res.output and "session OK" in res.output
    assert "supersecretvalue" not in res.output
    res = run("auth", "status", "--no-probe")
    assert res.exit_code == 0, res.output
    assert "HYSID, JSESSIONID" in res.output and "supersecretvalue" not in res.output
    assert "0/20 papers" in res.output
    res = run("auth", "status", "--json")
    data = json.loads(res.stdout)
    assert data["cookies"] == ["HYSID", "JSESSIONID"] and data["session"]["ok"] is True
    assert "supersecretvalue" not in res.stdout
    res = run("auth", "logout")
    assert "cookies removed" in res.output
    assert "none on file" in run("auth", "status", "--no-probe").output


def test_auth_login_rejects_two_sources(env):
    enable()
    res = run("auth", "login", "--paste", "--from-browser", "firefox")
    assert res.exit_code != 0 and "one of" in res.output


def test_auth_login_probe_failure_is_reported(env):
    web, _ = env
    enable()
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/Xplore/home.jsp"] = \
        R(302, headers={"Location": "https://search.lib.ntut.edu.tw/login?url=x"})
    res = run("auth", "login", "--paste", input="HYSID=x\n")
    assert res.exit_code == 0 and "session check failed" in res.output


def test_papers_fetch_cli(env):
    web, tmp = env
    enable()
    store_with()
    stamp = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/stampPDF/getPDF.jsp"
    web.routes["https://doi.org/10.1109/TPEL.2012.2223718"] = \
        R(302, headers={"Location": "https://ieeexplore.ieee.org/document/6329441"})
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/6329441"] = R(200, "<html>abs</html>")
    web.routes[stamp] = R(200, PDF, {"Content-Type": "application/pdf"})
    res = run("papers", "fetch", "https://doi.org/10.1109/TPEL.2012.2223718")
    assert res.exit_code == 0, res.output
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("RESULT ")][0]
    assert "provider=institution:ntut" in line and "status=ok" in line and f"bytes={len(PDF)}" in line
    path = line.split("path=", 1)[1].split(" provider=")[0]
    assert Path(path).read_bytes() == PDF and "fcrawl-saved" in path
    res = run("papers", "fetch", "doi:10.1109/TPEL.2012.2223718", "-o", str(tmp / "mine.pdf"), "--json")
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["ok"] and data["path"] == str(tmp / "mine.pdf") and data["provider"] == "institution:ntut"
    assert I.Usage.load("ntut").total == 2 and I.Usage.load("ntut").publishers == {"ieee.org": 2}


def test_papers_fetch_errors_and_free_ids(env):
    web, tmp = env
    enable()
    res = run("papers", "fetch", "doi:10.1109/TPEL.2012.2223718")
    assert res.exit_code != 0 and "fcrawl auth login" in res.output and "status=failed" in res.stdout
    with pytest.raises(click.BadParameter):
        from fcrawl.commands.papers import _fetch_target

        _fetch_target("pmid:34515826")
    web.routes["https://arxiv.org/pdf/1706.03762"] = R(200, PDF, {"Content-Type": "application/pdf"})
    res = run("papers", "fetch", "arxiv:1706.03762", "-o", str(tmp / "ax.pdf"))
    assert res.exit_code == 0, res.output
    assert "provider=direct" in res.stdout and (tmp / "ax.pdf").read_bytes() == PDF
    assert not I.Usage.path_for("ntut").exists()  # free sources are not counted


def test_papers_pdf_points_at_fetch_when_enabled(env, monkeypatch):
    from fcrawl.commands import papers as P

    monkeypatch.setattr(P, "openalex_work", lambda pid, nc: {"title": "T", "year": 2012, "ids": {"doi": "10.1/x"},
                                                              "locations": [], "oaStatus": "closed"})
    monkeypatch.setattr(P, "s2_open_pdf", lambda pid: (None, "no open PDF"))
    res = run("papers", "pdf", "doi:10.1/x", "--no-scholar")
    assert "school or library access" in res.output and "papers fetch" not in res.output
    enable()
    res = run("papers", "pdf", "doi:10.1/x", "--no-scholar")
    assert "fcrawl papers fetch doi:10.1/x" in res.output
