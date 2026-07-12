"""Crawl command for fcrawl"""

import re
import click
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from rich.console import Console
from typing import Optional, List

from ..utils.config import get_firecrawl_client
from ..utils.output import console, strip_links
from ..utils.cache import cache_key, read_cache, write_cache, crawl_result_to_dict, CachedCrawlResult


def get_default_output_dir(url: str) -> Path:
    """Generate default output directory name: crawl-{domain}-{YYYY-MM-DD}"""
    domain = urlparse(url).netloc
    today = datetime.now().strftime('%Y-%m-%d')
    return Path(f"crawl-{domain}-{today}")


def sanitize_filename(url: str, max_length: int = 80) -> str:
    """Convert URL to safe filename"""
    # Remove protocol
    name = re.sub(r'^https?://', '', url)
    # Replace unsafe chars with underscore
    name = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name)
    # Remove trailing underscores
    name = name.strip('_')
    # Truncate if too long
    if len(name) > max_length:
        name = name[:max_length]
    return name + '.md'


def get_meta(page, *attrs):
    """Safely get metadata attributes from a page"""
    if not hasattr(page, 'metadata') or page.metadata is None:
        return None
    meta = page.metadata
    for attr in attrs:
        val = getattr(meta, attr, None)
        if val is not None:
            return val
    return None


def write_page_md(output_dir: Path, page, no_links: bool) -> str:
    """Write single page as markdown file with metadata header. Returns filename."""
    page_url = get_meta(page, 'sourceURL', 'url', 'source_url') or 'unknown'
    title = get_meta(page, 'title') or 'Untitled'

    filename = sanitize_filename(page_url)
    filepath = output_dir / filename

    # Build content with metadata header
    lines = []
    lines.append(f"Title: {title}")
    lines.append(f"URL: {page_url}")
    lines.append(f"Crawled: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Add markdown content
    if hasattr(page, 'markdown') and page.markdown:
        md = page.markdown
        if no_links:
            md = strip_links(md)
        lines.append(md)

    filepath.write_text('\n'.join(lines))
    return filename


def write_index_md(output_dir: Path, pages: list, crawl_url: str, depth: Optional[int], limit: int):
    """Generate index.md with page listing"""
    domain = urlparse(crawl_url).netloc

    lines = []
    lines.append(f"# Crawl: {domain}")
    lines.append(f"")
    lines.append(f"**Source:** {crawl_url}")
    lines.append(f"**Crawled:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Pages:** {len(pages)}")
    if depth:
        lines.append(f"**Depth:** {depth}")
    lines.append(f"**Limit:** {limit}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Pages")
    lines.append("")

    for page in pages:
        page_url = get_meta(page, 'sourceURL', 'url', 'source_url') or 'unknown'
        title = get_meta(page, 'title') or 'Untitled'
        filename = sanitize_filename(page_url)
        lines.append(f"- [{title}](./{filename})")

    filepath = output_dir / 'index.md'
    filepath.write_text('\n'.join(lines))


@click.command()
@click.argument('url')
@click.option('--limit', type=int, default=10, help='Maximum number of pages to crawl')
@click.option('--depth', type=int, help='Maximum crawl depth')
@click.option('-o', '--output', help='Output directory (default: crawl-{domain}-{date})')
@click.option('--include-paths', multiple=True, help='Only crawl URLs matching these patterns')
@click.option('--exclude-paths', multiple=True, help='Skip URLs matching these patterns')
@click.option('--no-links', is_flag=True, help='Strip markdown links from output')
@click.option('--poll-interval', type=int, default=2, help='Polling interval in seconds')
@click.option('--timeout', type=int, default=300, help='Timeout in seconds')
@click.option('--no-cache', 'no_cache', is_flag=True, help='Bypass cache, force fresh fetch')
@click.option('--cache-only', 'cache_only', is_flag=True, help='Only read from cache, no API call')
def crawl(
    url: str,
    limit: int,
    depth: Optional[int],
    output: Optional[str],
    include_paths: List[str],
    exclude_paths: List[str],
    no_links: bool,
    poll_interval: int,
    timeout: int,
    no_cache: bool,
    cache_only: bool,
):
    """Crawl a website and save pages as markdown files

    \b
    NOTE: Crawl relies on Firecrawl's link discovery, which may not work
    on all sites (especially JS-heavy or anti-bot protected sites).

    \b
    WORKAROUND if crawl doesn't discover links:
        1. fcrawl scrape URL -f links   # Get all links on page
        2. fcrawl scrape each link as needed

    \b
    Examples:
        fcrawl crawl https://blog.com --limit 10
        fcrawl crawl https://docs.site.com --depth 2
        fcrawl crawl https://site.com -o ./my-docs/
    """
    # Prepare crawl options
    crawl_options = {
        'limit': limit,
        'scrape_options': {
            'formats': ['markdown']  # Always markdown for MD file output
        },
        'poll_interval': poll_interval,
        'timeout': timeout
    }

    if depth:
        crawl_options['max_discovery_depth'] = depth

    if include_paths:
        crawl_options['include_paths'] = list(include_paths)

    if exclude_paths:
        crawl_options['exclude_paths'] = list(exclude_paths)

    # Generate cache key based on options that affect API result
    cache_opts = {
        'limit': limit,
        'depth': depth,
        'include_paths': list(include_paths) if include_paths else None,
        'exclude_paths': list(exclude_paths) if exclude_paths else None,
    }
    key = cache_key(url, cache_opts)

    # Check cache first (unless --no-cache)
    result = None
    from_cache = False
    if not no_cache:
        cached = read_cache('crawl', key)
        if cached:
            result = CachedCrawlResult(cached)
            from_cache = True
            console.print(f"[dim]Using cached result ({len(result.data)} pages)[/dim]")

    # Handle --cache-only
    if cache_only and not from_cache:
        console.print(f"[red]Not in cache: {url}[/red]")
        raise click.Abort()

    # Fetch from API if not cached
    if not from_cache:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            "[progress.percentage]{task.percentage:>3.0f}%",
            TimeRemainingColumn(),
            console=console
        ) as progress:
            task = progress.add_task(f"Crawling {url}...", total=limit)

            try:
                client = get_firecrawl_client()

                console.print(f"[cyan]Starting crawl of {url} (limit: {limit} pages)[/cyan]")

                # Start the crawl - this blocks until complete with poll_interval
                result = client.crawl(url, **crawl_options)

                progress.stop()

                # Write to cache
                write_cache('crawl', key, crawl_result_to_dict(result))

            except Exception as e:
                progress.stop()
                console.print(f"[red]Error: {e}[/red]")
                raise click.Abort()

    # Process results
    if not hasattr(result, 'data') or not result.data:
        console.print("[yellow]No pages crawled[/yellow]")
        return

    console.print(f"[green]✓ Crawled {len(result.data)} pages[/green]")

    # Determine output directory
    output_dir = Path(output) if output else get_default_output_dir(url)

    # Create directory (warn if exists)
    if output_dir.exists():
        console.print(f"[yellow]Directory exists, overwriting: {output_dir}[/yellow]")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write individual page files
    console.print(f"[cyan]Writing {len(result.data)} pages to {output_dir}/[/cyan]")

    for page in result.data:
        filename = write_page_md(output_dir, page, no_links)

    # Write index.md
    write_index_md(output_dir, result.data, url, depth, limit)

    # Summary
    console.print(f"[green]✓ Saved to {output_dir}/[/green]")
    console.print(f"  {len(result.data)} pages + index.md")
