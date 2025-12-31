"""Extract command for fcrawl"""

import click
import json
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.console import Console
from typing import Optional, List

from ..utils.config import get_firecrawl_client
from ..utils.output import handle_output, console

@click.command()
@click.argument('urls', nargs=-1, required=True)
@click.option('--prompt', help='Extraction prompt for the AI')
@click.option('--fields', help='Comma-separated list of fields to extract')
@click.option('--schema', help='JSON schema file for extraction')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--pretty/--no-pretty', default=True, help='Pretty print output')
def extract(
    urls: List[str],
    prompt: Optional[str],
    fields: Optional[str],
    schema: Optional[str],
    output: Optional[str],
    json_output: bool,
    pretty: bool
):
    """Extract structured data from URLs using AI

    \b
    TIP: For flexible extraction, you can also use Claude Agent with
    Firecrawl MCP tools: scrape a page and ask Claude to extract/summarize
    the content in any format you need.

    \b
    Examples:
        fcrawl extract https://store.com --fields "price,title,description"
        fcrawl extract https://store.com --prompt "Extract product information"
        fcrawl extract url1 url2 url3 --schema schema.json
    """
    # Prepare extraction options
    extract_options = {
        'urls': list(urls)
    }

    # Build prompt from fields if provided
    if fields and not prompt:
        field_list = fields.split(',')
        prompt = f"Extract the following information: {', '.join(field_list)}"

    if prompt:
        extract_options['prompt'] = prompt

    # Load schema if provided
    if schema:
        try:
            with open(schema, 'r') as f:
                schema_data = json.load(f)
                extract_options['schema'] = schema_data
        except Exception as e:
            console.print(f"[red]Error loading schema file: {e}[/red]")
            raise click.Abort()

    if not prompt and not schema:
        console.print("[red]Error: Either --prompt, --fields, or --schema must be provided[/red]")
        raise click.Abort()

    # Show progress
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"Extracting from {len(urls)} URL(s)...", total=None)

        try:
            client = get_firecrawl_client()

            # Note: The extract method might be async/job-based
            # This is a simplified version - might need polling
            console.print(f"[cyan]Starting extraction from {len(urls)} URLs...[/cyan]")

            # For v2 API, extract returns immediately with the data
            # (unlike the v1 API which required polling)
            result = client.extract(**extract_options)

            progress.update(task, completed=True)

            # Process results
            if result:
                console.print(f"[green]✓ Extraction completed successfully[/green]")

                # Handle output
                handle_output(
                    result.data if hasattr(result, 'data') else result,
                    output_file=output,
                    json_output=True,  # Always JSON for structured data
                    pretty=pretty,
                    format_type='json'
                )
            else:
                console.print("[yellow]No data extracted[/yellow]")

        except AttributeError:
            # Extract might not be available or might work differently
            progress.stop()
            console.print("[yellow]Note: Extract feature may require API key or may not be available in self-hosted mode[/yellow]")
            console.print("[cyan]Alternative: Use 'fcrawl scrape' with format options for basic extraction[/cyan]")

        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()