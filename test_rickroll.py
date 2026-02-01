#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "funasr>=1.0.0",
#     "torch>=2.0.0",
#     "pydub>=0.25.1",
#     "opencc-python-reimplemented>=0.1.7",
#     "yt-dlp>=2025.1.0",
#     "rich>=14.0.0",
# ]
# ///
"""
Test script for SenseVoice transcription on Rick Roll.
Downloads first 60 seconds of audio and transcribes.
"""

import gc
import os
import re
import tempfile
import time
from pathlib import Path

import opencc
import torch
import yt_dlp
from pydub import AudioSegment
from rich.console import Console
from funasr import AutoModel

console = Console()

# Rick Roll URL
RICK_ROLL_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def clean_transcript(text: str) -> str:
    """Clean transcript text."""
    text = re.sub(r'[🎼😊🎵🎶👏😄😢😠]', '', text)
    text = re.sub(r'<\|[^|]+\|>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def detect_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    elif torch.cuda.is_available():
        return "cuda:0"
    return "cpu"


def download_audio(video_url: str, output_dir: str, max_duration: int = 60) -> str:
    """Download audio from YouTube video."""
    console.print(f"[dim]Downloading audio (first {max_duration}s)...[/dim]")

    audio_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(output_dir, '%(id)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'wav',
        }],
        'postprocessor_args': ['-ar', '16000', '-ac', '1', '-t', str(max_duration)],
        'quiet': True,
        'no_warnings': True,
    }

    with yt_dlp.YoutubeDL(audio_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        console.print(f"[dim]Video: {info.get('title', 'Unknown')}[/dim]")

    # Find the WAV file
    for f in os.listdir(output_dir):
        if f.endswith('.wav'):
            return os.path.join(output_dir, f)

    raise RuntimeError("Failed to download audio")


def transcribe_audio(audio_path: str, device: str) -> tuple[str, float]:
    """Transcribe audio using SenseVoice."""
    console.print(f"[dim]Loading SenseVoice on {device.upper()}...[/dim]")

    model = AutoModel(
        model="iic/SenseVoiceSmall",
        trust_remote_code=True,
        device=device,
        disable_update=True,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},
    )

    console.print("[dim]Transcribing...[/dim]")
    torch.set_num_threads(4)

    start_time = time.time()
    res = model.generate(
        input=audio_path,
        cache={},
        language="auto",
        use_itn=True,
        batch_size_s=60,
        merge_vad=True,
        merge_length_s=15,
    )
    elapsed = time.time() - start_time

    torch.set_num_threads(4)

    if res and res[0].get("text"):
        text = res[0]["text"]
        try:
            from funasr.utils.postprocess_utils import rich_transcription_postprocess
            text = rich_transcription_postprocess(text)
        except Exception:
            pass
        return text, elapsed

    return "", elapsed


def main():
    console.print("[bold]Testing SenseVoice on Rick Roll[/bold]")
    console.print()

    device = detect_device()
    console.print(f"Device: [cyan]{device.upper()}[/cyan]")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Download
        audio_path = download_audio(RICK_ROLL_URL, tmpdir, max_duration=60)

        # Get duration
        audio = AudioSegment.from_wav(audio_path)
        duration = len(audio) / 1000.0
        console.print(f"Audio duration: {duration:.1f}s")

        # Transcribe
        text, elapsed = transcribe_audio(audio_path, device)
        rtf = elapsed / duration

        console.print()
        console.print(f"[green]✓ Transcribed in {elapsed:.1f}s (RTF: {rtf:.2f})[/green]")
        console.print()

        # Clean and convert to Traditional
        text_clean = clean_transcript(text)
        converter = opencc.OpenCC('s2t')
        text_traditional = converter.convert(text_clean)

        console.print("[bold]Raw output:[/bold]")
        console.print(text_clean)
        console.print()
        console.print("[bold]Traditional Chinese:[/bold]")
        console.print(text_traditional)


if __name__ == "__main__":
    main()
