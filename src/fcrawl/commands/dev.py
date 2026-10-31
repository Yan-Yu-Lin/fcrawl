"""Search Firecrawl's Developer Index (READMEs, issues, PRs, curated docs)."""

import os
import time
from typing import Optional

import click
import requests
from ..utils.output import Progress, SpinnerColumn, TextColumn

from ..utils.cache import cache_key, read_cache, write_cache
from ..utils.config import load_config
from ..utils.output import console, handle_output, resolve_pretty


# The Developer Index only exists on Firecrawl Cloud, never on a self-hosted instance.
DEV_ENDPOINT = "https://api.firecrawl.dev/v2/search/developer"
RESULT_TYPES = ["doc", "issue", "pull_request", "readme"]


def _get_cloud_api_key() -> Optional[str]:
    """Firecrawl Cloud key, if one is configured. The endpoint also works keyless."""
    key = os.environ.get("FIRECRAWL_API_KEY") or load_config().get("api_key")
    if key and key.startswith("fc-"):
        return key
    return None


def _result_kind(item: dict) -> str:
    """Artifact kind from the id prefix (doc:, issue:, pull_request:, readme:, web:)."""
    return item.get("type") or item.get("id", "").split(":", 1)[0] or "?"


def _dev_search(payload: dict, api_key: Optional[str]) -> tuple[dict, float, Optional[str]]:
    """POST to the developer endpoint. Retries keyless if the key is rejected."""
    start = time.time()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = requests.post(DEV_ENDPOINT, headers=headers, json=payload, timeout=60)
        if response.status_code == 401 and api_key:
            headers.pop("Authorization")
            response = requests.post(DEV_ENDPOINT, headers=headers, json=payload, timeout=60)
    except requests.RequestException as e:
        return {}, time.time() - start, f"Request failed: {e}"

    elapsed = time.time() - start
    if response.status_code != 200:
        return {}, elapsed, f"API error: {response.status_code} - {response.text[:300]}"

    data = response.json()
    if not data.get("success", False):
        return {}, elapsed, f"API error: {str(data)[:300]}"
    return data, elapsed, None


def _display_scope_echo(data: dict):
    """Warn about repos/sources that are not in the index."""
    for entry in data.get("repos", []):
        if not entry.get("indexed"):
            console.print(f"[yellow]Repo not indexed: {entry.get('repo')}[/yellow]")
    for entry in data.get("sources", []):
        if not entry.get("indexed"):
            console.print(f"[yellow]Doc source not indexed: {entry.get('source')}[/yellow]")
    coverage = data.get("coverage") or {}
    for kind, state in coverage.items():
        if state in ("degraded", "unavailable"):
            console.print(f"[yellow]Coverage for {kind}: {state}[/yellow]")


def _display_results(results: list[dict]):
    """Display results in pretty mode."""
    console.print("\n[bold]Developer Index Results[/bold]", justify="center")
    console.print("=" * 60)

    for item in results:
        title = item.get("title") or item.get("url", "")
        console.print(f"[bold cyan]## [{_result_kind(item)}] {title}[/bold cyan]")
        console.print(f"[blue]{item.get('url', '')}[/blue]")
        for passage in item.get("passages", []):
            text = passage.get("text", "").strip()
            if text:
                console.print(text, markup=False, highlight=False)
                console.print("[dim]---[/dim]")
        console.print()

    console.print("=" * 60)


def _print_plain(results: list[dict], urls_only: bool):
    """Plain output for pipes and agents."""
    if urls_only:
        for item in results:
            print(item.get("url", ""))
        return

    for item in results:
        print(f"[{_result_kind(item)}] {item.get('url', '')}")
        title = item.get("title")
        if title:
            print(f"  {title}")
        for passage in item.get("passages", []):
            text = passage.get("text", "").strip()
            if text:
                print("  > " + text.replace("\n", "\n  > "))
        print()


