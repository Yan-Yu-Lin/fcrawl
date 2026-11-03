"""Institution proxy, review round 1: the file lock and slot booking, https-only
cookies, the config write, the tolerant cookies.txt reader, stricter presets, the PDF
check, configurable limits. All offline, fake values only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fcrawl.utils import institution as I
from test_institution import NTUT, PDF, R, store_with
from test_institution import env as _env_fixture

env = _env_fixture  # the same fixture, under the name the tests ask for

SECRET = "FAKE-SECRET-VALUE-123"


# --- M1: one lock, booked slots, unique temp files ---------------------------------------


CHILD = r"""
import sys
from fcrawl.utils import institution as I
lim = I.Limits(daily=20, per_publisher=10, spacing=30)
print(I.Usage.reserve("ntut", "ieee.org", 1_700_000_000.0, lim))
"""


def test_parallel_reservations_queue_up(env, tmp_path):
    """Four fcrawl processes at the same instant: every paper is counted, nobody crashes,
    and the slots come out 0, 30, 60, 90 s apart."""
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    envv = os.environ.copy()
    procs = [subprocess.Popen([sys.executable, str(script)], env=envv, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(4)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), [e for _, e in outs]
    waits = sorted(float(o) for o, _ in outs)
    assert waits == [0.0, 30.0, 60.0, 90.0]
    usage = I.Usage.load("ntut")
    assert usage.total == 4 and usage.publishers == {"ieee.org": 4}
    assert not list(I.state_dir().glob("*.tmp"))


def test_reserve_refuses_over_the_limit(env):
    lim = I.Limits(daily=2, per_publisher=2, spacing=10)
    assert I.Usage.reserve("ntut", "ieee.org", 1_700_000_000.0, lim) == 0
    assert I.Usage.reserve("ntut", "ieee.org", 1_700_000_000.0, lim) == 10
    with pytest.raises(I.RateLimited):
        I.Usage.reserve("ntut", "acm.org", 1_700_000_100.0, lim)
    assert I.Usage.load("ntut").total == 2


def test_clock_turned_back_waits_one_spacing_not_hours(env):
    u = I.Usage("ntut", day="2023-11-14", last=1_700_100_000.0)  # a day "in the future"
    assert u.check("ieee.org", 1_700_000_000.0, "2023-11-14") == I.MIN_SPACING


def test_concurrent_cookie_saves_merge(env):
    """A download saving the per-host cookies it picked up keeps what another process
    saved meanwhile."""
    a = store_with()
    b = I.CookieStore.load("ntut")
    b.cookies.append(I.Cookie("ERIGHTS", "b", ".ieeexplore-ieee-org.search.lib.ntut.edu.tw"))
    b.save(merge=True)
    a.cookies.append(I.Cookie("SPRINGER", "a", ".lib.ntut.edu.tw"))
    a.save(merge=True)
    assert sorted(c.name for c in I.CookieStore.load("ntut").cookies) == ["ERIGHTS", "HYSID", "SPRINGER"]


def test_no_live_session_costs_nothing(env):
    web, tmp = env
    s = store_with(expires=int(time.time()) - 5)
    with pytest.raises(I.NotLoggedIn):
        I.fetch_pdf(NTUT, s, "https://ieeexplore.ieee.org/document/1", tmp / "a.pdf")
    assert web.calls == [] and not I.Usage.path_for("ntut").exists()


# --- M2: cookies.txt -------------------------------------------------------------------


def test_bad_cookie_file_never_echoes_a_value(env, tmp_path, capsys):
    bad = tmp_path / "bad.txt"
    bad.write_text(f"# Netscape HTTP Cookie File\nsearch.lib.ntut.edu.tw HYSID {SECRET}\n")
    with pytest.raises(I.InstitutionError) as exc:
        I.cookies_from_file(NTUT, str(bad))
    assert SECRET not in str(exc.value) and "line" in str(exc.value)
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_tolerant_netscape_parser(env, tmp_path):
    """What Python's MozillaCookieJar refuses: a dotted domain with FALSE in the flag
    column, #HttpOnly_ lines, CRLF line ends, a BOM, a value-less trailing tab."""
    txt = tmp_path / "c.txt"
    txt.write_bytes(("﻿# Netscape HTTP Cookie File\r\n"
                     ".search.lib.ntut.edu.tw\tFALSE\t/\tTRUE\t0\tHYSID\tv1\r\n"
                     "#HttpOnly_.search.lib.ntut.edu.tw\tTRUE\t/\tTRUE\t4102444800\tJSESSIONID\tv2\r\n"
                     "search.lib.ntut.edu.tw\tFALSE\t/\tFALSE\t4102444800\tempty\r\n"
                     "garbage line\r\n").encode("utf-8"))
    got = {c.name: c for c in I.cookies_from_file(NTUT, str(txt))}
    assert set(got) == {"HYSID", "JSESSIONID", "empty"}
    assert got["HYSID"].expires is None and got["HYSID"].domain == ".search.lib.ntut.edu.tw"
    assert got["JSESSIONID"].expires == 4102444800 and got["empty"].value == ""


