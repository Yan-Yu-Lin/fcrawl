"""YouTube transcript command for fcrawl"""

import json
import re
import sys
import tempfile
import os
from typing import Optional

import click
import yt_dlp
from rich.progress import Progress, SpinnerColumn, TextColumn

from ..utils.output import handle_output, console


class YouTubeTranscriptDownloader:
    """Download transcripts from YouTube videos using yt-dlp's native downloader"""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet

    def log(self, msg: str):
        if not self.quiet:
            console.print(f"[dim]{msg}[/dim]", highlight=False)

    def extract_video_id(self, url: str) -> Optional[str]:
        """Extract video ID from various YouTube URL formats"""
        patterns = [
            r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
            r'(?:embed\/)([0-9A-Za-z_-]{11})',
            r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})'
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    def get_transcript(self, video_url: str, preferred_lang: Optional[str] = None) -> dict:
        """Get transcript and metadata from a YouTube video using yt-dlp's native downloader"""

        # First, get video info to find available languages
        info_opts = {
            'skip_download': True,
            'quiet': True,
            'no_warnings': True,
        }

        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)

                title = info.get('title', 'Unknown')
                video_id = info.get('id', '')
                duration = info.get('duration', 0)
                channel = info.get('channel', info.get('uploader', 'Unknown'))
                description = info.get('description', '')
                channel_id = info.get('channel_id', '')
                upload_date = info.get('upload_date', '')
                view_count = info.get('view_count', 0)

                # Get captions
                auto_captions = info.get('automatic_captions', {})
                subtitles = info.get('subtitles', {})

                # Merge manual subtitles (they take priority)
                all_captions = {**auto_captions, **subtitles}

                if not all_captions:
                    return {"error": "No subtitles available for this video"}

                available_langs = list(all_captions.keys())

                # Select language
                selected_lang = None
                original_lang = info.get('language') or info.get('original_language')

                if preferred_lang:
                    if preferred_lang in all_captions:
                        selected_lang = preferred_lang
                    else:
                        # Partial match
                        for lang in all_captions:
                            if preferred_lang.lower() in lang.lower():
                                selected_lang = lang
                                break
                        if not selected_lang:
                            return {
                                "error": f"Language '{preferred_lang}' not found",
                                "available_languages": available_langs
                            }
                else:
                    # Auto-select: original language > English > first available
                    if original_lang and original_lang in all_captions:
                        selected_lang = original_lang
                    else:
                        for lang in ['en', 'en-US', 'en-GB']:
                            if lang in all_captions:
                                selected_lang = lang
                                break
                        if not selected_lang:
                            selected_lang = available_langs[0]

            # Now download subtitles using yt-dlp's native downloader
            with tempfile.TemporaryDirectory() as tmpdir:
                sub_opts = {
                    'skip_download': True,
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': [selected_lang],
                    'subtitlesformat': 'vtt/best',
                    'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
                    'quiet': True,
                    'no_warnings': True,
                    'noprogress': True,
                    'logger': type('QuietLogger', (), {'debug': lambda *a: None, 'warning': lambda *a: None, 'error': lambda *a: None})(),
                }

                with yt_dlp.YoutubeDL(sub_opts) as ydl:
                    ydl.download([video_url])

                # Find the downloaded subtitle file
                subtitle_content = None
                for filename in os.listdir(tmpdir):
                    if filename.endswith('.vtt') or filename.endswith('.srt'):
                        with open(os.path.join(tmpdir, filename), 'r', encoding='utf-8') as f:
                            subtitle_content = f.read()
                        break
                    elif filename.endswith('.json3'):
                        with open(os.path.join(tmpdir, filename), 'r', encoding='utf-8') as f:
                            subtitle_content = f.read()
                        # Parse JSON3 format
                        transcript = self._parse_json3(subtitle_content)
                        return {
                            "title": title,
                            "video_id": video_id,
                            "channel": channel,
                            "channel_id": channel_id,
                            "description": description,
                            "upload_date": upload_date,
                            "view_count": view_count,
                            "duration": duration,
                            "language": selected_lang,
                            "available_languages": available_langs,
                            "transcript": transcript
                        }

                if not subtitle_content:
                    return {"error": "Could not download subtitles"}

                # Parse VTT format
                transcript = self._parse_vtt(subtitle_content)

                return {
                    "title": title,
                    "video_id": video_id,
                    "channel": channel,
                    "channel_id": channel_id,
                    "description": description,
                    "upload_date": upload_date,
                    "view_count": view_count,
                    "duration": duration,
                    "language": selected_lang,
                    "available_languages": available_langs,
                    "transcript": transcript
                }

        except yt_dlp.utils.DownloadError as e:
            return {"error": f"Download error: {str(e)}"}
        except Exception as e:
            return {"error": f"Error: {str(e)}"}

    def _parse_vtt(self, text: str) -> str:
        """Parse VTT subtitle format to plain text"""
        lines = []
        for line in text.split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('WEBVTT'):
                continue
            if stripped.startswith('Kind:') or stripped.startswith('Language:') or stripped.startswith('NOTE'):
                continue
            if '-->' in stripped:
                continue
            if stripped.isdigit():
                continue

            # Clean HTML tags
            clean = re.sub('<[^>]+>', '', stripped)
            clean = clean.replace('&nbsp;', ' ')
            clean = clean.replace('&amp;', '&')
            clean = clean.replace('&lt;', '<')
            clean = clean.replace('&gt;', '>')
            clean = clean.replace('&quot;', '"')

            if clean:
                lines.append(clean)

        # Remove consecutive duplicates
        result = []
        prev = None
        for line in lines:
            if line != prev:
                result.append(line)
                prev = line

        return '\n'.join(result)

    def _parse_json3(self, text: str) -> str:
        """Parse JSON3 subtitle format to plain text"""
        try:
            data = json.loads(text)
            lines = []

            if 'events' in data:
                for event in data['events']:
                    if 'segs' not in event:
                        continue
                    parts = []
                    for seg in event.get('segs', []):
                        if 'utf8' in seg:
                            parts.append(seg['utf8'])
                    if parts:
                        line = ''.join(parts).strip()
                        if line:
                            lines.append(line)

            # Remove consecutive duplicates
            result = []
            prev = None
            for line in lines:
                if line != prev:
                    result.append(line)
                    prev = line

            return '\n'.join(result)

        except json.JSONDecodeError:
            return self._parse_vtt(text)


