"""`fcrawl x` through the family gateway (fcrawl-gateway POST /gw/x/gql).

The gateway holds the X cookies and runs twscrape; we send a whitelisted
GraphQL op with variables and get the raw page back, then parse it with the
same twscrape parser the local backend uses. When the gateway had to fall
back to twitterapi.io it answers {"provider": "twitterapi", ...} and we map
that with the existing twitterapi mapper instead.

Every function returns the Tweet / User dataclasses x.py already renders.
"""

from __future__ import annotations

from typing import Any, Optional

import click
import requests
from rich.console import Console
from twscrape import Tweet, User
from twscrape.models import parse_tweets, parse_users

from ..utils.config import gateway_headers, gateway_url, load_config
from ..utils.article_parser import parse_article_from_response
from ..vendors.twitterapi_io import to_tweet, to_user

console = Console()
TIMEOUT = 90  # the gateway may wait up to 20 s for a free account, X itself can be slow


class GatewayXError(RuntimeError):
    def __init__(self, message: str, cause: str | None = None, retry_after: int | None = None):
        self.cause, self.retry_after = cause, retry_after
        super().__init__(message)


def gql(op: str, variables: dict[str, Any], cursor: str | None = None) -> dict:
    config = load_config()
    body = {"op": op, "variables": variables}
    if cursor:
        body["cursor"] = cursor
    try:
        resp = requests.post(f"{gateway_url(config)}/gw/x/gql", json=body,
                             headers=gateway_headers(config), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise GatewayXError(f"gateway unreachable: {exc}") from exc
    if resp.status_code != 200:
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        message = payload.get("error") or f"gateway HTTP {resp.status_code}"
        details = payload.get("details") or {}
        retry = resp.headers.get("Retry-After")
        raise GatewayXError(message, cause=details.get("cause"),
                            retry_after=int(retry) if retry and retry.isdigit() else None)
    return resp.json()


def explain(exc: GatewayXError) -> None:
    console.print(f"[red]{exc}[/red]")
    hints = {
        "rate_limited": "Every X account on the gateway is rate limited for this operation; wait and retry.",
        "busy": "Every X account is in use right now; retry in a few seconds.",
        "cookies_dead": "The gateway's X cookies are no longer valid. Someone needs to log in to X in a browser and add fresh cookies on jason (fcrawl-gw admin x add-cookies).",
        "xclid_broken": "X changed its frontend; the gateway's twscrape needs an update.",
        "gql_outdated": "X changed its GraphQL schema; the gateway's twscrape needs an update.",
        "fallback_exhausted": "twscrape is broken and the twitterapi.io fallback has no credits.",
        "fallback_unavailable": "twscrape is broken and no paid fallback is configured on the gateway.",
    }
    if exc.cause in hints:
        console.print(hints[exc.cause])
    if exc.retry_after:
        console.print(f"Retry after {exc.retry_after}s.")


def _run(fn):
    try:
        return fn()
    except GatewayXError as exc:
        explain(exc)
        raise click.Abort()


# --- paginated helpers ------------------------------------------------------


def _page_tweets(result: dict, limit: int) -> tuple[list[Tweet], str | None]:
    if result.get("provider") == "twitterapi":
        return [to_tweet(t) for t in result.get("tweets") or []][:limit], result.get("nextCursor")
    page = result["page"]
    tweets = list(parse_tweets(page, limit))
    return tweets, _bottom_cursor(page)


def _bottom_cursor(page: dict) -> str | None:
    """Bottom cursor of a timeline page, same walk twscrape's _get_cursor does."""
    from twscrape.utils import find_obj

    cur = find_obj(page, lambda x: isinstance(x, dict) and x.get("cursorType") == "Bottom")
    return cur.get("value") if cur else None


def _collect(op: str, variables: dict[str, Any], limit: int, max_pages: int = 10) -> list[Tweet]:
    out: list[Tweet] = []
    seen: set[int] = set()
    cursor = None
    for _ in range(max_pages):
        result = gql(op, variables, cursor)
        tweets, next_cursor = _page_tweets(result, limit - len(out))
        added = 0
        for t in tweets:
            if t.id in seen:
                continue
            seen.add(t.id)
            out.append(t)
            added += 1
            if len(out) >= limit:
                return out
        if not next_cursor or added == 0:
            break
        cursor = next_cursor
    return out


# --- commands ------------------------------------------------------------------


def fetch_search(query: str, *, limit: int, sort: str) -> list[Tweet]:
    product = {"top": "Top", "latest": "Latest", "photos": "Photos", "videos": "Videos"}.get(sort, "Top")
    return _run(lambda: _collect("SearchTimeline", {"rawQuery": query, "product": product, "count": 20}, limit))


def fetch_user(handle: str) -> User | None:
    def go():
        result = gql("UserByScreenName", {"screen_name": handle})
        if result.get("provider") == "twitterapi":
            return to_user(result["user"]) if result.get("user") else None
        users = list(parse_users(result["page"]))
        return users[0] if len(users) == 1 else None

    return _run(go)


def fetch_user_tweets(handle: str, *, limit: int) -> tuple[User | None, list[Tweet]]:
    user = fetch_user(handle)
    if user is None:
        return None, []
    tweets = _run(lambda: _collect("UserTweets", {"userId": str(user.id), "count": 40}, limit))
    return user, tweets


def fetch_tweet(tweet_id: int) -> tuple[Tweet | None, Any]:
    """(tweet, article) like the local tweet_details_raw path."""

    def go():
        result = gql("TweetDetail", {"focalTweetId": str(tweet_id)})
        if result.get("provider") == "twitterapi":
            tweets = result.get("tweets") or []
            return (to_tweet(tweets[0]) if tweets else None), None
        page = result["page"]
        tweet = next((t for t in parse_tweets(page) if t.id == tweet_id), None)
        article = parse_article_from_response(page) if tweet else None
        return tweet, article

    return _run(go)


def fetch_replies(tweet_id: int, limit: int) -> list[Tweet]:
    def go():
        result = gql("TweetDetail", {"focalTweetId": str(tweet_id), "referrer": "tweet"})
        if result.get("provider") == "twitterapi":
            return []  # the fallback has no reply listing in this shape
        return [t for t in parse_tweets(result["page"]) if t.inReplyToTweetId == tweet_id][:limit]

    return _run(go)


def fetch_thread_and_replies(tweet_id: int, *, with_replies: bool, reply_limit: int) -> tuple[list[Tweet], list[Tweet], Any]:
    """Author's chain + other users' replies, same walk as the local backend.

    Returns (thread, replies, article).
    """
    original, article = fetch_tweet(tweet_id)
    if original is None:
        return [], [], None
    author_id, author_username = original.user.id, original.user.username
    conv_id = getattr(original, "conversationId", original.id)

    thread: dict[int, Tweet] = {original.id: original}
    others: dict[int, Tweet] = {}

    def is_reply_to_others(t: Tweet) -> bool:
        content = t.rawContent.strip()
        if content.startswith("@"):
            first = content[1:].split()[0] if content[1:].split() else ""
            return first.lower() != author_username.lower()
        return False

    def add(t: Tweet) -> None:
        if t.id in thread or t.user.id != author_id or is_reply_to_others(t):
            return
        thread[t.id] = t

    # timeline with replies, same conversation
    for t in _run(lambda: _collect("UserTweetsAndReplies", {"userId": str(author_id), "count": 40}, 50, max_pages=2)):
        if getattr(t, "conversationId", None) == conv_id:
            add(t)

    # walk backwards to fill gaps
    for t in list(thread.values()):
        current = t.inReplyToTweetId
        hops = 0
        while current and current not in thread and hops < 20:
            parent, _ = fetch_tweet(current)
            if not parent or parent.user.id != author_id:
                break
            add(parent)
            current = parent.inReplyToTweetId
            hops += 1

    # walk forwards through replies
    if with_replies or len(thread) <= 1:
        to_check, checked = [original.id], set()
        while to_check:
            current = to_check.pop(0)
            if current in checked:
                continue
            checked.add(current)
            for reply in fetch_replies(current, 50):
                if reply.id in thread or reply.id in others:
                    continue
                if reply.user.id == author_id:
                    add(reply)
                    to_check.append(reply.id)
                elif with_replies and len(others) < reply_limit:
                    others[reply.id] = reply

    thread_list = sorted(thread.values(), key=lambda t: t.id)
    replies_list = sorted(others.values(), key=lambda t: t.likeCount, reverse=True)
    return thread_list, replies_list, article
