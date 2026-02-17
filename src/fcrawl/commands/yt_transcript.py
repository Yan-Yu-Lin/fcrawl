"""YouTube transcript command for fcrawl

Features:
- Download YouTube subtitles/captions (manual or auto-generated)
- Auto-fallback to local SenseVoice transcription when no subtitles
- Traditional Chinese output by default (configurable)
- Cookie support for age-restricted/region-locked videos
"""

import json
import re
import sys
import tempfile
import os
from typing import Any, Optional, cast

import click
import yt_dlp
from rich.progress import Progress, SpinnerColumn, TextColumn

from ..utils.output import handle_output, console
from ..utils.config import load_config


class YouTubeTranscriptDownloader:
    """Download transcripts from YouTube videos using yt-dlp's native downloader.

    Features:
    - Fetch manual or auto-generated subtitles
    - Auto-fallback to local SenseVoice transcription when no subtitles
    - Traditional Chinese conversion by default
    - Cookie support for restricted videos
    """

    def __init__(
        self,
        quiet: bool = False,
        cookies_file: Optional[str] = None,
        cookies_from_browser: Optional[str] = None,
        cookies_on_fail: bool = True,
        prefer_cookies: bool = False,
        no_transcribe: bool = False,
        force_transcribe: bool = False,
        simplified: bool = False,
    ):
        self.quiet = quiet
        self.cookies_file = cookies_file
        self.cookies_from_browser = cookies_from_browser
        self.cookies_on_fail = cookies_on_fail
        self.prefer_cookies = prefer_cookies
        self.no_transcribe = no_transcribe
        self.force_transcribe = force_transcribe
        self.simplified = simplified

    def log(self, msg: str):
        if not self.quiet:
            console.print(f"[dim]{msg}[/dim]", highlight=False)

    def _has_cookies(self) -> bool:
        return bool(self.cookies_file or self.cookies_from_browser)

    def _should_retry_with_cookies(self, err: BaseException) -> bool:
        if not self.cookies_on_fail or not self._has_cookies():
            return False

        msg = str(err).lower()
        needles = [
            "http error 429",
            "too many requests",
            "sign in to confirm",
            "not a bot",
            "captcha",
            "login required",
            "forbidden",
            "http error 403",
        ]
        return any(n in msg for n in needles)

    def _apply_cookie_opts(self, opts: dict[str, Any]) -> dict[str, Any]:
        """Mutate yt-dlp opts to include cookies, if configured."""
        if self.cookies_file:
            opts["cookiefile"] = self.cookies_file

        if self.cookies_from_browser:
            parts = self.cookies_from_browser.split(":", 1)
            browser = parts[0].strip()
            if not browser:
                return opts
            if len(parts) == 2 and parts[1].strip():
                profile = parts[1].strip()
                opts["cookiesfrombrowser"] = (browser, profile)
            else:
                opts["cookiesfrombrowser"] = (browser,)

        return opts

    def _extract_info(self, video_url: str, use_cookies: bool) -> dict[str, Any]:
        info_opts = {
            "skip_download": True,
            "ignore_no_formats_error": True,
            "quiet": True,
            "no_warnings": True,
        }
        if use_cookies:
            self._apply_cookie_opts(info_opts)
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            if not isinstance(info, dict):
                raise yt_dlp.utils.DownloadError("Could not extract YouTube info")
            return cast(dict[str, Any], info)

    def _get_asr_dependency_error(self) -> Optional[str]:
        """Return a user-friendly error if optional ASR deps are missing."""
        from ..utils.transcriber import missing_asr_dependencies, ASR_INSTALL_HINT

        missing = missing_asr_dependencies(include_opencc=not self.simplified)
        if not missing:
            return None

        deps = ", ".join(missing)
        return f"Local transcription fallback unavailable. Missing dependencies: {deps}. {ASR_INSTALL_HINT}"

    def _download_subtitles(
        self, video_url: str, selected_lang: str, use_cookies: bool
    ) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            sub_opts = {
                "skip_download": True,
                "ignore_no_formats_error": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [selected_lang],
                "subtitlesformat": "vtt/best",
                "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "logger": type(
                    "QuietLogger",
                    (),
                    {
                        "debug": lambda *a: None,
                        "warning": lambda *a: None,
                        "error": lambda *a: None,
                    },
                )(),
            }
            if use_cookies:
                self._apply_cookie_opts(sub_opts)

            with yt_dlp.YoutubeDL(sub_opts) as ydl:
                ydl.download([video_url])

            # Find the downloaded subtitle file
            for filename in os.listdir(tmpdir):
                path = os.path.join(tmpdir, filename)
                if filename.endswith(".vtt") or filename.endswith(".srt"):
                    with open(path, "r", encoding="utf-8") as f:
                        return f.read()
                if filename.endswith(".json3"):
                    with open(path, "r", encoding="utf-8") as f:
                        return f.read()

            raise yt_dlp.utils.DownloadError("Could not download subtitles")

    def get_available_languages(self, video_url: str) -> dict:
        """Fetch video metadata and list available subtitle languages."""

        def build_result(info: dict[str, Any]) -> dict:
            auto_captions = info.get("automatic_captions", {})
            subtitles = info.get("subtitles", {})
            all_captions = {**auto_captions, **subtitles}
            if not all_captions:
                return {"error": "No subtitles available for this video"}

            available_langs = list(all_captions.keys())
            return {
                "title": info.get("title", "Unknown"),
                "video_id": info.get("id", ""),
                "channel": info.get("channel", info.get("uploader", "Unknown")),
                "channel_id": info.get("channel_id", ""),
                "description": info.get("description", ""),
                "upload_date": info.get("upload_date", ""),
                "view_count": info.get("view_count", 0),
                "duration": info.get("duration", 0),
                "original_language": info.get("language")
                or info.get("original_language"),
                "available_languages": available_langs,
            }

        try:
            if self.prefer_cookies and self._has_cookies():
                info = self._extract_info(video_url, use_cookies=True)
                return build_result(info)

            info = self._extract_info(video_url, use_cookies=False)
            return build_result(info)
        except yt_dlp.utils.DownloadError as e:
            if self._should_retry_with_cookies(e):
                self.log("Retrying metadata fetch with cookies...")
                try:
                    info = self._extract_info(video_url, use_cookies=True)
                    return build_result(info)
                except yt_dlp.utils.DownloadError as e2:
                    return {"error": f"Download error: {str(e2)}"}
            return {"error": f"Download error: {str(e)}"}
        except Exception as e:
            return {"error": f"Error: {str(e)}"}

    def extract_video_id(self, url: str) -> Optional[str]:
        """Extract video ID from various YouTube URL formats"""
        patterns = [
            r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
            r"(?:embed\/)([0-9A-Za-z_-]{11})",
            r"(?:youtu\.be\/)([0-9A-Za-z_-]{11})",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    def get_transcript(
        self, video_url: str, preferred_lang: Optional[str] = None
    ) -> dict:
        """Get transcript and metadata from a YouTube video using yt-dlp."""

        # Force local transcription - skip subtitle fetch entirely
        if self.force_transcribe:
            asr_error = self._get_asr_dependency_error()
            if asr_error:
                return {"error": asr_error}

            self.log("Force transcribe enabled, using SenseVoice...")
            try:
                if self.prefer_cookies and self._has_cookies():
                    info = self._extract_info(video_url, use_cookies=True)
                else:
                    info = self._extract_info(video_url, use_cookies=False)
            except yt_dlp.utils.DownloadError as e:
                if self._should_retry_with_cookies(e):
                    info = self._extract_info(video_url, use_cookies=True)
                else:
                    raise
            return self._transcribe_audio(video_url, info)

        # First, get video info to find available languages
        meta = self.get_available_languages(video_url)

        # If no subtitles available, try ASR fallback
        if "error" in meta:
            if not self.no_transcribe and "No subtitles available" in meta.get(
                "error", ""
            ):
                asr_error = self._get_asr_dependency_error()
                if asr_error:
                    return {"error": asr_error}

                self.log("No subtitles found, transcribing with SenseVoice...")
                try:
                    if self.prefer_cookies and self._has_cookies():
                        info = self._extract_info(video_url, use_cookies=True)
                    else:
                        info = self._extract_info(video_url, use_cookies=False)
                except yt_dlp.utils.DownloadError as e:
                    if self._should_retry_with_cookies(e):
                        info = self._extract_info(video_url, use_cookies=True)
                    else:
                        return meta
                return self._transcribe_audio(video_url, info)
            return meta

        available_langs = meta.get("available_languages", [])
        original_lang = meta.get("original_language")

        # Select language
        selected_lang = None
        if preferred_lang:
            if preferred_lang in available_langs:
                selected_lang = preferred_lang
            else:
                for lang in available_langs:
                    if preferred_lang.lower() in lang.lower():
                        selected_lang = lang
                        break
                if not selected_lang:
                    return {
                        "error": f"Language '{preferred_lang}' not found",
                        "available_languages": available_langs,
                    }
        else:
            # Auto-select: original language > English > first available
            if original_lang and original_lang in available_langs:
                selected_lang = original_lang
            else:
                for lang in ["en", "en-US", "en-GB"]:
                    if lang in available_langs:
                        selected_lang = lang
                        break
                if not selected_lang and available_langs:
                    selected_lang = available_langs[0]

        if not selected_lang:
            return {"error": "Could not determine a subtitle language"}

        def to_result(subtitle_content: str) -> dict:
            if subtitle_content.lstrip().startswith("{"):
                transcript = self._parse_json3(subtitle_content)
            else:
                transcript = self._parse_vtt(subtitle_content)

            return {
                "title": meta.get("title", "Unknown"),
                "video_id": meta.get("video_id", ""),
                "channel": meta.get("channel", "Unknown"),
                "channel_id": meta.get("channel_id", ""),
                "description": meta.get("description", ""),
                "upload_date": meta.get("upload_date", ""),
                "view_count": meta.get("view_count", 0),
                "duration": meta.get("duration", 0),
                "language": selected_lang,
                "available_languages": available_langs,
                "transcript": transcript,
            }

        try:
            if self.prefer_cookies and self._has_cookies():
                return to_result(
                    self._download_subtitles(video_url, selected_lang, use_cookies=True)
                )

            return to_result(
                self._download_subtitles(video_url, selected_lang, use_cookies=False)
            )
        except yt_dlp.utils.DownloadError as e:
            if self._should_retry_with_cookies(e):
                self.log("Retrying transcript download with cookies...")
                try:
                    return to_result(
                        self._download_subtitles(
                            video_url, selected_lang, use_cookies=True
                        )
                    )
                except yt_dlp.utils.DownloadError as e2:
                    return {"error": f"Download error: {str(e2)}"}
            return {"error": f"Download error: {str(e)}"}
        except Exception as e:
            return {"error": f"Error: {str(e)}"}

    def _parse_vtt(self, text: str) -> str:
        """Parse VTT subtitle format to plain text"""
        lines = []
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("WEBVTT"):
                continue
            if (
                stripped.startswith("Kind:")
                or stripped.startswith("Language:")
                or stripped.startswith("NOTE")
            ):
                continue
            if "-->" in stripped:
                continue
            if stripped.isdigit():
                continue

            # Clean HTML tags
            clean = re.sub("<[^>]+>", "", stripped)
            clean = clean.replace("&nbsp;", " ")
            clean = clean.replace("&amp;", "&")
            clean = clean.replace("&lt;", "<")
            clean = clean.replace("&gt;", ">")
            clean = clean.replace("&quot;", '"')

            if clean:
                lines.append(clean)

        # Remove consecutive duplicates
        result = []
        prev = None
        for line in lines:
            if line != prev:
                result.append(line)
                prev = line

        return "\n".join(result)

    def _parse_json3(self, text: str) -> str:
        """Parse JSON3 subtitle format to plain text"""
        try:
            data = json.loads(text)
            lines = []

            if "events" in data:
                for event in data["events"]:
                    if "segs" not in event:
                        continue
                    parts = []
                    for seg in event.get("segs", []):
                        if "utf8" in seg:
                            parts.append(seg["utf8"])
                    if parts:
                        line = "".join(parts).strip()
                        if line:
                            lines.append(line)

            # Remove consecutive duplicates
            result = []
            prev = None
            for line in lines:
                if line != prev:
                    result.append(line)
                    prev = line

            return "\n".join(result)

        except json.JSONDecodeError:
            return self._parse_vtt(text)

    def _transcribe_audio(self, video_url: str, info: dict) -> dict:
        """
        Fallback: Download audio and transcribe locally with SenseVoice.

        Used when no subtitles are available for the video.
        """
        from ..utils.transcriber import SenseVoiceTranscriber

        title = info.get("title", "Unknown")
        video_id = info.get("id", "")
        duration = info.get("duration", 0)
        channel = info.get("channel", info.get("uploader", "Unknown"))
        channel_id = info.get("channel_id", "")
        description = info.get("description", "")
        upload_date = info.get("upload_date", "")
        view_count = info.get("view_count", 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            self.log("Downloading audio...")

            # Download audio only
            audio_opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "wav",
                        "preferredquality": "192",
                    }
                ],
                "postprocessor_args": ["-ar", "16000", "-ac", "1"],
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "logger": type(
                    "QuietLogger",
                    (),
                    {
                        "debug": lambda *a: None,
                        "warning": lambda *a: None,
                        "error": lambda *a: None,
                    },
                )(),
            }

            # Apply cookies if available
            if self._has_cookies():
                self._apply_cookie_opts(audio_opts)

            try:
                with yt_dlp.YoutubeDL(audio_opts) as ydl:
                    ydl.download([video_url])
            except Exception as e:
                return {"error": f"Failed to download audio: {e}"}

            # Find the downloaded audio file
            wav_file = None
            for filename in os.listdir(tmpdir):
                if filename.endswith(".wav"):
                    wav_file = os.path.join(tmpdir, filename)
                    break

            if not wav_file:
                # Try other formats if WAV wasn't created
                for ext in [".m4a", ".mp3", ".webm", ".opus"]:
                    for filename in os.listdir(tmpdir):
                        if filename.endswith(ext):
                            wav_file = os.path.join(tmpdir, filename)
                            break
                    if wav_file:
                        break

            if not wav_file:
                return {"error": "Failed to extract audio from video"}

            # Transcribe with SenseVoice
            self.log("Transcribing with SenseVoice...")
            transcriber = SenseVoiceTranscriber(quiet=self.quiet)

            try:
                result = transcriber.transcribe_file(
                    wav_file,
                    language="auto",
                    traditional=not self.simplified,
                )
            finally:
                transcriber.cleanup()

            if result.error:
                return {"error": f"Transcription failed: {result.error}"}

            transcript = result.text_clean

            return {
                "title": title,
                "video_id": video_id,
                "channel": channel,
                "channel_id": channel_id,
                "description": description,
                "upload_date": upload_date,
                "view_count": view_count,
                "duration": duration,
                "language": "auto-detected",
                "available_languages": [],
                "transcript": transcript,
                "transcription_method": "sensevoice-local",
            }


