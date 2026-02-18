"""Search command powered by Serper.dev API."""

import os
import time
from typing import Optional

import click
import requests
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..utils.cache import cache_key, read_cache, write_cache
from ..utils.config import load_config
from ..utils.output import console, handle_output, resolve_pretty


SERPER_ENDPOINT = "https://google.serper.dev/search"
SERPER_MAX_RESULTS_PER_PAGE = 100


def _get_serper_api_key() -> str:
    """Get Serper API key from env var or config file."""
    if os.environ.get("SERPER_API_KEY"):
        return os.environ["SERPER_API_KEY"]

    config = load_config()
    return config.get("serper_api_key", "")


def _parse_locale(locale: Optional[str]) -> tuple[str, str]:
    """Parse locale string into (gl, hl) for Serper."""
    if not locale:
        return "us", "en"

    parts = locale.replace("_", "-").split("-")
    lang = (parts[0] or "en").lower()
    gl = "us"

    if len(parts) > 1:
        region = (parts[1] or "").lower()
        if len(region) == 2 and region.isalpha():
            # Standard two-letter country codes pass through (hk->hk, mo->mo, tw->tw)
            gl = region
        elif region == "hant":
            gl = "tw"
        elif region == "hans":
            gl = "cn"

    return gl, lang


def _serper_search(
    query: str,
    limit: int,
    locale: Optional[str],
    location: Optional[str],
    api_key: str,
) -> tuple[list[dict], float, Optional[str], int, str, str]:
    """Search using Serper with pagination and deduplication."""
    gl, hl = _parse_locale(locale)
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    start = time.time()
    results: list[dict] = []
    seen_urls: set[str] = set()
    requests_made = 0
    page = 1

    try:
        while len(results) < limit:
            batch_size = min(SERPER_MAX_RESULTS_PER_PAGE, limit - len(results))
            payload = {
                "q": query,
                "gl": gl,
                "hl": hl,
                "num": batch_size,
                "page": page,
            }
            if location:
                payload["location"] = location

            response = requests.post(
                SERPER_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=30,
            )
            requests_made += 1

            if response.status_code != 200:
                elapsed = time.time() - start
                return (
                    [],
                    elapsed,
                    f"API error: {response.status_code} - {response.text[:120]}",
                    requests_made,
                    gl,
                    hl,
                )

            data = response.json()
            organic = data.get("organic", [])
            if not organic:
                break

            page_added = 0
            for item in organic:
                url = item.get("link", "")
                if not url or url in seen_urls:
                    continue

                seen_urls.add(url)
                results.append(
                    {
                        "title": item.get("title", ""),
                        "url": url,
                        "description": item.get("snippet", ""),
                        "position": len(results) + 1,
                        "engines": ["google"],
                    }
                )
                page_added += 1

                if len(results) >= limit:
                    break

            if page_added == 0:
                break

            page += 1

        elapsed = time.time() - start
        return results[:limit], elapsed, None, requests_made, gl, hl

    except requests.RequestException as e:
        elapsed = time.time() - start
        return [], elapsed, f"Request failed: {str(e)}", requests_made, gl, hl


def _display_debug_info(
    result_count: int,
    elapsed: float,
    gl: str,
    hl: str,
    pages: int,
    from_cache: bool,
    location: Optional[str],
):
    """Display debug information about Serper request handling."""
    console.print("\n[bold cyan]Search Debug:[/bold cyan]")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Field")
    table.add_column("Value")

    table.add_row("Provider", "Serper (Google)")
    table.add_row("Source", "Cache" if from_cache else "Network")
    table.add_row("Results", str(result_count))
    table.add_row("Response Time", f"{elapsed * 1000:.0f}ms")
    table.add_row("Pages Used", str(pages))
    table.add_row("Country (gl)", gl)
    table.add_row("Language (hl)", hl)
    table.add_row("Location", location or "(none)")

    console.print(table)


