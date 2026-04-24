"""twitterapi.io (KaitoTwitterAPI) HTTP client + mapper.

A paid third-party X/Twitter API that provides read and write endpoints
via a single `x-api-key` header. Used as an alternative to the vendored
twscrape backend when the user has a twitterapi.io API key configured.

The mapper emits the same `Tweet` / `User` dataclass instances that
twscrape's parser emits, so the command-layer `display_tweet()` and
`tweet_to_dict()` work unchanged regardless of which backend produced
the data.

Docs: https://docs.twitterapi.io/
Base URL: https://api.twitterapi.io
"""

from .client import TwitterApiClient
from .errors import (
    AuthError,
    BadRequestError,
    InsufficientCreditsError,
    NotFoundError,
    RateLimitedError,
    TwitterAPIError,
)
from .mapper import to_tweet, to_user

__all__ = [
    "TwitterApiClient",
    "TwitterAPIError",
    "RateLimitedError",
    "NotFoundError",
    "AuthError",
    "BadRequestError",
    "InsufficientCreditsError",
    "to_tweet",
    "to_user",
]
