"""Scrape command for fcrawl"""

import hashlib
import re
import time
import click
import requests
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from rich.progress import Progress, SpinnerColumn, TextColumn
from types import SimpleNamespace
from rich.console import Console
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from ..utils.config import get_firecrawl_client
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
DEFAULT_JINA_FALLBACK_DELAY = 5
MIN_JINA_FALLBACK_WINDOW = 2
AUTO_SAVE_DIR = Path("/tmp/fcrawl-saved")


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


def parse_jina_response(text: str, original_url: str) -> TaggedResult:
    """Parse Jina Reader output into the same shape as a scrape result."""
    metadata = {"source_url": original_url}
    content = text
    marker = "\nMarkdown Content:\n"
    if marker in text:
        header, content = text.split(marker, 1)
        for line in header.splitlines():
            if line.startswith("Title:"):
                metadata["title"] = line.split(":", 1)[1].strip()
            elif line.startswith("URL Source:"):
                metadata["source_url"] = line.split(":", 1)[1].strip()
            elif line.startswith("Published Time:"):
                metadata["published_time"] = line.split(":", 1)[1].strip()
    else:
        content = text.strip()

    result = SimpleNamespace(
        markdown=content.lstrip(),
        html=None,
        links=None,
        metadata=SimpleNamespace(**metadata),
    )
    return TaggedResult(
        result, provider="jina", fallback_url=f"https://r.jina.ai/{original_url}"
    )


def scrape_with_jina(url: str, timeout: int) -> TaggedResult:
    """Fetch markdown from Jina Reader."""
    fallback_url = f"https://r.jina.ai/{url}"
    response = requests.get(fallback_url, timeout=timeout)
    response.raise_for_status()
    result = parse_jina_response(response.text, url)
    result.fallback_url = fallback_url
    return result


def scrape_with_firecrawl(url: str, scrape_options: dict, timeout: int) -> TaggedResult:
    """Fetch content from Firecrawl with a bounded timeout."""
    client = get_firecrawl_client()
    result = client.scrape(url, timeout=timeout * 1000, **scrape_options)
    return TaggedResult(result, provider="firecrawl")


def fetch_scrape_result(url: str, scrape_options: dict, timeout: int, allow_jina: bool):
    """Run Firecrawl first, then Jina fallback if Firecrawl stalls or fails."""
    deadline = time.monotonic() + timeout
    fallback_delay = min(
        DEFAULT_JINA_FALLBACK_DELAY,
        max(1, timeout - MIN_JINA_FALLBACK_WINDOW),
    )
    executor = ThreadPoolExecutor(max_workers=2)
    firecrawl_future = executor.submit(
        scrape_with_firecrawl, url, scrape_options, timeout
    )
    pending = []
    errors = []

    try:
        done, _ = wait([firecrawl_future], timeout=fallback_delay)
        if firecrawl_future in done:
            try:
                return firecrawl_future.result()
            except Exception as exc:
                errors.append(("firecrawl", exc))
                if not allow_jina:
                    raise
                jina_timeout = max(1, int(deadline - time.monotonic()))
                pending = [executor.submit(scrape_with_jina, url, jina_timeout)]
        else:
            if not allow_jina:
                remaining = max(1, int(deadline - time.monotonic()))
                return firecrawl_future.result(timeout=remaining)
            jina_timeout = max(1, int(deadline - time.monotonic()))
            jina_future = executor.submit(scrape_with_jina, url, jina_timeout)
            pending = [firecrawl_future, jina_future]

        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            done, not_done = wait(
                pending, timeout=remaining, return_when=FIRST_COMPLETED
            )
            if not done:
                break

            pending = list(not_done)
            for future in done:
                try:
                    return future.result()
                except Exception as exc:
                    provider = "jina" if future is not firecrawl_future else "firecrawl"
                    errors.append((provider, exc))

        if errors:
            details = "; ".join(f"{provider}: {exc}" for provider, exc in errors)
            raise RuntimeError(f"All scrape attempts failed or timed out ({details})")
        raise RuntimeError(f"Scrape timed out after {timeout}s")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def announce_saved_result(
    path: str, provider: str, url: str, fallback_url: Optional[str], from_cache: bool
):
    """Emit a concise, parseable manifest line for saved results."""
    parts = [f"path={path}", f"provider={provider}", f"url={url}"]
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
        "timeout": timeout,
    }
    key = cache_key(url, cache_opts)

    # Check cache first (unless --no-cache)
    result = None
    from_cache = False
    if not no_cache:
        cached = read_cache("scrape", key)
        if cached:
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

            try:
                result = fetch_scrape_result(
                    url=url,
                    scrape_options=scrape_options,
                    timeout=timeout,
                    allow_jina=jina_fallback_supported(
                        formats=formats,
                        include_tags=include_tags,
                        exclude_tags=exclude_tags,
                        raw=raw,
                    ),
                )
                progress.stop()

                # Write to cache
                write_cache("scrape", key, result_to_dict(result))

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
