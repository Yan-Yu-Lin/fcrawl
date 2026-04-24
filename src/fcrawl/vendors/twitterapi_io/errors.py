"""Exception hierarchy for twitterapi.io client errors."""


class TwitterAPIError(Exception):
    """Base exception for all twitterapi.io client errors."""

    def __init__(self, message: str, status_code: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class AuthError(TwitterAPIError):
    """Raised on 401 Unauthorized — invalid or missing API key."""


class NotFoundError(TwitterAPIError):
    """Raised on 404 Not Found — tweet/user doesn't exist or is private."""


class RateLimitedError(TwitterAPIError):
    """Raised after exhausting retries on 429 Too Many Requests."""


class BadRequestError(TwitterAPIError):
    """Raised on 400 Bad Request — malformed query or unsupported params."""


class InsufficientCreditsError(TwitterAPIError):
    """Raised on 402 Payment Required — twitterapi.io balance exhausted.

    Users should recharge at https://twitterapi.io/dashboard.
    """
