"""Reddit client utilities for fcrawl.

Uses Reddit's public .json endpoints — no authentication required.
Appending .json to any Reddit URL returns structured JSON data.
Rate limit: ~100 req/min per IP (more than enough for CLI use).
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "fcrawl/1.0 (CLI tool; +https://github.com/user/fcrawl)"


class RedditClient:
    """Simple synchronous HTTP client for Reddit's .json endpoints."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # Retry on 429 and 5xx with exponential backoff
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def get(self, path: str, params: dict = None) -> dict:
        """GET reddit.com/{path}.json with params.

        Args:
            path: Reddit URL path (e.g., 'search', 'r/python/hot')
            params: Query parameters

        Returns:
            Parsed JSON response
        """
        url = f"https://www.reddit.com/{path}"
        if not url.endswith(".json"):
            url += ".json"
        params = params or {}
        params["raw_json"] = 1  # Avoid HTML entity escaping
        resp = self.session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
