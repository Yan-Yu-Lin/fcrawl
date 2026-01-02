"""Google search command using Camoufox (anti-detection browser)"""

import click
import time
import random
import platform
from pathlib import Path
from rich.progress import Progress, SpinnerColumn, TextColumn
from typing import Optional
from urllib.parse import quote_plus

from ..utils.output import handle_output, console
from ..utils.cache import cache_key, read_cache, write_cache


def _get_profiles_dir() -> Path:
    """Get the directory where browser profiles are stored"""
    return Path.home() / ".fcrawl" / "profiles"


def _get_profile_dir(profile_name: str) -> Path:
    """Get the directory for a specific profile"""
    profiles_dir = _get_profiles_dir()
    profiles_dir.mkdir(parents=True, exist_ok=True)
    return profiles_dir / profile_name


def _list_profiles() -> list[str]:
    """List all available profiles"""
    profiles_dir = _get_profiles_dir()
    if not profiles_dir.exists():
        return []
    return [p.name for p in profiles_dir.iterdir() if p.is_dir()]


def _check_camoufox_installed() -> bool:
    """Check if Camoufox Python package is installed"""
    try:
        from camoufox.sync_api import Camoufox
        return True
    except ImportError:
        return False


def _check_camoufox_browser() -> bool:
    """Check if Camoufox browser binary is downloaded"""
    try:
        from pathlib import Path
        # Camoufox caches the browser in user's cache directory
        import sys
        if sys.platform == "darwin":
            cache_dir = Path.home() / "Library" / "Caches" / "camoufox"
            browser_path = cache_dir / "Camoufox.app" / "Contents" / "MacOS" / "camoufox"
        elif sys.platform == "win32":
            cache_dir = Path.home() / "AppData" / "Local" / "camoufox"
            browser_path = cache_dir / "camoufox" / "camoufox.exe"
        else:  # Linux
            cache_dir = Path.home() / ".cache" / "camoufox"
            browser_path = cache_dir / "camoufox" / "camoufox"
        return browser_path.exists()
    except Exception:
        return False


def _get_os_name() -> str:
    """Get OS name for Camoufox spoofing"""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    else:
        return "linux"


def _extract_results_from_page(page) -> list[dict]:
    """Extract search results from current page"""
    results = []

    # Google's result structure: div.yuRUbf contains each result
    result_elements = page.locator("div.yuRUbf").all()

    # Fallback to div.tF2Cxc if yuRUbf not found
    if not result_elements:
        result_elements = page.locator("div.tF2Cxc").all()

    for elem in result_elements:
        try:
            # Title (h3 inside the result)
            title_elem = elem.locator("h3").first
            title = title_elem.text_content() if title_elem.count() > 0 else ""

            # URL (first anchor link)
            link_elem = elem.locator("a").first
            url = link_elem.get_attribute("href") if link_elem.count() > 0 else ""

            # Description/snippet - look in parent or sibling elements
            description = ""
            parent = elem.locator("xpath=..").first
            if parent.count() > 0:
                for desc_selector in [
                    "div.VwiC3b",
                    "div[data-sncf]",
                    "span.st",
                    "div[style*='line-clamp']"
                ]:
                    desc_elem = parent.locator(desc_selector).first
                    if desc_elem.count() > 0:
                        description = desc_elem.text_content() or ""
                        break

            # Only add if we have a valid URL
            if url and url.startswith("http"):
                results.append({
                    "title": title.strip() if title else "",
                    "url": url,
                    "description": description.strip() if description else ""
                })
        except Exception:
            continue

    return results


