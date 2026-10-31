"""`fcrawl resolve <url...>`: ask the family gateway for the real URL behind Google goto tickets."""

from __future__ import annotations

import json
from typing import Optional

import click

from ..utils.goto import is_goto, resolve_via_gateway
from ..utils.output import console


@click.command()
@click.argument("urls", nargs=-1, required=True)
@click.option("--wait", is_flag=True, help="Let the gateway wait out Google's cooldown (up to ~45 s per ticket)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def resolve(urls: tuple[str, ...], wait: bool, json_output: bool):
    """Resolve Google goto tickets (from `fcrawl search`) to their real URLs.

    \b
    Examples:
        fcrawl resolve "https://www.google.com/goto?url=CAES..."
        fcrawl resolve --wait $(fcrawl search "query" --urls-only | grep goto)

    Without --wait a ticket that is still cooling down is reported as pending;
    `fcrawl scrape <ticket>` always waits as a last resort, so it is fine to
    scrape tickets directly instead of resolving them first.
    """
    tickets = [u for u in urls if is_goto(u)]
    others = [u for u in urls if not is_goto(u)]
    rows: list[dict] = []
    try:
        answers = resolve_via_gateway(tickets, wait=wait) if tickets else {}
    except RuntimeError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise click.Abort()
    for u in urls:
        if u in answers:
            a = answers[u]
            rows.append({"url": u, "resolved": a.get("resolved"), "status": a.get("status", "failed")})
        else:
            rows.append({"url": u, "resolved": u, "status": "not_a_ticket"})
    if json_output:
        console.print_json(json.dumps(rows, indent=2))
        return
    for row in rows:
        status = row["status"]
        if status == "ok":
            print(f"{row['resolved']}")
        elif status == "not_a_ticket":
            print(f"{row['url']}  (not a goto ticket)")
        elif status == "cooldown":
            print(f"[goto pending] {row['url'][:80]}…  (cooling down; retry, or --wait)")
        else:
            print(f"[goto failed] {row['url'][:80]}…")
    pending = sum(1 for r in rows if r["status"] == "cooldown")
    failed = sum(1 for r in rows if r["status"] == "failed")
    if pending or failed:
        console.print(f"[dim]{pending} pending, {failed} failed[/dim]")
        if pending and not wait:
            console.print("[dim]Tip: fcrawl scrape <ticket> resolves on use (waits if it must), or rerun with --wait[/dim]")
    if failed and not pending:
        raise SystemExit(1)
