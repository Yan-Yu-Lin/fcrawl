"""Map command for fcrawl"""

import click
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.console import Console
from typing import Optional

from ..utils.config import get_firecrawl_client
from ..utils.output import handle_output, console, resolve_pretty


@click.command("map")
@click.argument("url")
@click.option("--search", help="Search for specific content in the sitemap")
@click.option("--limit", type=int, help="Limit number of URLs returned")
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--pretty/--no-pretty", default=None, help="Pretty print output")
@click.option(
    "--include-subdomains", is_flag=True, help="Include subdomains in the map"
)
def map_site(
    url: str,
    search: Optional[str],
    limit: Optional[int],
    output: Optional[str],
    json_output: bool,
    pretty: Optional[bool],
    include_subdomains: bool,
):
    """Map a website to discover all URLs

    Examples:
        fcrawl map https://docs.site.com
        fcrawl map https://docs.site.com --search "api"
        fcrawl map https://site.com --limit 100
    """
    pretty = resolve_pretty(pretty)

    # Prepare map options
    map_options = {}

    if search:
        map_options["search"] = search

    if limit:
        map_options["limit"] = limit

    if include_subdomains:
        map_options["includeSubdomains"] = True

    # Show progress
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Mapping {url}...", total=None)

        try:
            client = get_firecrawl_client()
            result = client.map(url, **map_options)

            progress.stop()

            # Helper to extract URL from link object
            def get_url(link):
                if isinstance(link, dict):
                    return link.get("url", str(link))
                elif hasattr(link, "url"):
                    return link.url
                else:
                    return str(link)

            # Process results
            if hasattr(result, "links") and result.links:
                console.print(f"[green]✓ Found {len(result.links)} URLs[/green]")

                if pretty and not output and not json_output:
                    # Display as clean list (no table)
                    console.print(f"\n[bold]Site Map - {url}[/bold]", justify="center")
                    console.print("─" * 60)

                    for i, link in enumerate(result.links[:50], 1):
                        url_str = get_url(link)
                        console.print(f"[dim]{i:3}.[/dim] [cyan]{url_str}[/cyan]")

                    console.print("─" * 60)

                    if len(result.links) > 50:
                        console.print(
                            f"[dim]... and {len(result.links) - 50} more URLs[/dim]"
                        )

                # Handle file/JSON output
                if output or json_output:
                    links_data = [{"url": get_url(link)} for link in result.links]
                    handle_output(
                        links_data,
                        output_file=output,
                        json_output=True,
                        pretty=pretty,
                        format_type="json",
                    )

            else:
                console.print("[yellow]No URLs found[/yellow]")

        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()
