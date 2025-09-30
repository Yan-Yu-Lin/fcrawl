"""Search command for fcrawl"""

import click
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.console import Console
from rich.table import Table
from typing import List, Optional

from utils.config import get_firecrawl_client
from utils.output import handle_output, console

@click.command()
@click.argument('query')
@click.option('--sources', '-s', multiple=True,
              type=click.Choice(['web', 'news', 'images']),
              help='Search sources (can specify multiple: -s web -s news)')
@click.option('--category', '-c', multiple=True,
              type=click.Choice(['github', 'research']),
              help='Category filters (github, research)')
@click.option('--limit', '-l', type=int, default=15,
              help='Maximum number of results per source (default: 15)')
@click.option('--tbs',
              type=click.Choice(['qdr:h', 'qdr:d', 'qdr:w', 'qdr:m', 'qdr:y']),
              help='Time filter (h=hour, d=day, w=week, m=month, y=year)')
@click.option('--location', help='Location for search results')
@click.option('--scrape', is_flag=True,
              help='Scrape the search results (returns full content)')
@click.option('-f', '--format', 'formats', multiple=True,
              default=['markdown'],
              type=click.Choice(['markdown', 'html', 'links']),
              help='Output formats when scraping (default: markdown)')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--pretty/--no-pretty', default=True, help='Pretty print output')
def search(
    query: str,
    sources: tuple,
    category: tuple,
    limit: int,
    tbs: Optional[str],
    location: Optional[str],
    scrape: bool,
    formats: tuple,
    output: Optional[str],
    json_output: bool,
    pretty: bool
):
    """Search the web and optionally scrape results

    Examples:
        fcrawl search "python tutorials"
        fcrawl search "AI news" --sources news --limit 10
        fcrawl search "python repos" --category github
        fcrawl search "recent articles" --tbs qdr:d
        fcrawl search "machine learning" --scrape -f markdown
    """
    # Prepare search options
    search_options = {
        'query': query,
        'limit': limit
    }

    # Add sources if specified
    if sources:
        search_options['sources'] = [{'type': s} for s in sources]

    # Add categories if specified
    if category:
        search_options['categories'] = [{'type': c} for c in category]

    # Add time filter if specified
    if tbs:
        search_options['tbs'] = tbs

    # Add location if specified
    if location:
        search_options['location'] = location

    # Add scrape options if scraping is enabled
    if scrape:
        from firecrawl.types import ScrapeOptions
        search_options['scrape_options'] = ScrapeOptions(
            formats=list(formats)
        )

    # Show progress
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"Searching for '{query}'...", total=None)

        try:
            client = get_firecrawl_client()
            result = client.search(**search_options)

            progress.update(task, completed=True)

            # Process results
            total_results = 0
            if hasattr(result, 'web') and result.web:
                total_results += len(result.web)
            if hasattr(result, 'news') and result.news:
                total_results += len(result.news)
            if hasattr(result, 'images') and result.images:
                total_results += len(result.images)

            if total_results == 0:
                console.print("[yellow]No results found[/yellow]")
                return

            console.print(f"[green]✓ Found {total_results} results[/green]")

            # Display results based on output mode
            if pretty and not output and not json_output:
                _display_search_results(result, scrape)

            # Prepare data for output
            output_data = {}
            if hasattr(result, 'web') and result.web:
                output_data['web'] = [_format_result(r, scrape) for r in result.web]
            if hasattr(result, 'news') and result.news:
                output_data['news'] = [_format_result(r, scrape) for r in result.news]
            if hasattr(result, 'images') and result.images:
                output_data['images'] = [_format_result(r, scrape) for r in result.images]

            # Handle output
            if output or json_output:
                handle_output(
                    output_data,
                    output_file=output,
                    json_output=True,
                    pretty=pretty,
                    format_type='json'
                )
            elif not pretty:
                # Plain output for piping
                for source_type, results in output_data.items():
                    for r in results:
                        print(r.get('url', ''))

        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()


def _display_search_results(result, include_content: bool):
    """Display search results in a pretty table"""

    # Display web results
    if hasattr(result, 'web') and result.web:
        table = Table(title="🌐 Web Results")
        table.add_column("#", style="dim", width=4)
        table.add_column("Title", style="cyan", no_wrap=False)
        table.add_column("URL", style="blue", no_wrap=False)
        if include_content:
            table.add_column("Content", style="green")

        for i, item in enumerate(result.web, 1):
            title = getattr(item, 'title', 'No title')
            url = getattr(item, 'url', '')

            # Truncate for display
            title_display = title[:60] + '...' if len(title) > 60 else title
            url_display = url[:50] + '...' if len(url) > 50 else url

            if include_content:
                content = getattr(item, 'markdown', '')
                content_preview = content[:100] + '...' if content else 'N/A'
                table.add_row(str(i), title_display, url_display, content_preview)
            else:
                table.add_row(str(i), title_display, url_display)

        console.print(table)

    # Display news results
    if hasattr(result, 'news') and result.news:
        table = Table(title="📰 News Results")
        table.add_column("#", style="dim", width=4)
        table.add_column("Title", style="cyan", no_wrap=False)
        table.add_column("URL", style="blue", no_wrap=False)

        for i, item in enumerate(result.news, 1):
            title = getattr(item, 'title', 'No title')
            url = getattr(item, 'url', '')

            title_display = title[:60] + '...' if len(title) > 60 else title
            url_display = url[:50] + '...' if len(url) > 50 else url

            table.add_row(str(i), title_display, url_display)

        console.print(table)

    # Display image results
    if hasattr(result, 'images') and result.images:
        table = Table(title="🖼️  Image Results")
        table.add_column("#", style="dim", width=4)
        table.add_column("Title", style="cyan", no_wrap=False)
        table.add_column("URL", style="blue", no_wrap=False)

        for i, item in enumerate(result.images, 1):
            title = getattr(item, 'title', 'No title')
            url = getattr(item, 'url', '')

            title_display = title[:60] + '...' if len(title) > 60 else title
            url_display = url[:50] + '...' if len(url) > 50 else url

            table.add_row(str(i), title_display, url_display)

        console.print(table)


def _format_result(item, include_content: bool) -> dict:
    """Format a search result item for JSON output"""
    result = {}

    # Basic fields
    if hasattr(item, 'url'):
        result['url'] = item.url
    if hasattr(item, 'title'):
        result['title'] = item.title
    if hasattr(item, 'description'):
        result['description'] = item.description

    # Content fields (if scraped)
    if include_content:
        if hasattr(item, 'markdown'):
            result['markdown'] = item.markdown
        if hasattr(item, 'html'):
            result['html'] = item.html
        if hasattr(item, 'links'):
            result['links'] = item.links
        if hasattr(item, 'metadata'):
            result['metadata'] = item.metadata.__dict__ if hasattr(item.metadata, '__dict__') else item.metadata

    return result