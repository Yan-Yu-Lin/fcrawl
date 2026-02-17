"""
Local audio transcription using SenseVoice/FunASR models.

Provides transcription functionality for:
- fcrawl transcribe command (standalone audio/video transcription)
- fcrawl yt-transcript fallback (when YouTube has no subtitles)
"""

import gc
import importlib.util
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from .output import console


ASR_INSTALL_HINT = (
    'Install optional ASR deps with: uv sync --extra asr (or pip install "fcrawl[asr]")'
)


class ASRDependencyError(RuntimeError):
    """Raised when optional ASR dependencies are missing."""

    def __init__(self, missing: list[str]):
        deps = ", ".join(missing)
        super().__init__(
            f"Missing optional ASR dependencies: {deps}. {ASR_INSTALL_HINT}"
        )
        self.missing = missing


def missing_asr_dependencies(include_opencc: bool = True) -> list[str]:
    """Return a list of missing optional ASR dependencies."""
    required = ["torch", "pydub", "funasr"]
    if include_opencc:
        required.append("opencc")
    return [name for name in required if importlib.util.find_spec(name) is None]


def ensure_asr_dependencies(include_opencc: bool = True):
    """Raise ASRDependencyError if optional ASR deps are missing."""
    missing = missing_asr_dependencies(include_opencc=include_opencc)
    if missing:
        raise ASRDependencyError(missing)


@dataclass
class TranscriptionResult:
    """Result of a transcription operation."""

    text: str
    text_clean: str
    duration_seconds: float
    inference_time: float
    model: str
    device: str
    error: Optional[str] = None


def clean_transcript(text: str) -> str:
    """Clean transcript text - remove emoji artifacts, normalize whitespace."""
    # Remove common SenseVoice emoji artifacts
    text = re.sub(r"[🎼😊🎵🎶👏😄😢😠]", "", text)
    # Remove tag prefixes like <|zh|>, <|en|>, <|NEUTRAL|>, etc.
    text = re.sub(r"<\|[^|]+\|>", "", text)
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def convert_to_traditional(text: str) -> str:
    """Convert Simplified Chinese to Traditional Chinese using OpenCC."""
    ensure_asr_dependencies(include_opencc=True)
    from opencc import OpenCC

    converter = OpenCC("s2t")
    return converter.convert(text)


def convert_to_wav_16k(input_path: str, output_dir: Optional[str] = None) -> str:
    """
    Convert any audio/video file to 16kHz mono WAV for ASR processing.

    Args:
        input_path: Path to input audio/video file
        output_dir: Directory for output file (uses temp dir if None)

    Returns:
        Path to converted WAV file
    """
    ensure_asr_dependencies(include_opencc=False)
    input_path_obj = Path(input_path)

    # Load audio (pydub handles most formats via ffmpeg)
    from pydub import AudioSegment

    audio = AudioSegment.from_file(str(input_path_obj))

    # Convert to 16kHz mono
    audio = audio.set_frame_rate(16000).set_channels(1)

    # Determine output path
    if output_dir is None:
        output_dir = tempfile.gettempdir()

    output_path = Path(output_dir) / f"{input_path_obj.stem}_16k.wav"
    audio.export(str(output_path), format="wav")

    return str(output_path)


