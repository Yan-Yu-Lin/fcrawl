"""twitterapi.io backend handlers for the `fcrawl x` commands.

Each public function here mirrors a command in ``x.py`` but calls the
twitterapi.io backend instead of the vendored twscrape backend. All
functions return data in the existing ``Tweet`` / ``User`` dataclass
shape so the display / serialization code in ``x.py`` works unchanged.

The command layer remains in ``x.py`` — this module only provides the
data-fetch-and-map step.
"""

from __future__ import annotations

import asyncio

import click
from rich.console import Console

from ..utils.config import get_twitterapi_io_key
from ..vendors.twitterapi_io import (
    AuthError,
    InsufficientCreditsError,
    NotFoundError,
    RateLimitedError,
    TwitterApiClient,
    TwitterAPIError,
    to_tweet,
    to_user,
)
from ..vendors.twscrape import Tweet, User

console = Console()


# ---- shared helpers --------------------------------------------------------

def require_api_key() -> str:
    """Return the API key or abort the command with clear guidance."""
    key = get_twitterapi_io_key()
    if not key:
        console.print("[red]No twitterapi.io API key configured.[/red]")
        console.print(
            "Set one of the following and retry:\n"
            "  • Environment variable: [bold]TWITTERAPI_IO_KEY[/bold]\n"
            "  • Config file (~/.fcrawlrc): [bold]{\"twitterapi_io_key\": \"...\"}[/bold]\n\n"
            "Get a key at: [blue]https://twitterapi.io/dashboard[/blue] "
            "(email signup, $0.10 free credit)."
        )
        raise click.Abort()
    return key


def _run_async(coro):
    """Run an async coroutine, converting known backend errors to Click aborts.

    We translate the twitterapi.io exception hierarchy into user-friendly
    messages here so every caller gets consistent behavior.
    """
    try:
        return asyncio.run(coro)
    except AuthError as e:
        console.print(f"[red]Auth error: {e}[/red]")
        console.print(
            "Check that your TWITTERAPI_IO_KEY is valid and has not expired."
        )
        raise click.Abort()
    except InsufficientCreditsError as e:
        console.print(f"[red]Out of credits on twitterapi.io: {e}[/red]")
        console.print(
            "Recharge at [blue]https://twitterapi.io/dashboard[/blue]\n"
            "  • $1  → ~100,000 credits (~6,600 tweet lookups)\n"
            "  • $5  → ~500,000 credits (years of personal use)\n"
            "Current balance is visible on the dashboard."
        )
        raise click.Abort()
    except NotFoundError:
        # Caller usually treats this as "no result"; re-raise for upstream
        # to show a yellow warning rather than a red abort.
        raise
    except RateLimitedError as e:
        console.print(f"[red]Rate limited by twitterapi.io: {e}[/red]")
        raise click.Abort()
    except TwitterAPIError as e:
        console.print(f"[red]twitterapi.io error: {e}[/red]")
        raise click.Abort()


# ---- per-command fetch handlers --------------------------------------------

async def _fetch_tweet(client: TwitterApiClient, tweet_id: int) -> Tweet | None:
    raw = await client.get_tweet(tweet_id)
    return to_tweet(raw) if raw else None


async def _fetch_thread_and_replies(
    client: TwitterApiClient,
    tweet_id: int,
    *,
    with_replies: bool,
    reply_limit: int,
) -> tuple[list[Tweet], list[Tweet]]:
    """Fetch thread context + (optionally) replies.

    Returns (thread_tweets, other_replies).

    twitterapi.io's thread_context endpoint returns the whole thread
    (parents + siblings) in one paginated call — much simpler than
    twscrape's manual chain-walking logic. Replies from other users go
    through the replies/v2 endpoint.
    """
    # Step 1: thread context gives us the author's chain. Cap at ~40
    # tweets (2 pages of 20) — real author threads rarely exceed that,
    # and each page is billable.
    thread_raw: list[dict] = []
    async for t in client.tweet_thread_context(tweet_id, max_results=40):
        thread_raw.append(t)

    # Step 2: if the anchor tweet isn't in the thread_context response,
    # fetch it explicitly so the caller always has it.
    anchor = await client.get_tweet(tweet_id)
    if anchor and not any(str(t.get("id")) == str(anchor.get("id")) for t in thread_raw):
        thread_raw.append(anchor)

    # Map + sort chronologically by id.
    thread_tweets = [to_tweet(t) for t in thread_raw]
    thread_tweets.sort(key=lambda t: t.id)

    # Deduplicate by id in case thread_context returned overlapping pages.
    seen_ids: set[int] = set()
    deduped: list[Tweet] = []
    for t in thread_tweets:
        if t.id in seen_ids:
            continue
        seen_ids.add(t.id)
        deduped.append(t)
    thread_tweets = deduped

    # Keep only tweets from the same author — matches the twscrape
    # behavior where "thread" means the author's own chain.
    if thread_tweets:
        author_id = thread_tweets[-1].user.id if anchor else thread_tweets[0].user.id
        thread_tweets = [t for t in thread_tweets if t.user.id == author_id]

    # Step 3: optionally fetch replies from other users.
    other_replies: list[Tweet] = []
    if with_replies:
        async for r in client.tweet_replies(tweet_id, max_results=reply_limit):
            tw = to_tweet(r)
            if any(tw.id == existing.id for existing in thread_tweets):
                continue
            other_replies.append(tw)
            if len(other_replies) >= reply_limit:
                break

    # Sort replies by like count (popularity), matching twscrape backend.
    other_replies.sort(key=lambda t: t.likeCount, reverse=True)

    return thread_tweets, other_replies


