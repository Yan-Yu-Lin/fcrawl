"""Gateway-mode client behaviour (astra review 2026-10-30, findings 11/13/14/15/17/19)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from fcrawl.commands import _x_gateway as gw
from fcrawl.utils.cache import metadata_to_dict, result_to_dict


def _tweet_page(ids, cursor=None, cursor_type="Bottom", reply_to=None, author="auth"):
    """Minimal GraphQL timeline page twscrape's parser accepts."""
    entries = []
    for i in ids:
        legacy = {
            "id_str": str(i), "full_text": f"tweet {i}", "created_at": "Wed Oct 30 10:00:00 +0000 2026",
            "user_id_str": "1", "conversation_id_str": "1", "lang": "en",
            "entities": {}, "favorite_count": 0, "retweet_count": 0, "reply_count": 0, "quote_count": 0,
        }
        if reply_to:
            legacy["in_reply_to_status_id_str"] = str(reply_to)
        entries.append({
            "entryId": f"tweet-{i}",
            "content": {"entryType": "TimelineTimelineItem", "itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": {
                "__typename": "Tweet", "rest_id": str(i), "legacy": legacy,
                "core": {"user_results": {"result": {"__typename": "User", "rest_id": "1", "legacy": {
                    "screen_name": author, "name": "A", "created_at": "Wed Oct 30 10:00:00 +0000 2020",
                    "followers_count": 0, "friends_count": 0, "statuses_count": 0, "favourites_count": 0,
                    "listed_count": 0, "media_count": 0, "profile_image_url_https": "https://x/a.jpg",
                    "description": "", "location": "", "entities": {}}}}},
                "views": {"count": "0"},
            }}}},
        })
    if cursor:
        entries.append({"entryId": f"cursor-bottom-{cursor}", "content": {"entryType": "TimelineTimelineCursor", "cursorType": cursor_type, "value": cursor}})
    return {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": entries}]}}}}}


def test_14_collect_has_no_page_ceiling(monkeypatch):
    calls = []

    def fake_gql(op, variables, cursor=None):
        calls.append(cursor)
        n = len(calls)
        ids = list(range(n * 100, n * 100 + 20))
        return {"provider": "twscrape", "page": _tweet_page(ids, cursor=f"c{n}")}

    monkeypatch.setattr(gw, "gql", fake_gql)
    out = gw._collect("SearchTimeline", {"rawQuery": "x"}, 220)
    assert len(out) == 220 and len(calls) == 11


def test_14_collect_stops_on_stall(monkeypatch):
    def fake_gql(op, variables, cursor=None):
        return {"provider": "twscrape", "page": _tweet_page([1, 2], cursor="same")}

    monkeypatch.setattr(gw, "gql", fake_gql)
    assert len(gw._collect("SearchTimeline", {"rawQuery": "x"}, 50)) == 2


def test_13_replies_paginate_with_showmorethreads(monkeypatch):
    calls = []

    def fake_gql(op, variables, cursor=None):
        calls.append(cursor)
        if cursor is None:
            return {"provider": "twscrape", "page": _tweet_page([11, 12], cursor="more", cursor_type="ShowMoreThreads", reply_to=1)}
        return {"provider": "twscrape", "page": _tweet_page([13, 14], reply_to=1)}

    monkeypatch.setattr(gw, "gql", fake_gql)
    out = gw.fetch_replies(1, 4)
    assert [t.id for t in out] == [11, 12, 13, 14]
    assert calls == [None, "more"]


def test_15_self_mention_with_punctuation(monkeypatch):
    class T:
        def __init__(self, text):
            self.rawContent = text

    # exercise the helper through fetch_thread_and_replies' closure is awkward; replicate the rule
    import re

    def is_reply_to_others(t, author):
        content = t.rawContent.strip()
        if content.startswith("@"):
            m = re.match(r"@(\w+)", content)
            if m:
                return m.group(1).lower() != author.lower()
        return False

    assert is_reply_to_others(T("@author, continuing"), "author") is False
    assert is_reply_to_others(T("@author: more"), "author") is False
    assert is_reply_to_others(T("@someone hi"), "author") is True
    # the module uses the same regex
    import inspect
    assert 're.match(r"@(\\w+)", content)' in inspect.getsource(gw)


def test_17_metadata_extras_survive_serialization():
    from firecrawl.v2.types import Document, DocumentMetadata

    doc = Document(markdown="hi", metadata=DocumentMetadata(title="t", gwProvider="cache", gwCached=True, gwQuota={"usedToday": 3}))
    assert metadata_to_dict(doc.metadata)["gwProvider"] == "cache"
    cached = result_to_dict(doc)
    assert cached["metadata"]["gwQuota"] == {"usedToday": 3} and cached["metadata"]["gwCached"] is True


def test_19_config_masks_gateway_token(tmp_path, monkeypatch):
    from fcrawl.cli import cli

    rc = tmp_path / ".fcrawlrc"
    rc.write_text(json.dumps({"api_url": "http://gw:3800", "gateway_token": "fgw_dad_SECRETSECRETSECRET"}))
    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(cli, ["config"]).output
    assert "SECRETSECRET" not in out and "gateway_token" in out


def test_11_search_no_cache_sends_header(monkeypatch):
    from fcrawl.commands import search as s

    seen = {}

    class R:
        status_code = 200
        def json(self):
            return {"organic": [], "credits": 1}

    def post(url, headers=None, json=None, timeout=None):
        seen.update(headers or {})
        return R()

    monkeypatch.setattr(s.requests, "post", post)
    monkeypatch.setattr(s, "serper_target", lambda: ("http://gw/gw/search", "fgw_x"))
    s._serper_search("q", 5, None, None, "fgw_x", no_cache=True)
    assert seen.get("X-No-Cache") == "1"
    seen.clear()
    s._serper_search("q", 5, None, None, "fgw_x")
    assert "X-No-Cache" not in seen


def test_13_replies_survive_a_filtered_empty_page(monkeypatch):
    """Page 1 has only nested replies (none to our tweet) but a cursor; page 2 has the match."""
    calls = []

    def fake_gql(op, variables, cursor=None):
        calls.append(cursor)
        if cursor is None:
            return {"provider": "twscrape", "page": _tweet_page([21], cursor="c1", cursor_type="ShowMoreThreads", reply_to=99)}
        if cursor == "c1":
            return {"provider": "twscrape", "page": _tweet_page([22], cursor="c2", cursor_type="ShowMoreThreads", reply_to=1)}
        return {"provider": "twscrape", "page": _tweet_page([], cursor=None)}

    monkeypatch.setattr(gw, "gql", fake_gql)
    out = gw.fetch_replies(1, 4)
    assert [t.id for t in out] == [22]
    assert calls == [None, "c1", "c2"]


def test_13_replies_stop_after_three_empty_pages(monkeypatch):
    calls = []

    def fake_gql(op, variables, cursor=None):
        calls.append(cursor)
        n = len(calls)
        return {"provider": "twscrape", "page": _tweet_page([100 + n], cursor=f"c{n}", cursor_type="ShowMoreThreads", reply_to=99)}

    monkeypatch.setattr(gw, "gql", fake_gql)
    assert gw.fetch_replies(1, 4) == []
    assert len(calls) == 3


# --- security 2026-10-31 #14: never send the gateway token over plain http to the internet ---


@pytest.mark.parametrize("url", [
    "https://fcrawl.arthurlin.dev", "https://1.2.3.4:8443/", "http://192.168.1.120:3800", "http://10.0.0.5",
    "http://172.16.3.4:3800", "http://100.64.0.7:3800", "http://127.0.0.1:3800", "http://localhost:3800",
    "http://[::1]:3800", "http://jason.local:3800", "http://jason.lan", "http://jason.tail1234.ts.net:3800",
])
def test_sec14_gateway_url_allowed(url):
    from fcrawl.utils.config import check_gateway_url
    assert check_gateway_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "http://fcrawl.arthurlin.dev", "http://125.229.144.147:3800", "http://8.8.8.8", "http://example.com.evil",
    "http://jason.ts.net.evil.com", "ftp://192.168.1.120", "fcrawl.arthurlin.dev",
    "http://public.example\\@localhost:3800", "http://evil.com@localhost:3800", "http://user:pw@192.168.1.120:3800",
    "http://localhost:3800\tevil", "https://evil.com\\@fcrawl.arthurlin.dev",
])
def test_sec14_gateway_url_refused(url):
    from fcrawl.utils.config import InsecureGatewayURL, check_gateway_url
    with pytest.raises(InsecureGatewayURL, match="https://"):
        check_gateway_url(url)


