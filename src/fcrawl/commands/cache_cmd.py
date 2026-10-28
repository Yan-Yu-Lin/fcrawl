"""`fcrawl cache` — inspect and clean the /tmp/fcrawl-cache store."""

import subprocess
from collections import Counter

import click

from ..utils.cache import CACHE_DIR, clear_cache, iter_cache, validate_result
from ..utils.output import console


@click.group()
def cache():
    """Inspect or clean the local result cache"""


@cache.command()
def stats():
    """Count cached files per command / provider and how many are junk"""
    if not CACHE_DIR.exists():
        console.print(f"[dim]No cache at {CACHE_DIR}[/dim]")
        return
    for folder in sorted(p for p in CACHE_DIR.iterdir() if p.is_dir()):
        command = folder.name
        total = 0
        providers: Counter = Counter()
        invalid = 0
        for _, data in iter_cache(command):
            total += 1
            if command != "scrape":
                continue
            providers[(data or {}).get("source_provider") or "firecrawl"] += 1
            if data is None or not validate_result(data)[0]:
                invalid += 1
        line = f"[bold]{command}[/bold]: {total} files"
        if command == "scrape":
            by_provider = ", ".join(f"{k} {v}" for k, v in providers.most_common())
            line += f" ({by_provider}); [red]{invalid} invalid[/red]" if invalid else f" ({by_provider})"
        console.print(line)


@cache.command()
@click.option("--yes", is_flag=True, help="Actually move the junk to the trash")
def clean(yes: bool):
    """List (and with --yes, trash) scrape cache entries that are junk"""
    junk = []
    for path, data in iter_cache("scrape"):
        if data is None:
            junk.append((path, "unreadable", ""))
            continue
        ok, reason = validate_result(data)
        if not ok:
            url = (data.get("metadata") or {}).get("source_url") or (data.get("metadata") or {}).get("sourceURL") or ""
            junk.append((path, reason, url))

    if not junk:
        console.print("[green]No junk in scrape cache[/green]")
        return
    for path, reason, url in junk:
        console.print(f"  [red]{reason:<32}[/red] {url}  [dim]{path.name}[/dim]", soft_wrap=True)
    console.print(f"{len(junk)} junk entries")
    if not yes:
        console.print("[dim]Re-run with --yes to move them to the trash[/dim]")
        return
    subprocess.run(["trash", *[str(p) for p, _, _ in junk]], check=True)
    console.print(f"[green]Trashed {len(junk)} entries[/green]")


@cache.command()
@click.argument("command", required=False)
def clear(command):
    """Delete the whole cache, or one command's folder (scrape, search, …)"""
    clear_cache(command)
    console.print(f"[green]Cleared {command or 'all'} cache[/green]")
