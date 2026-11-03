"""Institution proxy, review round 2: the session cookie decides whether a request is
worth a quota slot, reservations across midnight, a broken .fcrawlrc preset, URLs whose
host the HTTP library and urllib read differently. Offline, fake values only."""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest
import requests

from fcrawl.utils import institution as I
from test_institution import NTUT, R, enable, run, store_with
from test_institution import env as _env_fixture

env = _env_fixture

DOI = "10.1109/tpel.2012.2223718"


# --- the session cookie, not any cookie ------------------------------------------------------


def test_expired_session_cookie_with_a_live_other_cookie_is_no_session(env):
    web, tmp = env
    s = I.CookieStore("ntut", [I.Cookie("HYSID", "old", ".search.lib.ntut.edu.tw", expires=int(time.time()) - 5),
                               I.Cookie("JSESSIONID", "live", "search.lib.ntut.edu.tw")])
    s.save()
    assert not s.live(NTUT)
    with pytest.raises(I.NotLoggedIn, match="HYSID"):
        I.fetch_pdf(NTUT, s, "https://ieeexplore.ieee.org/document/1", tmp / "a.pdf")
    assert web.calls == [] and not I.Usage.path_for("ntut").exists()
    assert I.probe(NTUT, s)["ok"] is False and web.calls == []


def test_session_cookie_on_a_foreign_domain_or_empty_does_not_count():
    assert not I.CookieStore("ntut", [I.Cookie("HYSID", "v", ".evil.example")]).live(NTUT)
    assert not I.CookieStore("ntut", [I.Cookie("HYSID", "", ".search.lib.ntut.edu.tw")]).live(NTUT)
    assert I.CookieStore("ntut", [I.Cookie("HYSID", "v", ".search.lib.ntut.edu.tw")]).live(NTUT)


def test_papers_pdf_fallback_needs_the_session_cookie(env, monkeypatch):
    web, _ = env
    from fcrawl.commands import papers as P

    monkeypatch.setattr(P, "openalex_work", lambda pid, nc: {"title": "T", "year": 2012, "ids": {"doi": DOI},
                                                              "locations": [], "oaStatus": "closed"})
    monkeypatch.setattr(P, "s2_open_pdf", lambda pid: (None, "no open PDF"))
    enable()
    I.CookieStore("ntut", [I.Cookie("HYSID", "old", ".search.lib.ntut.edu.tw", expires=1),
                           I.Cookie("JSESSIONID", "live", "search.lib.ntut.edu.tw")]).save()
    data = json.loads(run("papers", "pdf", f"doi:{DOI}", "--no-scholar", "--json").stdout)
    assert data["institution"]["tried"] is False and "auth login" in data["institution"]["error"]
    assert web.calls == [] and not I.Usage.path_for("ntut").exists()


def test_pdf_json_unchanged_when_a_free_copy_exists(env, monkeypatch):
    from fcrawl.commands import papers as P

    monkeypatch.setattr(P, "openalex_work", lambda pid, nc: {
        "title": "T", "year": 2012, "ids": {"doi": DOI}, "oaStatus": "gold",
        "locations": [{"url": "https://arxiv.org/pdf/1", "pdf": True, "sourceType": "repository"}]})
    enable()
    store_with()
    data = json.loads(run("papers", "pdf", f"doi:{DOI}", "--no-scholar", "--json").stdout)
    assert data["found"] is True and "institution" not in data


# --- midnight ----------------------------------------------------------------------------------


def _ts(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s).timestamp()


def test_a_queue_across_midnight_respects_the_next_days_limit(env):
    """20 reservations at 23:59:59 (all but the first land after midnight) and 20 more
    just after midnight: the next day never gets more than its 20."""
    lim = I.Limits(daily=20, per_publisher=20, spacing=10)
    booked: list[float] = []
    refused = 0
    for t in [_ts(2026, 11, 3, 23, 59, 59)] * 20 + [_ts(2026, 11, 4, 0, 0, 5)] * 20:
        try:
            booked.append(t + I.Usage.reserve("ntut", "ieee.org", t, lim))
        except I.RateLimited:
            refused += 1
    per_day: dict[str, int] = {}
    for slot in booked:
        day = datetime.fromtimestamp(slot).date().isoformat()
        per_day[day] = per_day.get(day, 0) + 1
    assert per_day == {"2026-11-03": 1, "2026-11-04": 20}
    assert refused == 19
    usage = I.Usage.load("ntut", lim)
    assert usage.day == "2026-11-04" and usage.total == 20


