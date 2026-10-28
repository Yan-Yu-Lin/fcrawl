"""Scrape command for fcrawl"""

import hashlib
import re
import time
import click
import requests
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from rich.progress import Progress, SpinnerColumn, TextColumn
from types import SimpleNamespace
from rich.console import Console
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from ..utils.config import get_firecrawl_client, get_key_pool, load_config
from ..utils.keypool import KeyPoolExhausted
from ..utils.output import (
    handle_output,
    console,
    strip_links,
    extract_markdown_links,
    resolve_pretty,
)
from ..utils.cache import (
    cache_key,
    read_cache,
    write_cache,
    result_to_dict,
    validate_result,
    CachedResult,
)


# Article mode: aggressive filtering for clean article extraction
ARTICLE_INCLUDE_TAGS = [
    "article",
    "main",
    ".post-content",
    ".post-body",
    ".entry-content",
    ".article-content",
    ".article-body",
    ".content-body",
    "#content",
    "#article",
    "[role='main']",
]

ARTICLE_EXCLUDE_TAGS = [
    "nav",
    "header",
    "footer",
    "aside",
    ".sidebar",
    ".share",
    ".social",
    ".social-share",
    ".share-buttons",
    ".popup",
    ".modal",
    ".newsletter",
    ".subscribe",
    ".comments",
    ".comment-section",
    "#comments",
    ".related",
    ".related-posts",
    ".recommended",
    ".advertisement",
    ".ads",
    ".ad",
    ".cookie",
    ".banner",
    ".promo",
    ".cta",
]

DEFAULT_SCRAPE_TIMEOUT = 12
AUTO_SAVE_DIR = Path("/tmp/fcrawl-saved")


class BlockedResult(RuntimeError):
    """A provider answered, but with a challenge/error page instead of content."""


class TaggedResult:
    """Result wrapper that adds provider metadata."""

    def __init__(self, result, provider: str, fallback_url: Optional[str] = None):
        self.markdown = getattr(result, "markdown", None)
        self.html = getattr(result, "html", None)
        self.links = getattr(result, "links", None)
        self.screenshot = getattr(result, "screenshot", None)
        self.metadata = getattr(result, "metadata", None)
        self.source_provider = provider
        self.fallback_url = fallback_url


def sanitize_url_for_path(url: str) -> str:
    """Convert URL to a readable filesystem slug."""
    parsed = urlparse(url)
    host = re.sub(r"[^a-zA-Z0-9.-]", "-", parsed.netloc or "page")
    path = re.sub(r"[^a-zA-Z0-9._-]+", "-", parsed.path.strip("/"))
    path = path.strip("-")[:40]
    base = "-".join(part for part in [host, path] if part) or "page"
    return re.sub(r"-+", "-", base).strip("-")


def get_auto_save_path(url: str, formats: List[str], json_output: bool) -> str:
    """Generate an automatic temp file path for scrape output."""
    AUTO_SAVE_DIR.mkdir(parents=True, exist_ok=True)
    suffix_map = {
        "markdown": ".md",
        "html": ".html",
        "links": ".txt",
        "screenshot": ".txt",
        "extract": ".json",
    }
    suffix = (
        ".json"
        if json_output or len(formats) > 1
        else suffix_map.get(formats[0], ".txt")
    )
    digest = hashlib.sha256(f"{url}-{time.time_ns()}".encode()).hexdigest()[:8]
    filename = f"{sanitize_url_for_path(url)}-{digest}{suffix}"
    return str(AUTO_SAVE_DIR / filename)


def jina_fallback_supported(
    formats: List[str],
    include_tags: Tuple[str, ...],
    exclude_tags: Tuple[str, ...],
    raw: bool,
) -> bool:
    """Return whether Jina can be used as a reasonable fallback."""
    unsupported_formats = {fmt for fmt in formats if fmt not in {"markdown", "links"}}
    if unsupported_formats:
        return False
    if include_tags or exclude_tags or raw:
        return False
    return True


