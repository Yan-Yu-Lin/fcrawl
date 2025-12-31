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
from .utils.config import load_config, get_firecrawl_client

console = Console()

@click.group(invoke_without_command=True)
@click.pass_context
@click.version_option(version='0.1.0', prog_name='fcrawl')
def cli(ctx):
    """
    fcrawl - Firecrawl CLI Tool

    Simple and powerful web scraping from your terminal.

    Examples:
        fcrawl scrape https://example.com
        fcrawl crawl https://blog.com --limit 10
        fcrawl map https://docs.site.com --search "api"
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())

# Register commands
cli.add_command(scrape)
cli.add_command(crawl)
cli.add_command(map_site, name='map')
cli.add_command(extract)
cli.add_command(search)

@cli.command()
def config():
    """View or edit configuration"""
    config_data = load_config()
    console.print("[bold cyan]Current Configuration:[/bold cyan]")
    for key, value in config_data.items():
        console.print(f"  {key}: {value}")

@cli.command()
@click.argument('url')
def quick(url):
    """Quick scrape with default settings (markdown to stdout)"""
    from .utils.output import display_content

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"Scraping {url}...", total=None)

        client = get_firecrawl_client()
        result = client.scrape(url, formats=['markdown'])

        progress.stop()

    display_content(result.markdown, format_type='markdown')

if __name__ == '__main__':
    cli()
