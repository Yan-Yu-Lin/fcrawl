"""Configuration management for fcrawl"""

import os
import json
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, TYPE_CHECKING
from urllib.parse import urlparse

from dotenv import load_dotenv

if TYPE_CHECKING:
    from firecrawl import Firecrawl
    from .keypool import KeyPool

# Load environment variables
load_dotenv()

DEFAULT_CONFIG = {
    "api_url": "http://localhost:3002",
    "api_key": None,
    "api_keys": [],  # extra Firecrawl keys (one per account) for the key pool
    "rate_limit_per_minute": 10,  # Firecrawl Free tier /scrape budget per account
    "max_wait": 75,  # seconds to wait when every key is rate limited
    "jina_fallback": True,
    # Family gateway (fcrawl-gateway). When set, api_url points at the gateway
    # and this token replaces every real key: Firecrawl, Serper and X all go
    # through the gateway, which holds the keys, the key pool and the cache.
    "gateway_token": None,
    "serper_api_key": None,
    "twitterapi_io_key": None,
    "default_format": "markdown",
    "cache_ttl_days": 7,  # 0 = never expire
}


def get_config_path() -> Path:
    """Get the configuration file path"""
    # Check for local config first
    local_config = Path(".fcrawlrc")
    if local_config.exists():
        return local_config

    # Then check home directory
    home_config = Path.home() / ".fcrawlrc"
    return home_config


def load_config() -> Dict[str, Any]:
    """Load configuration from file and environment"""
    config = DEFAULT_CONFIG.copy()

    # Load from config file if it exists
    config_path = get_config_path()
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception as exc:
            print(f"Warning: ignoring invalid config {config_path}: {exc}", file=sys.stderr)

    # Override with environment variables
    if os.getenv("FIRECRAWL_API_URL"):
        config["api_url"] = os.getenv("FIRECRAWL_API_URL")
    if os.getenv("FIRECRAWL_API_KEY"):
        config["api_key"] = os.getenv("FIRECRAWL_API_KEY")
    if os.getenv("FIRECRAWL_API_KEYS"):
        config["api_keys"] = [
            k.strip() for k in os.environ["FIRECRAWL_API_KEYS"].split(",") if k.strip()
        ]

    # Optional: YouTube transcript helpers (used by yt-dlp)
    # These are intentionally generic and only used if referenced by commands.
    if os.getenv("FCRAWL_YT_COOKIES_FILE"):
        config["yt_cookies_file"] = os.getenv("FCRAWL_YT_COOKIES_FILE")
    if os.getenv("FCRAWL_YT_COOKIES_FROM_BROWSER"):
        config["yt_cookies_from_browser"] = os.getenv("FCRAWL_YT_COOKIES_FROM_BROWSER")

    # twitterapi.io (alternative X/Twitter backend for `fcrawl x` commands)
    if os.getenv("TWITTERAPI_IO_KEY"):
        config["twitterapi_io_key"] = os.getenv("TWITTERAPI_IO_KEY")
    if os.getenv("FCRAWL_GATEWAY_TOKEN"):
        config["gateway_token"] = os.getenv("FCRAWL_GATEWAY_TOKEN")

    return config


def is_gateway(config: Optional[Dict[str, Any]] = None) -> bool:
    """True when fcrawl talks to a family gateway instead of Firecrawl directly."""
    config = config or load_config()
    return bool(config.get("gateway_token"))


def gateway_url(config: Optional[Dict[str, Any]] = None) -> str:
    config = config or load_config()
    return str(config["api_url"]).rstrip("/")


def gateway_headers(config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    config = config or load_config()
    return {"Authorization": f"Bearer {config['gateway_token']}", "Content-Type": "application/json"}


def gateway_error_message(exc: Exception) -> str:
    """A gateway error, readable: strips the SDK's 'Failed to scrape.' wrapping.

    The gateway answers {"error": "<code>: <why>"}; the SDK wraps that into a
    longer sentence. Return the gateway's own line when we can find it.
    """
    text = str(exc)
    for code in ("personal_quota", "family_quota", "pool_busy", "pool_exhausted", "token_busy",
                 "blocked_by_target", "upstream_error", "bad_token", "x_unavailable", "not_supported"):
        idx = text.find(code + ":")
        if idx >= 0:
            end = text.find(" - ", idx)
            return text[idx:end if end > 0 else None]
    return text


def get_twitterapi_io_key() -> str:
    """Get twitterapi.io API key from env var or config file.

    Returns empty string if not configured; callers should check and
    surface a clear error (e.g. `fcrawl x tweet --provider twitterapi`
    without a configured key should fail with guidance, not a TypeError).
    """
    if os.environ.get("TWITTERAPI_IO_KEY"):
        return os.environ["TWITTERAPI_IO_KEY"]
    return load_config().get("twitterapi_io_key") or ""


def save_config(config: Dict[str, Any], path: Optional[Path] = None):
    """Save configuration to file"""
    config_path = path or get_config_path()
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)


