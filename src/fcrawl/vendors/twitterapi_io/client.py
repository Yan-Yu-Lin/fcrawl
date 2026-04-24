"""Async HTTP client for twitterapi.io (KaitoTwitterAPI).

All endpoints use a single ``x-api-key`` header for authentication.
The client retries on 429/5xx with exponential backoff and maps HTTP
status codes to the exceptions defined in ``.errors``.

Read-only endpoints implemented:
    get_tweet(tweet_id)
    get_tweets(tweet_ids)                   — batch
    search(query, ...)                      — async generator
    user_info(user_name)
    user_last_tweets(user_name, ...)        — async generator
    tweet_replies(tweet_id, ...)            — async generator
    tweet_thread_context(tweet_id)          — async generator

Write ops, webhooks, community/list endpoints are intentionally
out of scope for this provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .errors import (
    AuthError,
    BadRequestError,
    NotFoundError,
    RateLimitedError,
    TwitterAPIError,
)

BASE_URL = "https://api.twitterapi.io"

# Conservative retry schedule. Exponential with jitter is overkill for a
# personal CLI — deterministic waits are easier to reason about in logs.
_RETRY_WAITS = (1.0, 2.0, 5.0, 10.0)  # seconds


class TwitterApiClient:
    """Async twitterapi.io HTTP client.

    Usage:
        async with TwitterApiClient(api_key) as c:
            tweet = await c.get_tweet("2047359035268345995")
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 4,
    ):
        if not api_key:
            raise AuthError("twitterapi.io API key is empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    # ---- context management ------------------------------------------------

    async def __aenter__(self) -> "TwitterApiClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"x-api-key": self._api_key},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "TwitterApiClient must be used as an async context manager "
                "(async with TwitterApiClient(key) as c: ...)"
            )
        return self._client

    # ---- low-level GET with retry/error mapping ----------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET a JSON endpoint. Retries on 429/5xx. Raises on persistent errors."""
        client = self._require_client()
        # Drop None values so httpx doesn't serialize them as 'None'.
        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await client.get(path, params=clean_params)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                if attempt >= self._max_retries:
                    raise TwitterAPIError(f"Network error: {e}") from e
                await asyncio.sleep(_RETRY_WAITS[min(attempt, len(_RETRY_WAITS) - 1)])
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as e:
                    raise TwitterAPIError(
                        f"Non-JSON 200 response: {resp.text[:200]}"
                    ) from e

            if resp.status_code == 401:
                raise AuthError(
                    "Unauthorized — check your twitterapi.io API key",
                    status_code=401,
                    payload=_safe_json(resp),
                )

            if resp.status_code == 404:
                raise NotFoundError(
                    "Resource not found",
                    status_code=404,
                    payload=_safe_json(resp),
                )

            if resp.status_code == 400:
                body = _safe_json(resp)
                raise BadRequestError(
                    body.get("message") or body.get("msg") or "Bad request",
                    status_code=400,
                    payload=body,
                )

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self._max_retries:
                    cls = RateLimitedError if resp.status_code == 429 else TwitterAPIError
                    raise cls(
                        f"Exhausted retries, last status {resp.status_code}",
                        status_code=resp.status_code,
                        payload=_safe_json(resp),
                    )
                await asyncio.sleep(_RETRY_WAITS[min(attempt, len(_RETRY_WAITS) - 1)])
                continue

            # Unhandled status → bubble up with detail.
            raise TwitterAPIError(
                f"Unexpected status {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
                payload=_safe_json(resp),
            )

        # Unreachable in normal flow — safety net.
        raise TwitterAPIError(f"Retry loop exited without response: {last_exc}")

    # ---- tweet endpoints ---------------------------------------------------

    async def get_tweet(self, tweet_id: str | int) -> dict | None:
        """Fetch a single tweet by ID. Returns None if not found."""
        data = await self._get("/twitter/tweets", {"tweet_ids": str(tweet_id)})
        tweets = data.get("tweets") or []
        return tweets[0] if tweets else None

    async def get_tweets(self, tweet_ids: list[str | int]) -> list[dict]:
        """Fetch multiple tweets in one request (batch)."""
        if not tweet_ids:
            return []
        ids_param = ",".join(str(t) for t in tweet_ids)
        data = await self._get("/twitter/tweets", {"tweet_ids": ids_param})
        return data.get("tweets") or []

    async def search(
        self,
        query: str,
        *,
        query_type: str = "Latest",
        since_time: int | None = None,
        until_time: int | None = None,
        max_results: int | None = None,
    ) -> AsyncIterator[dict]:
        """Advanced search. Yields tweet dicts until exhausted or limit hit.

        twitterapi.io recommends combining ``since_time``/``until_time`` into
        the ``query`` itself (e.g. ``"AI since_time:1776045662 until_time:..."``)
        rather than passing them as separate params — cursor pagination on
        historical data is buggy on their side. This wrapper accepts both
        forms for convenience; when since_time/until_time kwargs are given,
        they're merged into the query string.
        """
        q = query
        if since_time is not None:
            q += f" since_time:{since_time}"
        if until_time is not None:
            q += f" until_time:{until_time}"

        cursor = ""
        yielded = 0
        while True:
            data = await self._get(
                "/twitter/tweet/advanced_search",
                {"query": q, "queryType": query_type, "cursor": cursor or None},
            )
            for t in data.get("tweets") or []:
                yield t
                yielded += 1
                if max_results is not None and yielded >= max_results:
                    return
            if not data.get("has_next_page"):
                return
            next_cursor = data.get("next_cursor") or ""
            if not next_cursor or next_cursor == cursor:
                return
            cursor = next_cursor

    async def tweet_replies(
        self,
        tweet_id: str | int,
        *,
        query_type: str = "Latest",
        max_results: int | None = None,
    ) -> AsyncIterator[dict]:
        """Fetch replies to a tweet. Yields reply tweet dicts."""
        cursor = ""
        yielded = 0
        while True:
            data = await self._get(
                "/twitter/tweet/replies/v2",
                {
                    "tweetId": str(tweet_id),
                    "queryType": query_type,
                    "cursor": cursor or None,
                },
            )
            for r in data.get("replies") or []:
                yield r
                yielded += 1
                if max_results is not None and yielded >= max_results:
                    return
            if not data.get("has_next_page"):
                return
            next_cursor = data.get("next_cursor") or ""
            if not next_cursor or next_cursor == cursor:
                return
            cursor = next_cursor

    async def tweet_thread_context(
        self,
        tweet_id: str | int,
        *,
        max_results: int | None = None,
    ) -> AsyncIterator[dict]:
        """Fetch the thread context (parents + siblings) of a tweet.

        Returns tweets in the order twitterapi.io provides them (roughly
        chronological by position in the thread).
        """
        cursor = ""
        yielded = 0
        # Safety valve: has_next_page can lie per their docs, so cap loop.
        hard_cap = 50
        loops = 0
        while loops < hard_cap:
            data = await self._get(
                "/twitter/tweet/thread_context",
                {"tweetId": str(tweet_id), "cursor": cursor or None},
            )
            replies = data.get("replies") or []
            if not replies:
                return
            for r in replies:
                yield r
                yielded += 1
                if max_results is not None and yielded >= max_results:
                    return
            if not data.get("has_next_page"):
                return
            next_cursor = data.get("next_cursor") or ""
            if not next_cursor or next_cursor == cursor:
                return
            cursor = next_cursor
            loops += 1

    # ---- user endpoints ----------------------------------------------------

    async def user_info(self, user_name: str) -> dict | None:
        """Fetch user profile by handle. Returns None if not found."""
        data = await self._get("/twitter/user/info", {"userName": user_name})
        # Note: this endpoint uses `msg` instead of `message` for error text.
        return data.get("data")

    async def user_last_tweets(
        self,
        user_name: str | None = None,
        *,
        user_id: str | None = None,
        include_replies: bool = False,
        max_results: int | None = None,
    ) -> AsyncIterator[dict]:
        """Fetch a user's recent tweets. Pass either user_name or user_id.

        user_id is preferred (more stable per twitterapi.io docs).
        """
        if not user_name and not user_id:
            raise ValueError("user_last_tweets requires user_name or user_id")

        cursor = ""
        yielded = 0
        while True:
            params = {
                "cursor": cursor or None,
                "includeReplies": str(include_replies).lower(),
            }
            if user_id:
                params["userId"] = user_id
            else:
                params["userName"] = user_name

            data = await self._get("/twitter/user/last_tweets", params)
            for t in data.get("tweets") or []:
                yield t
                yielded += 1
                if max_results is not None and yielded >= max_results:
                    return
            if not data.get("has_next_page"):
                return
            next_cursor = data.get("next_cursor") or ""
            if not next_cursor or next_cursor == cursor:
                return
            cursor = next_cursor


def _safe_json(resp: httpx.Response) -> dict:
    """Parse a response as JSON without raising. Returns {} on failure."""
    try:
        parsed = resp.json()
    except ValueError:
        return {"_raw": resp.text[:500]}
    return parsed if isinstance(parsed, dict) else {"_list": parsed}
