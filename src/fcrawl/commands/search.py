"""Search command powered by Serper.dev API."""

import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import click
import requests
from ..utils.output import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..utils.cache import cache_key, read_cache, write_cache
from ..utils.config import gateway_url, is_gateway, load_config
from ..utils.goto import goto_label, is_goto, is_short_id, resolve_via_gateway
from ..utils.output import console, handle_output, resolve_pretty


SERPER_ENDPOINT = "https://google.serper.dev/search"
SERPER_NEWS_ENDPOINT = "https://google.serper.dev/news"
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


# Han, Hiragana/Katakana, Hangul, CJK punctuation/fullwidth: a query written in one of
# these is almost always a Taiwanese user's query; Google ranks local sources better
# with gl=tw and shows dates in the snippet with hl=zh-tw
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _parse_locale(locale: Optional[str], query: str = "") -> tuple[str, str, bool]:
    """(gl, hl, auto) for Serper. `hl` keeps the region when the tag has one
    ("zh-TW" -> hl=zh-tw, gl=tw) because Google's own code for Traditional Chinese is
    zh-TW, not zh. With no locale a CJK query defaults to Taiwan (auto=True)."""
    if not locale:
        if has_cjk(query):
            return "tw", "zh-tw", True
        return "us", "en", False

    parts = locale.replace("_", "-").split("-")
    lang = (parts[0] or "en").lower()
    gl = "us"
    hl = lang

    if len(parts) > 1:
        region = (parts[1] or "").lower()
        if len(region) == 2 and region.isalpha():
            # Standard two-letter country codes pass through (hk->hk, mo->mo, tw->tw)
            gl = region
            hl = f"{lang}-{region}"
        elif region == "hant":
            gl, hl = "tw", f"{lang}-tw"
        elif region == "hans":
            gl, hl = "cn", f"{lang}-cn"

    return gl, hl, False


_SINCE_RE = re.compile(r"^(\d{1,3})([dwmy])$")


def since_to_tbs(since: Optional[str]) -> Optional[str]:
    """--since 7d / 2w / 3m / 1y -> Serper tbs. Exact Google buckets (qdr:d/w/m/y) when the
    number is 1, else a custom date range (cdr) from that many days/weeks/months ago."""
    if not since:
        return None
    m = _SINCE_RE.match(since.strip().lower())
    if not m:
        raise click.BadParameter("use Nd / Nw / Nm / Ny, e.g. 7d or 2w", param_hint="--since")
    n, unit = int(m.group(1)), m.group(2)
    if n < 1:
        raise click.BadParameter("must be at least 1", param_hint="--since")
    if n == 1:
        return f"qdr:{unit}"
    days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * n
    start = date.today() - timedelta(days=days)
    return f"cdr:1,cd_min:{start.month}/{start.day}/{start.year},cd_max:"


_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%Y年%m月%d日")


