"""Scrape command for fcrawl"""

import re
import click
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.console import Console
from typing import List, Optional, Tuple

from ..utils.config import get_firecrawl_client
from ..utils.output import handle_output, console, strip_links


# Article mode: aggressive filtering for clean article extraction
ARTICLE_INCLUDE_TAGS = [
    "article",
    "main",
    ".post-content",
    ".post-body",
    ".entry-content",
    ".article-content",
    ".article-body",
    ".content-body",
    "#content",
    "#article",
    "[role='main']",
]

ARTICLE_EXCLUDE_TAGS = [
    "nav",
    "header",
    "footer",
    "aside",
    ".sidebar",
    ".share",
    ".social",
    ".social-share",
    ".share-buttons",
    ".popup",
    ".modal",
    ".newsletter",
    ".subscribe",
    ".comments",
    ".comment-section",
    "#comments",
    ".related",
    ".related-posts",
    ".recommended",
    ".advertisement",
    ".ads",
    ".ad",
    ".cookie",
    ".banner",
    ".promo",
    ".cta",
]


def clean_article_content(content: str) -> str:
    """Post-process markdown to remove common noise patterns"""
    lines = content.split('\n')
    cleaned_lines = []

    # Patterns to skip
    skip_patterns = [
        r'^Close$',  # Popup close buttons
        r'^\s*\*\s*\[\s*\]\(https?://(www\.)?(twitter|facebook|linkedin|reddit|pinterest|x\.com)',  # Empty share links
        r'^\s*\*\s*\[.*\]\(https?://(www\.)?(twitter\.com/share|facebook\.com/sharer|linkedin\.com/share|reddit\.com/submit)',  # Share links with text
        r'^Share (on|this|article)',  # Share text
        r'^(Tweet|Pin|Share|Follow us)',  # Social CTAs
        r'^\s*US\s*$',  # Random country codes from popups
        r'^Start Now$',  # CTA buttons
        r'^Subscribe',  # Newsletter prompts
        r'^Sign up',  # Sign up prompts
        r'^\s*$',  # Empty lines at start (will be handled by consecutive empty check)
    ]

    skip_regex = [re.compile(p, re.IGNORECASE) for p in skip_patterns]

    # Track consecutive empty lines
    prev_empty = False

    for line in lines:
        # Check if line matches any skip pattern
        should_skip = any(r.search(line) for r in skip_regex)

        if should_skip:
            continue

        # Collapse multiple empty lines
        is_empty = line.strip() == ''
        if is_empty and prev_empty:
            continue

        cleaned_lines.append(line)
        prev_empty = is_empty

    return '\n'.join(cleaned_lines)


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
@click.option('-i', '--include', 'include_tags', multiple=True,
              help='CSS selectors to include (can specify multiple)')
@click.option('-e', '--exclude', 'exclude_tags', multiple=True,
              help='CSS selectors to exclude (can specify multiple)')
@click.option('--article', is_flag=True,
              help='Article mode: aggressive filtering for clean article extraction')
@click.option('--raw', is_flag=True,
              help='Raw mode: disable all content filtering')
@click.option('--no-links', is_flag=True,
              help='Strip markdown links, keep display text')
@click.option('--wait', type=int, help='Wait time in milliseconds before scraping')
@click.option('--screenshot-full', is_flag=True, help='Take full page screenshot')
def scrape(
    url: str,
    formats: List[str],
    output: Optional[str],
    copy: bool,
    json_output: bool,
    pretty: bool,
    include_tags: Tuple[str, ...],
    exclude_tags: Tuple[str, ...],
    article: bool,
    raw: bool,
    no_links: bool,
    wait: Optional[int],
    screenshot_full: bool,
):
    """Scrape a single URL and extract content

    Examples:
        fcrawl scrape https://example.com
        fcrawl scrape https://example.com --article
        fcrawl scrape https://example.com -i ".content" -e ".sidebar"
        fcrawl scrape https://example.com --raw
        fcrawl scrape https://example.com -f markdown -f links
        fcrawl scrape https://example.com -o output.md --copy
    """
    # Prepare scrape options
    scrape_options = {
        'formats': list(formats)
    }

    # Handle content filtering modes
    if raw:
        # Disable all filtering
        scrape_options['only_main_content'] = False
    elif article:
        # Aggressive article mode
        scrape_options['include_tags'] = list(ARTICLE_INCLUDE_TAGS)
        scrape_options['exclude_tags'] = list(ARTICLE_EXCLUDE_TAGS)
        scrape_options['only_main_content'] = False  # We handle filtering ourselves
    else:
        # Custom include/exclude tags
        if include_tags:
            scrape_options['include_tags'] = list(include_tags)
        if exclude_tags:
            scrape_options['exclude_tags'] = list(exclude_tags)

    if wait:
        scrape_options['wait_for'] = wait

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
            # Article mode: apply post-processing cleanup
            if article:
                content = clean_article_content(content)
            # Strip links if requested
            if no_links:
                content = strip_links(content)
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