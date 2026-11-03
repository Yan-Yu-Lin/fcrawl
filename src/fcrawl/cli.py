#!/usr/bin/env python3
"""
fcrawl - web search, scraping, Reddit, X, YouTube and Dcard for AI coding agents
"""

import click
from rich.console import Console

from .utils.output import force_utf8_stdio

# before anything prints: on a cp950 (zh-TW Windows) console, piped output would
# otherwise raise UnicodeEncodeError on the first non-ASCII character
force_utf8_stdio()

from .commands.scrape import scrape
from .commands.resolve import resolve
from .commands.map import map_site
from .commands.search import search
from .commands.dev import dev
from .commands.papers import papers
from .commands.yt_transcript import yt_transcript
from .commands.yt_channel import yt_channel
from .commands.yt_search import yt_search
from .commands.transcribe import transcribe
from .commands.x import x
from .commands.reddit import reddit
from .commands.cache_cmd import cache
from .commands.dcard import dcard
from .commands.setup import setup
from .commands.auth import auth
from .utils.config import load_config, mask_key

console = Console()

@click.group(invoke_without_command=True)
@click.pass_context
@click.version_option(package_name="fcrawl", prog_name="fcrawl")
def cli(ctx):
    """
    fcrawl - the web for AI coding agents

    Search Google, read pages as markdown, and read Reddit, X, YouTube and Dcard.

    \b
    Common commands:
        fcrawl search "query"                       # Google search (Serper)
        fcrawl scrape URL --save-temp               # Page to markdown file
        fcrawl reddit post URL --json               # Reddit thread + comments
        fcrawl x tweet URL --thread                 # X thread
        fcrawl yt-transcript URL                    # YouTube transcript
        fcrawl dcard search "query"                 # Dcard posts
        fcrawl dev "error message"                  # GitHub issues, PRs, docs
        fcrawl papers "query"                       # Academic papers (Scholar + Research Index)
        fcrawl setup claude-code                    # Install skill + hook for Claude Code
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# Register commands
cli.add_command(scrape)
cli.add_command(map_site, name="map")
cli.add_command(search)
cli.add_command(resolve)
cli.add_command(dev)
cli.add_command(papers)
cli.add_command(yt_transcript)
cli.add_command(yt_channel)
cli.add_command(yt_search)
cli.add_command(transcribe)
cli.add_command(x)
cli.add_command(reddit)
cli.add_command(cache)
cli.add_command(dcard)
cli.add_command(setup)
cli.add_command(auth)


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


if __name__ == "__main__":
    cli()

