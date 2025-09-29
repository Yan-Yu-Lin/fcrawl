"""Crawl command for fcrawl"""

import click
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from rich.console import Console
from rich.table import Table
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

            # Update progress based on result
            if hasattr(result, 'data') and result.data:
                progress.update(task, completed=len(result.data))
            else:
                progress.update(task, completed=limit)

            # Process results
            if hasattr(result, 'data') and result.data:
                console.print(f"[green]✓ Crawled {len(result.data)} pages successfully[/green]")

                # Create summary table
                if pretty and not output:
                    table = Table(title=f"Crawl Results - {url}")
                    table.add_column("Page", style="cyan", no_wrap=False)
                    table.add_column("Title", style="magenta")
                    table.add_column("Status", style="green")

                    for page in result.data[:10]:  # Show first 10 in table
                        page_url = page.metadata.sourceURL if hasattr(page, 'metadata') else 'Unknown'
                        page_title = page.metadata.title if hasattr(page, 'metadata') else 'No title'
                        status = page.metadata.statusCode if hasattr(page, 'metadata') else 'N/A'
                        table.add_row(
                            page_url[:50] + '...' if len(page_url) > 50 else page_url,
                            page_title[:30] + '...' if len(page_title) > 30 else page_title,
                            str(status)
                        )

                    console.print(table)

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