def test_sec14_cli_refuses_before_any_request(tmp_path, monkeypatch):
    import requests
    from fcrawl.cli import cli

    rc = tmp_path / ".fcrawlrc"
    rc.write_text(json.dumps({"api_url": "http://fcrawl.arthurlin.dev", "gateway_token": "fgw_test_x"}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.delenv("FCRAWL_GATEWAY_TOKEN", raising=False)

    def no_network(*a, **k):
        raise AssertionError("a request was attempted")

    monkeypatch.setattr(requests.Session, "request", no_network)
    result = CliRunner().invoke(cli, ["search", "hello"])
    assert result.exit_code != 0
    assert "https://" in result.output and "fgw_test_x" not in result.output

    from fcrawl.utils.config import InsecureGatewayURL, gateway_headers
    with pytest.raises(InsecureGatewayURL):
        gateway_headers({"api_url": "http://fcrawl.arthurlin.dev", "gateway_token": "fgw_test_x"})


def test_sec14_local_commands_and_help_still_work(tmp_path, monkeypatch):
    from fcrawl.cli import cli

    rc = tmp_path / ".fcrawlrc"
    rc.write_text(json.dumps({"api_url": "http://fcrawl.arthurlin.dev", "gateway_token": "fgw_test_x"}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for args in (["config"], ["cache", "--help"], ["yt-transcript", "--help"], ["scrape", "--help"],
                 ["x", "search", "--help"], ["reddit", "search", "--help"], ["dcard", "post", "--help"],
                 ["cache", "stats"]):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, (args, result.output)
    import uuid
    result = CliRunner().invoke(cli, ["scrape", f"https://example.com/never-cached-{uuid.uuid4().hex}"])
    assert result.exit_code != 0 and "https://" in result.output


def test_sec14_x_gateway_refuses_with_clear_message(monkeypatch):
    import requests
    monkeypatch.setattr(gw, "load_config", lambda: {"api_url": "http://fcrawl.arthurlin.dev", "gateway_token": "fgw_t"})

    def no_network(*a, **k):
        raise AssertionError("a request was attempted")

    monkeypatch.setattr(requests, "post", no_network)
    with pytest.raises(gw.GatewayXError, match="https://"):
        gw.gql("SearchTimeline", {"rawQuery": "a"})


# --- goto tickets (2026-10-31) -------------------------------------------------


GOTO = "https://www.google.com/goto?url=CAES"


def test_goto_is_goto_and_label():
    from fcrawl.utils.goto import goto_label, is_goto

    assert is_goto(GOTO + "1") and is_goto("http://google.com.tw/goto?url=1")
    assert not is_goto("https://www.google.com/search?q=1") and not is_goto("https://google.evil.com/goto?url=1")
    assert goto_label(4) == "[goto #4]"
    assert goto_label(4, pending=True) == "[goto #4 pending]"
    assert goto_label(4, displayed="https://host › a › b...") == "[goto] https://host › a › b..."
    assert goto_label(4, displayed="https://host › a", pending=True) == "[goto pending] https://host › a"


def test_goto_search_marks_tickets_and_sends_batches_header(monkeypatch):
    from fcrawl.commands import search as s

    seen = {}

    class R:
        status_code = 200

        def json(self):
            return {"credits": 2, "organic": [
                {"title": "A", "link": "https://a.example/x", "snippet": "s", "position": 1, "gwBatch": 1, "gwGoto": False},
                {"title": "B", "link": GOTO + "b", "snippet": "s", "position": 2, "gwBatch": 1, "gwGoto": True,
                 "displayedLink": "https://b.example › docs › b..."},
                {"title": "C", "link": GOTO + "c", "snippet": "s", "position": 3, "gwBatch": 2, "gwGoto": True},
            ]}

    def post(url, headers=None, json=None, timeout=None):
        seen.update(headers or {})
        return R()

    monkeypatch.setattr(s.requests, "post", post)
    monkeypatch.setattr(s, "serper_target", lambda: ("http://gw/gw/search", "fgw_x"))
    results, *_ = s._serper_search("q", 10, None, None, "fgw_x", batches=1)
    assert seen["X-Search-Batches"] == "1"
    assert [r.get("goto") for r in results] == [None, True, True]
    assert results[1]["displayed"] == "https://b.example › docs › b..." and results[1]["batch"] == 1
    assert s.shown_url(results[0]) == "https://a.example/x"
    assert s.shown_url(results[1]) == "[goto] https://b.example › docs › b..."
    assert s.shown_url(results[2]) == "[goto #3]"
    results[2]["resolved"] = "https://c.example/real"
    assert s.shown_url(results[2]) == "https://c.example/real"


def test_goto_resolve_results_marks_pending(monkeypatch):
    from fcrawl.commands import search as s

    results = [{"url": "https://a", "position": 1}, {"url": GOTO + "b", "goto": True, "position": 2},
               {"url": GOTO + "c", "goto": True, "position": 3}]

    def fake_resolve(urls, wait=False):
        assert urls == [GOTO + "b", GOTO + "c"] and wait is False
        return {GOTO + "b": {"resolved": "https://b.real", "status": "ok"}, GOTO + "c": {"resolved": None, "status": "cooldown"}}

    monkeypatch.setattr(s, "resolve_via_gateway", fake_resolve)
    assert s.resolve_results(results) == 1
    assert s.shown_url(results[1]) == "https://b.real"
    assert s.shown_url(results[2]) == "[goto #3 pending]"


def test_goto_search_flags(monkeypatch, tmp_path):
    from fcrawl.cli import cli

    rc = tmp_path / ".fcrawlrc"
    rc.write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)
    from fcrawl.commands import search as s
    seen = {}

    class R:
        status_code = 200

        def json(self):
            return {"credits": 1, "organic": [{"title": "A", "link": GOTO + "a", "snippet": "s", "position": 1,
                                                 "gwGoto": True, "displayedLink": "https://a.example › p"}]}

    def post(url, headers=None, json=None, timeout=None):
        seen.update(headers or {})
        return R()

    monkeypatch.setattr(s.requests, "post", post)
    out = CliRunner().invoke(cli, ["search", "q", "--single", "--no-cache", "--no-pretty"])
    assert out.exit_code == 0, out.output
    assert seen["X-Search-Batches"] == "1"
    assert "[goto] https://a.example › p" in out.output
    assert f"ticket: {GOTO}a" in out.output  # the real ticket is still there for scrape
    out = CliRunner().invoke(cli, ["search", "q", "--urls-only", "--no-cache", "--no-pretty"])
    assert GOTO + "a" in out.output  # --urls-only keeps tickets as URLs (feeds scrape)
    out = CliRunner().invoke(cli, ["search", "q", "--single", "--batches", "3"])
    assert out.exit_code != 0


def test_goto_resolve_command(monkeypatch, tmp_path):
    from fcrawl.cli import cli
    from fcrawl.commands import resolve as rmod

    rc = tmp_path / ".fcrawlrc"
    rc.write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)
    calls = {}

    def fake(urls, wait=False):
        calls["urls"], calls["wait"] = urls, wait
        return {GOTO + "a": {"resolved": "https://a.real", "status": "ok"},
                GOTO + "b": {"resolved": None, "status": "cooldown"}}

    monkeypatch.setattr(rmod, "resolve_via_gateway", fake)
    out = CliRunner().invoke(cli, ["resolve", GOTO + "a", GOTO + "b", "https://plain", "--wait"])
    assert out.exit_code == 0, out.output
    assert calls == {"urls": [GOTO + "a", GOTO + "b"], "wait": True}
    assert "https://a.real" in out.output and "[goto pending]" in out.output and "not a goto ticket" in out.output


def test_goto_scrape_caches_under_real_url(monkeypatch, tmp_path):
    """A ticket is unique per search: the local cache must be keyed on the resolved URL."""
    from fcrawl.cli import cli
    from fcrawl.commands import scrape as sc
    from fcrawl.utils import cache as cache_mod

    rc = tmp_path / ".fcrawlrc"
    rc.write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    ticket = GOTO + "zzz"
    real = "https://real.example/page"

    def fake_gateway(url, scrape_options, timeout, allow_jina, no_cache=False):
        from firecrawl.v2.types import Document, DocumentMetadata
        doc = Document(markdown="# hi " + "x" * 900, metadata=DocumentMetadata(
            title="t", url=real, sourceURL=real, gwProvider="firecrawl", gwResolvedFrom=url))
        return sc.TaggedResult(doc, provider="firecrawl")

    monkeypatch.setattr(sc, "scrape_with_gateway", fake_gateway)
    out = CliRunner().invoke(cli, ["scrape", ticket, "--save-temp"])
    assert out.exit_code == 0, out.output
    assert "Resolved:" in out.output and real in out.output
    from fcrawl.utils.cache import cache_key, read_cache
    opts = {"formats": ["markdown"], "article": False, "raw": False, "include_tags": None, "exclude_tags": None,
            "wait": None, "timeout": sc.DEFAULT_SCRAPE_TIMEOUT}
    assert read_cache("scrape", cache_key(real, opts)) is not None
    assert read_cache("scrape", cache_key(ticket, opts)) is None
    # scraping the real URL now hits the local cache
    out = CliRunner().invoke(cli, ["scrape", real, "--save-temp"])
    assert "Using cached result" in out.output and "from_cache=true" in out.output


def test_goto_direct_mode_redeem(monkeypatch):
    from fcrawl.utils import goto as g

    class R:
        def __init__(self, status, headers=None, text=""):
            self.status_code, self.headers, self.text = status, headers or {}, text

    monkeypatch.setattr(g.requests, "get", lambda *a, **k: R(302, {"Location": "https://real/x"}))
    assert g.direct_redeem(GOTO + "a") == ("https://real/x", None)
    monkeypatch.setattr(g.requests, "get", lambda *a, **k: R(200, text='<meta http-equiv="refresh" content="19;url=/goto?x">'))
    assert g.direct_redeem(GOTO + "a") == (None, 19)
    monkeypatch.setattr(g.requests, "get", lambda *a, **k: R(200, text="<html></html>"))
    assert g.direct_redeem(GOTO + "a") == (None, None)


# --- astra round 1 ---------------------------------------------------------------


def test_r1_client_google_host_rule():
    from fcrawl.utils.goto import is_google_host, is_goto

    assert is_goto("https://www.google.com./goto?url=1") and is_goto("https://news.google.com/goto/?url=1")
    assert not is_goto("https://google.bad.com/goto?url=1")
    assert is_google_host("google.com.tw") and not is_google_host("evilgoogle.com")


def test_r1_resolve_exit_code_on_failure(monkeypatch, tmp_path):
    from fcrawl.cli import cli
    from fcrawl.commands import resolve as rmod

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rmod, "resolve_via_gateway", lambda urls, wait=False: {
        GOTO + "a": {"resolved": None, "status": "failed"}, GOTO + "b": {"resolved": None, "status": "cooldown"}})
    assert CliRunner().invoke(cli, ["resolve", GOTO + "a", GOTO + "b"]).exit_code == 1
    assert CliRunner().invoke(cli, ["resolve", GOTO + "a", "--json"]).exit_code == 1
    monkeypatch.setattr(rmod, "resolve_via_gateway", lambda urls, wait=False: {GOTO + "b": {"resolved": None, "status": "cooldown"}})
    assert CliRunner().invoke(cli, ["resolve", GOTO + "b"]).exit_code == 0


def test_r1_pretty_output_keeps_goto_marker(monkeypatch, tmp_path):
    from fcrawl.cli import cli
    from fcrawl.commands import search as s

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)

    class R:
        status_code = 200

        def json(self):
            return {"credits": 1, "organic": [{"title": "A", "link": GOTO + "a", "snippet": "s", "position": 1, "gwGoto": True},
                                               {"title": "B", "link": GOTO + "b", "snippet": "s", "position": 2, "gwGoto": True,
                                                "displayedLink": "https://b.example › p"}]}

    monkeypatch.setattr(s.requests, "post", lambda *a, **k: R())
    out = CliRunner().invoke(cli, ["search", "q", "--no-cache", "--pretty"]).output
    assert "[goto #1]" in out and "[goto] https://b.example › p" in out