# --- M3: the config write ----------------------------------------------------------------


def test_enable_refuses_to_overwrite_a_broken_config(env):
    _, tmp = env
    rc = tmp / ".fcrawlrc"
    broken = '{"api_url": "https://gw.example", "gateway_token": "fgw_t",}'
    rc.write_text(broken)
    with pytest.raises(I.InstitutionError, match="not valid JSON"):
        I.set_enabled("ntut", True)
    assert rc.read_text() == broken
    from test_institution import run

    res = run("auth", "enable", "ntut")
    assert res.exit_code != 0 and "not valid JSON" in res.output and "fgw_t" not in res.output
    assert rc.read_text() == broken


def test_enable_writes_owner_only(env):
    _, tmp = env
    I.set_enabled("ntut", True)
    rc = tmp / ".fcrawlrc"
    assert json.loads(rc.read_text())["gateway_token"] == "fgw_t"
    if sys.platform != "win32":
        assert rc.stat().st_mode & 0o777 == 0o600


# --- M4: https only ------------------------------------------------------------------------


def test_no_cookies_over_http(env):
    s = store_with()
    assert s.for_url(NTUT, "http://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1") == {}
    assert s.for_url(NTUT, "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1") == {"HYSID": "s3cret-value"}


def test_http_downgrade_is_taken_back_to_https(env):
    web, _ = env
    s = store_with()
    doc = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1"
    web.routes[doc + "?x=1"] = R(302, headers={"Location": "http://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1"})
    web.routes[doc] = R(200, b"<html>ok</html>")
    resp = I.follow(NTUT, s, doc + "?x=1", 10)
    assert resp.status_code == 200
    assert [u.split(":")[0] for u, _ in web.calls] == ["https", "https"]
    assert all(c == ["HYSID"] for _, c in web.hosts())


# --- S2 / S3: cookie domains and presets --------------------------------------------------


def test_parent_domains_only_when_listed():
    assert NTUT.cookie_domain_ok(".lib.ntut.edu.tw")  # extra_cookie_domains of the ntut preset
    assert not NTUT.cookie_domain_ok(".ntut.edu.tw")
    assert not NTUT.cookie_domain_ok(".edu.tw")
    assert NTUT.cookie_domain_ok(".ieeexplore-ieee-org.search.lib.ntut.edu.tw")


GOOD = {"name": "X", "proxy_host": "ezproxy.lib.x.edu", "style": "prefix",
        "login_url": "https://ezproxy.lib.x.edu/login?url={url}", "login_marker": "https://ezproxy.lib.x.edu/login"}


@pytest.mark.parametrize("change,why", [
    ({"login_marker": ""}, "login_marker"),
    ({"login_url": "http://ezproxy.lib.x.edu/login?url={url}"}, "login_url"),
    ({"login_url": "https://evil.example/login?url={url}"}, "login_url"),
    ({"proxy_host": "x.edu"}, "proxy_host"),
    ({"proxy_host": "com"}, "proxy_host"),
    ({"probe_url": "http://ieeexplore.ieee.org/"}, "probe_url"),
    ({"extra_cookie_domains": [".edu"]}, "extra_cookie_domains"),
    ({"style": "magic"}, "style"),
])
def test_invalid_custom_presets_are_refused(change, why):
    errors: dict = {}
    table = I.presets({"institution": {"presets": {"x": GOOD | change}}}, errors)
    assert "x" not in table and why in errors["x"]


def test_valid_custom_preset_and_no_builtin_override():
    cfg = {"institution": {"presets": {
        "x": GOOD | {"extra_cookie_domains": [".lib.x.edu"]},
        "ntut": {"name": "fake", "proxy_host": "evil.example.org", "style": "hostname",
                 "login_url": "https://evil.example.org/?u={url}", "login_marker": "https://evil.example.org/l"},
    }}}
    errors: dict = {}
    table = I.presets(cfg, errors)
    assert table["x"].experimental and table["x"].cookie_domain_ok(".lib.x.edu")
    assert table["ntut"] is I.BUILTIN_PRESETS["ntut"] and "built-in" in errors["ntut"]
    # only limits for a built-in preset is fine, and silent
    errors = {}
    I.presets({"institution": {"presets": {"ntut": {"limits": {"daily": 5}}}}}, errors)
    assert errors == {}


# --- S4: what counts as a PDF ----------------------------------------------------------------


def test_pdf_check_needs_the_magic_bytes():
    assert I._is_pdf(b"%PDF-1.7\n...")
    assert I._is_pdf(b"\n\n%PDF-1.4")  # junk before the header is allowed
    assert not I._is_pdf(b"\n\n  <html><body>Please log in")
    assert not I._is_pdf(b"")


