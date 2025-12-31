"""Output handling utilities for fcrawl"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, List
from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich import print as rprint
import pyperclip

console = Console()


def strip_links(content: str) -> str:
    """Remove markdown links, preserving display text. Images get [Image: alt] marker."""
    # Nested image links: [![alt](img)](url) → [Image: alt]
    content = re.sub(r'\[!\[([^\]]*)\]\([^)]+\)\]\([^)]+\)', r'[Image: \1]', content)
    # Images with alt: ![alt](url) → [Image: alt]
    content = re.sub(r'!\[([^\]]+)\]\([^)]+\)', r'[Image: \1]', content)
    # Images without alt: ![](url) → [Image]
    content = re.sub(r'!\[\]\([^)]+\)', '[Image]', content)
    # Wikipedia-style citations: [\[N\]](url) or [[note N]](url) → [N] or [note N]
    content = re.sub(r'\[\\?\[([^\]]+)\\?\]\]\([^)]+\)', r'[\1]', content)
    # Links with brackets inside text: [text [with brackets]](url) → text [with brackets]
    content = re.sub(r'\[([^\]]+(?:\[[^\]]+\][^\]]*)*)\]\(https?://[^)]+(?:\s+"[^"]*")?\)', r'\1', content)
    # Run link removal multiple times to handle remaining cases
    for _ in range(3):
        # Links: [text](url) → text (handles simple cases)
        content = re.sub(r'\[([^\[\]]+)\]\([^)]+\)', r'\1', content)
    # Clean up stray escaped brackets: \[ → [ and \] → ]
    content = re.sub(r'\\([\[\]])', r'\1', content)
    return content

def display_content(content: Any, format_type: str = 'markdown', pretty: bool = True):
    """Display content in the terminal with formatting"""
    if not pretty or not sys.stdout.isatty():
        # Plain output for pipes or non-interactive
        print(content)
        return

    if format_type == 'markdown':
        md = Markdown(content)
        console.print(md)
    elif format_type == 'json':
        if isinstance(content, str):
            content = json.loads(content)
        syntax = Syntax(json.dumps(content, indent=2), "json", theme="monokai")
        console.print(syntax)
    elif format_type == 'html':
        syntax = Syntax(content, "html", theme="monokai")
        console.print(syntax)
    elif format_type == 'links':
        if isinstance(content, list):
            table = Table(title="Links Found")
            table.add_column("URL", style="cyan")
            for link in content:
                table.add_row(link if isinstance(link, str) else link.get('url', str(link)))
            console.print(table)
        else:
            console.print(content)
    else:
        console.print(content)

def save_to_file(content: Any, filepath: str, format_type: str = 'markdown'):
    """Save content to a file"""
    path = Path(filepath)

    # Create parent directories if they don't exist
    path.parent.mkdir(parents=True, exist_ok=True)

    if format_type == 'json':
        with open(path, 'w') as f:
            if isinstance(content, str):
                f.write(content)
            else:
                json.dump(content, f, indent=2)
    else:
        with open(path, 'w') as f:
            f.write(str(content))

    console.print(f"[green]✓ Saved to {filepath}[/green]")

def copy_to_clipboard(content: Any):
    """Copy content to clipboard"""
    try:
        text = str(content) if not isinstance(content, str) else content
        pyperclip.copy(text)
        console.print("[green]✓ Copied to clipboard[/green]")
    except Exception as e:
        console.print(f"[red]Failed to copy to clipboard: {e}[/red]")

def format_result(result: Any, formats: List[str]) -> Dict[str, Any]:
    """Format Firecrawl result based on requested formats"""
    formatted = {}

    if hasattr(result, 'markdown') and 'markdown' in formats:
        formatted['markdown'] = result.markdown
    if hasattr(result, 'html') and 'html' in formats:
        formatted['html'] = result.html
    if hasattr(result, 'links') and 'links' in formats:
        formatted['links'] = result.links
    if hasattr(result, 'screenshot') and 'screenshot' in formats:
        formatted['screenshot'] = result.screenshot
    if hasattr(result, 'metadata'):
        formatted['metadata'] = result.metadata

    return formatted

def handle_output(
    content: Any,
    output_file: Optional[str] = None,
    copy: bool = False,
    json_output: bool = False,
    pretty: bool = True,
    format_type: str = 'markdown'
):
    """Handle all output options"""
    # Prepare content for output
    if json_output:
        if hasattr(content, '__dict__'):
            content = content.__dict__
        output_content = json.dumps(content, indent=2 if pretty else None)
        format_type = 'json'
    else:
        if isinstance(content, dict):
            # If multiple formats, use the first available
            for fmt in ['markdown', 'html', 'links']:
                if fmt in content:
                    output_content = content[fmt]
                    format_type = fmt
                    break
            else:
                output_content = str(content)
        else:
            output_content = content

    # Handle output destinations
    if output_file:
        save_to_file(output_content, output_file, format_type)

    if copy:
        copy_to_clipboard(output_content)

    if not output_file or sys.stdout.isatty():
        # Display in terminal if not saving to file, or if interactive
        display_content(output_content, format_type, pretty)