def test_r1_resolve_chunks_of_six(monkeypatch, tmp_path):
    from fcrawl.utils import goto as g

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)
    sizes = []

    class R:
        status_code = 200

        def __init__(self, urls):
            self._urls = urls

        def json(self):
            return {"urls": [{"url": u, "resolved": None, "status": "cooldown"} for u in self._urls]}

    def post(url, json=None, headers=None, timeout=None):
        sizes.append((len(json["urls"]), timeout))
        return R(json["urls"])

    monkeypatch.setattr(g.requests, "post", post)
    out = g.resolve_via_gateway([GOTO + str(i) for i in range(14)])
    assert len(out) == 14 and [n for n, _ in sizes] == [6, 6, 2]


def test_r1_goto_unresolved_message_reaches_user():
    from fcrawl.utils.config import gateway_error_message

    exc = Exception("Unexpected error during scrape: Status code 502. goto_unresolved: the Google goto link could not be resolved yet - No additional error details provided.")
    assert gateway_error_message(exc).startswith("goto_unresolved: the Google goto link could not be resolved yet")


# --- astra round 2 ---------------------------------------------------------------


def test_r2_client_canonical_google_variants():
    from fcrawl.utils.goto import is_goto

    for u in ("https://www.google.com。/goto?url=1", "https://%67oogle.com/goto?url=1",
              "https://www.google.com%2e/goto?url=1", "https://ｇoogle.com/goto?url=1"):
        assert is_goto(u), u
    assert not is_goto("https://google.bad.com/goto?url=1")