def test_clock_back_does_not_reopen_a_booked_day(env):
    lim = I.Limits(daily=2, per_publisher=2, spacing=10)
    I.Usage.reserve("ntut", "ieee.org", _ts(2026, 11, 4, 9, 0, 0), lim)
    I.Usage.reserve("ntut", "ieee.org", _ts(2026, 11, 4, 9, 0, 0), lim)
    with pytest.raises(I.RateLimited):  # the clock jumped back to yesterday evening
        I.Usage.reserve("ntut", "ieee.org", _ts(2026, 11, 3, 20, 0, 0), lim)


# --- a broken .fcrawlrc preset ---------------------------------------------------------------


@pytest.mark.parametrize("login_url", [
    "https://[?url={url}",  # urlsplit raises ValueError
    "https://ezproxy.lib.x.edu/login?url={url}&u={0}",  # a positional placeholder
    "https://ezproxy.lib.x.edu/login?url={url}&u={other}",  # an unknown placeholder
    "https://ezproxy.lib.x.edu/login?url={url",  # unbalanced brace
    "https://ezproxy.lib.x.edu:99999/login?url={url}",  # bad port
])
def test_a_broken_preset_is_reported_not_fatal(env, login_url):
    _, tmp = env
    rc = tmp / ".fcrawlrc"
    cfg = json.loads(rc.read_text())
    cfg["institution"] = {"enabled": True, "preset": "ntut", "presets": {"bad": {
        "name": "x", "proxy_host": "ezproxy.lib.x.edu", "style": "prefix", "login_url": login_url,
        "login_marker": "https://ezproxy.lib.x.edu/login"}}}
    rc.write_text(json.dumps(cfg))
    errors = I.preset_errors()
    assert "bad" in errors and "bad" not in I.presets()
    res = run("auth", "status", "--no-probe")
    assert res.exit_code == 0, res.output
    assert "preset: ntut" in res.output
    assert "bad: IGNORED" in run("auth", "presets").output


# --- URLs whose host two parsers read differently --------------------------------------------


P = "ieeexplore-ieee-org.search.lib.ntut.edu.tw"


@pytest.mark.parametrize("url", [
    f"https://evil.example\\@{P}/x",
    f"HTTPS://evil.example\\@{P}/x",
    f"https://evil.example:443\\@{P}/x",
    f"https://user@{P}/x",
    f"https://user:pw@{P}/x",
    f"https://evil.example\n.{P}/x",
    f"https://evil.example\t@{P}/x",
    f"https://{P}:99999/x",
    f"https://{P}%2F@evil.example/x",
])
def test_odd_urls_never_get_cookies(url):
    s = I.CookieStore("ntut", [I.Cookie("HYSID", "secret", ".search.lib.ntut.edu.tw")])
    assert not I.plain_url(url)
    assert s.for_url(NTUT, url) == {}


def test_plain_urls_still_get_cookies():
    s = I.CookieStore("ntut", [I.Cookie("HYSID", "secret", ".search.lib.ntut.edu.tw")])
    assert I.plain_url(f"https://{P}/document/1?x=a@b&y=c\\d".replace("\\", "%5C"))
    assert s.for_url(NTUT, f"https://{P}/document/1?a=b@c") == {"HYSID": "secret"}


def test_a_redirect_to_a_backslash_url_carries_no_cookie(env, monkeypatch):
    """The proxy (or anything it forwards to) redirects to a URL that urllib reads as the
    proxy and requests as another host: requests is what really connects, and it must
    see no cookie."""
    s = store_with()
    sent: list[tuple[str, dict]] = []

    def get(url, cookies, timeout, stream=False):
        real_host = requests.Request("GET", url).prepare().url.split("/")[2]
        sent.append((real_host, dict(cookies or {})))
        if url.startswith("https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1"):
            return R(302, headers={"Location": f"https://evil.example\\@{P}/steal"})
        return R(200, b"<html>x</html>")

    with pytest.raises(I.InstitutionError, match="malformed"):
        I.follow(NTUT, s, "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1", 5, get=get)
    assert [h for h, _ in sent] == [P]  # the odd URL is never requested at all


def test_rewrite_refuses_odd_urls():
    with pytest.raises(I.InstitutionError):
        NTUT.rewrite(f"https://evil.example\\@{P}/x")
    with pytest.raises(I.InstitutionError):
        NTUT.rewrite("https://user@ieeexplore.ieee.org/document/1")


def test_browser_reader_error_keeps_the_searched_paths(monkeypatch):
    """yt-dlp lists every directory it searched; the user needs the whole line."""
    import yt_dlp.cookies as ytc

    paths = ", ".join(f"'/home/u/.var/app/org.mozilla.firefox/dir{i}'" for i in range(6))

    def boom(*a, **k):
        raise RuntimeError(f"could not find firefox cookies database in {paths}")

    monkeypatch.setattr(ytc, "extract_cookies_from_browser", boom)
    with pytest.raises(I.InstitutionError) as exc:
        I.cookies_from_browser(NTUT, "firefox")
    assert "dir5'" in str(exc.value)
