"""`fcrawl resolve <ticket|g:id ...>`: ask the family gateway for the real URL behind Google goto tickets."""

from __future__ import annotations

import json

import click

from ..utils.config import is_gateway
from ..utils.goto import is_ticket_ref, resolve_via_gateway
from ..utils.output import console


@click.command()
@click.argument("urls", nargs=-1, required=True)
@click.option("--wait", is_flag=True, help="Let the gateway wait out Google's cooldown (up to ~45 s per ticket)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def resolve(urls: tuple[str, ...], wait: bool, json_output: bool):
    """Resolve Google goto tickets (from `fcrawl search`) to their real URLs.

    \b
    Examples:
        fcrawl resolve g:7kq2mx g:p9z4rt
        fcrawl resolve "https://www.google.com/goto?url=CAES..."
        fcrawl resolve --wait $(fcrawl search "query" --urls-only | grep '^g:')

    A ticket can be given as the g:xxxxxx id shown by `fcrawl search` or as the
    full google.com/goto URL. Without --wait a ticket that is still cooling
    down is reported as pending; `fcrawl scrape g:xxxxxx` always waits as a
    last resort, so it is fine to scrape tickets directly instead.
    """
    tickets = [u for u in urls if is_ticket_ref(u)]
    if tickets and not is_gateway():
        console.print("[red]Error: goto ids/tickets need gateway mode (gateway_token in .fcrawlrc)[/red]")
        raise click.Abort()
    rows: list[dict] = []
    try:
        answers = resolve_via_gateway(tickets, wait=wait) if tickets else {}
    except RuntimeError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise click.Abort()
    for u in urls:
        if u in tickets:
            a = answers[u]  # resolve_via_gateway guarantees an answer for every ticket it was given
            rows.append({"url": u, "ticket": a.get("ticket"), "gotoId": a.get("gotoId"),
                         "resolved": a["resolved"], "status": a["status"]})
        else:
            rows.append({"url": u, "ticket": None, "gotoId": None, "resolved": u, "status": "not_a_ticket"})
    failed = sum(1 for r in rows if r["status"] in ("failed", "expired"))
    pending = sum(1 for r in rows if r["status"] == "cooldown")
    if json_output:
        console.print_json(json.dumps(rows, indent=2))
        if failed:
            raise SystemExit(1)
        return
    for row in rows:
        status = row["status"]
        shown = row["gotoId"] or row["url"][:80]
        if status == "ok":
            print(f"{row['resolved']}")
        elif status == "not_a_ticket":
            print(f"{row['url']}  (not a goto ticket)")
        elif status == "cooldown":
            print(f"[goto pending] {shown}  (cooling down; retry, or --wait)")
        elif status == "expired":
            print(f"[goto expired] {shown}  (unknown or older than 2 h; re-run the search)")
        else:
            print(f"[goto failed] {shown}")
    if pending or failed:
        console.print(f"[dim]{pending} pending, {failed} failed[/dim]")
        if pending and not wait:
            console.print("[dim]Tip: fcrawl scrape g:xxxxxx resolves on use (waits if it must), or rerun with --wait[/dim]")
    if failed:
        raise SystemExit(1)
