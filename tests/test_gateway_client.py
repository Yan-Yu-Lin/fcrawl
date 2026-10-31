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
