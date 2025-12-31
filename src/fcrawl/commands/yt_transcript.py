"""YouTube transcript command for fcrawl"""

import json
import re
import sys
import time
from typing import Optional

import click
import requests
import yt_dlp

# Browser-like headers to avoid rate limiting
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}
from rich.progress import Progress, SpinnerColumn, TextColumn

from ..utils.output import handle_output, console


class YouTubeTranscriptDownloader:
    """Download transcripts from YouTube videos using yt-dlp"""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.ydl_opts = {
            'writeautomaticsub': True,
            'skip_download': True,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }

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
        """Get transcript and metadata from a YouTube video"""
        with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
            try:
                info = ydl.extract_info(video_url, download=False)

                title = info.get('title', 'Unknown')
                video_id = info.get('id', '')
                duration = info.get('duration', 0)
                channel = info.get('channel', info.get('uploader', 'Unknown'))

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

                # Get subtitle URL
                caption_url = None
                caption_format = None

                for caption in all_captions[selected_lang]:
                    ext = caption.get('ext')
                    if ext == 'vtt':
                        caption_url = caption['url']
                        caption_format = 'vtt'
                        break

                if not caption_url:
                    for caption in all_captions[selected_lang]:
                        ext = caption.get('ext')
                        if ext in ['srv1', 'srv2', 'srv3', 'json3']:
                            caption_url = caption['url']
                            caption_format = ext
                            break

                if not caption_url:
                    return {"error": "Could not get subtitle URL"}

                # Download subtitle with retry logic
                for attempt in range(3):
                    try:
                        response = requests.get(caption_url, headers=HEADERS, timeout=30)
                        response.raise_for_status()
                        break
                    except requests.exceptions.HTTPError as e:
                        if e.response.status_code == 429 and attempt < 2:
                            time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s
                            continue
                        raise
                    except requests.exceptions.RequestException:
                        if attempt < 2:
                            time.sleep(1)
                            continue
                        raise

                # Parse
                if caption_format == 'json3':
                    transcript = self._parse_json3(response.text)
                else:
                    transcript = self._parse_vtt(response.text)

                return {
                    "title": title,
                    "video_id": video_id,
                    "channel": channel,
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
        # Handle M3U8 playlist
        if text.startswith('#EXTM3U'):
            urls = re.findall(r'https://[^\s]+', text)
            if urls:
                try:
                    response = requests.get(urls[0], headers=HEADERS, timeout=30)
                    text = response.text
                except:
                    pass

        lines = []
        for line in text.split('\n'):
            if line.startswith('WEBVTT'):
                continue
            if '-->' in line:
                continue
            if not line.strip() or line.strip().isdigit():
                continue

            # Clean HTML tags
            clean = re.sub('<[^>]+>', '', line)
            clean = clean.replace('&nbsp;', ' ')
            clean = clean.replace('&amp;', '&')
            clean = clean.replace('&lt;', '<')
            clean = clean.replace('&gt;', '>')
            clean = clean.replace('&quot;', '"')

            if clean.strip():
                lines.append(clean.strip())

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
        content = result["transcript"]
        format_type = 'markdown'

    handle_output(
        content,
        output_file=output,
        copy=copy,
        json_output=json_output,
        pretty=is_tty,
        format_type=format_type
    )
