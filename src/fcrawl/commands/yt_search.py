"""YouTube video search command for fcrawl"""

import json
import sys
from typing import Optional

import click
import yt_dlp
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..utils.output import console, copy_to_clipboard


class YouTubeSearcher:
    """Search YouTube videos with yt-dlp search extractors"""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet

    def log(self, msg: str):
        if not self.quiet:
            console.print(f"[dim]{msg}[/dim]", highlight=False)

    def search_videos(
        self,
        query: str,
        limit: int = 20,
        sort_by: str = "relevance",
        with_dates: bool = False,
    ) -> dict:
        """Search YouTube videos and return normalized metadata"""
        if limit < 1:
            return {"error": "limit must be >= 1"}

        search_scheme = "ytsearchdate" if sort_by == "date" else "ytsearch"

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": not with_dates,  # Flat is faster but has less metadata
            "ignoreerrors": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    f"{search_scheme}{limit}:{query}", download=False
                )

            if not info:
                return {"error": "Could not fetch search results"}

            entries = list(info.get("entries", []))

            videos = []
            for entry in entries:
                if entry is None:
                    continue

                video_id = entry.get("id")
                if not video_id:
                    continue

                video = {
                    "id": video_id,
                    "title": entry.get("title", "Unknown"),
                    "url": f"https://youtube.com/watch?v={video_id}",
                    "duration": entry.get("duration") or 0,
                    "duration_human": self._format_duration(entry.get("duration")),
                    "views": entry.get("view_count") or 0,
                    "channel": entry.get("channel")
                    or entry.get("uploader")
                    or "Unknown",
                    "channel_handle": entry.get("uploader_id") or "",
                    "channel_id": entry.get("channel_id") or "",
                    "channel_url": entry.get("channel_url")
                    or entry.get("uploader_url")
                    or "",
                }

                if with_dates and entry.get("upload_date"):
                    video["upload_date"] = entry.get("upload_date")

                videos.append(video)

            # Native YouTube ordering is used for relevance/date.
            if sort_by == "views":
                videos.sort(key=lambda x: x["views"], reverse=True)
            elif sort_by == "duration":
                videos.sort(key=lambda x: x["duration"], reverse=True)
            elif sort_by == "duration_asc":
                videos.sort(key=lambda x: x["duration"])

            # Reindex after sorting so displayed numbering is always in visible order.
            for i, video in enumerate(videos, start=1):
                video["index"] = i

            return {
                "query": query,
                "sort": sort_by,
                "limit": limit,
                "with_dates": with_dates,
                "total_count": len(entries),
                "returned_count": len(videos),
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


def _format_upload_date(upload_date: str) -> str:
    """Format YYYYMMDD to YYYY-MM-DD"""
    if len(upload_date) == 8 and upload_date.isdigit():
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    return upload_date or "N/A"


def _channel_label(video: dict) -> str:
    """Build channel label with handle/ID when available"""
    channel = video.get("channel", "Unknown")
    handle = video.get("channel_handle")
    channel_id = video.get("channel_id")

    if handle:
        return f"{channel} ({handle})"
    if channel_id:
        return f"{channel} ({channel_id})"
    return channel


def _build_plain_output(result: dict) -> str:
    """Build plain text output for non-TTY/copy/file output"""
    lines = [
        f"Query: {result['query']}",
        f"Showing: {result['returned_count']} (sorted by {result['sort']})",
        "-" * 80,
    ]

    for video in result["videos"]:
        views_str = f"{video['views']:,}" if video["views"] else "N/A"
        channel_text = _channel_label(video)
        date_text = ""
        if result.get("with_dates"):
            date_text = f" | Date: {_format_upload_date(video.get('upload_date', ''))}"

        lines.append(
            f"{video['index']:3d}. [{video['duration_human']:>8}] {video['title']}"
        )
        lines.append(f"     Channel: {channel_text} | Views: {views_str}{date_text}")
        lines.append(f"     {video['url']}")
        lines.append("")

    return "\n".join(lines).rstrip()


@click.command("yt-search")
@click.argument("query")
@click.option(
    "-n",
    "--limit",
    type=int,
    default=20,
    help="Number of videos to return (default: 20)",
)
@click.option(
    "-s",
    "--sort",
    "sort_by",
    type=click.Choice(["relevance", "date", "views", "duration", "duration_asc"]),
    default="relevance",
    help="Sort order (default: relevance)",
)
@click.option("--with-dates", is_flag=True, help="Include upload dates (slower)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--ids-only", is_flag=True, help="Output only video IDs (one per line)")
@click.option("-o", "--output", help="Save output to file")
@click.option("--copy", is_flag=True, help="Copy output to clipboard")
@click.option("-q", "--quiet", is_flag=True, help="Suppress progress messages")
def yt_search(
    query: str,
    limit: int,
    sort_by: str,
    with_dates: bool,
    json_output: bool,
    ids_only: bool,
    output: Optional[str],
    copy: bool,
    quiet: bool,
):
    """Search YouTube videos.

    QUERY is a free-form YouTube search query.

    Examples:
        fcrawl yt-search "python tutorial"
        fcrawl yt-search "python tutorial" -n 10
        fcrawl yt-search "python tutorial" --sort date
        fcrawl yt-search "python tutorial" --sort views
        fcrawl yt-search "python tutorial" --json
        fcrawl yt-search "python tutorial" --ids-only
    """
    if limit < 1:
        raise click.BadParameter("limit must be >= 1", param_hint="--limit")

    searcher = YouTubeSearcher(quiet=quiet or json_output or ids_only)

    # Show progress (unless quiet or special output modes)
    is_tty = sys.stdout.isatty()
    show_progress = is_tty and not quiet and not json_output and not ids_only

    if show_progress:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task("Searching YouTube...", total=None)
            result = searcher.search_videos(
                query, limit=limit, sort_by=sort_by, with_dates=with_dates
            )
    else:
        result = searcher.search_videos(
            query, limit=limit, sort_by=sort_by, with_dates=with_dates
        )

    # Handle errors
    if "error" in result:
        if json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            console.print(f"[red]Error: {result['error']}[/red]")
        raise click.Abort()

    if ids_only:
        output_text = "\n".join(v["id"] for v in result["videos"])

        if output:
            with open(output, "w") as f:
                f.write(output_text)
            console.print(f"[green]Saved to {output}[/green]")
        else:
            print(output_text)

        if copy:
            copy_to_clipboard(output_text)
        return

    if json_output:
        output_text = json.dumps(result, indent=2, ensure_ascii=False)

        if output:
            with open(output, "w") as f:
                f.write(output_text)
            console.print(f"[green]Saved to {output}[/green]")
        else:
            print(output_text)

        if copy:
            copy_to_clipboard(output_text)
        return

    # Human-readable output (TTY table / plain text)
    plain_output = _build_plain_output(result)

    if is_tty:
        console.print(f"[bold]Query:[/bold] {result['query']}")
        console.print(
            f"[bold]Showing:[/bold] {result['returned_count']} (sorted by {result['sort']})"
        )
        console.print()

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("#", style="dim", width=4)
        table.add_column("Duration", width=10)
        if with_dates:
            table.add_column("Date", width=10)
        table.add_column("Title", no_wrap=False)
        table.add_column("Channel", no_wrap=False)
        table.add_column("Views", justify="right", width=12)

        for video in result["videos"]:
            views_str = f"{video['views']:,}" if video["views"] else "N/A"
            title = video["title"][:60] + ("..." if len(video["title"]) > 60 else "")
            clickable_title = (
                f"[link={video['url']}][bright_cyan]{title}[/bright_cyan][/link]"
            )
            channel_text = _channel_label(video)

            row = [
                str(video["index"]),
                video["duration_human"],
            ]
            if with_dates:
                row.append(_format_upload_date(video.get("upload_date", "")))
            row.extend(
                [
                    clickable_title,
                    channel_text,
                    views_str,
                ]
            )

            table.add_row(*row)

        console.print(table)
    else:
        print(plain_output)

    if output:
        with open(output, "w") as f:
            f.write(plain_output)
        console.print(f"[green]Saved to {output}[/green]")

    if copy:
        copy_to_clipboard(plain_output)