def _display_results(results: list[dict]):
    """Display search results in pretty mode."""
    console.print("\n[bold]Search Results[/bold]", justify="center")
    console.print("=" * 60)

    for item in results:
        title = item.get("title", "No title")
        url = item.get("url", "")
        description = item.get("description", "")

        console.print(f"[bold cyan]## {title}[/bold cyan]")
        console.print(f"[blue]{url}[/blue]")
        if description:
            console.print(description)
        console.print()

    console.print("=" * 60)


@click.command()
@click.argument("query")
@click.option(
    "--limit",
    "-l",
    type=int,
    default=20,
    help="Maximum number of results (default: 20)",
)
@click.option(
    "--locale",
    "-L",
    default=None,
    help="Locale for regional results (e.g., ja-JP, en-GB, zh-TW)",
)
@click.option(
    "--location", default=None, help="Geographic location (e.g., 'Taipei, Taiwan')"
)
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--pretty/--no-pretty", default=None, help="Pretty print output")
@click.option(
    "--no-cache", "no_cache", is_flag=True, help="Bypass cache, force fresh fetch"
)
@click.option(
    "--cache-only", "cache_only", is_flag=True, help="Only read from cache, no search"
)
@click.option("--debug", is_flag=True, help="Show search provider stats")
def search(
    query: str,
    limit: int,
    locale: Optional[str],
    location: Optional[str],
    output: Optional[str],
    json_output: bool,
    pretty: Optional[bool],
    no_cache: bool,
    cache_only: bool,
    debug: bool,
):
    """Search the web using Serper.dev (Google API)."""
    pretty = resolve_pretty(pretty)

    if limit < 1:
        raise click.BadParameter("limit must be >= 1", param_hint="--limit")

    gl, hl = _parse_locale(locale)
    cache_opts = {
        "engine": "serper",
        "limit": limit,
        "gl": gl,
        "hl": hl,
        "location": location,
    }
    key = cache_key(query, cache_opts)

    cached_data = None
    from_cache = False
    if not no_cache:
        cached = read_cache("search", key)
        if cached:
            cached_data = cached
            from_cache = True
            console.print("[dim]Using cached result[/dim]")

    if cache_only and not from_cache:
        console.print(f"[red]Not in cache: {query}[/red]")
        raise click.Abort()

    if not from_cache:
        api_key = _get_serper_api_key()
        if not api_key:
            console.print("[red]SERPER_API_KEY environment variable not set.[/red]")
            console.print("Get your API key at: [cyan]https://serper.dev[/cyan]")
            console.print("Then: [cyan]export SERPER_API_KEY='your_key'[/cyan]")
            raise click.Abort()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task(f"Searching '{query}'...", total=None)
            results, elapsed, error, pages, gl, hl = _serper_search(
                query=query,
                limit=limit,
                locale=locale,
                location=location,
                api_key=api_key,
            )

        if error:
            console.print(f"[red]Error: {error}[/red]")
            raise click.Abort()

        cached_data = {
            "results": results,
            "elapsed": elapsed,
            "engine": "serper",
            "gl": gl,
            "hl": hl,
            "pages": pages,
            "location": location,
        }
        write_cache("search", key, cached_data)

    cache_data = cached_data or {}
    results = cache_data.get("results", [])
    elapsed = float(cache_data.get("elapsed", 0.0))
    pages = int(cache_data.get("pages", 0))
    gl = str(cache_data.get("gl", gl))
    hl = str(cache_data.get("hl", hl))

    if not results:
        console.print("[yellow]No results found[/yellow]")
        return

    console.print(f"[green]Found {len(results)} results[/green]")

    if debug:
        _display_debug_info(
            result_count=len(results),
            elapsed=elapsed,
            gl=gl,
            hl=hl,
            pages=pages,
            from_cache=from_cache,
            location=location,
        )

    if pretty and not output and not json_output:
        _display_results(results)

    if output or json_output:
        output_data = {
            "query": query,
            "engine": "serper",
            "results": results,
            "meta": {
                "gl": gl,
                "hl": hl,
                "pages": pages,
                "location": location,
                "from_cache": from_cache,
            },
        }
        handle_output(
            output_data,
            output_file=output,
            json_output=True,
            pretty=pretty,
            format_type="json",
        )
    elif not pretty:
        for item in results:
            print(item.get("url", ""))