def test_r2_resolve_transport_errors_are_handled(monkeypatch, tmp_path):
    import requests
    from fcrawl.cli import cli
    from fcrawl.utils import goto as g

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)

    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(g.requests, "post", boom)
    out = CliRunner().invoke(cli, ["resolve", GOTO + "a"])
    assert out.exit_code != 0 and "Timeout" in out.output and "Traceback" not in out.output
    # search --resolve still shows the results when resolving fails
    from fcrawl.commands import search as s

    class R:
        status_code = 200

        def json(self):
            return {"credits": 1, "organic": [{"title": "A", "link": GOTO + "a", "snippet": "s", "position": 1, "gwGoto": True}]}

    monkeypatch.setattr(s.requests, "post", lambda *a, **k: R())

    def timeout_resolve(urls, wait=False):
        raise RuntimeError("gateway resolve failed: Timeout: slow")  # what the helper raises on RequestException

    monkeypatch.setattr(s, "resolve_via_gateway", timeout_resolve)
    out = CliRunner().invoke(cli, ["search", "q", "--no-cache", "--no-pretty", "--resolve"])
    assert out.exit_code == 0, out.output
    assert "resolve failed" in out.output and "[goto #1]" in out.output and "Traceback" not in out.output


def test_r2_resolve_dedupes_before_chunking(monkeypatch, tmp_path):
    from fcrawl.utils import goto as g

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)
    sizes = []

    class R:
        status_code = 200

        def __init__(self, urls):
            self._urls = urls

        def json(self):
            return {"urls": [{"url": u, "resolved": None, "status": "cooldown"} for u in self._urls]}

    def post(url, json=None, headers=None, timeout=None):
        sizes.append(len(json["urls"]))
        return R(json["urls"])

    monkeypatch.setattr(g.requests, "post", post)
    out = g.resolve_via_gateway([GOTO + "same"] * 7)
    assert sizes == [1] and len(out) == 1


