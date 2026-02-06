"""Fast Google search using Serper.dev API"""

import os
import click
import time
import requests
from typing import Optional

from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..utils.output import handle_output, console
from ..utils.cache import cache_key, read_cache, write_cache
from ..utils.config import load_config


SERPER_ENDPOINT = "https://google.serper.dev/search"


def _get_serper_api_key() -> str:
    """Get Serper API key from env var or config file"""
    # Env var takes priority
    if os.environ.get("SERPER_API_KEY"):
        return os.environ["SERPER_API_KEY"]
    # Fall back to config file
    config = load_config()
    return config.get("serper_api_key", "")


def _parse_locale(locale: Optional[str]) -> tuple[str, str]:
    """Parse locale string (e.g., 'ja-JP') into gl and hl parameters"""
    if not locale:
        return "us", "en"

    parts = locale.lower().split("-")
    hl = parts[0]  # Language code (e.g., "ja")
    gl = parts[1] if len(parts) > 1 else parts[0]  # Country code (e.g., "jp")
    return gl, hl


def _serper_search(
    query: str,
    limit: int,
    locale: Optional[str],
    api_key: str,
) -> tuple[list[dict], float, Optional[str]]:
    """
    Search using Serper API.
    Returns: (results, elapsed_time, error_message)
    """
    gl, hl = _parse_locale(locale)

    payload = {
        "q": query,
        "gl": gl,
        "hl": hl,
        "num": min(limit, 100),  # Serper max is 100
    }

    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    start = time.time()

    try:
        response = requests.post(
            SERPER_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=30,
        )
        elapsed = time.time() - start

        if response.status_code != 200:
            return [], elapsed, f"API error: {response.status_code} - {response.text[:100]}"

        data = response.json()

        # Convert Serper response to our format
        results = []
        for item in data.get("organic", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "description": item.get("snippet", ""),
                "position": item.get("position", 0),
                "engines": ["google"],  # Serper = Google only
            })

        return results[:limit], elapsed, None

    except requests.RequestException as e:
        elapsed = time.time() - start
        return [], elapsed, f"Request failed: {str(e)}"


def _display_debug_info(elapsed: float, result_count: int, locale: Optional[str]):
    """Display debug information about the search"""
    gl, hl = _parse_locale(locale)

    console.print("\n[bold cyan]Serper API Status:[/bold cyan]")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Field")
    table.add_column("Value")

    table.add_row("Engine", "Google (via Serper)")
    table.add_row("Status", "[green]OK[/green]")
    table.add_row("Results", str(result_count))
    table.add_row("Response Time", f"{elapsed*1000:.0f}ms")
    table.add_row("Country (gl)", gl)
    table.add_row("Language (hl)", hl)

    console.print(table)


def _display_results(results: list[dict]):
    """Display search results"""
    console.print("\n[bold]Search Results[/bold]", justify="center")
    console.print("=" * 60)

    for r in results:
        title = r.get('title', 'No title')
        url = r.get('url', '')
        description = r.get('description', '')

        console.print(f"[bold cyan]## {title}[/bold cyan]")
        console.print(f"[blue]{url}[/blue]")
        if description:
            console.print(f"{description}")
        console.print()

    console.print("=" * 60)


@click.command()
@click.argument('query')
@click.option('--engine', '-e', default='all',
              help='(Ignored) Serper uses Google only')
@click.option('--limit', '-l', type=int, default=20,
              help='Maximum number of results (default: 20)')
@click.option('--headful', is_flag=True, hidden=True,
              help='(Ignored) No browser used')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--pretty/--no-pretty', default=True, help='Pretty print output')
@click.option('--no-cache', 'no_cache', is_flag=True, help='Bypass cache, force fresh fetch')
@click.option('--cache-only', 'cache_only', is_flag=True, help='Only read from cache, no search')
@click.option('--locale', '-L', default=None,
              help='Locale for regional results (e.g., ja-JP, en-GB, zh-TW)')
@click.option('--debug', is_flag=True, help='Show detailed API status and stats')
@click.option('--parallel/--sequential', default=True, hidden=True,
              help='(Ignored) Single API call')
def csearch(
    query: str,
    engine: str,
    limit: int,
    headful: bool,
    output: Optional[str],
    json_output: bool,
    pretty: bool,
    no_cache: bool,
    cache_only: bool,
    locale: Optional[str],
    debug: bool,
    parallel: bool,
):
    """Fast Google search using Serper.dev API

    Lightning-fast Google search results (~1 second) via Serper API.
    Requires SERPER_API_KEY environment variable.

    \b
    Setup:
        export SERPER_API_KEY="your_api_key"

    \b
    Examples:
        fcrawl csearch "python tutorials"
        fcrawl csearch "python" -l 30
        fcrawl csearch "news" -L ja-JP              # Japanese results
        fcrawl csearch "restaurants" -L zh-TW       # Taiwan results
        fcrawl csearch "python" --debug             # Show API stats
    """
    # Check API key
    api_key = _get_serper_api_key()
    if not api_key:
        console.print("[red]SERPER_API_KEY environment variable not set.[/red]")
        console.print("Get your API key at: [cyan]https://serper.dev[/cyan]")
        console.print("Then: [cyan]export SERPER_API_KEY='your_key'[/cyan]")
        raise click.Abort()

    # Generate cache key
    gl, hl = _parse_locale(locale)
    cache_opts = {
        'engine': 'serper',
        'limit': limit,
        'gl': gl,
        'hl': hl,
    }
    key = cache_key(query, cache_opts)

    # Check cache
    cached_results = None
    from_cache = False
    if not no_cache:
        cached = read_cache('csearch', key)
        if cached:
            cached_results = cached
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
            progress.add_task(f"Searching '{query}'...", total=None)

            results, elapsed, error = _serper_search(query, limit, locale, api_key)

        if error:
            console.print(f"[red]Error: {error}[/red]")
            raise click.Abort()

        # Show debug info if requested
        if debug:
            _display_debug_info(elapsed, len(results), locale)

        # Prepare cache data
        cache_data = {
            'results': results,
            'elapsed': elapsed,
            'engine': 'serper',
        }
        write_cache('csearch', key, cache_data)
        cached_results = cache_data

    # Extract results from cache data
    results = cached_results.get('results', [])

    # Handle empty results
    if not results:
        console.print("[yellow]No results found[/yellow]")
        return

    console.print(f"[green]Found {len(results)} results[/green]")

    # Display results
    if pretty and not output and not json_output:
        _display_results(results)

    # Handle file/JSON output
    if output or json_output:
        output_data = {
            'query': query,
            'engine': 'serper',
            'results': results,
        }
        handle_output(
            output_data,
            output_file=output,
            json_output=True,
            pretty=pretty,
            format_type='json'
        )
    elif not pretty:
        # Plain output for piping
        for r in results:
            print(r.get('url', ''))
