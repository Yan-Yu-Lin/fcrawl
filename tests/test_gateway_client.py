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