class SenseVoiceTranscriber:
    """
    SenseVoice transcriber using FunASR.

    Supports:
    - SenseVoice (default): Multilingual ASR from Alibaba DAMO (~234M params)
    - Paraformer: Chinese-focused ASR (~220M params, CPU only)
    - Fun-ASR-Nano: LLM-based ASR (~800M params, 31 languages)
    """

    MODELS = {
        "sensevoice": "iic/SenseVoiceSmall",
        "paraformer": "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "fun-asr-nano": "FunAudioLLM/Fun-ASR-Nano-2512",
    }

    def __init__(
        self,
        model: str = "sensevoice",
        device: Optional[str] = None,
        quiet: bool = False,
    ):
        """
        Initialize transcriber.

        Args:
            model: Model name (sensevoice, paraformer, fun-asr-nano)
            device: Device to use (auto-detected if None)
            quiet: Suppress progress messages
        """
        self.model_name = model
        self.model_id = self.MODELS.get(model, model)
        self._device = device
        self.quiet = quiet
        self.model = None
        self._loaded = False

    def _detect_device(self) -> str:
        """Auto-detect best available device."""
        try:
            import torch
        except ModuleNotFoundError:
            # Optional ASR deps may not be installed.
            return "cpu"

        if torch.backends.mps.is_available():
            return "mps"
        elif torch.cuda.is_available():
            return "cuda:0"
        return "cpu"

    @property
    def device(self) -> str:
        if self._device is None:
            self._device = self._detect_device()
        return self._device

    def log(self, msg: str):
        if not self.quiet:
            console.print(f"[dim]{msg}[/dim]", highlight=False)

    def load(self):
        """Load the ASR model."""
        if self._loaded:
            return

        ensure_asr_dependencies(include_opencc=False)

        from funasr import AutoModel

        self.log(f"Loading {self.model_name} model on {self.device.upper()}...")

        is_paraformer = "paraformer" in self.model_id.lower()
        is_fun_asr_nano = (
            "fun-asr" in self.model_id.lower() or "funaudiollm" in self.model_id.lower()
        )

        # Paraformer has MPS issues, force CPU
        device = "cpu" if is_paraformer else self.device

        model_kwargs = {
            "model": self.model_id,
            "device": device,
            "disable_update": True,
            "vad_model": "fsmn-vad",
            "vad_kwargs": {"max_single_segment_time": 30000},
        }

        if is_fun_asr_nano:
            model_kwargs["trust_remote_code"] = True
            # Fun-ASR-Nano may need remote_code path if custom model.py is needed
        elif "sensevoice" in self.model_id.lower():
            model_kwargs["trust_remote_code"] = True

        if is_paraformer:
            model_kwargs["punc_model"] = "ct-punc"

        self.model = AutoModel(**model_kwargs)
        self._loaded = True
        self._device = device  # Update in case it was changed

        self.log("Model loaded!")

    def transcribe(self, audio_path: str, language: str = "auto") -> str:
        """
        Transcribe an audio file.

        Args:
            audio_path: Path to audio file (should be 16kHz mono WAV)
            language: Language hint (auto, zh, en, ja, etc.)

        Returns:
            Transcribed text
        """
        ensure_asr_dependencies(include_opencc=False)
        import torch

        if not self._loaded:
            self.load()

        if self.model is None:
            raise RuntimeError("ASR model failed to load")

        model = cast(Any, self.model)

        # Fix for FunASR thread count drift bug
        torch.set_num_threads(4)

        is_fun_asr_nano = (
            "fun-asr" in self.model_id.lower() or "funaudiollm" in self.model_id.lower()
        )

        if is_fun_asr_nano:
            res = model.generate(
                input=[audio_path],
                cache={},
                batch_size=1,
                language=language,
                itn=True,
            )
        else:
            res = model.generate(
                input=audio_path,
                cache={},
                language=language,
                use_itn=True,
                batch_size_s=60,
                merge_vad=True,
                merge_length_s=15,
            )

        # Reset thread count
        torch.set_num_threads(4)

        if res and res[0].get("text"):
            text = res[0]["text"]
            # SenseVoice outputs emotion/event tags that need postprocessing
            try:
                from funasr.utils.postprocess_utils import (
                    rich_transcription_postprocess,
                )

                return rich_transcription_postprocess(text)
            except Exception:
                return text

        return ""

    def transcribe_file(
        self,
        input_path: str,
        language: str = "auto",
        traditional: bool = True,
    ) -> TranscriptionResult:
        """
        Transcribe any audio/video file with full result metadata.

        Args:
            input_path: Path to audio/video file
            language: Language hint
            traditional: Convert to Traditional Chinese (default True)

        Returns:
            TranscriptionResult with text and metadata
        """
        import time

        input_path_obj = Path(input_path)

        if not input_path_obj.exists():
            return TranscriptionResult(
                text="",
                text_clean="",
                duration_seconds=0,
                inference_time=0,
                model=self.model_name,
                device=self.device,
                error=f"File not found: {input_path}",
            )

        # Convert to 16kHz WAV if needed
        temp_wav = None
        try:
            if input_path_obj.suffix.lower() != ".wav":
                self.log(f"Converting {input_path_obj.suffix} to WAV...")
                temp_wav = convert_to_wav_16k(str(input_path_obj))
                audio_path = temp_wav
            else:
                # Check if already 16kHz mono
                ensure_asr_dependencies(include_opencc=False)
                from pydub import AudioSegment

                audio = AudioSegment.from_wav(str(input_path_obj))
                if audio.frame_rate != 16000 or audio.channels != 1:
                    self.log("Resampling to 16kHz mono...")
                    temp_wav = convert_to_wav_16k(str(input_path_obj))
                    audio_path = temp_wav
                else:
                    audio_path = str(input_path_obj)

            # Get duration
            ensure_asr_dependencies(include_opencc=False)
            from pydub import AudioSegment

            audio = AudioSegment.from_file(audio_path)
            duration = len(audio) / 1000.0

            # Transcribe
            start_time = time.time()
            text = self.transcribe(audio_path, language)
            inference_time = time.time() - start_time

            # Clean text
            text_clean = clean_transcript(text)

            # Convert to Traditional Chinese if requested
            if traditional:
                text_clean = convert_to_traditional(text_clean)

            return TranscriptionResult(
                text=text,
                text_clean=text_clean,
                duration_seconds=duration,
                inference_time=inference_time,
                model=self.model_name,
                device=self.device,
            )

        except Exception as e:
            return TranscriptionResult(
                text="",
                text_clean="",
                duration_seconds=0,
                inference_time=0,
                model=self.model_name,
                device=self.device,
                error=str(e),
            )

        finally:
            # Clean up temp file
            if temp_wav and os.path.exists(temp_wav):
                try:
                    os.remove(temp_wav)
                except Exception:
                    pass

    def cleanup(self):
        """Clean up model to free memory."""
        try:
            import torch
        except ModuleNotFoundError:
            torch = None

        if self.model is not None:
            del self.model
            self.model = None
            self._loaded = False

        gc.collect()

        if torch is not None and torch.backends.mps.is_available():
            torch.mps.empty_cache()