def test_login_page_served_as_pdf_is_not_saved(env):
    web, tmp = env
    s = store_with()
    stamp = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/stampPDF/getPDF.jsp"
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1"] = R(200, "<html>abstract</html>")
    web.routes[stamp] = R(200, b"\n  <html>log in</html>", {"Content-Type": "application/pdf"})
    with pytest.raises(I.InstitutionError, match="no PDF found"):
        I.fetch_pdf(NTUT, s, "https://ieeexplore.ieee.org/document/1", tmp / "a.pdf")
    assert not (tmp / "a.pdf").exists()


def test_tiny_pdf_is_refused(env):
    web, tmp = env
    web.routes["https://arxiv.org/pdf/1"] = R(200, b"%PDF-1.4 tiny", {"Content-Type": "application/pdf"})
    with pytest.raises(I.InstitutionError, match="not a paper"):
        I.fetch_free_pdf("https://arxiv.org/pdf/1", tmp / "a.pdf")
    assert not (tmp / "a.pdf").exists()


def test_saved_pdf_is_owner_only(env):
    web, tmp = env
    web.routes["https://arxiv.org/pdf/1"] = R(200, PDF, {"Content-Type": "application/pdf"})
    got = I.fetch_free_pdf("https://arxiv.org/pdf/1", tmp / "a.pdf")
    if sys.platform != "win32":
        assert got.path.stat().st_mode & 0o777 == 0o600


# --- C: configurable limits ---------------------------------------------------------------


def test_limits_defaults_overrides_and_ceiling():
    assert I.limits("ntut", {}) == I.Limits(20, 10, 30)
    cfg = {"institution": {"limits": {"daily": 30, "spacing_seconds": 45},
                           "presets": {"ntut": {"limits": {"per_publisher": 5}}}}}
    lim = I.limits("ntut", cfg)
    assert (lim.daily, lim.per_publisher, lim.spacing) == (30, 5, 45) and lim.notes == ()
    lim = I.limits("ntut", {"institution": {"limits": {"daily": 500, "spacing_seconds": 1, "per_publisher": "x"}}})
    assert (lim.daily, lim.spacing, lim.per_publisher) == (I.HARD_DAILY_MAX, I.HARD_MIN_SPACING, 10)
    assert len(lim.notes) == 3


def test_limits_notice_is_shown_once(env):
    seen: list[str] = []
    I.notify_once("institution limits: daily 500 is above the hard ceiling; using 50", seen.append)
    I.notify_once("institution limits: daily 500 is above the hard ceiling; using 50", seen.append)
    assert len(seen) == 1


def test_spacing_from_config_is_used(env):
    web, tmp = env
    s = store_with()
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/stampPDF/getPDF.jsp"] = \
        lambda: R(200, PDF, {"Content-Type": "application/pdf"})
    slept: list[float] = []
    lim = I.Limits(20, 10, 12)
    for n in (1, 2):
        I.fetch_pdf(NTUT, s, f"https://ieeexplore.ieee.org/document/{n}", tmp / f"{n}.pdf", lim=lim,
                    clock=lambda: 1_700_000_000.0, sleep=slept.append)
    assert slept == [12.0]


# --- N2: an already-proxied URL counts for its publisher ------------------------------------


def test_proxied_url_counts_for_the_publisher(env):
    web, tmp = env
    s = store_with()
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/stampPDF/getPDF.jsp"] = \
        R(200, PDF, {"Content-Type": "application/pdf"})
    I.fetch_pdf(NTUT, s, "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/stampPDF/getPDF.jsp?arnumber=1",
                tmp / "p.pdf")
    assert I.Usage.load("ntut").publishers == {"ieee.org": 1}


# --- S1 / S8: browser cookie store fallback ----------------------------------------------------


def test_browser_fallback_passes_warnings_and_explains_session_cookies(env, monkeypatch):
    import http.cookiejar

    import yt_dlp.cookies as ytc

    def fake(browser, profile=None, logger=None, **_):
        logger.warning("cannot decrypt v11 cookies: no key found")
        logger.warning("secretstorage not available")
        return http.cookiejar.CookieJar()

    monkeypatch.setattr(ytc, "extract_cookies_from_browser", fake)
    warned: list[str] = []
    with pytest.raises(I.NotLoggedIn, match="session cookie") as exc:
        I.cookies_from_browser(NTUT, "chrome", warn=warned.append)
    assert "HYSID" in str(exc.value)
    assert warned == ["chrome: cannot decrypt v11 cookies: no key found", "chrome: secretstorage not available"]


def test_state_files_are_written_through_unique_temp_names(env, monkeypatch):
    names: list[str] = []
    real = I.tempfile.mkstemp

    def spy(*a, **k):
        fd, path = real(*a, **k)
        names.append(Path(path).name)
        return fd, path

    monkeypatch.setattr(I.tempfile, "mkstemp", spy)
    store_with().save()
    store_with().save()
    assert len(set(names)) == len(names) >= 2
