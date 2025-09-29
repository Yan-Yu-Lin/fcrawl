"""Map command for fcrawl"""

import click
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.console import Console
from rich.table import Table
from typing import Optional

from utils.config import get_firecrawl_client
from utils.output import handle_output, console

@click.command('map')
@click.argument('url')
@click.option('--search', help='Search for specific content in the sitemap')
@click.option('--limit', type=int, help='Limit number of URLs returned')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--pretty/--no-pretty', default=True, help='Pretty print output')
@click.option('--include-subdomains', is_flag=True, help='Include subdomains in the map')
def map_site(
    url: str,
    search: Optional[str],
    limit: Optional[int],
    output: Optional[str],
    json_output: bool,
    pretty: bool,
    include_subdomains: bool
):
    """Map a website to discover all URLs

    Examples:
        fcrawl map https://docs.site.com
        fcrawl map https://docs.site.com --search "api"
        fcrawl map https://site.com --limit 100
    """
    # Prepare map options
    map_options = {}

    if search:
        map_options['search'] = search

    if limit:
        map_options['limit'] = limit

    if include_subdomains:
        map_options['includeSubdomains'] = True

    # Show progress
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"Mapping {url}...", total=None)

        try:
            client = get_firecrawl_client()
            result = client.map(url, **map_options)

            progress.update(task, completed=True)

            # Process results
            if hasattr(result, 'links') and result.links:
                console.print(f"[green]✓ Found {len(result.links)} URLs[/green]")

                if pretty and not output and not json_output:
                    # Display as table
                    table = Table(title=f"Site Map - {url}")
                    table.add_column("#", style="dim", width=6)
                    table.add_column("URL", style="cyan", no_wrap=False)

                    for i, link in enumerate(result.links[:50], 1):  # Show first 50
                        if isinstance(link, dict):
                            url_str = link.get('url', str(link))
                        else:
                            url_str = str(link)
                        table.add_row(str(i), url_str)

                    console.print(table)

                    if len(result.links) > 50:
                        console.print(f"\n[dim]... and {len(result.links) - 50} more URLs[/dim]")

                # Prepare data for output
                links_data = []
                for link in result.links:
                    if isinstance(link, dict):
                        links_data.append(link)
                    else:
                        links_data.append({'url': str(link)})

                handle_output(
                    links_data if json_output or output else result.links,
                    output_file=output,
                    json_output=json_output,
                    pretty=pretty,
                    format_type='links' if not json_output else 'json'
                )

            else:
                console.print("[yellow]No URLs found[/yellow]")

        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()