def iso_date(raw: Optional[str]) -> Optional[str]:
    """Serper's date ("Jun 11, 2026", "2026年6月11日", "3 days ago") as ISO when it is an
    absolute date; relative ones are kept as given."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def _serper_search(
    query: str,
    limit: int,
    locale: Optional[str],
    location: Optional[str],
    api_key: str,
    no_cache: bool = False,
    batches: Optional[int] = None,
    tbs: Optional[str] = None,
    news: bool = False,
    engine: str = "serper",
) -> tuple[list[dict], float, Optional[str], int, str, str, dict]:
    """Search using Serper with pagination and deduplication.

    Returns (results, elapsed, error, pages, gl, hl, extras).
    extras contains knowledgeGraph, peopleAlsoAsk, relatedSearches from page 1,
    and `gw` (gateway mode): which engine answered and why.
    `batches` (gateway mode only): how many identical searches the gateway fires
    and merges; None = the gateway's default for this token.
    """
    gl, hl, _ = _parse_locale(locale, query)
    endpoint, _ = serper_target()  # api_key already came from serper_target()
    if news and not is_gateway():
        endpoint = SERPER_NEWS_ENDPOINT
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
            if tbs:
                payload["tbs"] = tbs
            if news:
                payload["type"] = "news"  # gateway: Serper /news; direct: endpoint above
            if engine != "serper":
                payload["engine"] = engine  # gateway only; Serper ignores unknown fields

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

            gw = data.get("gw") if isinstance(data.get("gw"), dict) else {}
            # the engine that actually answered THIS page: gwEngine (round-2 gateway; a
            # cache hit says which engine filled it), else gwProvider (older gateway:
            # always Serper). Never the flag we sent.
            answered_by = gw.get("gwEngine") or ("serper" if gw.get("gwProvider") in ("serper", "cache") else None)
            modern = "gwEngine" in gw  # a gateway that understands engine/type at all
            if page == 1:
                if data.get("knowledgeGraph"):
                    extras["knowledgeGraph"] = data["knowledgeGraph"]
                if data.get("peopleAlsoAsk"):
                    extras["peopleAlsoAsk"] = data["peopleAlsoAsk"]
                if data.get("relatedSearches"):
                    extras["relatedSearches"] = data["relatedSearches"]
                if gw:
                    extras["gw"] = {k: v for k, v in gw.items() if k in ("gwEngine", "gwSerperError", "gwProvider")}
                if is_gateway() and gw:
                    if engine == "firecrawl" and not modern:
                        return [], time.time() - start, ("this gateway does not support --engine firecrawl yet "
                                                         "(it answered with Serper); update the gateway"), requests_made, gl, hl, {}
                    sp = data.get("searchParameters") if isinstance(data.get("searchParameters"), dict) else {}
                    served_type = gw.get("gwType") or sp.get("type") or ("news" if data.get("news") else None)
                    if news and served_type != "news":
                        return [], time.time() - start, ("this gateway does not support --news yet "
                                                         "(it answered with web results); update the gateway"), requests_made, gl, hl, {}
                if answered_by:
                    extras.setdefault("gw", {})["gwEngine"] = answered_by
            # tickets on this page expire when the gateway says (gwGotoExpiresAt; an older
            # gateway gives only gwFetchedAt: + its 2 h registry TTL). The listing keeps the
            # EARLIEST across pages; a ticket page with no usable stamp makes the whole
            # listing uncacheable (None, never "now + 2 h")
            if gw.get("gwGoto") or any(is_goto((it or {}).get("link")) for it in data.get("organic") or [] if isinstance(it, dict)):
                exp = page_goto_expiry(gw)
                extras["goto_expires_at"] = min_expiry(extras.get("goto_expires_at", "unset"), exp)

            organic = data.get("organic") or (data.get("news") if news else None) or []
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
                if item.get("source") and news:
                    result_item["source"] = item["source"]
                if item.get("sitelinks"):
                    result_item["sitelinks"] = item["sitelinks"]
                if is_goto(url) or item.get("gwGoto"):
                    # a Google goto ticket: opaque here, the gateway resolves it on use
                    result_item["goto"] = True
                    if is_short_id(item.get("gotoId")):
                        # the gateway's short handle: what we show and what scrape accepts
                        result_item["gotoId"] = item["gotoId"]
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
            if answered_by == "firecrawl":
                # Firecrawl search has no pages: what it gave is all there is
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
    engine: str = "serper",
    auto_locale: bool = False,
    tbs: Optional[str] = None,
    gw: Optional[dict] = None,
):
    """Display debug information about Serper request handling."""
    console.print("\n[bold cyan]Search Debug:[/bold cyan]")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Field")
    table.add_column("Value")

    table.add_row("Provider", "Firecrawl search" if engine == "firecrawl" else "Serper (Google)")
    if gw and gw.get("gwSerperError"):
        table.add_row("Serper error", str(gw["gwSerperError"]))
    table.add_row("Source", "Cache" if from_cache else "Network")
    table.add_row("Results", str(result_count))
    table.add_row("Response Time", f"{elapsed * 1000:.0f}ms")
    table.add_row("Pages Used", str(pages))
    table.add_row("Country (gl)", gl + (" (auto: CJK query)" if auto_locale else ""))
    table.add_row("Language (hl)", hl + (" (auto: CJK query)" if auto_locale else ""))
    table.add_row("Location", location or "(none)")
    table.add_row("Time filter (tbs)", tbs or "(none)")

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


GOTO_LISTING_TTL = 2 * 3600  # the gateway's ticket registry TTL
EXPIRY_MARGIN = 300


def iso_ts(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def page_goto_expiry(gw: dict) -> Optional[str]:
    """When this page's tickets die, per the gateway: gwGotoExpiresAt, else gwFetchedAt +
    the registry TTL (older gateway). None when the gateway gave no usable stamp."""
    if iso_ts(gw.get("gwGotoExpiresAt")) is not None:
        return gw["gwGotoExpiresAt"]
    fetched = iso_ts(gw.get("gwFetchedAt"))
    if fetched is None:
        return None
    return datetime.fromtimestamp(fetched + GOTO_LISTING_TTL, tz=timezone.utc).isoformat(timespec="seconds")


def min_expiry(current: Any, new: Optional[str]) -> Optional[str]:
    """Earliest of two expiries; None (unknown) wins over anything; "unset" means no page yet."""
    if current == "unset":
        return new
    if current is None or new is None:
        return None
    return new if iso_ts(new) < iso_ts(current) else current


def listing_still_usable(cached: dict) -> bool:
    """A cached listing with goto tickets is only good while the gateway still knows the
    tickets (their g: ids 404 afterwards): until the gateway's own expiry stamp, less a
    margin. No stamp (an entry from before stamps, or a page the gateway did not date)
    means the age cannot be established: not served. Listings without tickets live the
    full cache TTL."""
    results = cached.get("results") or []
    if not any(isinstance(r, dict) and (r.get("goto") or is_goto(r.get("url"))) for r in results):
        return True
    exp = iso_ts(cached.get("goto_expires_at"))
    return exp is not None and exp - EXPIRY_MARGIN > time.time()


def goto_ref(item: dict) -> str:
    """What to hand to scrape/resolve for a goto result: the short id when the gateway
    gave one, else the full ticket (results cached before short ids)."""
    return item.get("gotoId") or item.get("url", "")


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
        shown_date = iso_date(item.get("date"))
        if shown_date:
            console.print(f"  · {shown_date}", style="dim", markup=False)
        if item.get("goto") and not item.get("resolved"):
            console.print(f"  → fcrawl scrape {goto_ref(item)}", style="dim", markup=False)
        console.print()

    console.print("=" * 60)


def resolve_results(results: list[dict], wait: bool = False) -> int:
    """Ask the gateway for the real URL behind every goto ticket; mark the rest pending.
    Returns how many were resolved."""
    refs = [goto_ref(r) for r in results if r.get("goto") and not r.get("resolved")]
    if not refs:
        return 0
    answers = resolve_via_gateway(refs, wait=wait)
    n = 0
    for r in results:
        if not r.get("goto") or r.get("resolved"):
            continue
        a = answers.get(goto_ref(r)) or {}
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
@click.option("--engine", type=click.Choice(["serper", "firecrawl"]), default="serper", show_default=True,
              help="Gateway: search engine. Serper (Google) is default and cheap; firecrawl costs 2 Firecrawl "
                   "credits per 10 results but returns real URLs, no goto tickets")
@click.option("--tbs", default=None, help="Google time filter: qdr:d (day), qdr:w, qdr:m, qdr:y")
@click.option("--since", default=None, help="Only results newer than Nd/Nw/Nm/Ny (e.g. 7d, 2w); sugar for --tbs")
@click.option("--news", is_flag=True, help="Google News results (Serper /news) instead of web results")
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
    engine: str,
    tbs: Optional[str],
    since: Optional[str],
    news: bool,
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
    if engine != "serper" and not is_gateway():
        console.print("[red]--engine firecrawl needs gateway mode (gateway_token in .fcrawlrc)[/red]")
        raise click.Abort()
    if tbs and since:
        raise click.BadParameter("use --tbs or --since, not both", param_hint="--since")
    if since:
        tbs = since_to_tbs(since)
    if tbs and not re.match(r"^[A-Za-z0-9:,/_-]{1,64}$", tbs):
        raise click.BadParameter("unexpected characters", param_hint="--tbs")

    gl, hl, auto_locale = _parse_locale(locale, query)
    cache_opts = {
        "engine": "serper",  # the gateway caches both engines under one key; so do we
        "limit": limit,
        "gl": gl,
        "hl": hl,
        "location": location,
    }
    if batches is not None:
        cache_opts["batches"] = batches
    if tbs:
        cache_opts["tbs"] = tbs
    if news:
        cache_opts["news"] = True
    key = cache_key(query, cache_opts)

    cached_data = None
    from_cache = False
    if not no_cache:
        cached = read_cache("search", key)
        if cached and listing_still_usable(cached):
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
                tbs=tbs,
                news=news,
                engine=engine,
            )

        if error:
            console.print(f"[red]Error: {error}[/red]")
            raise click.Abort()

        cached_data = {
            "results": results,
            "elapsed": elapsed,
            # tickets and their g: ids die with the gateway's registry: the gateway says
            # when (earliest across pages); a listing with tickets is not served from this
            # cache past that
            "goto_expires_at": extras.pop("goto_expires_at", None),
            "engine": (extras.get("gw") or {}).get("gwEngine") or engine,
            "gl": gl,
            "hl": hl,
            "pages": pages,
            "location": location,
            "tbs": tbs,
            "news": news,
            "extras": extras,
        }
        write_cache("search", key, cached_data)

    cache_data = cached_data or {}
    results = cache_data.get("results", [])
    for item in results:
        # rows cached before goto support carry the ticket but no flag
        if "goto" not in item and is_goto(item.get("url")):
            item["goto"] = True
    extras = cache_data.get("extras", {})
    elapsed = float(cache_data.get("elapsed", 0.0))
    pages = int(cache_data.get("pages", 0))
    gl = str(cache_data.get("gl", gl))
    hl = str(cache_data.get("hl", hl))
    used_engine = str(cache_data.get("engine") or "serper")
    gw_info = extras.get("gw") if isinstance(extras.get("gw"), dict) else {}

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
    if used_engine == "firecrawl":
        why = gw_info.get("gwSerperError")
        console.print(f"[dim]Engine: firecrawl{f' (Serper failed: {why})' if why else ''}[/dim]", markup=True,
                      highlight=False)

    if debug:
        _display_debug_info(
            result_count=len(results),
            elapsed=elapsed,
            gl=gl,
            hl=hl,
            pages=pages,
            from_cache=from_cache,
            location=location,
            engine=used_engine,
            auto_locale=auto_locale,
            tbs=tbs,
            gw=gw_info,
        )

    if pretty and not output and not json_output:
        _display_results(results)

    if output or json_output:
        output_data = {
            "query": query,
            "engine": used_engine,
            "results": results,
            "meta": {
                "gl": gl,
                "hl": hl,
                "auto_locale": auto_locale,
                "pages": pages,
                "location": location,
                "tbs": tbs,
                "news": news,
                "from_cache": from_cache,
                **({"serper_error": gw_info["gwSerperError"]} if gw_info.get("gwSerperError") else {}),
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
                # a goto result prints as its g:xxxxxx id (or the ticket when there is
                # none): this output feeds scrape, which resolves either
                if item.get("resolved"):
                    print(item["resolved"])
                elif item.get("goto"):
                    print(goto_ref(item))
                else:
                    print(item.get("url", ""))
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
                shown_date = iso_date(item.get("date"))
                if shown_date:
                    print(f"  · {shown_date}")
                if item.get("goto") and not item.get("resolved"):
                    print(f"  → fcrawl scrape {goto_ref(item)}")