async def _fetch_search(
    client: TwitterApiClient,
    query: str,
    *,
    limit: int,
    query_type: str,
) -> list[Tweet]:
    out: list[Tweet] = []
    async for raw in client.search(query, query_type=query_type, max_results=limit):
        out.append(to_tweet(raw))
        if len(out) >= limit:
            break
    return out


async def _fetch_user(client: TwitterApiClient, handle: str) -> User | None:
    raw = await client.user_info(handle)
    return to_user(raw) if raw else None


async def _fetch_user_tweets(
    client: TwitterApiClient,
    handle: str,
    *,
    limit: int,
) -> tuple[User | None, list[Tweet]]:
    user_raw = await client.user_info(handle)
    if not user_raw:
        return None, []
    user = to_user(user_raw)

    tweets: list[Tweet] = []
    async for raw in client.user_last_tweets(handle, max_results=limit):
        tweets.append(to_tweet(raw))
        if len(tweets) >= limit:
            break
    return user, tweets


# ---- sync entry points used by x.py ----------------------------------------

def fetch_tweet(tweet_id: int) -> Tweet | None:
    """Sync entry point: fetch a single tweet."""
    key = require_api_key()

    async def _go():
        async with TwitterApiClient(key) as c:
            return await _fetch_tweet(c, tweet_id)

    try:
        return _run_async(_go())
    except NotFoundError:
        return None


def fetch_thread_and_replies(
    tweet_id: int,
    *,
    with_replies: bool,
    reply_limit: int,
) -> tuple[list[Tweet], list[Tweet]]:
    """Sync entry point: fetch thread + optional replies."""
    key = require_api_key()

    async def _go():
        async with TwitterApiClient(key) as c:
            return await _fetch_thread_and_replies(
                c, tweet_id, with_replies=with_replies, reply_limit=reply_limit
            )

    try:
        return _run_async(_go())
    except NotFoundError:
        return [], []


def fetch_search(query: str, *, limit: int, sort: str) -> list[Tweet]:
    """Sync entry point: advanced search.

    The twscrape backend accepts sort options {top, latest, photos, videos};
    twitterapi.io only supports {Latest, Top} on advanced_search, so we
    fold photos/videos into query operators (filter:media, filter:images,
    filter:videos) and keep Latest as the ordering.
    """
    key = require_api_key()

    # Map the fcrawl sort to twitterapi.io's queryType + optional filters.
    sort_map = {
        "top": ("Top", None),
        "latest": ("Latest", None),
        "photos": ("Latest", "filter:images"),
        "videos": ("Latest", "filter:videos"),
    }
    query_type, extra_filter = sort_map.get(sort, ("Latest", None))
    effective_query = f"{query} {extra_filter}".strip() if extra_filter else query

    async def _go():
        async with TwitterApiClient(key) as c:
            return await _fetch_search(
                c, effective_query, limit=limit, query_type=query_type
            )

    return _run_async(_go())


def fetch_user(handle: str) -> User | None:
    """Sync entry point: user profile by handle."""
    key = require_api_key()

    async def _go():
        async with TwitterApiClient(key) as c:
            return await _fetch_user(c, handle)

    try:
        return _run_async(_go())
    except NotFoundError:
        return None


def fetch_user_tweets(handle: str, *, limit: int) -> tuple[User | None, list[Tweet]]:
    """Sync entry point: user timeline."""
    key = require_api_key()

    async def _go():
        async with TwitterApiClient(key) as c:
            return await _fetch_user_tweets(c, handle, limit=limit)

    try:
        return _run_async(_go())
    except NotFoundError:
        return None, []
