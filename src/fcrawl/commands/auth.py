"""fcrawl auth: the institution (school library) proxy session.

    fcrawl auth presets                      # which libraries fcrawl knows
    fcrawl auth enable ntut                  # turn the feature on (off by default)
    fcrawl auth login                        # open the library login page in your browser, then
    fcrawl auth login --from-browser firefox # hand the session cookies to fcrawl
    fcrawl auth status                       # is the session alive? today's usage
    fcrawl auth logout / disable

fcrawl never asks for the password: you log in yourself, in your own browser, captcha
included. The cookies stay in ~/.fcrawl/institution/ (owner-only) and are only ever
sent to the library proxy host, never to the gateway or Firecrawl.
"""

from __future__ import annotations

import sys
import webbrowser
from datetime import date, datetime, timezone
from typing import Optional

import click

from ..utils import institution as inst
from ..utils.config import load_config
from ..utils.output import err_console as console


def _fail(exc: Exception) -> None:
    console.print(f"[red]Error: {exc}[/red]", highlight=False)
    raise click.Abort()


def _preset_or_fail(override: Optional[str]) -> inst.Preset:
    try:
        return inst.active_preset(load_config(), override)
    except inst.InstitutionError as exc:
        _fail(exc)
        raise  # unreachable; keeps type checkers calm


def _fmt_epoch(ts: Optional[int]) -> str:
    if not ts:
        return "session (until the browser/proxy ends it)"
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


@click.group()
def auth():
    """Institution proxy session: read paywalled papers through your own library (off by default)."""


@auth.command("presets")
def presets_cmd():
    """List the library proxies fcrawl knows (built-in + institution.presets in .fcrawlrc)."""
    cfg = load_config()
    current = inst.settings(cfg)
    for pid, p in inst.presets(cfg).items():
        mark = " (enabled)" if current["enabled"] and current["preset"] == pid else ""
        click.echo(f"{pid}{mark}: {p.name}")
        click.echo(f"  proxy: {p.proxy_host} ({p.style})")
        click.echo(f"  login: {p.login_page()}")
        if p.notes:
            click.echo(f"  note: {p.notes}")


@auth.command("enable")
@click.argument("preset", metavar="PRESET")
def enable_cmd(preset: str):
    """Turn the institution proxy on for PRESET (e.g. ntut). Written to .fcrawlrc."""
    if preset not in inst.presets(load_config()):
        _fail(inst.InstitutionError(f"unknown preset {preset!r}; fcrawl auth presets lists them"))
    written = inst.set_enabled(preset, True)
    click.echo(f"institution proxy enabled: {written['preset']}")
    click.echo("next: fcrawl auth login")


@auth.command("disable")
def disable_cmd():
    """Turn the institution proxy off (cookies stay on disk; fcrawl auth logout removes them)."""
    written = inst.set_enabled(None, False)
    click.echo(f"institution proxy disabled (preset kept: {written.get('preset') or 'none'})")


@auth.command("login")
@click.option("--preset", "preset_id", default=None, help="Preset id (default: the enabled one)")
@click.option("--from-browser", "browser", default=None, metavar="BROWSER[:PROFILE]",
              help="Take the cookies from this browser's cookie store: firefox, chrome, edge, brave, "
                   "chromium, vivaldi, opera, safari, whale (Chrome/Edge on Windows: see note)")