def _google_search(query: str, limit: int, headless: bool, locale: Optional[str] = None) -> list[dict]:
    """Perform Google search using Camoufox with pagination support"""
    from camoufox.sync_api import Camoufox

    results = []
    seen_urls = set()  # Avoid duplicates across pages
    results_per_page = 10
    max_pages = (limit // results_per_page) + 1

    # Build Camoufox options
    camoufox_opts = {
        "headless": headless,
        "humanize": True,
        "block_images": True,
        "i_know_what_im_doing": True,
        "os": _get_os_name(),
    }
    if locale:
        camoufox_opts["locale"] = locale

    with Camoufox(**camoufox_opts) as browser:
        page = browser.new_page()

        # Build base Google search URL
        base_url = f"https://www.google.com/search?q={quote_plus(query)}"

        # Add locale parameters to URL (hl=language, gl=country)
        if locale:
            parts = locale.split("-")
            lang = parts[0]  # e.g., "ja" from "ja-JP"
            base_url += f"&hl={lang}"
            if len(parts) > 1:
                country = parts[1]  # e.g., "JP" from "ja-JP"
                base_url += f"&gl={country}"

        # Paginate through results
        for page_num in range(max_pages):
            if len(results) >= limit:
                break

            # Build URL with pagination (start=0, 10, 20, ...)
            start = page_num * results_per_page
            search_url = f"{base_url}&start={start}"

            page.goto(search_url, wait_until="domcontentloaded")

            # Small random delay to appear human
            time.sleep(random.uniform(0.5, 1.5))

            # Handle cookie consent popup (first page only typically)
            if page_num == 0:
                try:
                    for selector in [
                        "button:has-text('Accept all')",
                        "button:has-text('Accept')",
                        "button:has-text('I agree')",
                        "[aria-label='Accept all']",
                    ]:
                        consent = page.locator(selector).first
                        if consent.is_visible(timeout=1000):
                            consent.click()
                            time.sleep(0.5)
                            break
                except Exception:
                    pass

            # Extract results from this page
            page_results = _extract_results_from_page(page)

            # No more results available
            if not page_results:
                break

            # Add unique results
            for r in page_results:
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    results.append(r)
                    if len(results) >= limit:
                        break

            # Small delay between pages to appear human
            if page_num < max_pages - 1 and len(results) < limit:
                time.sleep(random.uniform(1.0, 2.0))

    return results[:limit]


@click.command()
@click.argument('query')
@click.option('--limit', '-l', type=int, default=10,
              help='Maximum number of results (default: 10)')
@click.option('--headful', is_flag=True,
              help='Show browser window (for debugging)')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--pretty/--no-pretty', default=True, help='Pretty print output')
@click.option('--no-cache', 'no_cache', is_flag=True, help='Bypass cache, force fresh fetch')
@click.option('--cache-only', 'cache_only', is_flag=True, help='Only read from cache, no search')
@click.option('--locale', '-L', default=None,
              help='Locale for regional results (e.g., ja-JP, en-GB, zh-TW)')
def gsearch(
    query: str,
    limit: int,
    headful: bool,
    output: Optional[str],
    json_output: bool,
    pretty: bool,
    no_cache: bool,
    cache_only: bool,
    locale: Optional[str],
):
    """Search Google directly using Camoufox (anti-detection browser)

    Bypasses SearXNG and uses a stealth Firefox browser to search Google
    without getting blocked by bot detection.

    \b
    First-time setup:
        python -m camoufox fetch    # Download the browser (~100MB)

    \b
    Examples:
        fcrawl gsearch "python tutorials"
        fcrawl gsearch "site:github.com firecrawl" -l 20
        fcrawl gsearch "Claude Code" --headful    # Debug mode
        fcrawl gsearch "AI news" -o results.md
        fcrawl gsearch "news" --locale ja-JP      # Japanese results
        fcrawl gsearch "restaurants" -L en-GB     # UK results
    """
    # Check if Camoufox is installed
    if not _check_camoufox_installed():
        console.print("[red]Camoufox is not installed.[/red]")
        console.print("Install with: [cyan]pip install camoufox[geoip][/cyan]")
        raise click.Abort()

    # Check if browser binary exists
    if not _check_camoufox_browser():
        console.print("[yellow]Camoufox browser not found.[/yellow]")
        console.print("Download with: [cyan]python -m camoufox fetch[/cyan]")
        raise click.Abort()

    # Generate cache key (include locale for different regional results)
    cache_opts = {'limit': limit, 'locale': locale}
    key = cache_key(query, cache_opts)

    # Check cache first (unless --no-cache)
    result = None
    from_cache = False
    if not no_cache:
        cached = read_cache('gsearch', key)
        if cached:
            result = cached
            from_cache = True
            console.print("[dim]Using cached result[/dim]")

    # Handle --cache-only
    if cache_only and not from_cache:
        console.print(f"[red]Not in cache: {query}[/red]")
        raise click.Abort()

    # Perform search if not cached
    if not from_cache:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task(f"Searching Google for '{query}'...", total=None)

            try:
                result = _google_search(query, limit, headless=not headful, locale=locale)
                progress.stop()

                # Write to cache
                write_cache('gsearch', key, result)

            except Exception as e:
                progress.stop()
                console.print(f"[red]Error: {e}[/red]")
                raise click.Abort()

    # Handle empty results
    if not result:
        console.print("[yellow]No results found[/yellow]")
        return

    console.print(f"[green]Found {len(result)} results[/green]")

    # Display results
    if pretty and not output and not json_output:
        _display_results(result)

    # Handle file/JSON output
    if output or json_output:
        handle_output(
            {'web': result},
            output_file=output,
            json_output=True,
            pretty=pretty,
            format_type='json'
        )
    elif not pretty:
        # Plain output for piping
        for r in result:
            print(r.get('url', ''))


def _display_results(results: list[dict]):
    """Display search results in formatted output"""
    console.print("\n[bold]Google Search Results[/bold]", justify="center")
    console.print("=" * 60)

    for i, r in enumerate(results, 1):
        title = r.get('title', 'No title')
        url = r.get('url', '')
        description = r.get('description', '')

        console.print(f"[bold cyan]## {title}[/bold cyan]")
        console.print(f"[blue]{url}[/blue]")
        if description:
            console.print(f"{description}")
        console.print()

    console.print("=" * 60)