def parse_jina_response(data: dict, original_url: str) -> TaggedResult:
    """Turn Jina Reader's JSON payload into the same shape as a scrape result."""
    metadata = {
        "source_url": data.get("url") or original_url,
        "title": data.get("title"),
        "published_time": data.get("publishedTime"),
        # Jina reports the target site's real status here even when it
        # answers 200 itself (Cloudflare challenge pages come back as 403).
        "status_code": data.get("httpStatus"),
    }
    result = SimpleNamespace(
        markdown=(data.get("content") or "").lstrip(),
        html=None,
        links=None,
        metadata=SimpleNamespace(**{k: v for k, v in metadata.items() if v is not None}),
    )
    return TaggedResult(
        result, provider="jina", fallback_url=f"https://r.jina.ai/{original_url}"
    )


def scrape_with_jina(url: str, timeout: int) -> TaggedResult:
    """Fetch markdown from Jina Reader (JSON mode so we can see httpStatus)."""
    fallback_url = f"https://r.jina.ai/{url}"
    response = requests.get(
        fallback_url,
        headers={"Accept": "application/json", "X-Return-Format": "markdown"},
        timeout=timeout,
    )
    response.raise_for_status()
    result = parse_jina_response(response.json().get("data") or {}, url)
    result.fallback_url = fallback_url
    return result


def scrape_with_firecrawl(
    url: str, scrape_options: dict, timeout: int, max_wait: Optional[int]
) -> TaggedResult:
    """Fetch content from Firecrawl through the key pool, bounded by timeout.

    429s are handled inside the pool (rotate key / wait); only other errors
    and the deadline escape from here.
    """
    pool = get_key_pool(max_wait)

    def attempt(key: str):
        client = get_firecrawl_client(key)
        return client.scrape(url, timeout=timeout * 1000, **scrape_options)

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(pool.call, attempt)
    started = time.monotonic()
    try:
        # Time the pool spends sleeping on rate limits is not charged to the
        # scrape timeout: keep extending the deadline by what it has waited.
        while True:
            remaining = timeout + pool.waited - (time.monotonic() - started)
            if remaining <= 0:
                # Stop the worker from issuing another (paid) request once
                # we have given up on it; an in-flight HTTP call still ends.
                pool.cancelled = True
                raise RuntimeError(f"Firecrawl timed out after {timeout}s")
            try:
                result = future.result(timeout=min(remaining, 1.0))
                break
            except TimeoutError:
                continue
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return TaggedResult(result, provider="firecrawl")


def fetch_scrape_result(
    url: str,
    scrape_options: dict,
    timeout: int,
    allow_jina: bool,
    max_wait: Optional[int] = None,
):
    """Firecrawl first; Jina only as a relay when Firecrawl fails or is blocked.

    Both providers' results go through validate_result so a challenge page
    never counts as success. 429s never reach Jina — the key pool handles
    them.
    """
    errors = []
    try:
        result = scrape_with_firecrawl(url, scrape_options, timeout, max_wait)
        ok, reason = validate_result(result)
        if ok:
            return result
        errors.append(("firecrawl", BlockedResult(reason)))
    except KeyPoolExhausted:
        raise
    except Exception as exc:
        errors.append(("firecrawl", exc))

    if allow_jina:
        try:
            result = scrape_with_jina(url, timeout)
            ok, reason = validate_result(result)
            if ok:
                return result
            errors.append(("jina", BlockedResult(reason)))
        except Exception as exc:
            errors.append(("jina", exc))

    details = "; ".join(f"{provider}: {exc}" for provider, exc in errors)
    if all(isinstance(exc, BlockedResult) for _, exc in errors):
        raise BlockedResult(f"Blocked by the target site ({details})")
    raise RuntimeError(f"All scrape attempts failed ({details})")


