"""Crawl command for fcrawl"""

import click
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from rich.console import Console
from typing import Optional, List

from utils.config import get_firecrawl_client
from utils.output import handle_output, console

@click.command()
@click.argument('url')
@click.option('--limit', type=int, default=10, help='Maximum number of pages to crawl')
@click.option('--depth', type=int, help='Maximum crawl depth')
@click.option('-f', '--format', 'formats', multiple=True,
              default=['markdown'],
              type=click.Choice(['markdown', 'html', 'links']),
              help='Output formats for each page')
@click.option('-o', '--output', help='Save output to file (JSON format for multiple pages)')
@click.option('--include-paths', multiple=True, help='Only crawl URLs matching these patterns')
@click.option('--exclude-paths', multiple=True, help='Skip URLs matching these patterns')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--pretty/--no-pretty', default=True, help='Pretty print output')
@click.option('--poll-interval', type=int, default=2, help='Polling interval in seconds')
@click.option('--timeout', type=int, default=300, help='Timeout in seconds')
def crawl(
    url: str,
    limit: int,
    depth: Optional[int],
    formats: List[str],
    output: Optional[str],
    include_paths: List[str],
    exclude_paths: List[str],
    json_output: bool,
    pretty: bool,
    poll_interval: int,
    timeout: int
):
    """Crawl a website and extract content from multiple pages

    Examples:
        fcrawl crawl https://blog.com --limit 10
        fcrawl crawl https://docs.site.com --depth 2
        fcrawl crawl https://site.com --exclude-paths "/admin/*" "/private/*"
    """
    # Prepare crawl options
    crawl_options = {
        'limit': limit,
        'scrape_options': {
            'formats': list(formats)
        },
        'poll_interval': poll_interval,
        'timeout': timeout
    }

    if depth:
        crawl_options['max_depth'] = depth

    if include_paths:
        crawl_options['include_paths'] = list(include_paths)

    if exclude_paths:
        crawl_options['exclude_paths'] = list(exclude_paths)

    # Show progress
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeRemainingColumn(),
        console=console
    ) as progress:
        task = progress.add_task(f"Crawling {url}...", total=limit)

        try:
            client = get_firecrawl_client()

            console.print(f"[cyan]Starting crawl of {url} (limit: {limit} pages)[/cyan]")

            # Start the crawl - this blocks until complete with poll_interval
            result = client.crawl(url, **crawl_options)

            progress.stop()

            # Helper to safely get metadata attributes
            def get_meta(page, *attrs):
                if not hasattr(page, 'metadata') or page.metadata is None:
                    return None
                meta = page.metadata
                for attr in attrs:
                    val = getattr(meta, attr, None)
                    if val is not None:
                        return val
                return None

            # Process results
            if hasattr(result, 'data') and result.data:
                console.print(f"[green]✓ Crawled {len(result.data)} pages successfully[/green]")

                # Display as clean list (no table)
                if pretty and not output:
                    console.print(f"\n[bold]Crawl Results - {url}[/bold]", justify="center")
                    console.print("─" * 60)

                    for i, page in enumerate(result.data[:10], 1):
                        page_url = get_meta(page, 'sourceURL', 'url', 'source_url') or 'Unknown'
                        page_title = get_meta(page, 'title') or 'No title'
                        status = get_meta(page, 'statusCode', 'status_code') or ''

                        console.print(f"[bold cyan]## {page_title}[/bold cyan]")
                        console.print(f"[blue]{page_url}[/blue]")
                        if status:
                            console.print(f"[dim]Status: {status}[/dim]")
                        console.print()

                    console.print("─" * 60)
                    if len(result.data) > 10:
                        console.print(f"[dim]... and {len(result.data) - 10} more pages[/dim]")

                # Prepare data for output
                if json_output or output:
                    output_data = []
                    for page in result.data:
                        page_data = {}
                        if hasattr(page, 'metadata'):
                            page_data['metadata'] = page.metadata.__dict__ if hasattr(page.metadata, '__dict__') else page.metadata
                        if 'markdown' in formats and hasattr(page, 'markdown'):
                            page_data['markdown'] = page.markdown
                        if 'html' in formats and hasattr(page, 'html'):
                            page_data['html'] = page.html
                        if 'links' in formats and hasattr(page, 'links'):
                            page_data['links'] = page.links
                        output_data.append(page_data)

                    handle_output(
                        output_data,
                        output_file=output,
                        json_output=True,  # Always JSON for multiple pages
                        pretty=pretty,
                        format_type='json'
                    )
                elif not pretty:
                    # Raw output for piping
                    for page in result.data:
                        if hasattr(page, 'markdown'):
                            print(page.markdown)
                            print("\n---\n")  # Page separator

            else:
                console.print("[yellow]No pages crawled[/yellow]")

        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()