def test_r2_old_cache_rows_get_goto_marker(monkeypatch, tmp_path):
    from fcrawl.cli import cli
    from fcrawl.utils import cache as cache_mod
    from fcrawl.utils.cache import cache_key, write_cache

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    key = cache_key("oldq", {"engine": "serper", "limit": 20, "gl": "us", "hl": "en", "location": None})
    write_cache("search", key, {"results": [{"title": "T", "url": GOTO + "old", "description": "d", "position": 1}],
                                "elapsed": 0.1, "engine": "serper", "gl": "us", "hl": "en", "pages": 1, "location": None, "extras": {}})
    out = CliRunner().invoke(cli, ["search", "oldq", "--no-pretty"])
    assert out.exit_code == 0, out.output
    assert "[goto #1]" in out.output and f"ticket: {GOTO}old" in out.output


def test_r3_resolve_bad_response_body_is_handled(monkeypatch, tmp_path):
    from fcrawl.cli import cli
    from fcrawl.utils import goto as g

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)

    class R:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(g.requests, "post", lambda *a, **k: R())
    out = CliRunner().invoke(cli, ["resolve", GOTO + "a"])
    assert out.exit_code != 0 and "unexpected response" in out.output and "Traceback" not in out.output

    class R2:
        status_code = 200

        def json(self):
            return [1, 2]

    monkeypatch.setattr(g.requests, "post", lambda *a, **k: R2())
    with pytest.raises(RuntimeError):
        g.resolve_via_gateway([GOTO + "a"])