@click.command()
@click.argument("query")
@click.option("--limit", "-l", type=click.IntRange(1, 100), default=10, help="Number of results, 1-100 (default: 10)")
@click.option(
    "--type",
    "-t",
    "types",
    multiple=True,
    type=click.Choice(RESULT_TYPES),
    help="Only these kinds (repeatable): doc, issue, pull_request, readme",
)
@click.option("--repo", "-r", "repos", multiple=True, help="Scope issues/PRs/READMEs to a repo, e.g. astral-sh/uv (repeatable)")
@click.option("--source", "sources", multiple=True, help="Scope docs to a doc source id (repeatable, max 20)")
@click.option("--passages", "-p", type=click.IntRange(1, 5), default=1, help="Matched passages per result, 1-5 (default: 1)")
@click.option("--skills", is_flag=True, help="Only search indexed agent-skill files")
@click.option("--language", help="Repo language filter, e.g. Rust (drops doc results)")
@click.option("--topic", help="Repo topic filter, e.g. async (drops doc results)")
@click.option("--license", "license_", help="Repo license filter, e.g. MIT (drops doc results)")
@click.option("--min-stars", type=click.IntRange(min=0), help="Minimum repo stars (drops doc results)")
@click.option("--max-stars", type=click.IntRange(min=0), help="Maximum repo stars (drops doc results)")
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--pretty/--no-pretty", default=None, help="Pretty print output")
@click.option("--no-cache", "no_cache", is_flag=True, help="Bypass cache, force fresh fetch")
@click.option("--cache-only", "cache_only", is_flag=True, help="Only read from cache, no search")
@click.option("--urls-only", is_flag=True, help="Only output URLs")
def dev(
    query: str,
    limit: int,
    types: tuple[str, ...],
    repos: tuple[str, ...],
    sources: tuple[str, ...],
    passages: int,
    skills: bool,
    language: Optional[str],
    topic: Optional[str],
    license_: Optional[str],
    min_stars: Optional[int],
    max_stars: Optional[int],
    output: Optional[str],
    json_output: bool,
    pretty: Optional[bool],
    no_cache: bool,
    cache_only: bool,
    urls_only: bool,
):
    """Search Firecrawl's Developer Index: GitHub issues, merged PRs, READMEs, and docs.

    \b
    Built for coding questions: bugs, error messages, API behavior, library docs.
    Returns the matching passage from each source, not just a link.

    \b
    Examples:
        fcrawl dev "uv tool install editable"
        fcrawl dev "Could not find chunk mapping in page" -t issue -t pull_request
        fcrawl dev "retry on 429" -r firecrawl/firecrawl -p 3
        fcrawl dev "nuxt runtime config" -t doc
    """
    pretty = resolve_pretty(pretty)

    if len(sources) > 20:
        raise click.BadParameter("at most 20 sources", param_hint="--source")

    payload: dict = {"query": query, "k": limit, "passages": passages}
    if types:
        payload["types"] = list(types)
    if repos:
        payload["repos"] = list(repos)
    if sources:
        payload["sources"] = list(sources)
    if skills:
        payload["skills"] = "only"
    for field, value in (
        ("language", language),
        ("topic", topic),
        ("license", license_),
        ("min_stars", min_stars),
        ("max_stars", max_stars),
    ):
        if value is not None:
            payload[field] = value

    key = cache_key(query, {"engine": "firecrawl-dev", **payload})

    data = None
    from_cache = False
    if not no_cache:
        cached = read_cache("dev", key)
        if cached:
            data = cached
            from_cache = True
            console.print("[dim]Using cached result[/dim]")

    if cache_only and not from_cache:
        console.print(f"[red]Not in cache: {query}[/red]")
        raise click.Abort()

    if not from_cache:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task(f"Searching Developer Index for '{query}'...", total=None)
            data, elapsed, error = _dev_search(payload, _get_cloud_api_key())

        if error:
            console.print(f"[red]Error: {error}[/red]")
            raise click.Abort()

        data["elapsed"] = elapsed
        write_cache("dev", key, data)

    _display_scope_echo(data)

    results = data.get("results", [])
    if not results:
        console.print("[yellow]No results found[/yellow]")
        return

    console.print(f"[green]Found {len(results)} results[/green]")

    if pretty and not output and not json_output:
        _display_results(results)

    if output or json_output:
        output_data = {
            "query": query,
            "engine": "firecrawl-dev",
            "request": payload,
            "results": results,
            "meta": {
                key_: data[key_]
                for key_ in ("coverage", "reranked", "partial", "repos", "sources")
                if key_ in data
            }
            | {"from_cache": from_cache},
        }
        handle_output(
            output_data,
            output_file=output,
            json_output=True,
            pretty=pretty,
            format_type="json",
        )
    elif not pretty:
        _print_plain(results, urls_only)
