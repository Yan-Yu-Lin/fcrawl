"""fcrawl auth: the institution (school library) proxy session.

    fcrawl auth presets                      # which libraries fcrawl knows
    fcrawl auth enable ntut                  # turn the feature on (off by default)
    fcrawl auth login                        # a browser window opens: log in there yourself
    fcrawl auth status                       # is the session alive? today's usage and limits
    fcrawl auth logout / disable

fcrawl never asks for the password: you log in yourself, captcha included, in a browser
window fcrawl opens (a Chrome/Edge/Brave/Chromium already on the machine, with a fresh
temporary profile). fcrawl reads the session cookies from that window over the DevTools
protocol. Fallbacks: --cookie-file, --paste, --from-clipboard, --from-browser. The
cookies stay in ~/.fcrawl/institution/ (owner-only) and are only ever sent, over https,
to the library proxy host, never to the gateway or Firecrawl.
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
    console.print(f"[red]Error: {exc}[/red]", highlight=False, markup=True)
    raise click.Abort()


def _note(text: str) -> None:
    console.print(text, highlight=False, markup=False)


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
        exp = " [experimental: not verified against this library yet]" if p.experimental else ""
        click.echo(f"{pid}{mark}: {p.name}{exp}")
        click.echo(f"  proxy: {p.proxy_host} ({p.style}{', experimental' if p.style == 'prefix' else ''})")
        click.echo(f"  login: {p.login_page()}")
        if p.notes:
            click.echo(f"  note: {p.notes}")
    for pid, why in inst.preset_errors(cfg).items():
        click.echo(f"{pid}: IGNORED ({why})")


@auth.command("enable")
@click.argument("preset", metavar="PRESET")
def enable_cmd(preset: str):
    """Turn the institution proxy on for PRESET (e.g. ntut). Written to .fcrawlrc."""
    if preset not in inst.presets(load_config()):
        _fail(inst.InstitutionError(f"unknown preset {preset!r}; fcrawl auth presets lists them"))
    try:
        written = inst.set_enabled(preset, True)
    except inst.InstitutionError as exc:
        _fail(exc)
        return
    click.echo(f"institution proxy enabled: {written['preset']}")
    click.echo("next: fcrawl auth login")


@auth.command("disable")
def disable_cmd():
    """Turn the institution proxy off (cookies stay on disk; fcrawl auth logout removes them)."""
    try:
        written = inst.set_enabled(None, False)
    except inst.InstitutionError as exc:
        _fail(exc)
        return
    click.echo(f"institution proxy disabled (preset kept: {written.get('preset') or 'none'})")


def _read_header(from_clipboard: bool) -> str:
    """The Cookie: header for --paste / --from-clipboard. From the clipboard, or from stdin
    when it is not a terminal (`fcrawl auth login --paste < header.txt`), or hidden typed
    input. A terminal's line buffer (about 1 KB on macOS) cuts long pastes short: the
    clipboard has no such limit."""
    if from_clipboard:
        import pyperclip

        try:
            text = pyperclip.paste() or ""
        except Exception as exc:  # no clipboard tool (headless Linux)
            raise inst.InstitutionError(f"cannot read the clipboard ({type(exc).__name__}); use --paste") from None
        if not text.strip():
            raise inst.InstitutionError("the clipboard is empty; copy the Cookie header value first")
        return text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    text = click.prompt("Cookie header value (hidden; for long values use --from-clipboard)", hide_input=True)
    if len(text) in range(1000, 1100):
        _note("note: that is about the size a terminal cuts a paste at; if the session check fails, "
              "use --from-clipboard")
    return text


@auth.command("login")
@click.option("--preset", "preset_id", default=None, help="Preset id (default: the enabled one)")
@click.option("--browser", "browser_name", default=None, metavar="NAME|PATH",
              help="Which browser opens the login window: edge, chrome, brave, chromium, or a path")
@click.option("--timeout", "timeout", default=600, show_default=True, type=click.IntRange(30, 3600),
              help="Seconds to wait for you to finish logging in")
@click.option("--from-browser", "browser", default=None, metavar="BROWSER[:PROFILE]",
              help="Fallback: read the cookies from a browser's own cookie store (firefox, chrome, edge, …); "
                   "often fails for session cookies and on Windows Chrome/Edge")
@click.option("--cookie-file", "cookie_file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Fallback: a Netscape cookies.txt exported from the browser after logging in")
@click.option("--paste", is_flag=True, help="Fallback: paste the Cookie: header value (hidden input, or stdin)")
@click.option("--from-clipboard", "from_clipboard", is_flag=True,
              help="Fallback: take the Cookie: header value from the clipboard")
@click.option("--no-open", is_flag=True, help="Do not open any browser: print the login page and the fallbacks")
def login_cmd(preset_id: Optional[str], browser_name: Optional[str], timeout: int, browser: Optional[str],
              cookie_file: Optional[str], paste: bool, from_clipboard: bool, no_open: bool):
    """Log in to the library in a browser window fcrawl opens; fcrawl keeps the session cookies.

    \b
    Default: opens a fresh window of the Chrome/Edge/Brave/Chromium on this machine.
    Log in there as yourself (captcha included); the window closes by itself when the
    library session exists. fcrawl never sees your password.
    No such browser, or it fails: --cookie-file, --paste, --from-clipboard or --from-browser.
    """
    preset = _preset_or_fail(preset_id)
    captures = [x for x in (browser, cookie_file, paste, from_clipboard) if x]
    if len(captures) > 1:
        raise click.BadParameter("use one of --from-browser, --cookie-file, --paste, --from-clipboard")

    try:
        if browser:
            name, _, profile = browser.partition(":")
            cookies = inst.cookies_from_browser(preset, name.strip().lower(), profile.strip() or None, warn=_note)
            source = name.strip().lower()
        elif cookie_file:
            cookies = inst.cookies_from_file(preset, cookie_file)
            source = "file"
        elif paste or from_clipboard:
            cookies = inst.cookies_from_header(preset, _read_header(from_clipboard))
            source = "clipboard" if from_clipboard else "paste"
        elif no_open:
            _manual_instructions(preset, open_page=False)
            return
        else:
            from ..utils import browser_login

            try:
                cookies, used = browser_login.browser_login(preset, browser_name, timeout, note=_note)
            except browser_login.BrowserNotFound as exc:
                _note(f"{exc}")
                _manual_instructions(preset, open_page=True)
                return
            source = f"browser-window:{used}"
    except inst.InstitutionError as exc:
        _fail(exc)
        return
    except KeyboardInterrupt:
        _fail(inst.InstitutionError("login cancelled"))
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


def _manual_instructions(preset: inst.Preset, open_page: bool) -> None:
    from ..utils.browser_login import has_display

    url = preset.login_page()
    click.echo(f"login page: {url}")
    # without a screen (ssh into Linux) webbrowser would start a text-mode browser in this
    # terminal, or nothing useful: print the URL only
    if open_page and has_display():
        try:
            webbrowser.open(url)
        except Exception:
            pass
    click.echo("log in there as yourself (fcrawl never takes your password), then hand over the cookies:")
    click.echo("  fcrawl auth login --cookie-file cookies.txt   # exported by a 'Get cookies.txt' extension")
    click.echo("  fcrawl auth login --from-clipboard            # Cookie: header value copied from the dev tools")
    click.echo("  fcrawl auth login --paste                     # same, typed/pasted (hidden; short values only)")
    click.echo("  fcrawl auth login --from-browser chromium     # a Chromium browser's cookie store (Firefox never "
               "stores the session cookie there)")


@auth.command("status")
@click.option("--preset", "preset_id", default=None, help="Preset id (default: the enabled one)")
@click.option("--no-probe", is_flag=True, help="Do not contact the proxy; local state only")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def status_cmd(preset_id: Optional[str], no_probe: bool, json_output: bool):
    """Is the session alive, what is on file, today's usage and the limits in force."""
    cfg = load_config()
    conf = inst.settings(cfg)
    try:
        preset = inst.active_preset(cfg, preset_id or conf["preset"])
    except inst.InstitutionError as exc:
        _fail(exc)
        return
    store = inst.CookieStore.load(preset.id)
    lim = inst.limits(preset.id, cfg)
    for n in lim.notes:
        _note(n)
    usage = inst.Usage.load(preset.id, lim)
    today = date.today().isoformat()
    used_today = usage.total if usage.day == today else 0
    pubs_today = usage.publishers if usage.day == today else {}
    result = None if no_probe else inst.probe(preset, store)
    data = {
        "preset": preset.id, "enabled": conf["enabled"] and conf["preset"] == preset.id,
        "experimental": preset.experimental or preset.style == "prefix",
        "proxyHost": preset.proxy_host, "cookies": sorted({c.name for c in store.cookies}),
        "cookieDomains": sorted({c.domain for c in store.cookies}), "captured": store.captured, "source": store.source,
        "soonestExpiry": store.soonest_expiry(), "session": result,
        "usageToday": {"total": used_today, "limit": lim.daily, "publishers": pubs_today,
                       "perPublisherLimit": lim.per_publisher, "minSpacingSeconds": lim.spacing},
    }
    if json_output:
        import json

        click.echo(json.dumps(data, indent=2, ensure_ascii=False))
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
    pubs = ", ".join(f"{k} {v}/{lim.per_publisher}" for k, v in sorted(pubs_today.items())) or "none yet"
    click.echo(f"today: {used_today}/{lim.daily} papers; per publisher: {pubs}; spacing: {lim.spacing}s")


@auth.command("logout")
@click.option("--preset", "preset_id", default=None, help="Preset id (default: the enabled one)")
def logout_cmd(preset_id: Optional[str]):
    """Delete the cookies on file (the browser's own login is untouched)."""
    cfg = load_config()
    preset = _preset_or_fail(preset_id or inst.settings(cfg)["preset"])
    removed = inst.CookieStore(preset.id).delete()
    click.echo("cookies removed" if removed else "no cookies were on file")