def announce_saved_result(
    path: str,
    provider: str,
    url: str,
    fallback_url: Optional[str],
    from_cache: bool,
    status: Optional[str] = None,
):
    """Emit a concise, parseable manifest line for saved results."""
    parts = [f"provider={provider}", f"url={url}"]
    if path:
        parts.insert(0, f"path={path}")
    if status:
        parts.append(f"status={status}")
    if fallback_url:
        parts.append(f"fallback_url={fallback_url}")
    if from_cache:
        parts.append("from_cache=true")
    print("RESULT " + " ".join(parts))


def clean_article_content(content: str) -> str:
    """Post-process markdown to remove common noise patterns"""
    lines = content.split("\n")
    cleaned_lines = []

    # Patterns to skip
    skip_patterns = [
        r"^Close$",  # Popup close buttons
        r"^\s*\*\s*\[\s*\]\(https?://(www\.)?(twitter|facebook|linkedin|reddit|pinterest|x\.com)",  # Empty share links
        r"^\s*\*\s*\[.*\]\(https?://(www\.)?(twitter\.com/share|facebook\.com/sharer|linkedin\.com/share|reddit\.com/submit)",  # Share links with text
        r"^Share (on|this|article)",  # Share text
        r"^(Tweet|Pin|Share|Follow us)",  # Social CTAs
        r"^\s*US\s*$",  # Random country codes from popups
        r"^Start Now$",  # CTA buttons
        r"^Subscribe",  # Newsletter prompts
        r"^Sign up",  # Sign up prompts
        r"^\s*$",  # Empty lines at start (will be handled by consecutive empty check)
    ]

    skip_regex = [re.compile(p, re.IGNORECASE) for p in skip_patterns]

    # Track consecutive empty lines
    prev_empty = False

    for line in lines:
        # Check if line matches any skip pattern
        should_skip = any(r.search(line) for r in skip_regex)

        if should_skip:
            continue

        # Collapse multiple empty lines
        is_empty = line.strip() == ""
        if is_empty and prev_empty:
            continue

        cleaned_lines.append(line)
        prev_empty = is_empty

    return "\n".join(cleaned_lines)


def format_with_metadata(result, content: str) -> str:
    """Prepend metadata header to content (like Jina's r.jina.ai output)"""
    header_lines = []

    if hasattr(result, "source_provider") and result.source_provider:
        header_lines.append(f"Source Provider: {result.source_provider}")
    if hasattr(result, "fallback_url") and result.fallback_url:
        header_lines.append(f"Fallback URL: {result.fallback_url}")

    if hasattr(result, "metadata") and result.metadata:
        md = result.metadata
        if hasattr(md, "title") and md.title:
            header_lines.append(f"Title: {md.title}")
        if hasattr(md, "source_url") and md.source_url:
            header_lines.append(f"URL Source: {md.source_url}")
        elif hasattr(md, "url") and md.url:
            header_lines.append(f"URL Source: {md.url}")
        if hasattr(md, "published_time") and md.published_time:
            header_lines.append(f"Published Time: {md.published_time}")

    if header_lines:
        return "\n".join(header_lines) + "\n\nMarkdown Content:\n" + content
    return content