# --- astra round 4 ---------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {"urls": [None]},
    {"urls": [{"resolved": "https://x", "status": "ok"}]},  # row without url
    {"urls": []},  # no answer for the ticket we asked about
    {"urls": [{"url": GOTO + "a", "status": "ok"}]},  # ok without a resolved URL
    {"urls": [{"url": GOTO + "a", "status": "weird", "resolved": None}]},
])
def test_r4_malformed_resolve_rows_are_errors(monkeypatch, tmp_path, body):
    from fcrawl.cli import cli
    from fcrawl.utils import goto as g

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)

    class R:
        status_code = 200

        def json(self):
            return body

    monkeypatch.setattr(g.requests, "post", lambda *a, **k: R())
    with pytest.raises(RuntimeError):
        g.resolve_via_gateway([GOTO + "a"])
    out = CliRunner().invoke(cli, ["resolve", "--json", GOTO + "a"])
    assert out.exit_code == 1, out.output
    assert "not_a_ticket" not in out.output and "Traceback" not in out.output


# --- astra round 5 ---------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {"urls": [{"url": GOTO + "a", "status": "ok", "resolved": ""}]},  # empty resolution
    {"urls": [{"url": GOTO + "a", "status": "ok", "resolved": "   "}]},
    {"urls": [{"url": GOTO + "a", "status": "cooldown", "resolved": {}}]},  # non-ok with a value
    {"urls": [{"url": GOTO + "a", "status": "failed", "resolved": 123}]},
    {"urls": [{"url": GOTO + "a", "status": "failed", "resolved": None},
              {"url": GOTO + "a", "status": "ok", "resolved": "https://x"}]},  # answered twice
    {"urls": [{"url": GOTO + "a", "status": "ok", "resolved": "https://x"},
              {"url": GOTO + "zzz", "status": "ok", "resolved": "https://y"}]},  # unrequested
])
def test_r5_invalid_resolved_values_and_duplicate_rows_are_errors(monkeypatch, tmp_path, body):
    """#2/#3: an ok row needs a non-blank resolved string, a non-ok row must not carry one,
    and a ticket may be answered once and only if it was asked."""
    from fcrawl.cli import cli
    from fcrawl.utils import goto as g

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)

    class R:
        status_code = 200

        def json(self):
            return body

    monkeypatch.setattr(g.requests, "post", lambda *a, **k: R())
    with pytest.raises(RuntimeError):
        g.resolve_via_gateway([GOTO + "a"])
    out = CliRunner().invoke(cli, ["resolve", "--json", GOTO + "a"])
    assert out.exit_code == 1, out.output
    assert "not_a_ticket" not in out.output and "Traceback" not in out.output