@click.command("yt-transcript")
@click.argument("url")
@click.option("-l", "--lang", help="Preferred language code (e.g., en, zh-Hant)")
@click.option("--list-langs", is_flag=True, help="List available languages")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("-o", "--output", help="Save output to file")
@click.option("--copy", is_flag=True, help="Copy to clipboard")
@click.option("-q", "--quiet", is_flag=True, help="Suppress progress messages")
@click.option(
    "--cookies",
    type=click.Path(exists=True, dir_okay=False),
    help="Netscape cookie file for YouTube (yt-dlp --cookies)",
)
@click.option(
    "--cookies-from-browser",
    help='Load YouTube cookies from a browser (e.g., chrome, firefox). Optional ":PROFILE" suffix.',
)
@click.option(
    "--cookies-on-fail/--no-cookies-on-fail",
    default=True,
    help="Retry with cookies if YouTube throttles/blocks unauthenticated requests",
)
@click.option(
    "--no-transcribe",
    is_flag=True,
    help="Disable auto-transcription fallback when no subtitles",
)
@click.option(
    "--force-transcribe",
    "-t",
    is_flag=True,
    help="Force local transcription (ignore YouTube subtitles)",
)
@click.option(
    "--simplified",
    is_flag=True,
    help="Output Simplified Chinese (default: Traditional)",
)
def yt_transcript(
    url: str,
    lang: Optional[str],
    list_langs: bool,
    json_output: bool,
    output: Optional[str],
    copy: bool,
    quiet: bool,
    cookies: Optional[str],
    cookies_from_browser: Optional[str],
    cookies_on_fail: bool,
    no_transcribe: bool,
    force_transcribe: bool,
    simplified: bool,
):
    """Download transcript from a YouTube video.

    Auto-transcribes with local SenseVoice if no subtitles are available.
    Outputs Traditional Chinese by default.

    Examples:

        fcrawl yt-transcript "https://youtube.com/watch?v=VIDEO_ID"

        fcrawl yt-transcript "https://youtu.be/VIDEO_ID" --lang zh-Hant

        fcrawl yt-transcript "https://youtube.com/watch?v=VIDEO_ID" --list-langs

        fcrawl yt-transcript "https://youtube.com/watch?v=VIDEO_ID" --json

        fcrawl yt-transcript URL --no-transcribe  # Disable local ASR fallback

        fcrawl yt-transcript URL -t               # Force local transcription

        fcrawl yt-transcript URL --simplified     # Output Simplified Chinese
    """
    cfg = load_config()
    cookies_file = cookies or cfg.get("yt_cookies_file")
    cookies_browser = cookies_from_browser or cfg.get("yt_cookies_from_browser")

    if cookies_file and not os.path.exists(cookies_file):
        # If the user provided cookies explicitly, fail fast.
        if cookies:
            raise click.BadParameter(
                f"cookie file not found: {cookies_file}", param_hint="--cookies"
            )

        # If cookies came from config/env, just warn and continue.
        console.print(
            f"[dim]Configured yt_cookies_file not found, ignoring: {cookies_file}[/dim]",
            highlight=False,
        )
        cookies_file = None

    if cookies_browser is not None and not str(cookies_browser).strip():
        cookies_browser = None

    downloader = YouTubeTranscriptDownloader(
        quiet=quiet or json_output,
        cookies_file=cookies_file,
        cookies_from_browser=cookies_browser,
        cookies_on_fail=cookies_on_fail,
        # If the user explicitly provides cookies on the CLI, use them immediately.
        prefer_cookies=bool(cookies or cookies_from_browser),
        no_transcribe=no_transcribe,
        force_transcribe=force_transcribe,
        simplified=simplified,
    )

    # Validate URL
    video_id = downloader.extract_video_id(url)
    if not video_id:
        console.print("[red]Error: Invalid YouTube URL[/red]")
        raise click.Abort()

    # Show progress (unless quiet or json output)
    is_tty = sys.stdout.isatty()
    show_progress = is_tty and not quiet and not json_output

    if list_langs:
        # Only fetch metadata; do not download subtitles.
        if show_progress:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Fetching available languages...", total=None)
                result = downloader.get_available_languages(url)
        else:
            result = downloader.get_available_languages(url)
    else:
        if show_progress:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Fetching transcript...", total=None)
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
                console.print(
                    f"[yellow]Available: {', '.join(result['available_languages'])}[/yellow]"
                )
            err_msg = str(result.get("error", ""))
            if ("HTTP Error 429" in err_msg or "Too Many Requests" in err_msg) and not (
                cookies_file or cookies_browser
            ):
                console.print(
                    "[dim]Tip: YouTube is rate-limiting unauthenticated requests. Try: fcrawl yt-transcript URL --cookies-from-browser chrome[/dim]"
                )
        raise click.Abort()

    # List languages only
    if list_langs:
        if json_output:
            print(
                json.dumps(
                    {"available_languages": result["available_languages"]}, indent=2
                )
            )
        else:
            console.print("[bold]Available languages:[/bold]")
            for language in result["available_languages"]:
                console.print(f"  {language}")
        return

    # Output
    if json_output:
        content = result
        format_type = "json"
    else:
        # Format duration
        duration = result.get("duration", 0)
        mins, secs = divmod(duration, 60)
        hours, mins = divmod(mins, 60)
        duration_str = (
            f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"
        )

        # Format upload date (YYYYMMDD -> YYYY-MM-DD)
        upload_date = result.get("upload_date", "")
        if len(upload_date) == 8:
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

        # Format view count with commas
        view_count = result.get("view_count", 0)
        view_count_str = f"{view_count:,}" if view_count else "N/A"

        # Build metadata header
        lines = [
            f"Title: {result['title']}",
            f"Channel: {result['channel']} ({result.get('channel_id', 'N/A')})",
            f"Upload Date: {upload_date or 'N/A'}",
            f"Duration: {duration_str}",
            f"Views: {view_count_str}",
        ]
        if result.get("description"):
            lines.append(f"Description: {result['description']}")
        if result.get("transcription_method"):
            lines.append(f"Transcription: {result['transcription_method']}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(result["transcript"])
        content = "\n".join(lines)
        format_type = "markdown"

    handle_output(
        content,
        output_file=output,
        copy=copy,
        json_output=json_output,
        pretty=is_tty,
        format_type=format_type,
    )