@click.command()
@click.argument("url")
@click.option(
    "-f",
    "--format",
    "formats",
    multiple=True,
    default=["markdown"],
    type=click.Choice(["markdown", "html", "links", "screenshot", "extract"]),
    help="Output formats (can specify multiple)",
)
@click.option("-o", "--output", help="Save output to file")
@click.option("--copy", is_flag=True, help="Copy to clipboard")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--pretty/--no-pretty", default=None, help="Pretty print output")
@click.option(
    "-i",
    "--include",
    "include_tags",
    multiple=True,
    help="CSS selectors to include (can specify multiple)",
)
@click.option(
    "-e",
    "--exclude",
    "exclude_tags",
    multiple=True,
    help="CSS selectors to exclude (can specify multiple)",
)
@click.option(
    "--article",
    is_flag=True,
    help="Article mode: aggressive filtering for clean article extraction",
)
@click.option("--raw", is_flag=True, help="Raw mode: disable all content filtering")
@click.option(
    "--no-links", is_flag=True, help="Strip markdown links, keep display text"
)
@click.option("--wait", type=int, help="Wait time in milliseconds before scraping")
@click.option("--screenshot-full", is_flag=True, help="Take full page screenshot")
@click.option(
    "--timeout",
    type=int,
    default=DEFAULT_SCRAPE_TIMEOUT,
    show_default=True,
    help="Hard timeout in seconds for scraping attempts",
)
@click.option(
    "--no-cache", "no_cache", is_flag=True, help="Bypass cache, force fresh fetch"
)
@click.option(
    "--cache-only", "cache_only", is_flag=True, help="Only read from cache, no API call"
)
@click.option(
    "--save-temp",
    is_flag=True,
    help="Save full result to an auto-generated temp file and print only its path",
)
@click.option(
    "--no-jina", "no_jina", is_flag=True, help="Never fall back to Jina Reader"
)
@click.option(
    "--max-wait",
    type=int,
    default=None,
    help="Max seconds to wait when every Firecrawl key is rate limited (config max_wait)",
)
def scrape(
    url: str,
    formats: List[str],
    output: Optional[str],
    copy: bool,
    json_output: bool,
    pretty: Optional[bool],
    include_tags: Tuple[str, ...],
    exclude_tags: Tuple[str, ...],
    article: bool,
    raw: bool,
    no_links: bool,
    wait: Optional[int],
    screenshot_full: bool,
    timeout: int,
    no_cache: bool,
    cache_only: bool,
    save_temp: bool,
    no_jina: bool,
    max_wait: Optional[int],
):
    """Scrape a single URL and extract content

    Examples:
        fcrawl scrape https://example.com
        fcrawl scrape https://example.com --article
        fcrawl scrape https://example.com -i ".content" -e ".sidebar"
        fcrawl scrape https://example.com --raw
        fcrawl scrape https://example.com -f markdown -f links
        fcrawl scrape https://example.com -o output.md --copy
        fcrawl scrape https://example.com --save-temp
    """
    pretty = resolve_pretty(pretty)

    if timeout < 1:
        raise click.BadParameter("timeout must be >= 1", param_hint="--timeout")
    if output and save_temp:
        raise click.BadParameter(
            "Use either --output or --save-temp, not both", param_hint="--save-temp"
        )

    output_path = get_auto_save_path(url, formats, json_output) if save_temp else output

    # Prepare scrape options
    # 'links' is an output format (client-side extraction), not an API format
    api_formats = [f for f in formats if f != "links"]
    # Always need markdown for link extraction
    if "links" in formats and "markdown" not in api_formats:
        api_formats.append("markdown")
    # Default to markdown if no API formats specified
    if not api_formats:
        api_formats = ["markdown"]
    scrape_options: dict[str, object] = {"formats": api_formats}

    # Handle content filtering modes
    if raw:
        # Disable all filtering
        scrape_options["only_main_content"] = False
    elif article:
        # Aggressive article mode
        scrape_options["include_tags"] = list(ARTICLE_INCLUDE_TAGS)
        scrape_options["exclude_tags"] = list(ARTICLE_EXCLUDE_TAGS)
        scrape_options["only_main_content"] = False  # We handle filtering ourselves
    else:
        # Custom include/exclude tags
        if include_tags:
            scrape_options["include_tags"] = list(include_tags)
        if exclude_tags:
            scrape_options["exclude_tags"] = list(exclude_tags)

    if wait:
        scrape_options["wait_for"] = wait

    if screenshot_full and "screenshot" in formats:
        scrape_options["screenshot"] = {"fullPage": True}

    # Generate cache key based on options that affect API result
    cache_opts = {
        "formats": list(formats),
        "article": article,
        "raw": raw,
        "include_tags": list(include_tags) if include_tags else None,
        "exclude_tags": list(exclude_tags) if exclude_tags else None,
        "wait": wait,
        # timeout does not affect content, but it has always been part of the
        # key; pin it so existing cache entries stay reachable
        "timeout": DEFAULT_SCRAPE_TIMEOUT,
    }
    key = cache_key(url, cache_opts)

    # Check cache first (unless --no-cache); ignore entries that are junk
    result = None
    from_cache = False
    if not no_cache:
        cached = read_cache("scrape", key)
        if cached and validate_result(cached)[0]:
            result = CachedResult(cached)
            from_cache = True
            console.print(f"[dim]Using cached result[/dim]")

    # Handle --cache-only
    if cache_only and not from_cache:
        console.print(f"[red]Not in cache: {url}[/red]")
        raise click.Abort()

    # Fetch from API if not cached
    if not from_cache:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task(f"Scraping {url}...", total=None)

            allow_jina = (
                not no_jina
                and bool(load_config().get("jina_fallback", True))
                and jina_fallback_supported(
                    formats=formats,
                    include_tags=include_tags,
                    exclude_tags=exclude_tags,
                    raw=raw,
                )
            )
            try:
                result = fetch_scrape_result(
                    url=url,
                    scrape_options=scrape_options,
                    timeout=timeout,
                    allow_jina=allow_jina,
                    max_wait=max_wait,
                )
                progress.stop()

                # Only validated results reach here, so this never caches junk
                write_cache("scrape", key, result_to_dict(result))

            except BlockedResult as e:
                progress.stop()
                console.print(f"[red]Error: {e}[/red]")
                if save_temp and output_path:
                    announce_saved_result(
                        path="", provider="none", url=url, fallback_url=None,
                        from_cache=False, status="blocked",
                    )
                raise click.Abort()
            except Exception as e:
                progress.stop()
                console.print(f"[red]Error: {e}[/red]")
                raise click.Abort()

    if result is None:
        console.print(f"[red]No scrape result available for {url}[/red]")
        raise click.Abort()

    # Handle output AFTER progress is done
    if len(formats) == 1:
        format_type = formats[0]
        if format_type == "markdown" and hasattr(result, "markdown"):
            content = getattr(result, "markdown", "") or ""
            # Article mode: apply post-processing cleanup
            if article:
                content = clean_article_content(content)
            # Strip links if requested
            if no_links:
                content = strip_links(content)
            # Prepend metadata header (like Jina) unless JSON output
            if not json_output:
                content = format_with_metadata(result, content)
        elif format_type == "html" and hasattr(result, "html"):
            content = getattr(result, "html", "") or ""
        elif format_type == "links":
            # Extract links from markdown content (client-side)
            md_content = getattr(result, "markdown", "") or ""
            content = extract_markdown_links(md_content)
        else:
            content = result
    else:
        content = {}
        if "markdown" in formats and hasattr(result, "markdown"):
            content["markdown"] = getattr(result, "markdown", "") or ""
        if "html" in formats and hasattr(result, "html"):
            content["html"] = getattr(result, "html", "") or ""
        if "links" in formats:
            # Extract links from markdown content (client-side)
            md_content = getattr(result, "markdown", "") or ""
            content["links"] = extract_markdown_links(md_content)
        if hasattr(result, "metadata"):
            metadata = getattr(result, "metadata", None)
            content["metadata"] = (
                metadata.__dict__
                if metadata is not None and hasattr(metadata, "__dict__")
                else metadata
            )
        if hasattr(result, "source_provider") and getattr(
            result, "source_provider", None
        ):
            content["source_provider"] = getattr(result, "source_provider")
        if hasattr(result, "fallback_url") and getattr(result, "fallback_url", None):
            content["fallback_url"] = getattr(result, "fallback_url")

    handle_output(
        content,
        output_file=output_path,
        copy=copy,
        json_output=json_output,
        pretty=pretty,
        format_type=formats[0] if len(formats) == 1 else "json",
        display_output=not save_temp,
        announce_saved=not save_temp,
    )

    if save_temp and output_path:
        announce_saved_result(
            path=output_path,
            provider=getattr(result, "source_provider", "unknown"),
            url=url,
            fallback_url=getattr(result, "fallback_url", None),
            from_cache=from_cache,
        )