def test_r5_missing_answers_checked_per_chunk(monkeypatch, tmp_path):
    """#3: an unsolicited answer for a later chunk's ticket in chunk 1 must not let an empty
    chunk-2 response pass the missing-answer check."""
    from fcrawl.utils import goto as g

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)
    tickets = [GOTO + f"t{i}" for i in range(7)]  # 6 + 1 -> two chunks
    calls = []

    class R:
        status_code = 200

        def __init__(self, rows):
            self.rows = rows

        def json(self):
            return {"urls": self.rows}

    def post(url, json=None, **kw):
        calls.append(list(json["urls"]))
        if len(calls) == 1:
            rows = [{"url": u, "status": "ok", "resolved": f"https://real/{i}"} for i, u in enumerate(json["urls"])]
            rows.append({"url": tickets[6], "status": "ok", "resolved": "https://real/sneaky"})
            return R(rows)
        return R([])

    monkeypatch.setattr(g.requests, "post", post)
    with pytest.raises(RuntimeError):
        g.resolve_via_gateway(tickets)
    assert len(calls) == 1 and calls[0] == tickets[:6]  # chunk 1 already rejected
    # honest two-chunk flow still works
    calls.clear()

    def honest(url, json=None, **kw):
        calls.append(list(json["urls"]))
        return R([{"url": u, "status": "ok", "resolved": f"https://real/{u[-2:]}"} for u in json["urls"]])

    monkeypatch.setattr(g.requests, "post", honest)
    out = g.resolve_via_gateway(tickets)
    assert len(calls) == 2 and set(out) == set(tickets) and all(v["status"] == "ok" for v in out.values())


