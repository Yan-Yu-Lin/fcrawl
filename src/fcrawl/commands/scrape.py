"""Scrape command for fcrawl"""

import click
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.console import Console
from typing import List, Optional

from ..utils.config import get_firecrawl_client
from ..utils.output import handle_output, console


def format_with_metadata(result, content: str) -> str:
    """Prepend metadata header to content (like Jina's r.jina.ai output)"""
    header_lines = []

    if hasattr(result, 'metadata') and result.metadata:
        md = result.metadata
        if hasattr(md, 'title') and md.title:
            header_lines.append(f"Title: {md.title}")
        if hasattr(md, 'source_url') and md.source_url:
            header_lines.append(f"URL Source: {md.source_url}")
        elif hasattr(md, 'url') and md.url:
            header_lines.append(f"URL Source: {md.url}")
        if hasattr(md, 'published_time') and md.published_time:
            header_lines.append(f"Published Time: {md.published_time}")

    if header_lines:
        return '\n'.join(header_lines) + '\n\nMarkdown Content:\n' + content
    return content

@click.command()
@click.argument('url')
@click.option('-f', '--format', 'formats', multiple=True,
              default=['markdown'],
              type=click.Choice(['markdown', 'html', 'links', 'screenshot', 'extract']),
              help='Output formats (can specify multiple)')
@click.option('-o', '--output', help='Save output to file')
@click.option('--copy', is_flag=True, help='Copy to clipboard')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--pretty/--no-pretty', default=True, help='Pretty print output')
@click.option('--selector', help='CSS selector to extract specific content')
@click.option('--wait', type=int, help='Wait time in milliseconds before scraping')
@click.option('--screenshot-full', is_flag=True, help='Take full page screenshot')
@click.option('--no-cache', is_flag=True, help='Bypass cache')
def scrape(
    url: str,
    formats: List[str],
    output: Optional[str],
    copy: bool,
    json_output: bool,
    pretty: bool,
    selector: Optional[str],
    wait: Optional[int],
    screenshot_full: bool,
    no_cache: bool
):
    """Scrape a single URL and extract content

    Examples:
        fcrawl scrape https://example.com
        fcrawl scrape https://example.com -f markdown -f links
        fcrawl scrape https://example.com -o output.md
        fcrawl scrape https://example.com --copy
    """
    # Prepare scrape options
    scrape_options = {
        'formats': list(formats)
    }

    if wait:
        scrape_options['wait'] = wait

    if screenshot_full and 'screenshot' in formats:
        scrape_options['screenshot'] = {'fullPage': True}

    # Show progress
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"Scraping {url}...", total=None)

        try:
            client = get_firecrawl_client()
            result = client.scrape(url, **scrape_options)
            progress.stop()

        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    # Handle output AFTER progress is done
    if len(formats) == 1:
        format_type = formats[0]
        if format_type == 'markdown' and hasattr(result, 'markdown'):
            content = result.markdown
            # Prepend metadata header (like Jina) unless JSON output
            if not json_output:
                content = format_with_metadata(result, content)
        elif format_type == 'html' and hasattr(result, 'html'):
            content = result.html
        elif format_type == 'links' and hasattr(result, 'links'):
            content = result.links
        else:
            content = result
    else:
        content = {}
        if 'markdown' in formats and hasattr(result, 'markdown'):
            content['markdown'] = result.markdown
        if 'html' in formats and hasattr(result, 'html'):
            content['html'] = result.html
        if 'links' in formats and hasattr(result, 'links'):
            content['links'] = result.links
        if hasattr(result, 'metadata'):
            content['metadata'] = result.metadata.__dict__ if hasattr(result.metadata, '__dict__') else result.metadata

    handle_output(
        content,
        output_file=output,
        copy=copy,
        json_output=json_output,
        pretty=pretty,
        format_type=formats[0] if len(formats) == 1 else 'json'
    )