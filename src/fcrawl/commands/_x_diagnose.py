"""Preflight diagnostics for the vendored twscrape X backend."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from rich.console import Console
from rich.table import Table

from ..utils.x_client import get_x_api, get_x_pool
from ..vendors.twscrape import NoAccountError
from ..vendors.twscrape.xclid import get_scripts_debug_snippet, get_scripts_list

X_HOME_URL = "https://x.com/"
STABLE_TWEET_ID = 20


async def fetch_x_home() -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        rep = await client.get(X_HOME_URL)
        rep.raise_for_status()
        return rep.text


async def check_xclid(
    fetch: Callable[[], Any] = fetch_x_home,
) -> dict[str, Any]:
    html = ""
    try:
        html = await fetch()
        scripts = list(get_scripts_list(html))
        if not scripts:
            raise RuntimeError("get_scripts_list() returned no scripts")
        return {
            "ok": True,
            "scripts_count": len(scripts),
            "sample_script": scripts[0],
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "snippet": get_scripts_debug_snippet(html) if html else "",
        }


def lock_state(account: dict[str, Any]) -> str:
    locks = account.get("locks") or {}
    if not locks:
        return "-"
    return ", ".join(f"{queue}: {until:%Y-%m-%d %H:%M:%S}" for queue, until in locks.items())


def latest_error(accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
    with_errors = [
        x for x in accounts if x.get("last_error_status") is not None or x.get("last_error_body")
    ]
    if not with_errors:
        return None
    old_time = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return max(with_errors, key=lambda x: x.get("last_used") or old_time)


async def collect_pool_status(pool: Any | None = None) -> dict[str, Any]:
    pool = pool or get_x_pool()
    accounts = await pool.accounts_info()
    return {
        "ok": bool(accounts),
        "accounts": accounts,
        "db_file": getattr(pool, "_db_file", None),
    }


async def check_authenticated_call(
    api: Any | None = None,
    pool_status: dict[str, Any] | None = None,
    pool: Any | None = None,
    tweet_id: int = STABLE_TWEET_ID,
) -> dict[str, Any]:
    api = api or get_x_api(raise_when_no_account=True)
    try:
        rep = await api.tweet_details_raw(tweet_id)
        if rep is None:
            return {
                "ok": False,
                "tweet_id": tweet_id,
                "error": "No response returned by twscrape",
            }

        body = rep.text or ""
        return {
            "ok": rep.status_code < 400,
            "tweet_id": tweet_id,
            "status": rep.status_code,
            "body": " ".join(body.split())[:200],
        }
    except NoAccountError as e:
        current_pool_status = pool_status or {}
        try:
            current_pool_status = await collect_pool_status(pool)
        except Exception:
            pass
        account_error = latest_error(current_pool_status.get("accounts", []))
        return {
            "ok": False,
            "tweet_id": tweet_id,
            "error": str(e),
            "cause": e.cause,
            "next_unlock_at": e.next_unlock_at,
            "status": account_error.get("last_error_status") if account_error else None,
            "body": (account_error.get("last_error_body") or "")[:200] if account_error else "",
        }
    except httpx.HTTPStatusError as e:
        return {
            "ok": False,
            "tweet_id": tweet_id,
            "error": str(e),
            "status": e.response.status_code,
            "body": " ".join((e.response.text or "").split())[:200],
        }
    except Exception as e:
        return {
            "ok": False,
            "tweet_id": tweet_id,
            "error": f"{type(e).__name__}: {e}",
        }


async def run_diagnostics() -> dict[str, Any]:
    xclid = await check_xclid()
    pool = await collect_pool_status()
    auth = await check_authenticated_call(pool_status=pool)
    pool = await collect_pool_status()
    ok = xclid["ok"] and pool["ok"] and auth["ok"]

    if not xclid["ok"]:
        summary = "xclid preflight failed"
    elif not pool["ok"]:
        summary = "no X accounts configured"
    elif not auth["ok"]:
        summary = "authenticated X request failed"
    else:
        summary = "all X diagnostics passed"

    return {
        "ok": ok,
        "summary": summary,
        "xclid": xclid,
        "pool": pool,
        "auth": auth,
    }


def render_diagnostics(report: dict[str, Any], console: Console) -> None:
    console.print("[bold cyan]X/Twitter twscrape diagnostics[/bold cyan]")

    xclid = report["xclid"]
    if xclid["ok"]:
        console.print(
            f"[green]1. xclid preflight:[/green] parsed {xclid['scripts_count']} JS chunks"
        )
        console.print(f"   sample: [dim]{xclid['sample_script']}[/dim]")
    else:
        console.print("[red]1. xclid preflight: failed[/red]")
        console.print(
            "   X likely changed their JS format — xclid.py needs updating"
        )
        console.print(f"   error: {xclid.get('error') or '-'}")
        if xclid.get("snippet"):
            console.print("   snippet:")
            console.print(f"   [dim]{xclid['snippet'][:1000]}[/dim]")

    pool = report["pool"]
    console.print("\n[bold]2. Account pool[/bold]")
    accounts = pool["accounts"]
    if not accounts:
        console.print("[yellow]No accounts configured.[/yellow]")
    else:
        table = Table()
        table.add_column("Username", style="cyan")
        table.add_column("Active")
        table.add_column("Logged In")
        table.add_column("Locks")
        table.add_column("Cause")
        table.add_column("HTTP")
        table.add_column("Last Error")
        for account in accounts:
            error_text = account.get("error_msg") or account.get("last_error_body") or "-"
            table.add_row(
                account["username"],
                "yes" if account["active"] else "no",
                "yes" if account["logged_in"] else "no",
                lock_state(account),
                account.get("last_error_cause") or "-",
                str(account.get("last_error_status") or "-"),
                error_text[:80],
            )
        console.print(table)
    if pool.get("db_file"):
        console.print(f"[dim]Database: {pool['db_file']}[/dim]")

    auth = report["auth"]
    console.print(f"\n[bold]3. Authenticated smoke test[/bold] [dim]tweet {auth['tweet_id']}[/dim]")
    if auth["ok"]:
        console.print(f"[green]OK[/green] HTTP {auth.get('status')}")
    else:
        console.print("[red]Failed[/red]")
        if auth.get("cause"):
            console.print(f"   cause: {auth['cause']}")
        if auth.get("next_unlock_at"):
            console.print(f"   next unlock: {auth['next_unlock_at']:%Y-%m-%d %H:%M:%S}")
        if auth.get("status"):
            console.print(f"   HTTP: {auth['status']}")
        if auth.get("body"):
            console.print(f"   body: {auth['body']}")
        if auth.get("error"):
            console.print(f"   error: {auth['error']}")

    console.print()
    if report["ok"]:
        console.print("[green]Summary: all X diagnostics passed[/green]")
    else:
        console.print(f"[red]Summary: {report['summary']}[/red]")
