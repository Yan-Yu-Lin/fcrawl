"""Fixture-based unit tests for the twitterapi.io -> twscrape dataclass mapper.

These tests run without an API key — they load hand-crafted fixture JSON
shaped according to twitterapi.io's OpenAPI schema and verify that the
mapper produces correct Tweet and User dataclass instances.

Run with: uv run pytest tests/test_twitterapi_mapper.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fcrawl.commands.x import display_tweet, display_user, tweet_to_dict, user_to_dict
from fcrawl.vendors.twitterapi_io import to_tweet, to_user
from fcrawl.vendors.twscrape import Tweet, User

FIXTURES = Path(__file__).parent / "fixtures" / "twitterapi_io"


def _load(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


# ---- tweet mapper -----------------------------------------------------------

def test_tweet_simple_maps_core_fields():
    raw = _load("tweet_simple.json")
    t = to_tweet(raw)

    assert isinstance(t, Tweet)
    assert t.id == 1868244766405870076
    assert t.id_str == "1868244766405870076"
    assert t.url == "https://x.com/elonmusk/status/1868244766405870076"
    assert t.rawContent == "Grok 3 is going to be quite a thing."
    assert t.lang == "en"
    assert t.source == "Twitter for iPhone"
    assert t.retweetCount == 12500
    assert t.replyCount == 3400
    assert t.likeCount == 85000
    assert t.quoteCount == 420
    assert t.viewCount == 4200000
    assert t.bookmarkedCount == 1100
    assert t.conversationId == 1868244766405870076
    assert t.quotedTweet is None
    assert t.retweetedTweet is None
    assert t.inReplyToTweetId is None
    assert t.inReplyToUser is None


def test_tweet_simple_maps_author():
    raw = _load("tweet_simple.json")
    t = to_tweet(raw)

    assert isinstance(t.user, User)
    assert t.user.username == "elonmusk"
    assert t.user.displayname == "Elon Musk"
    assert t.user.id == 44196397
    assert t.user.followersCount == 210000000
    assert t.user.friendsCount == 850
    assert t.user.statusesCount == 50000
    assert t.user.mediaCount == 8500
    assert t.user.blue is True
    assert t.user.url == "https://x.com/elonmusk"


def test_tweet_with_quote_inlines_polymarket():
    raw = _load("tweet_with_quote.json")
    t = to_tweet(raw)

    # Outer tweet (CG)
    assert t.user.username == "cgtwts"
    assert t.likeCount == 74533
    assert t.viewCount == 11294284
    assert t.quoteCount == 183

    # Inner quoted tweet (Polymarket)
    assert t.quotedTweet is not None
    assert isinstance(t.quotedTweet, Tweet)
    assert t.quotedTweet.user.username == "Polymarket"
    assert t.quotedTweet.user.followersCount == 1400000
    assert t.quotedTweet.id_str == "2047358019621536252"
    assert "voluntary retirement" in t.quotedTweet.rawContent
    assert t.quotedTweet.viewCount == 12800000


def test_tweet_reply_exposes_inreplyto():
    raw = _load("tweet_reply.json")
    t = to_tweet(raw)

    assert t.inReplyToTweetId == 2047359035268345995
    assert t.inReplyToTweetIdStr == "2047359035268345995"
    assert t.inReplyToUser is not None
    assert t.inReplyToUser.username == "cgtwts"
    assert t.inReplyToUser.id == 1277585390254186496

    # user_mentions from entities
    assert len(t.mentionedUsers) == 1
    assert t.mentionedUsers[0].username == "cgtwts"


def test_tweet_with_missing_optionals_is_resilient():
    """Mapper must not crash on a minimal payload."""
    minimal = {
        "id": "1",
        "text": "hi",
        "createdAt": "Tue Dec 10 07:00:30 +0000 2024",
        "author": {
            "userName": "bob",
            "id": "999",
            "name": "Bob",
        },
    }
    t = to_tweet(minimal)
    assert t.id == 1
    assert t.rawContent == "hi"
    assert t.user.username == "bob"
    assert t.likeCount == 0
    assert t.quotedTweet is None
    assert t.mentionedUsers == []
    assert t.links == []


# ---- user mapper ------------------------------------------------------------

def test_user_profile_maps_core_fields():
    raw = _load("user_profile.json")
    u = to_user(raw)

    assert isinstance(u, User)
    assert u.username == "anthropicai"
    assert u.displayname == "Anthropic"
    assert u.id == 1234567890
    assert u.followersCount == 500000
    assert u.friendsCount == 150
    assert u.mediaCount == 420
    assert u.statusesCount == 1200
    assert u.location == "San Francisco"
    assert u.blue is True
    assert u.blueType == "business"
    assert u.pinnedIds == [1868244766405870076]


def test_user_empty_payload_is_resilient():
    u = to_user({})
    assert isinstance(u, User)
    assert u.username == ""
    assert u.id == 0
    assert u.followersCount == 0


# ---- display / serialization compatibility ---------------------------------

def test_display_tweet_accepts_mapped_tweet_with_quote(capsys):
    """End-to-end: mapper output must render through display_tweet unchanged."""
    raw = _load("tweet_with_quote.json")
    t = to_tweet(raw)
    display_tweet(t)  # would raise on any field shape mismatch
    captured = capsys.readouterr()
    assert "cgtwts" in captured.out
    assert "Polymarket" in captured.out
    # The quote-display patch should show the nested Polymarket content.
    assert "voluntary retirement" in captured.out


def test_tweet_to_dict_serializes_quote_recursively():
    raw = _load("tweet_with_quote.json")
    t = to_tweet(raw)
    data = tweet_to_dict(t)

    assert data["user"]["username"] == "cgtwts"
    assert "quotedTweet" in data
    assert data["quotedTweet"]["user"]["username"] == "Polymarket"
    # Full JSON serialization must not raise.
    json.dumps(data, default=str)


def test_user_to_dict_serializes(capsys):
    raw = _load("user_profile.json")
    u = to_user(raw)
    display_user(u)  # just ensure it doesn't crash
    captured = capsys.readouterr()
    assert "anthropicai" in captured.out

    data = user_to_dict(u)
    assert data["username"] == "anthropicai"
    json.dumps(data, default=str)


# ---- recursive quote safety -------------------------------------------------

def test_nested_quote_tree_maps_recursively():
    """A quote tweet that quotes another quote tweet should map end-to-end."""
    raw = {
        "id": "100", "text": "outer",
        "createdAt": "Tue Dec 10 07:00:30 +0000 2024",
        "author": {"userName": "a", "id": "1", "name": "A"},
        "quoted_tweet": {
            "id": "200", "text": "middle",
            "createdAt": "Tue Dec 10 07:00:30 +0000 2024",
            "author": {"userName": "b", "id": "2", "name": "B"},
            "quoted_tweet": {
                "id": "300", "text": "inner",
                "createdAt": "Tue Dec 10 07:00:30 +0000 2024",
                "author": {"userName": "c", "id": "3", "name": "C"},
            },
        },
    }
    t = to_tweet(raw)
    assert t.rawContent == "outer"
    assert t.quotedTweet.rawContent == "middle"
    assert t.quotedTweet.quotedTweet.rawContent == "inner"
    assert t.quotedTweet.quotedTweet.user.username == "c"