@click.option("--cookie-file", "cookie_file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="A Netscape cookies.txt exported from the browser after logging in")
@click.option("--paste", is_flag=True, help="Paste the Cookie: header value from the browser's dev tools")
@click.option("--no-open", is_flag=True, help="Do not open the login page in a browser")
def login_cmd(preset_id: Optional[str], browser: Optional[str], cookie_file: Optional[str], paste: bool,
              no_open: bool):
    """Log in to the library in your browser, then hand the session cookies to fcrawl.

    \b
    Step 1 (no capture option): opens the login page. Log in there as yourself.
    Step 2: run again with one of --from-browser, --cookie-file or --paste.
    """
    preset = _preset_or_fail(preset_id)
    captures = [x for x in (browser, cookie_file, paste) if x]
    if len(captures) > 1:
        raise click.BadParameter("use one of --from-browser, --cookie-file, --paste")
    if not captures:
        url = preset.login_page()
        click.echo(f"login page: {url}")
        if not no_open:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        click.echo("log in there as yourself (fcrawl never takes your password), then hand over the cookies:")
        click.echo("  fcrawl auth login --from-browser firefox      # or edge, chrome, …")
        click.echo("  fcrawl auth login --cookie-file cookies.txt   # exported by a 'Get cookies.txt' extension")
        click.echo("  fcrawl auth login --paste                     # Cookie: header from the dev tools")
        if sys.platform == "win32":
            click.echo("note: on Windows, Chrome and Edge encrypt their cookies so only they can read them; "
                       "Firefox works, or use --cookie-file / --paste.")
        return

    try:
        if browser:
            name, _, profile = browser.partition(":")
            cookies = inst.cookies_from_browser(preset, name.strip().lower(), profile.strip() or None)
            source = name.strip().lower()
        elif cookie_file:
            cookies = inst.cookies_from_file(preset, cookie_file)
            source = "file"
        else:
            header = click.prompt("Cookie header value", hide_input=True)
            cookies = inst.cookies_from_header(preset, header)
            source = "paste"
    except inst.InstitutionError as exc:
        _fail(exc)
        return
    store = inst.CookieStore(preset.id, cookies, captured=inst.iso_now(), source=source)
    path = store.save()
    names = ", ".join(sorted({c.name for c in cookies}))
    click.echo(f"saved {len(cookies)} cookie(s) for {preset.proxy_host} ({names}) to {path}")
    result = inst.probe(preset, store)
    if result["ok"]:
        click.echo(f"session OK: {result['detail']}")
    else:
        click.echo(f"session check failed: {result['detail']}")
        click.echo("the login may not have finished, or the cookies are from before it; log in again and re-run")


@auth.command("status")
@click.option("--preset", "preset_id", default=None, help="Preset id (default: the enabled one)")
@click.option("--no-probe", is_flag=True, help="Do not contact the proxy; local state only")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def status_cmd(preset_id: Optional[str], no_probe: bool, json_output: bool):
    """Is the session alive, what is on file, how much of today's quota is used."""
    cfg = load_config()
    conf = inst.settings(cfg)
    try:
        preset = inst.active_preset(cfg, preset_id or conf["preset"])
    except inst.InstitutionError as exc:
        _fail(exc)
        return
    store = inst.CookieStore.load(preset.id)
    usage = inst.Usage.load(preset.id)
    today = date.today().isoformat()
    used_today = usage.total if usage.day == today else 0
    pubs_today = usage.publishers if usage.day == today else {}
    result = None if no_probe else inst.probe(preset, store)
    data = {
        "preset": preset.id, "enabled": conf["enabled"] and conf["preset"] == preset.id,
        "proxyHost": preset.proxy_host, "cookies": sorted({c.name for c in store.cookies}),
        "cookieDomains": sorted({c.domain for c in store.cookies}), "captured": store.captured, "source": store.source,
        "soonestExpiry": store.soonest_expiry(), "session": result,
        "usageToday": {"total": used_today, "limit": inst.DAILY_LIMIT, "publishers": pubs_today,
                       "perPublisherLimit": inst.PUBLISHER_DAILY_LIMIT, "minSpacingSeconds": inst.MIN_SPACING},
    }
    if json_output:
        import json

        click.echo(json.dumps(data, indent=2))
        return
    click.echo(f"preset: {preset.id} ({preset.name}) — {'enabled' if data['enabled'] else 'DISABLED'}")
    click.echo(f"proxy: {preset.proxy_host}")
    if store.cookies:
        click.echo(f"cookies: {', '.join(data['cookies'])} on {', '.join(data['cookieDomains'])}")
        click.echo(f"captured: {store.captured or '?'} via {store.source or '?'}; "
                   f"expires: {_fmt_epoch(store.soonest_expiry())}")
    else:
        click.echo("cookies: none on file (fcrawl auth login)")
    if result is not None:
        click.echo(f"session: {'OK' if result['ok'] else 'NOT OK'} ({result['detail']})")
    pubs = ", ".join(f"{k} {v}/{inst.PUBLISHER_DAILY_LIMIT}" for k, v in sorted(pubs_today.items())) or "none yet"
    click.echo(f"today: {used_today}/{inst.DAILY_LIMIT} papers; per publisher: {pubs}; "
               f"spacing: {inst.MIN_SPACING}s")


@auth.command("logout")
@click.option("--preset", "preset_id", default=None, help="Preset id (default: the enabled one)")
def logout_cmd(preset_id: Optional[str]):
    """Delete the cookies on file (the browser's own login is untouched)."""
    cfg = load_config()
    preset = _preset_or_fail(preset_id or inst.settings(cfg)["preset"])
    removed = inst.CookieStore(preset.id).delete()
    click.echo("cookies removed" if removed else "no cookies were on file")
