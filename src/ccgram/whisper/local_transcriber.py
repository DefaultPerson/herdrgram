"""Local Whisper transcription, in-process, via faster-whisper (CTranslate2).

``CCGRAM_WHISPER_PROVIDER=local`` keeps voice input on the machine the bot runs
on: no API key, no audio leaving the host, no per-minute bill. The cost is CPU
time — on a 4-core desktop without a GPU, ``large-v3-turbo`` at int8 runs at
roughly 1.2x the length of the recording, and ``small`` at roughly 0.4x with
visibly weaker Russian.

Three properties make it safe to run inside the bot process:

  - the model is loaded lazily, once, on the first voice message, so a bot that
    never transcribes never pays the ~1.5 GB the weights cost;
  - both the load and the decode run in a worker thread, so the polling loop
    keeps answering while a transcription is in flight;
  - decodes are serialized behind one lock and given one CPU thread fewer than
    the machine has, so two voice messages arriving together cannot starve the
    rest of the bot.

``faster_whisper`` is an optional dependency, imported at first use: an install
without it behaves exactly like an unconfigured provider, with an error that
names the command to fix it.
"""

from __future__ import annotations

import asyncio
import io
import os
import time
from collections.abc import Callable
from typing import Any

import structlog

from .base import TranscriptionResult

logger = structlog.get_logger()

DEFAULT_MODEL = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
"""Best quality-per-second of the multilingual set on CPU (809M params, 4 decoder layers)."""

DEFAULT_COMPUTE_TYPE = "int8"
DEFAULT_DEVICE = "cpu"
DEFAULT_BEAM_SIZE = 5

MISSING_DEPENDENCY = (
    "Local transcription needs faster-whisper. Install it into the ccgram "
    "environment: uv pip install faster-whisper"
)


def default_cpu_threads() -> int:
    """One thread fewer than the machine has, so the bot keeps a core to run on."""
    cores = os.cpu_count() or 2
    return max(1, cores - 1)


def _load_whisper_model(
    model: str, device: str, compute_type: str, cpu_threads: int
) -> Any:
    """Build a ``WhisperModel``; the only place faster-whisper is imported."""
    try:
        # Lazy: optional dependency. Importing it at module load would make the
        # whisper package unimportable wherever local transcription is not used.
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - exercised via injection
        raise RuntimeError(MISSING_DEPENDENCY) from exc

    return WhisperModel(
        model,
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
    )


class LocalWhisperTranscriber:
    """``WhisperTranscriber`` backed by an in-process faster-whisper model."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
        cpu_threads: int = 0,
        beam_size: int = DEFAULT_BEAM_SIZE,
        language: str | None = None,
        model_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads if cpu_threads > 0 else default_cpu_threads()
        self.beam_size = max(1, beam_size)
        self.language = language or None
        self._model_loader = model_loader or (
            lambda: _load_whisper_model(
                self.model, self.device, self.compute_type, self.cpu_threads
            )
        )
        self._model: Any | None = None
        # Loading is minutes-long on a cold cache and decoding is CPU-bound;
        # neither may run twice concurrently for the same transcriber.
        self._load_lock = asyncio.Lock()
        self._decode_lock = asyncio.Lock()

    async def _ensure_model(self) -> Any:
        """Load the weights once, off the event loop."""
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is not None:
                return self._model
            started = time.perf_counter()
            try:
                model = await asyncio.to_thread(self._model_loader)
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(f"Could not load {self.model}: {exc}") from exc
            logger.info(
                "Local whisper model loaded",
                model=self.model,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                seconds=round(time.perf_counter() - started, 1),
            )
            self._model = model
            return model

    def _decode(self, model: Any, audio_bytes: bytes) -> TranscriptionResult:
        """Run the model. Called in a worker thread, never on the event loop.

        ``vad_filter`` drops the silence Whisper is known to hallucinate words
        into, and ``condition_on_previous_text=False`` stops a short clip from
        looping a phrase it already emitted.
        """
        segments, info = model.transcribe(
            io.BytesIO(audio_bytes),
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        # faster-whisper yields segments lazily: the decode happens here.
        text = "".join(segment.text for segment in segments).strip()
        return TranscriptionResult(text=text, language=getattr(info, "language", None))

    async def transcribe(
        self, audio_bytes: bytes, filename: str
    ) -> TranscriptionResult:
        """Transcribe audio bytes locally.

        Args:
            audio_bytes: Raw audio file content (Telegram sends OGG/Opus, which
                PyAV decodes directly — no ffmpeg subprocess, no temp file).
            filename: Unused; the format is detected from the bytes themselves.
                Part of the ``WhisperTranscriber`` contract.

        Returns:
            TranscriptionResult with text and the detected language.

        Raises:
            RuntimeError: faster-whisper is missing, the model cannot be
                loaded, or decoding failed.
        """
        del filename  # format comes from the container, not the name
        model = await self._ensure_model()
        started = time.perf_counter()
        async with self._decode_lock:
            try:
                result = await asyncio.to_thread(self._decode, model, audio_bytes)
            except Exception as exc:
                raise RuntimeError(f"Transcription failed: {exc}") from exc
        logger.info(
            "Transcribed locally",
            model=self.model,
            seconds=round(time.perf_counter() - started, 1),
            characters=len(result.text),
            language=result.language,
        )
        return result
