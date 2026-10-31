#!/usr/bin/env python3
"""
fcrawl - A powerful CLI tool for Firecrawl
Simple web scraping from your terminal
"""

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .commands.scrape import scrape
from .commands.crawl import crawl
from .commands.map import map_site
from .commands.extract import extract
from .commands.search import search
from .commands.dev import dev
from .commands.gsearch import gsearch
from .commands.csearch import csearch
from .commands.yt_transcript import yt_transcript
from .commands.yt_channel import yt_channel
from .commands.yt_search import yt_search
from .commands.transcribe import transcribe
from .commands.x import x
from .commands.reddit import reddit
from .commands.cache_cmd import cache
from .commands.dcard import dcard
from .utils.config import InsecureGatewayURL, check_gateway_url, is_gateway, load_config, mask_key

console = Console()

# subcommands that talk to the gateway in gateway mode (with the family token)
GATEWAY_COMMANDS = {"scrape", "crawl", "map", "extract", "search", "x", "reddit", "dcard"}


def _guard_gateway_url(cmd: click.Command) -> None:
    """Fail a gateway-bound command early, with one clear message, if the configured
    gateway URL would put the token on the wire in clear text. Wraps invoke(), which
    click only reaches after parsing, so `--help` (handled during parsing) and the
    other commands are untouched. utils/config.py checks again on every request."""
    original = cmd.invoke

    def invoke(ctx: click.Context):
        config = load_config()
        if is_gateway(config):
            try:
                check_gateway_url(config["api_url"])
            except InsecureGatewayURL as exc:
                raise click.ClickException(str(exc)) from None
        return original(ctx)

    cmd.invoke = invoke  # type: ignore[method-assign]


@click.group(invoke_without_command=True)
@click.pass_context
@click.version_option(version="0.1.0", prog_name="fcrawl")
def cli(ctx):
    """
    fcrawl - Firecrawl CLI Tool

    Simple and powerful web scraping for Claude Code.

    \b
    Quick start:
        fcrawl scrape https://example.com           # Scrape to terminal
        fcrawl scrape https://example.com -o out.md # Save to file
        fcrawl scrape https://example.com -f links  # Get links only

    \b
    More commands:
        fcrawl crawl https://blog.com --limit 10    # Crawl site to folder
        fcrawl map https://docs.site.com            # Discover URLs
        fcrawl search "query"                       # Fast web search via Serper
        fcrawl dev "query"                          # Code search: issues, PRs, READMEs, docs
        fcrawl csearch "query"                      # Multi-engine browser search

    \b
    Notes:
        - extract: NOT WORKING. Use a sub-agent with scrape instead.
        - crawl: May not discover links on some sites. Workaround:
          1. fcrawl scrape URL -f links  (discover links)
          2. fcrawl scrape each link as needed
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# Register commands
cli.add_command(scrape)
cli.add_command(crawl)
cli.add_command(map_site, name="map")
cli.add_command(extract)
cli.add_command(search)
cli.add_command(dev)
cli.add_command(gsearch)
cli.add_command(csearch)
cli.add_command(yt_transcript)
cli.add_command(yt_channel)
cli.add_command(yt_search)
cli.add_command(transcribe)
cli.add_command(x)
cli.add_command(reddit)
cli.add_command(cache)
cli.add_command(dcard)


@cli.command()
def config():
    """View or edit configuration"""
    config_data = load_config()
    console.print("[bold cyan]Current Configuration:[/bold cyan]")
    for key, value in config_data.items():
        if key == "api_keys":
            value = [mask_key(k) for k in value]
        elif key.endswith("_key") or key.endswith("_token"):
            value = mask_key(value)
        console.print(f"  {key}: {value}")


for _name in GATEWAY_COMMANDS:
    if _name in cli.commands:
        _guard_gateway_url(cli.commands[_name])


# REMOVED: quick is redundant - just use `fcrawl scrape URL`
# @cli.command()
# @click.argument('url')
# def quick(url):
#     """Quick scrape with default settings (markdown to stdout)"""
#     ...

if __name__ == "__main__":
    cli()