@click.command('yt-transcript')
@click.argument('url')
@click.option('-l', '--lang', help='Preferred language code (e.g., en, zh-Hant)')
@click.option('--list-langs', is_flag=True, help='List available languages')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('-o', '--output', help='Save output to file')
@click.option('--copy', is_flag=True, help='Copy to clipboard')
@click.option('-q', '--quiet', is_flag=True, help='Suppress progress messages')
def yt_transcript(
    url: str,
    lang: Optional[str],
    list_langs: bool,
    json_output: bool,
    output: Optional[str],
    copy: bool,
    quiet: bool
):
    """Download transcript from a YouTube video

    Examples:
        fcrawl yt-transcript "https://youtube.com/watch?v=VIDEO_ID"
        fcrawl yt-transcript "https://youtu.be/VIDEO_ID" --lang zh-Hant
        fcrawl yt-transcript "https://youtube.com/watch?v=VIDEO_ID" --list-langs
        fcrawl yt-transcript "https://youtube.com/watch?v=VIDEO_ID" --json
    """
    downloader = YouTubeTranscriptDownloader(quiet=quiet or json_output)

    # Validate URL
    video_id = downloader.extract_video_id(url)
    if not video_id:
        console.print("[red]Error: Invalid YouTube URL[/red]")
        raise click.Abort()

    # Show progress (unless quiet or json output)
    is_tty = sys.stdout.isatty()
    show_progress = is_tty and not quiet and not json_output

    if show_progress:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            progress.add_task(f"Fetching transcript...", total=None)
            result = downloader.get_transcript(url, preferred_lang=lang)
    else:
        result = downloader.get_transcript(url, preferred_lang=lang)

    # Handle errors
    if "error" in result:
        if json_output:
            print(json.dumps(result, indent=2))
        else:
            console.print(f"[red]Error: {result['error']}[/red]")
            if "available_languages" in result:
                console.print(f"[yellow]Available: {', '.join(result['available_languages'])}[/yellow]")
        raise click.Abort()

    # List languages only
    if list_langs:
        if json_output:
            print(json.dumps({"available_languages": result["available_languages"]}, indent=2))
        else:
            console.print("[bold]Available languages:[/bold]")
            for language in result["available_languages"]:
                console.print(f"  {language}")
        return

    # Output
    if json_output:
        content = result
        format_type = 'json'
    else:
        # Format duration
        duration = result.get('duration', 0)
        mins, secs = divmod(duration, 60)
        hours, mins = divmod(mins, 60)
        duration_str = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"

        # Format upload date (YYYYMMDD -> YYYY-MM-DD)
        upload_date = result.get('upload_date', '')
        if len(upload_date) == 8:
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

        # Format view count with commas
        view_count = result.get('view_count', 0)
        view_count_str = f"{view_count:,}" if view_count else "N/A"

        # Build metadata header
        lines = [
            f"Title: {result['title']}",
            f"Channel: {result['channel']} ({result.get('channel_id', 'N/A')})",
            f"Upload Date: {upload_date or 'N/A'}",
            f"Duration: {duration_str}",
            f"Views: {view_count_str}",
        ]
        if result.get('description'):
            lines.append(f"Description: {result['description']}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(result["transcript"])
        content = "\n".join(lines)
        format_type = 'markdown'

    handle_output(
        content,
        output_file=output,
        copy=copy,
        json_output=json_output,
        pretty=is_tty,
        format_type=format_type
    )