def get_api_keys(config: Optional[Dict[str, Any]] = None) -> List[str]:
    """All configured Firecrawl keys: `api_key` first, then `api_keys`, deduped."""
    config = config or load_config()
    keys = [config.get("api_key")] + list(config.get("api_keys") or [])
    return list(dict.fromkeys(k for k in keys if k))


def mask_key(key: Optional[str]) -> str:
    if not key:
        return str(key)
    return f"{key[:3]}…{key[-4:]}" if len(key) > 8 else "…"


def is_self_hosted(api_url: str) -> bool:
    """Loopback Firecrawl: no API key and no cloud rate limit."""
    host = urlparse(api_url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def get_firecrawl_client(api_key: Optional[str] = None) -> "Firecrawl":
    """Get a configured Firecrawl client (optionally for a specific key)."""
    # Import lazily so commands that only need load_config()
    # (e.g. yt-transcript) don't pay the import cost.
    from firecrawl import Firecrawl

    config = load_config()

    api_key = api_key or config.get("gateway_token") or config.get("api_key")
    # For local instances, use a dummy API key if none is set
    if not api_key and is_self_hosted(config["api_url"]):
        api_key = "local-dummy-key"

    client_args = {
        "api_url": config["api_url"],
        "api_key": api_key or "dummy-key",  # SDK requires some value
    }

    return Firecrawl(**client_args)


def get_key_pool(max_wait: Optional[int] = None) -> "KeyPool":
    """Key pool over every configured Firecrawl key.

    Self-hosted (localhost) instances have no rate limit and need no key, so
    the pool degrades to a single dummy key there.
    """
    from .keypool import KeyPool

    config = load_config()
    keys = get_api_keys(config)
    if is_self_hosted(config["api_url"]):
        # Local server: no rate limit, no shared cooldown/disabled state from
        # cloud keys. Its own state dir keeps it independent of the cloud pool.
        return KeyPool(
            keys[:1] or ["local-dummy-key"],
            rate_limit=10**9,
            max_wait=0,
            state_dir=Path("/tmp/fcrawl-keys-local"),
        )
    return KeyPool(
        keys,
        rate_limit=int(config.get("rate_limit_per_minute") or 10),
        max_wait=int(max_wait if max_wait is not None else config.get("max_wait") or 75),
    )


def firecrawl_call(fn, max_wait: Optional[int] = None):
    """Run fn(client) through the key pool, rotating keys on 429.

    fn must be a single request. Multi-step SDK calls (crawl/extract, which
    start a job and then poll it) should pool only the job start, see
    firecrawl_job().

    Gateway mode skips the pool entirely: the gateway runs the pool and
    answers quota problems with 429 + Retry-After, never 402, so the local
    pool's "disable on 402" rule must not see those responses.
    """
    config = load_config()
    if is_gateway(config):
        return fn(get_firecrawl_client(config["gateway_token"]))
    pool = get_key_pool(max_wait)
    return pool.call(lambda key: fn(get_firecrawl_client(key)))


def firecrawl_job(start, wait, max_wait: Optional[int] = None):
    """Pool the job-start request, then poll on the same client/key.

    start(client) -> job (something with .id); wait(client, job_id) -> result.
    Polling never switches key: a 429 on a status check must not create a
    second job on another account.
    """
    config = load_config()
    if is_gateway(config):
        # the gateway answers 501 for crawl/extract; let that error surface as is
        client = get_firecrawl_client(config["gateway_token"])
        job = start(client)
        return wait(client, job.id)
    pool = get_key_pool(max_wait)
    holder: dict = {}

    def begin(key: str):
        client = get_firecrawl_client(key)
        holder["client"] = client
        return start(client)

    job = pool.call(begin)
    return wait(holder["client"], job.id)
