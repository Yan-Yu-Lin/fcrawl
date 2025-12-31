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
from .commands.yt_transcript import yt_transcript
from .commands.yt_channel import yt_channel
from .commands.x import x
from .utils.config import load_config, get_firecrawl_client

console = Console()

@click.group(invoke_without_command=True)
@click.pass_context
@click.version_option(version='0.1.0', prog_name='fcrawl')
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
        fcrawl search "query" --scrape              # Web search + scrape

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
cli.add_command(map_site, name='map')
cli.add_command(extract)
cli.add_command(search)
cli.add_command(yt_transcript)
cli.add_command(yt_channel)
cli.add_command(x)

@cli.command()
def config():
    """View or edit configuration"""
    config_data = load_config()
    console.print("[bold cyan]Current Configuration:[/bold cyan]")
    for key, value in config_data.items():
        console.print(f"  {key}: {value}")

# REMOVED: quick is redundant - just use `fcrawl scrape URL`
# @cli.command()
# @click.argument('url')
# def quick(url):
#     """Quick scrape with default settings (markdown to stdout)"""
#     ...

if __name__ == '__main__':
    cli()
