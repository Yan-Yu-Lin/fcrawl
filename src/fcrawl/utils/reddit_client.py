"""Reddit client utilities for fcrawl.

Uses Reddit's public .json endpoints when available. Reddit increasingly blocks
unauthenticated .json requests from some networks, so the client also has a
read-only fallback path:

- Arctic Shift for archived Reddit posts/comments/subreddit/user data.
- Serper Google search for global Reddit keyword search, then Arctic Shift ID
  lookup for structured post metadata.

The fallback keeps the response shape close to Reddit listings so command code
can continue to render/search/post JSON without special cases.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import load_config

USER_AGENT = "python:fcrawl:v0.1.0 (read-only Reddit research; contact: fcrawl)"
ARCTIC_BASE_URL = "https://arctic-shift.photon-reddit.com"
SERPER_ENDPOINT = "https://google.serper.dev/search"
REDDIT_POST_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|old\.)?reddit\.com/r/([^/]+)/comments/([a-z0-9]+)",
    re.IGNORECASE,
)
REDDIT_SHORT_URL_RE = re.compile(
    r"(?:https?://)?redd\.it/([a-z0-9]+)",
    re.IGNORECASE,
)


class RedditClient:
    """Simple synchronous HTTP client for Reddit data."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/html;q=0.8,*/*;q=0.5",
            }
        )
        # Retry on 429 and 5xx with exponential backoff
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods={"GET", "POST"},
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def get(self, path: str, params: dict | None = None) -> dict | list:
        """GET reddit.com/{path}.json with params.

        Falls back to public archive/search providers if Reddit blocks the
        unauthenticated .json request.

        Args:
            path: Reddit URL path (e.g., 'search', 'r/python/hot')
            params: Query parameters

        Returns:
            Parsed JSON response, normally in Reddit listing shape.
        """
        request_params = dict(params or {})
        request_params["raw_json"] = 1  # Avoid HTML entity escaping
        url = f"https://www.reddit.com/{path}"
        if not url.endswith(".json"):
            url += ".json"

        try:
            resp = self.session.get(url, params=request_params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in {403, 429}:
                raise
            return self._fallback_get(path, params or {})

    def resolve_share_url(self, url: str) -> str:
        """Resolve a Reddit share URL by following redirects.

        If Reddit blocks the redirect lookup, fall back to Arctic Shift's short
        link endpoint for /r/.../s/... and /u/.../s/... links.
        """
        try:
            resp = self.session.get(url, allow_redirects=True, timeout=15)
            resp.raise_for_status()
            return resp.url
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in {403, 429}:
                raise

        parsed = urlparse(url if url.startswith("http") else f"https://reddit.com{url}")
        path = parsed.path.rstrip("/")
        data = self._arctic_get("/api/short_links", {"paths": path})
        items = data.get("data") or []
        if items:
            value = items[0]
            if isinstance(value, dict):
                return (
                    value.get("url")
                    or value.get("path")
                    or value.get("full_link")
                    or url
                )
            if isinstance(value, str):
                return value
        return url

    # ------------------------------------------------------------------
    # Fallback routing
    # ------------------------------------------------------------------

    def _fallback_get(self, path: str, params: dict[str, Any]) -> dict | list:
        clean_path = path.rstrip("/")

        if clean_path == "search":
            return self._fallback_search(params=params, subreddit=None)

        subreddit_search = re.fullmatch(r"r/([^/]+)/search", clean_path, re.I)
        if subreddit_search:
            return self._fallback_search(
                params=params, subreddit=subreddit_search.group(1)
            )

        comments_match = re.fullmatch(r"comments/([a-z0-9]+)", clean_path, re.I)
        if comments_match:
            return self._fallback_post(comments_match.group(1), params)

        subreddit_about = re.fullmatch(r"r/([^/]+)/about", clean_path, re.I)
        if subreddit_about:
            return self._fallback_subreddit_about(subreddit_about.group(1))

        subreddit_feed = re.fullmatch(
            r"r/([^/]+)/(hot|new|top|rising)", clean_path, re.I
        )
        if subreddit_feed:
            return self._fallback_subreddit_feed(
                subreddit_feed.group(1), subreddit_feed.group(2), params
            )

        user_about = re.fullmatch(r"user/([^/]+)/about", clean_path, re.I)
        if user_about:
            return self._fallback_user_about(user_about.group(1))

        user_activity = re.fullmatch(
            r"user/([^/]+)/(overview|submitted|comments)", clean_path, re.I
        )
        if user_activity:
            return self._fallback_user_activity(
                user_activity.group(1), user_activity.group(2), params
            )

        raise RuntimeError(f"Reddit blocked the request and no fallback exists: {path}")

    # ------------------------------------------------------------------
    # Provider helpers
    # ------------------------------------------------------------------

    def _arctic_get(self, endpoint: str, params: dict[str, Any]) -> dict:
        response = self.session.get(
            f"{ARCTIC_BASE_URL}{endpoint}", params=params, timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Arctic Shift error: {data['error']}")
        return data

    def _get_serper_api_key(self) -> str:
        if os.environ.get("SERPER_API_KEY"):
            return os.environ["SERPER_API_KEY"]
        config = load_config()
        return config.get("serper_api_key", "") or ""

    def _serper_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        from ..commands.search import serper_target

        endpoint, api_key = serper_target()
        if not api_key:
            raise RuntimeError(
                "Reddit blocked public .json search and SERPER_API_KEY is not set "
                "for global Reddit search fallback"
            )

        response = self.session.post(
            endpoint,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "gl": "us", "hl": "en", "num": min(limit, 100)},
            timeout=30,
        )
        response.raise_for_status()
        return response.json().get("organic", [])

    # ------------------------------------------------------------------
    # Fallback implementations
    # ------------------------------------------------------------------

    def _fallback_search(
        self, params: dict[str, Any], subreddit: str | None
    ) -> dict[str, Any]:
        query = str(params.get("q") or "").strip()
        limit = self._safe_limit(params.get("limit"), default=20, maximum=100)
        sort = str(params.get("sort") or "relevance")
        time_filter = str(params.get("t") or "all")

        author = None
        author_match = re.search(r"(?:^|\s)author:([^\s]+)", query)
        if author_match:
            author = author_match.group(1).strip()
            query = re.sub(r"(?:^|\s)author:[^\s]+", " ", query).strip()

        children: list[dict[str, Any]] = []
        if query and not author:
            children = self._search_serper_reddit(
                query=query, limit=limit, subreddit=subreddit
            )

        if (subreddit or author) and len(children) < limit:
            try:
                archive_children = self._search_arctic_posts(
                    query=query,
                    subreddit=subreddit,
                    author=author,
                    limit=limit,
                    sort=sort,
                    time_filter=time_filter,
                )
                children = self._merge_post_children(children, archive_children, limit)
            except Exception:
                if not children:
                    raise
        elif not children:
            children = self._search_serper_reddit(query=query, limit=limit)

        return {"kind": "Listing", "data": {"children": children, "after": None}}

    def _search_arctic_posts(
        self,
        query: str,
        subreddit: str | None,
        author: str | None,
        limit: int,
        sort: str,
        time_filter: str,
    ) -> list[dict[str, Any]]:
        fields = (
            "id,title,author,subreddit,score,num_comments,created_utc,selftext,"
            "url,link_flair_text,over_18,spoiler"
        )
        base_params: dict[str, Any] = {
            "limit": limit,
            "sort": "desc",
            "fields": fields,
        }
        if subreddit:
            base_params["subreddit"] = subreddit
        if author:
            base_params["author"] = author
        after = self._time_filter_to_after(time_filter)
        if after:
            base_params["after"] = after

        candidates = []
        if query:
            candidates.extend([("query", query), ("title", query)])
        else:
            candidates.append(("", ""))

        last_error: Exception | None = None
        for key, value in candidates:
            request_params = dict(base_params)
            if key:
                request_params[key] = value
            try:
                data = self._arctic_get("/api/posts/search", request_params)
                posts = data.get("data") or []
                children = [self._post_child_from_arctic(post) for post in posts]
                return self._sort_children(children, sort)[:limit]
            except Exception as exc:  # Retry with less expensive keyword mode.
                last_error = exc
                continue

        if last_error:
            raise last_error
        return []

    def _search_serper_reddit(
        self, query: str, limit: int, subreddit: str | None = None
    ) -> list[dict[str, Any]]:
        search_terms = [query, "Reddit"]
        if subreddit:
            search_terms.append(f"r/{subreddit}")
        search_query = " ".join(term for term in search_terms if term).strip()
        results = self._serper_search(search_query, limit=max(limit * 3, 10))

        ranked_ids: list[str] = []
        seen: set[str] = set()
        fallback_posts: list[dict[str, Any]] = []

        for item in results:
            url = item.get("link") or ""
            post_id = self._extract_post_id(url)
            result_subreddit = self._extract_subreddit(url)
            if not post_id:
                continue
            if (
                subreddit
                and result_subreddit
                and result_subreddit.lower() != subreddit.lower()
            ):
                continue
            if post_id not in seen:
                ranked_ids.append(post_id)
                seen.add(post_id)
                fallback_posts.append(self._post_child_from_serper(item, post_id))

        children_by_id: dict[str, dict[str, Any]] = {}
        if ranked_ids:
            try:
                data = self._arctic_get(
                    "/api/posts/ids",
                    {
                        "ids": ",".join(ranked_ids[: min(len(ranked_ids), 100)]),
                        "fields": (
                            "id,title,author,subreddit,score,num_comments,created_utc,"
                            "selftext,url,link_flair_text"
                        ),
                    },
                )
                for post in data.get("data") or []:
                    children_by_id[str(post.get("id"))] = self._post_child_from_arctic(
                        post
                    )
            except Exception:
                children_by_id = {}

        merged: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        for fallback in fallback_posts:
            post_id = fallback.get("data", {}).get("id")
            if post_id and post_id in children_by_id:
                merged.append(children_by_id[post_id])
                used_ids.add(post_id)
            elif post_id and post_id not in used_ids:
                merged.append(fallback)
                used_ids.add(post_id)
            elif not post_id:
                merged.append(fallback)
            if len(merged) >= limit:
                break
        return merged

    def _fallback_post(
        self, post_id: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        post_data = (
            self._arctic_get("/api/posts/ids", {"ids": post_id}).get("data") or []
        )
        post_child = (
            self._post_child_from_arctic(post_data[0])
            if post_data
            else {"kind": "t3", "data": {"id": post_id}}
        )

        comment_limit = int(params.get("limit") or 0)
        depth = max(int(params.get("depth") or 3), 1)
        comments: list[dict[str, Any]] = []
        if comment_limit != 0:
            tree_limit = comment_limit if comment_limit > 0 else 9999
            tree_data = self._arctic_get(
                "/api/comments/tree",
                {
                    "link_id": f"t3_{post_id}",
                    "limit": min(tree_limit, 25000),
                    "start_depth": depth,
                    "start_breadth": max(comment_limit, 20)
                    if comment_limit > 0
                    else 9999,
                },
            )
            comments = tree_data.get("data") or []

        return [
            {"kind": "Listing", "data": {"children": [post_child], "after": None}},
            {"kind": "Listing", "data": {"children": comments, "after": None}},
        ]

    def _fallback_subreddit_about(self, subreddit: str) -> dict[str, Any]:
        data = self._arctic_get(
            "/api/subreddits/search", {"subreddit": subreddit, "limit": 1}
        )
        items = data.get("data") or []
        return {
            "kind": "t5",
            "data": items[0] if items else {"display_name": subreddit},
        }

    def _fallback_subreddit_feed(
        self, subreddit: str, sort: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        limit = self._safe_limit(params.get("limit"), default=20, maximum=100)
        time_filter = str(params.get("t") or "all")
        children = self._search_arctic_posts(
            query="",
            subreddit=subreddit,
            author=None,
            limit=limit,
            sort=sort,
            time_filter=time_filter,
        )
        return {"kind": "Listing", "data": {"children": children, "after": None}}

    def _fallback_user_about(self, username: str) -> dict[str, Any]:
        data = self._arctic_get("/api/users/search", {"author": username, "limit": 1})
        items = data.get("data") or []
        item = items[0] if items else {"author": username}
        # Reddit user about endpoint uses name/link_karma/comment_karma keys.
        item.setdefault("name", item.get("author", username))
        item.setdefault("link_karma", item.get("post_karma", 0))
        item.setdefault("comment_karma", item.get("comment_karma", 0))
        item.setdefault("total_karma", item.get("total_karma", 0))
        return {"kind": "t2", "data": item}

    def _fallback_user_activity(
        self, username: str, activity_type: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        limit = self._safe_limit(params.get("limit"), default=20, maximum=100)
        time_filter = str(params.get("t") or "all")
        after = self._time_filter_to_after(time_filter)
        children: list[dict[str, Any]] = []

        if activity_type in {"overview", "submitted"}:
            request_params: dict[str, Any] = {
                "author": username,
                "limit": limit,
                "sort": "desc",
            }
            if after:
                request_params["after"] = after
            posts = (
                self._arctic_get("/api/posts/search", request_params).get("data") or []
            )
            children.extend(self._post_child_from_arctic(post) for post in posts)

        if activity_type in {"overview", "comments"}:
            request_params = {"author": username, "limit": limit, "sort": "desc"}
            if after:
                request_params["after"] = after
            comments = (
                self._arctic_get("/api/comments/search", request_params).get("data")
                or []
            )
            children.extend({"kind": "t1", "data": comment} for comment in comments)

        children.sort(
            key=lambda child: child.get("data", {}).get("created_utc") or 0,
            reverse=True,
        )
        return {
            "kind": "Listing",
            "data": {"children": children[:limit], "after": None},
        }

    # ------------------------------------------------------------------
    # Shape conversion helpers
    # ------------------------------------------------------------------

    def _post_child_from_arctic(self, post: dict[str, Any]) -> dict[str, Any]:
        data = dict(post)
        post_id = str(data.get("id") or "")
        subreddit = data.get("subreddit") or ""
        data.setdefault("name", f"t3_{post_id}" if post_id else "")
        data.setdefault("author", "[deleted]")
        data.setdefault("score", 0)
        data.setdefault("num_comments", 0)
        data.setdefault("total_awards_received", 0)
        data.setdefault("selftext", "")
        data.setdefault("link_flair_text", None)
        if not data.get("permalink") and post_id:
            data["permalink"] = f"/r/{subreddit}/comments/{post_id}/"
        if not data.get("url") and post_id:
            data["url"] = f"https://www.reddit.com{data['permalink']}"
        return {"kind": "t3", "data": data}

    def _post_child_from_serper(
        self, item: dict[str, Any], post_id: str | None
    ) -> dict[str, Any]:
        url = item.get("link") or ""
        subreddit = self._extract_subreddit(url) or ""
        data = {
            "id": post_id,
            "name": f"t3_{post_id}" if post_id else None,
            "title": item.get("title") or "",
            "author": "unknown",
            "subreddit": subreddit,
            "score": 0,
            "num_comments": 0,
            "total_awards_received": 0,
            "selftext": item.get("snippet") or "",
            "created_utc": None,
            "link_flair_text": None,
            "url": url,
            "permalink": self._path_from_url(url),
        }
        return {"kind": "t3", "data": data}

    def _merge_post_children(
        self,
        primary: list[dict[str, Any]],
        secondary: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for child in [*primary, *secondary]:
            post_id = child.get("data", {}).get("id")
            if post_id and post_id in seen:
                continue
            if post_id:
                seen.add(post_id)
            merged.append(child)
            if len(merged) >= limit:
                break
        return merged

    def _sort_children(
        self, children: list[dict[str, Any]], sort: str
    ) -> list[dict[str, Any]]:
        if sort == "top":
            return sorted(
                children,
                key=lambda item: item.get("data", {}).get("score") or 0,
                reverse=True,
            )
        if sort == "comments":
            return sorted(
                children,
                key=lambda item: item.get("data", {}).get("num_comments") or 0,
                reverse=True,
            )
        if sort in {"new", "hot", "relevance"}:
            return children
        return children

    def _extract_post_id(self, url: str) -> str | None:
        match = REDDIT_POST_URL_RE.search(url)
        if match:
            return match.group(2).lower()
        match = REDDIT_SHORT_URL_RE.search(url)
        if match:
            return match.group(1).lower()
        return None

    def _extract_subreddit(self, url: str) -> str | None:
        match = REDDIT_POST_URL_RE.search(url)
        if match:
            return match.group(1)
        return None

    def _path_from_url(self, url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        if parsed.netloc.endswith("reddit.com"):
            return parsed.path
        return url

    def _safe_limit(self, value: Any, default: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(1, min(parsed, maximum))

    def _time_filter_to_after(self, time_filter: str) -> str | None:
        now = datetime.now(timezone.utc)
        delta = {
            "hour": timedelta(hours=1),
            "day": timedelta(days=1),
            "week": timedelta(weeks=1),
            "month": timedelta(days=31),
            "year": timedelta(days=366),
        }.get(time_filter)
        if not delta:
            return None
        return (now - delta).strftime("%Y-%m-%d")