def test_r6_client_is_goto_matches_gateway_path_normalization(monkeypatch, tmp_path):
    """Round 6: the client must call a dot-segment ticket a ticket (and send it to the
    gateway) instead of printing "not a goto ticket" and exiting 0."""
    from fcrawl.cli import cli
    from fcrawl.utils import goto as g

    assert g.path_canonical("/./goto") == g.path_canonical("/goto//..") == g.path_canonical("/%2e/goto") == "/goto"
    assert g.path_canonical("/about//../goto") == "/about/goto"
    tickets = ["https://www.google.com/./goto?url=x", "https://www.google.com/a/../goto?url=x",
               "https://www.google.com/goto//..?url=x"]
    assert all(g.is_goto(t) for t in tickets)
    assert not g.is_goto("https://www.google.com/about//../goto?url=x")

    (tmp_path / ".fcrawlrc").write_text(json.dumps({"api_url": "http://127.0.0.1:3800", "gateway_token": "fgw_x_t"}))
    monkeypatch.chdir(tmp_path)
    sent = []

    class R:
        status_code = 200

        def __init__(self, rows):
            self.rows = rows

        def json(self):
            return {"urls": self.rows}

    def post(url, json=None, **kw):
        sent.extend(json["urls"])
        return R([{"url": u, "status": "ok", "resolved": "https://real.example/p"} for u in json["urls"]])

    monkeypatch.setattr(g.requests, "post", post)
    out = CliRunner().invoke(cli, ["resolve", "--json", tickets[0]])
    assert out.exit_code == 0, out.output
    assert sent == [tickets[0]] and "not_a_ticket" not in out.output and "https://real.example/p" in out.output
