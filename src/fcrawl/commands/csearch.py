"""Multi-engine search command using Camoufox (anti-detection browser)"""

import click
import json
import time
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from ..utils.output import handle_output, console
from ..utils.cache import cache_key, read_cache, write_cache
from ..engines import get_engine, get_all_engines, ENGINES
from ..engines.base import SearchResult, EngineStatus
from ..engines.aggregator import aggregate_results, format_engines_badge, get_aggregation_stats


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


def _search_single_engine(
    engine_name: str,
    query: str,
    limit: int,
    headless: bool,
    locale: Optional[str]
) -> tuple[str, list[SearchResult], EngineStatus]:
    """Search a single engine and return results with status (creates own browser)"""
    engine_class = get_engine(engine_name)
    engine = engine_class()
    results, status = engine.search(query, limit, headless=headless, locale=locale)
    return engine_name, results, status


def _search_with_shared_browser(
    browser,
    engine_name: str,
    query: str,
    limit: int,
    locale: Optional[str]
) -> tuple[str, list[SearchResult], EngineStatus]:
    """
    Search using a shared browser with engine-specific context.
    Each call creates its own BrowserContext (isolated cookies, storage).
    Must be called from the same thread that created the browser.
    """
    engine_class = get_engine(engine_name)
    engine = engine_class()

    # Create context with engine-specific options (locale, headers)
    context_opts = engine.get_context_options(locale)
    context = browser.new_context(**context_opts)

    try:
        # Set up engine-specific cookies
        engine.setup_context(context, locale)

        # Create page and run search
        page = context.new_page()
        results, status = engine.search_with_page(page, query, limit, locale)
        return engine_name, results, status
    finally:
        # Always close the context to free resources
        context.close()


def _display_debug_info(statuses: list[EngineStatus], stats: dict, raw_count: int):
    """Display debug information about the search"""
    console.print("\n[bold cyan]Engine Status:[/bold cyan]")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Engine")
    table.add_column("Status")
    table.add_column("Results")
    table.add_column("Time")
    table.add_column("Error")

    for status in statuses:
        status_icon = "[green]OK[/green]" if status.success else "[red]FAIL[/red]"
        error_msg = status.error[:40] + "..." if status.error and len(status.error) > 40 else (status.error or "")
        table.add_row(
            status.engine,
            status_icon,
            str(status.result_count),
            f"{status.elapsed_time:.2f}s",
            error_msg
        )

    console.print(table)

    console.print("\n[bold cyan]Aggregation:[/bold cyan]")
    console.print(f"  Total raw results: {raw_count}")
    console.print(f"  After deduplication: {stats['total']}")

    if stats['by_engine_count']:
        console.print("\n[bold cyan]Results by source count:[/bold cyan]")
        for count in sorted(stats['by_engine_count'].keys(), reverse=True):
            num = stats['by_engine_count'][count]
            engine_word = "engine" if count == 1 else "engines"
            console.print(f"  Found by {count} {engine_word}: {num} results")


def _display_results(results: list[dict], show_engines: bool = True):
    """Display aggregated search results"""
    console.print("\n[bold]Search Results[/bold]", justify="center")
    console.print("=" * 60)

    for r in results:
        # Engine badge
        if show_engines and len(r.get('engines', [])) > 1:
            badge = format_engines_badge(r['engines'])
            console.print(f"[dim]{badge}[/dim]")

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
              help='Engine to use: google, bing, brave, or all (default: all)')
@click.option('--limit', '-l', type=int, default=20,
              help='Maximum number of results (default: 20)')
@click.option('--headful', is_flag=True,
              help='Show browser window (for debugging)')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--pretty/--no-pretty', default=True, help='Pretty print output')
@click.option('--no-cache', 'no_cache', is_flag=True, help='Bypass cache, force fresh fetch')
@click.option('--cache-only', 'cache_only', is_flag=True, help='Only read from cache, no search')
@click.option('--locale', '-L', default=None,
              help='Locale for regional results (e.g., ja-JP, en-GB, zh-TW)')
