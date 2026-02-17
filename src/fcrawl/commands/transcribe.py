"""
Transcribe command for fcrawl - local audio/video transcription using SenseVoice.

Usage:
    fcrawl transcribe audio.mp3
    fcrawl transcribe video.mp4 -o transcript.txt
    fcrawl transcribe recording.wav --json
    fcrawl transcribe podcast.mp3 -m paraformer --simplified
"""

import json
import sys
from pathlib import Path
from typing import Optional

import click
from rich.progress import Progress, SpinnerColumn, TextColumn

from ..utils.output import handle_output, console


def format_duration(seconds: float) -> str:
    """Format duration in human-readable format."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_as_srt(text: str, duration: float) -> str:
    """
    Format transcript as SRT subtitle format.
    Simple implementation - creates segments by splitting on sentence boundaries.
    """
    import re

    # Split on sentence boundaries
    sentences = re.split(r'(?<=[。！？.!?])\s*', text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return ""

    # Distribute duration across sentences (rough approximation)
    avg_duration = duration / len(sentences) if sentences else duration

    srt_lines = []
    current_time = 0.0

    for i, sentence in enumerate(sentences, 1):
        start_time = current_time
        # Vary duration by sentence length
        sentence_duration = min(avg_duration * (len(sentence) / 20), 10.0)
        sentence_duration = max(sentence_duration, 1.0)
        end_time = start_time + sentence_duration
        current_time = end_time

        # Format timestamps
        def format_srt_time(t):
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s = int(t % 60)
            ms = int((t % 1) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        srt_lines.append(str(i))
        srt_lines.append(f"{format_srt_time(start_time)} --> {format_srt_time(end_time)}")
        srt_lines.append(sentence)
        srt_lines.append("")

    return "\n".join(srt_lines)


def format_as_vtt(text: str, duration: float) -> str:
    """
    Format transcript as WebVTT subtitle format.
    """
    srt = format_as_srt(text, duration)
    if not srt:
        return "WEBVTT\n\n"

    # Convert SRT to VTT
    vtt_lines = ["WEBVTT", ""]
    for line in srt.split("\n"):
        # Replace comma with dot in timestamps
        if " --> " in line:
            line = line.replace(",", ".")
        vtt_lines.append(line)

    return "\n".join(vtt_lines)


@click.command('transcribe')
@click.argument('file', type=click.Path(exists=True))
@click.option('-m', '--model', default='sensevoice',
              type=click.Choice(['sensevoice', 'paraformer', 'fun-asr-nano']),
              help='ASR model to use (default: sensevoice)')
@click.option('-l', '--lang', default='auto',
              help='Language hint (auto, zh, en, ja, ko, etc.)')
@click.option('-f', '--format', 'output_format', default='txt',
              type=click.Choice(['txt', 'srt', 'vtt', 'json']),
              help='Output format (default: txt)')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True,
              help='Output as JSON with metadata')
@click.option('--copy', is_flag=True, help='Copy to clipboard')
@click.option('--simplified', is_flag=True,
              help='Output Simplified Chinese (default: Traditional)')
@click.option('-q', '--quiet', is_flag=True,
              help='Suppress progress messages')
def transcribe(
    file: str,
    model: str,
    lang: str,
    output_format: str,
    output: Optional[str],
    json_output: bool,
    copy: bool,
    simplified: bool,
    quiet: bool,
):
    """Transcribe audio/video file using local ASR model.

    Supported formats: wav, mp3, m4a, flac, ogg, mp4, mkv, webm

    Examples:

        fcrawl transcribe recording.mp3

        fcrawl transcribe video.mp4 -o transcript.txt

        fcrawl transcribe audio.wav --json

        fcrawl transcribe podcast.mp3 -f srt -o subtitles.srt

        fcrawl transcribe chinese_audio.wav --simplified
    """
    file_path = Path(file)
    is_tty = sys.stdout.isatty()
    show_progress = is_tty and not quiet and not json_output

    # Initialize transcriber (lazy import to avoid loading heavy deps on every fcrawl invocation)
    from ..utils.transcriber import SenseVoiceTranscriber
    transcriber = SenseVoiceTranscriber(
        model=model,
        quiet=quiet or json_output,
    )

    # Transcribe with progress indicator
    if show_progress:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            progress.add_task(f"Transcribing {file_path.name}...", total=None)
            result = transcriber.transcribe_file(
                str(file_path),
                language=lang,
                traditional=not simplified,
            )
    else:
        result = transcriber.transcribe_file(
            str(file_path),
            language=lang,
            traditional=not simplified,
        )

    # Clean up model
    transcriber.cleanup()

    # Handle errors
    if result.error:
        if json_output:
            print(json.dumps({"error": result.error}, indent=2))
        else:
            console.print(f"[red]Error: {result.error}[/red]")
        raise click.Abort()

    # Format output
    if json_output or output_format == 'json':
        content = {
            "file": str(file_path.absolute()),
            "duration": result.duration_seconds,
            "duration_formatted": format_duration(result.duration_seconds),
            "inference_time": result.inference_time,
            "rtf": result.inference_time / result.duration_seconds if result.duration_seconds > 0 else 0,
            "model": result.model,
            "device": result.device,
            "transcript": result.text_clean,
        }
        format_type = 'json'
    elif output_format == 'srt':
        content = format_as_srt(result.text_clean, result.duration_seconds)
        format_type = 'text'
    elif output_format == 'vtt':
        content = format_as_vtt(result.text_clean, result.duration_seconds)
        format_type = 'text'
    else:  # txt
        content = result.text_clean
        format_type = 'text'

    # Show stats if not quiet
    if not quiet and not json_output and is_tty:
        rtf = result.inference_time / result.duration_seconds if result.duration_seconds > 0 else 0
        console.print(f"[green]✓ Transcribed {format_duration(result.duration_seconds)} in {result.inference_time:.1f}s (RTF: {rtf:.2f})[/green]")
        console.print(f"[dim]Model: {result.model} | Device: {result.device.upper()}[/dim]")
        console.print()

    # Handle output
    handle_output(
        content,
        output_file=output,
        copy=copy,
        json_output=json_output or output_format == 'json',
        pretty=is_tty,
        format_type=format_type,
    )
