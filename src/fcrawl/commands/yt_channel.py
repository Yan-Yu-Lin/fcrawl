"""YouTube channel explorer command for fcrawl"""

import json
import re
import sys
from typing import Optional

import click
import yt_dlp
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..utils.output import console


class YouTubeChannelExplorer:
    """Explore a YouTuber's channel - list videos, search, sort by views"""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet

    def log(self, msg: str):
        if not self.quiet:
            console.print(f"[dim]{msg}[/dim]", highlight=False)

    def normalize_channel_url(self, channel: str) -> str:
        """Convert various channel formats to a proper URL"""
        channel = channel.strip()

        # Already a full URL
        if channel.startswith("http"):
            # Ensure we have /videos suffix for proper listing
            if "/videos" not in channel and "/shorts" not in channel and "/streams" not in channel:
                channel = channel.rstrip("/") + "/videos"
            return channel

        # Handle @username format
        if channel.startswith("@"):
            return f"https://www.youtube.com/{channel}/videos"

        # Handle channel ID (UC...)
        if channel.startswith("UC") and len(channel) == 24:
            return f"https://www.youtube.com/channel/{channel}/videos"

        # Assume it's a handle without @
        return f"https://www.youtube.com/@{channel}/videos"

    def get_channel_videos(
        self,
        channel_url: str,
        limit: Optional[int] = None,
        sort_by: str = "recency",
        search: Optional[str] = None,
        content_type: str = "videos",
        with_dates: bool = False,
    ) -> dict:
        """Get videos from a channel with optional filtering and sorting"""

        # Adjust URL for content type
        base_url = re.sub(r"/(videos|shorts|streams)/?$", "", channel_url)
        if content_type == "shorts":
            channel_url = base_url + "/shorts"
        elif content_type == "streams":
            channel_url = base_url + "/streams"
        else:
            channel_url = base_url + "/videos"

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': not with_dates,  # Flat is faster but less info
            'ignoreerrors': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)

                if not info:
                    return {"error": "Could not fetch channel information"}

                entries = info.get('entries', [])
                if not entries:
                    return {"error": "No videos found"}

                # Convert generator to list if needed
                entries = list(entries)

                channel_name = info.get('channel', info.get('uploader', 'Unknown'))
                channel_id = info.get('channel_id', info.get('uploader_id', ''))
                total_count = len(entries)

                # Process entries
                videos = []
                for i, entry in enumerate(entries):
                    if entry is None:
                        continue

                    video = {
                        "index": i + 1,
                        "id": entry.get('id', ''),
                        "title": entry.get('title', 'Unknown'),
                        "url": f"https://youtube.com/watch?v={entry.get('id', '')}",
                        "duration": entry.get('duration') or 0,
                        "duration_human": self._format_duration(entry.get('duration')),
                        "views": entry.get('view_count') or 0,
                    }

                    # Add upload date if available (slower fetch mode)
                    if with_dates and entry.get('upload_date'):
                        video["upload_date"] = entry.get('upload_date')

                    # Add description snippet if available
                    desc = entry.get('description', '')
                    if desc:
                        video["description_snippet"] = desc[:200] + "..." if len(desc) > 200 else desc

                    videos.append(video)

                # Filter by search term
                if search:
                    search_lower = search.lower()
                    videos = [
                        v for v in videos
                        if search_lower in v['title'].lower()
                        or search_lower in v.get('description_snippet', '').lower()
                    ]

                # Sort
                if sort_by == "views":
                    videos.sort(key=lambda x: x['views'], reverse=True)
                elif sort_by == "duration":
                    videos.sort(key=lambda x: x['duration'], reverse=True)
                elif sort_by == "duration_asc":
                    videos.sort(key=lambda x: x['duration'])
                # recency is already the default order from YouTube

                # Apply limit
                if limit and limit > 0:
                    videos = videos[:limit]

                return {
                    "channel": channel_name,
                    "channel_id": channel_id,
                    "channel_url": base_url,
                    "content_type": content_type,
                    "total_count": total_count,
                    "returned_count": len(videos),
                    "sort": sort_by,
                    "search": search,
                    "videos": videos,
                }

        except yt_dlp.utils.DownloadError as e:
            return {"error": f"Download error: {str(e)}"}
        except Exception as e:
            return {"error": f"Error: {str(e)}"}

    def _format_duration(self, seconds: Optional[float]) -> str:
        """Convert seconds to human-readable duration"""
        if not seconds:
            return "0:00"
        seconds = int(seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"


@click.command('yt-channel')
@click.argument('channel')
@click.option('-n', '--limit', type=int, default=20,
              help='Number of videos to return (default: 20, use 0 for all)')
@click.option('-s', '--sort', 'sort_by',
              type=click.Choice(['recency', 'views', 'duration', 'duration_asc']),
              default='recency', help='Sort order (default: recency)')
@click.option('-q', '--search', 'search_query', help='Filter videos by title keyword')
@click.option('-t', '--type', 'content_type',
              type=click.Choice(['videos', 'shorts', 'streams']),
              default='videos', help='Content type (default: videos)')
@click.option('--with-dates', is_flag=True, help='Include upload dates (slower)')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--ids-only', is_flag=True, help='Output only video IDs (one per line)')
@click.option('-o', '--output', help='Save output to file')
@click.option('--quiet', is_flag=True, help='Suppress progress messages')
def yt_channel(
    channel: str,
    limit: int,
    sort_by: str,
    search_query: Optional[str],
    content_type: str,
    with_dates: bool,
    json_output: bool,
    ids_only: bool,
    output: Optional[str],
    quiet: bool
):
    """Explore a YouTube channel's videos

    CHANNEL can be @handle, full URL, or channel ID (UC...).

    Examples:
        fcrawl yt-channel "@HealthyGamerGG"
        fcrawl yt-channel "@HealthyGamerGG" --limit 10
        fcrawl yt-channel "@HealthyGamerGG" --sort views
        fcrawl yt-channel "@HealthyGamerGG" --search "anxiety"
        fcrawl yt-channel "@HealthyGamerGG" --type shorts
        fcrawl yt-channel "@HealthyGamerGG" --json
        fcrawl yt-channel "@HealthyGamerGG" --ids-only
    """
    explorer = YouTubeChannelExplorer(quiet=quiet or json_output or ids_only)

    channel_url = explorer.normalize_channel_url(channel)

    # Use 0 as "no limit"
    effective_limit = limit if limit > 0 else None

    # Show progress (unless quiet or special output modes)
    is_tty = sys.stdout.isatty()
    show_progress = is_tty and not quiet and not json_output and not ids_only

    if show_progress:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            progress.add_task(f"Fetching channel videos...", total=None)
            result = explorer.get_channel_videos(
                channel_url,
                limit=effective_limit,
                sort_by=sort_by,
                search=search_query,
                content_type=content_type,
                with_dates=with_dates,
            )
    else:
        result = explorer.get_channel_videos(
            channel_url,
            limit=effective_limit,
            sort_by=sort_by,
            search=search_query,
            content_type=content_type,
            with_dates=with_dates,
        )

    # Handle errors
    if "error" in result:
        if json_output:
            print(json.dumps(result, indent=2))
        else:
            console.print(f"[red]Error: {result['error']}[/red]")
        raise click.Abort()

    # Output formats
    if ids_only:
        # Plain video IDs, one per line (for piping)
        output_text = '\n'.join(v["id"] for v in result["videos"])
        if output:
            with open(output, 'w') as f:
                f.write(output_text)
            console.print(f"[green]Saved to {output}[/green]")
        else:
            print(output_text)

    elif json_output:
        output_text = json.dumps(result, indent=2, ensure_ascii=False)
        if output:
            with open(output, 'w') as f:
                f.write(output_text)
            console.print(f"[green]Saved to {output}[/green]")
        else:
            print(output_text)

    else:
        # Human-readable output
        if is_tty:
            # Rich table with clickable titles
            console.print(f"[bold]Channel:[/bold] {result['channel']}")
            console.print(f"[bold]Total {result['content_type']}:[/bold] {result['total_count']}")
            console.print(f"[bold]Showing:[/bold] {result['returned_count']} (sorted by {result['sort']})")
            if result.get('search'):
                console.print(f"[bold]Search:[/bold] '{result['search']}'")
            console.print()

            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("#", style="dim", width=4)
            table.add_column("Duration", width=10)
            table.add_column("Title", no_wrap=False)
            table.add_column("Views", justify="right", width=12)

            for video in result["videos"]:
                views_str = f"{video['views']:,}" if video['views'] else "N/A"
                # Make title clickable with light blue color
                title = video['title'][:60] + ("..." if len(video['title']) > 60 else "")
                clickable_title = f"[link={video['url']}][bright_cyan]{title}[/bright_cyan][/link]"
                table.add_row(
                    str(video['index']),
                    video['duration_human'],
                    clickable_title,
                    views_str
                )

            console.print(table)

        else:
            print(f"Channel: {result['channel']}")
            print(f"Total {result['content_type']}: {result['total_count']}")
            print(f"Showing: {result['returned_count']} (sorted by {result['sort']})")
            if result.get('search'):
                print(f"Search: '{result['search']}'")
            print("-" * 60)

            for video in result["videos"]:
                views_str = f"{video['views']:,}" if video['views'] else "N/A"
                print(f"{video['index']:3d}. [{video['duration_human']:>8}] {video['title'][:50]}")
                print(f"     {views_str} views | {video['url']}")
                print()

        # Save to file if requested
        if output:
            with open(output, 'w') as f:
                f.write(json.dumps(result, indent=2, ensure_ascii=False))
            console.print(f"[green]Saved to {output}[/green]")
