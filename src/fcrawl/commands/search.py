"""Search command powered by Serper.dev API."""

import os
import time
from typing import Optional

import click
import requests
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..utils.cache import cache_key, read_cache, write_cache
from ..utils.config import gateway_url, is_gateway, load_config
from ..utils.goto import goto_label, is_goto, resolve_via_gateway
from ..utils.output import console, handle_output, resolve_pretty


SERPER_ENDPOINT = "https://google.serper.dev/search"
SERPER_MAX_RESULTS_PER_PAGE = 100


def serper_target() -> tuple[str, str]:
    """(endpoint, api key). In gateway mode Serper is proxied at /gw/search with the family token."""
    config = load_config()
    if is_gateway(config):
        return f"{gateway_url(config)}/gw/search", str(config["gateway_token"])
    return SERPER_ENDPOINT, _get_serper_api_key()


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
    no_cache: bool = False,
    batches: Optional[int] = None,
) -> tuple[list[dict], float, Optional[str], int, str, str, dict]:
    """Search using Serper with pagination and deduplication.

    Returns (results, elapsed, error, pages, gl, hl, extras).
    extras contains knowledgeGraph, peopleAlsoAsk, relatedSearches from page 1.
    `batches` (gateway mode only): how many identical searches the gateway fires
    and merges; None = the gateway's default for this token.
    """
    gl, hl = _parse_locale(locale)
    endpoint, _ = serper_target()  # api_key already came from serper_target()
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    if no_cache:
        headers["X-No-Cache"] = "1"  # family gateway: skip its search cache too
    if batches is not None:
        headers["X-Search-Batches"] = str(batches)

    start = time.time()
    results: list[dict] = []
    seen_urls: set[str] = set()
    requests_made = 0
    page = 1
    extras: dict = {}

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
                endpoint,
                headers=headers,
                json=payload,
                timeout=30,
            )
            requests_made += 1

            if response.status_code != 200:
                elapsed = time.time() - start
                detail = response.text[:120]
                try:
                    detail = response.json().get("error") or detail  # gateway: "<code>: <why>"
                except ValueError:
                    pass
                if response.status_code == 429 and response.headers.get("Retry-After"):
                    detail += f" (retry after {response.headers['Retry-After']}s)"
                return (
                    [],
                    elapsed,
                    f"API error: {response.status_code} - {detail}",
                    requests_made,
                    gl,
                    hl,
                    {},
                )

            data = response.json()

            if page == 1:
                if data.get("knowledgeGraph"):
                    extras["knowledgeGraph"] = data["knowledgeGraph"]
                if data.get("peopleAlsoAsk"):
                    extras["peopleAlsoAsk"] = data["peopleAlsoAsk"]
                if data.get("relatedSearches"):
                    extras["relatedSearches"] = data["relatedSearches"]

            organic = data.get("organic", [])
            if not organic:
                break

            page_added = 0
            for item in organic:
                url = item.get("link", "")
                if not url or url in seen_urls:
                    continue

                seen_urls.add(url)
                result_item = {
                    "title": item.get("title", ""),
                    "url": url,
                    "description": item.get("snippet", ""),
                    "position": len(results) + 1,
                    "engines": ["google"],
                }
                if item.get("date"):
                    result_item["date"] = item["date"]
                if item.get("sitelinks"):
                    result_item["sitelinks"] = item["sitelinks"]
                if is_goto(url) or item.get("gwGoto"):
                    # a Google goto ticket: opaque here, the gateway resolves it on use
                    result_item["goto"] = True
                if item.get("displayedLink"):
                    # Google's visible breadcrumb ("https://host › a › b"), untouched by goto
                    result_item["displayed"] = item["displayedLink"]
                if item.get("gwBatch"):
                    result_item["batch"] = item["gwBatch"]

                results.append(result_item)
                page_added += 1

                if len(results) >= limit:
                    break

            if page_added == 0:
                break

            page += 1

        elapsed = time.time() - start
        return results[:limit], elapsed, None, requests_made, gl, hl, extras

    except requests.RequestException as e:
        elapsed = time.time() - start
        return [], elapsed, f"Request failed: {str(e)}", requests_made, gl, hl, {}


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


def shown_url(item: dict) -> str:
    """The URL line of a result: the real URL, or a [goto #k] marker for a ticket.

    Tickets are valid URLs (fcrawl scrape <ticket> works, the gateway resolves it),
    but they are long, unique per search and say nothing about the page, so the
    listing shows a marker; --resolve or --json gives the ticket itself.
    """
    if item.get("goto"):
        if item.get("resolved"):
            return item["resolved"]
        return goto_label(item.get("position", 0), displayed=item.get("displayed"),
                          pending=item.get("goto_pending", False))
    return item.get("url", "")