@click.option('--debug', is_flag=True, help='Show detailed engine status and stats')
@click.option('--parallel/--sequential', default=True,
              help='Run engines in parallel (default) or sequentially')
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
    """Multi-engine search using Camoufox browser

    Search across multiple engines (Google, Bing, Brave) with automatic
    result aggregation and deduplication. Uses anti-detection browser
    to avoid being blocked.

    \b
    Engines:
        google  - Google Search
        bing    - Microsoft Bing
        brave   - Brave Search
        all     - Query all engines and aggregate results

    \b
    First-time setup:
        python -m camoufox fetch    # Download the browser (~100MB)

    \b
    Examples:
        fcrawl csearch "python tutorials"              # All engines
        fcrawl csearch "python" -e google              # Google only
        fcrawl csearch "python" -e bing -l 30          # Bing, 30 results
        fcrawl csearch "python" --debug                # Show engine stats
        fcrawl csearch "python" --headful              # Debug: show browser
        fcrawl csearch "news" -L ja-JP                 # Japanese results
        fcrawl csearch "python" --sequential           # One engine at a time
    """
    # Check Camoufox installation
    if not _check_camoufox_installed():
        console.print("[red]Camoufox is not installed.[/red]")
        console.print("Install with: [cyan]pip install camoufox[geoip][/cyan]")
        raise click.Abort()

    if not _check_camoufox_browser():
        console.print("[yellow]Camoufox browser not found.[/yellow]")
        console.print("Download with: [cyan]python -m camoufox fetch[/cyan]")
        raise click.Abort()

    # Determine which engines to use
    engine = engine.lower()
    if engine == 'all':
        engines_to_use = get_all_engines()
    else:
        # Support comma-separated list
        engines_to_use = [e.strip() for e in engine.split(',')]
        # Validate engines
        for e in engines_to_use:
            if e not in ENGINES:
                console.print(f"[red]Unknown engine: {e}[/red]")
                console.print(f"Available engines: {', '.join(get_all_engines())}")
                raise click.Abort()

    # Calculate per-engine limit for aggregation mode
    per_engine_limit = limit if len(engines_to_use) == 1 else max(10, limit // len(engines_to_use) + 5)

    # Generate cache key
    cache_opts = {
        'engines': sorted(engines_to_use),
        'limit': limit,
        'locale': locale,
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
        all_results: list[SearchResult] = []
        all_statuses: list[EngineStatus] = []

        if debug:
            console.print(f"\n[bold]Querying engines: {', '.join(engines_to_use)}[/bold]\n")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console
        ) as progress:
            main_task = progress.add_task(
                f"Searching {len(engines_to_use)} engine(s)...",
                total=len(engines_to_use)
            )

            if parallel and len(engines_to_use) > 1:
                # Shared browser execution: ONE browser, sequential contexts
                # This saves ~67% of browser startup overhead vs spawning 3 separate browsers
                # Each engine gets its own isolated context (cookies, storage)
                from camoufox.sync_api import Camoufox

                # Get OS name from first engine
                first_engine = get_engine(engines_to_use[0])()
                os_name = first_engine.os_name

                shared_opts = {
                    "headless": not headful,
                    "humanize": True,
                    "block_images": True,
                    "i_know_what_im_doing": True,
                    "os": os_name,
                }

                try:
                    with Camoufox(**shared_opts) as browser:
                        for eng in engines_to_use:
                            try:
                                _, results, status = _search_with_shared_browser(
                                    browser, eng, query, per_engine_limit, locale
                                )
                                all_results.extend(results)
                                all_statuses.append(status)
                            except Exception as e:
                                all_statuses.append(EngineStatus(
                                    engine=eng,
                                    success=False,
                                    error=str(e)
                                ))
                            progress.advance(main_task)
                except Exception as e:
                    # Browser creation failed
                    for eng in engines_to_use:
                        all_statuses.append(EngineStatus(
                            engine=eng,
                            success=False,
                            error=f"Browser creation failed: {str(e)}"
                        ))
                        progress.advance(main_task)
            else:
                # Sequential execution
                for eng in engines_to_use:
                    try:
                        _, results, status = _search_single_engine(
                            eng, query, per_engine_limit, not headful, locale
                        )
                        all_results.extend(results)
                        all_statuses.append(status)
                    except Exception as e:
                        all_statuses.append(EngineStatus(
                            engine=eng,
                            success=False,
                            error=str(e)
                        ))
                    progress.advance(main_task)

        # Aggregate results
        raw_count = len(all_results)
        aggregated = aggregate_results(all_results, limit=limit)
        stats = get_aggregation_stats(aggregated)

        # Show debug info if requested
        if debug:
            _display_debug_info(all_statuses, stats, raw_count)

        # Prepare cache data
        cache_data = {
            'results': aggregated,
            'raw_count': raw_count,
            'stats': stats,
            'engines': engines_to_use,
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
    show_engines = len(engines_to_use) > 1
    if pretty and not output and not json_output:
        _display_results(results, show_engines=show_engines)

    # Handle file/JSON output
    if output or json_output:
        # Format for JSON output
        output_data = {
            'query': query,
            'engines': cached_results.get('engines', engines_to_use),
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