def _display_results(results: list[dict]):
    """Display search results in pretty mode."""
    console.print("\n[bold]Search Results[/bold]", justify="center")
    console.print("=" * 60)

    for item in results:
        title = item.get("title", "No title")
        url = shown_url(item)
        description = item.get("description", "")

        console.print(f"## {title}", style="bold cyan", markup=False)
        # markup off: "[goto]" / "[goto #3]" would otherwise be eaten as Rich tags
        console.print(url, style="blue", markup=False)
        if description:
            console.print(description, markup=False)
        console.print()

    console.print("=" * 60)


def resolve_results(results: list[dict], wait: bool = False) -> int:
    """Ask the gateway for the real URL behind every goto ticket; mark the rest pending.
    Returns how many were resolved."""
    tickets = [r["url"] for r in results if r.get("goto") and not r.get("resolved")]
    if not tickets:
        return 0
    answers = resolve_via_gateway(tickets, wait=wait)
    n = 0
    for r in results:
        if not r.get("goto"):
            continue
        a = answers.get(r["url"]) or {}
        if a.get("resolved"):
            r["resolved"] = a["resolved"]
            n += 1
        else:
            r["goto_pending"] = True
    return n


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
@click.option("--urls-only", is_flag=True, help="Only output URLs (no titles or snippets)")
@click.option("--single", is_flag=True, help="Gateway: one Serper search instead of the default two merged batches")
@click.option("--batches", type=click.IntRange(1, 3), default=None,
              help="Gateway: number of identical Serper searches to merge (1-3; default: the gateway's)")
@click.option("--resolve", "resolve_goto", is_flag=True,
              help="Gateway: ask it for the real URL behind each [goto] ticket after the search")
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
    urls_only: bool,
    single: bool,
    batches: Optional[int],
    resolve_goto: bool,
):
    """Search the web using Serper.dev (Google API).

    Google wraps most results after the first few in "goto" tickets; they show as
    [goto #k]. `fcrawl scrape` accepts a ticket directly (the gateway resolves it on
    use), `--resolve` asks for the real URLs now, `fcrawl resolve` does it later.
    """
    pretty = resolve_pretty(pretty)

    if limit < 1:
        raise click.BadParameter("limit must be >= 1", param_hint="--limit")
    if single and batches not in (None, 1):
        raise click.BadParameter("--single means --batches 1", param_hint="--batches")
    if single:
        batches = 1
    if batches is not None and not is_gateway():
        console.print("[dim]--single/--batches only apply in gateway mode; ignored[/dim]")
        batches = None

    gl, hl = _parse_locale(locale)
    cache_opts = {
        "engine": "serper",
        "limit": limit,
        "gl": gl,
        "hl": hl,
        "location": location,
    }
    if batches is not None:
        cache_opts["batches"] = batches
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
        _, api_key = serper_target()
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
            results, elapsed, error, pages, gl, hl, extras = _serper_search(
                query=query,
                limit=limit,
                locale=locale,
                location=location,
                api_key=api_key,
                no_cache=no_cache,
                batches=batches,
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
            "extras": extras,
        }
        write_cache("search", key, cached_data)

    cache_data = cached_data or {}
    results = cache_data.get("results", [])
    extras = cache_data.get("extras", {})
    elapsed = float(cache_data.get("elapsed", 0.0))
    pages = int(cache_data.get("pages", 0))
    gl = str(cache_data.get("gl", gl))
    hl = str(cache_data.get("hl", hl))

    if not results:
        console.print("[yellow]No results found[/yellow]")
        return

    goto_count = sum(1 for r in results if r.get("goto"))
    if resolve_goto and goto_count:
        if not is_gateway():
            console.print("[yellow]--resolve needs gateway mode; tickets left as they are[/yellow]")
        else:
            try:
                n = resolve_results(results)
            except RuntimeError as exc:
                console.print(f"[yellow]resolve failed: {exc}[/yellow]")
                n = 0
            console.print(f"[dim]Resolved {n}/{goto_count} goto ticket(s)[/dim]")

    found = f"Found {len(results)} results"
    if goto_count:
        found += f" ({goto_count} goto ticket(s): scrape them directly, or use --resolve / fcrawl resolve)"
    console.print(f"[green]{found}[/green]")

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
        if extras:
            output_data["extras"] = extras
        handle_output(
            output_data,
            output_file=output,
            json_output=True,
            pretty=pretty,
            format_type="json",
        )
    elif not pretty:
        if urls_only:
            for item in results:
                # tickets stay as URLs here: this output feeds scrape, which resolves them
                print(item.get("resolved") or item.get("url", ""))
        else:
            for item in results:
                url = shown_url(item)
                title = item.get("title", "")
                desc = item.get("description", "")
                print(url)
                if title:
                    print(f"  {title}")
                if desc:
                    print(f"  {desc}")
                if item.get("goto") and not item.get("resolved"):
                    print(f"  ticket: {item['url']}")
