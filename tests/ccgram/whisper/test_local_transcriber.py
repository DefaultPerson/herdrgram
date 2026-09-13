"""Unit tests for the in-process faster-whisper transcriber.

The model itself is injected, so nothing here loads weights or touches audio:
what is under test is the contract around the model — one lazy load, decoding
off the event loop, and the parameters the decode is given.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ccgram.whisper.local_transcriber import (
    DEFAULT_MODEL,
    LocalWhisperTranscriber,
    default_cpu_threads,
)


def _segment(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


def _fake_model(segments: list[str] | None = None, language: str = "ru") -> MagicMock:
    model = MagicMock()
    if segments is None:
        segments = [" hello", " world "]
    model.transcribe.return_value = (
        iter(_segment(t) for t in segments),
        SimpleNamespace(language=language),
    )
    return model


class TestTranscribe:
    async def test_joins_segments_and_reports_language(self) -> None:
        model = _fake_model([" Посмотри", " в конфиг."])
        t = LocalWhisperTranscriber(model_loader=lambda: model)

        result = await t.transcribe(b"ogg-bytes", "voice.ogg")

        assert result.text == "Посмотри в конфиг."
        assert result.language == "ru"

    async def test_decode_parameters(self) -> None:
        model = _fake_model()
        t = LocalWhisperTranscriber(
            model_loader=lambda: model, beam_size=3, language="ru"
        )

        await t.transcribe(b"ogg-bytes", "voice.ogg")

        audio, kwargs = (
            model.transcribe.call_args.args[0],
            model.transcribe.call_args.kwargs,
        )
        assert audio.read() == b"ogg-bytes"  # decoded from memory, no temp file
        assert kwargs["beam_size"] == 3
        assert kwargs["language"] == "ru"
        # Whisper invents words in silence and loops on short clips; both off.
        assert kwargs["vad_filter"] is True
        assert kwargs["condition_on_previous_text"] is False

    async def test_language_none_means_autodetect(self) -> None:
        model = _fake_model()
        t = LocalWhisperTranscriber(model_loader=lambda: model, language="")

        await t.transcribe(b"a", "voice.ogg")

        assert model.transcribe.call_args.kwargs["language"] is None

    async def test_empty_audio_yields_empty_text(self) -> None:
        model = _fake_model([])
        t = LocalWhisperTranscriber(model_loader=lambda: model)

        result = await t.transcribe(b"", "voice.ogg")

        assert result.text == ""

    async def test_decode_failure_becomes_runtime_error(self) -> None:
        model = MagicMock()
        model.transcribe.side_effect = OSError("libctranslate2 exploded")
        t = LocalWhisperTranscriber(model_loader=lambda: model)

        with pytest.raises(RuntimeError, match="Transcription failed"):
            await t.transcribe(b"a", "voice.ogg")

    async def test_does_not_block_the_event_loop(self) -> None:
        """The decode is CPU-bound for seconds; the loop must keep running."""
        ticks = 0

        def slow_transcribe(*_args, **_kwargs):
            import time

            time.sleep(0.2)
            return iter([_segment("done")]), SimpleNamespace(language="en")

        model = MagicMock()
        model.transcribe.side_effect = slow_transcribe
        t = LocalWhisperTranscriber(model_loader=lambda: model)

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        result = await t.transcribe(b"a", "voice.ogg")
        task.cancel()

        assert result.text == "done"
        assert ticks > 3


class TestModelLifecycle:
    async def test_model_is_loaded_once_and_reused(self) -> None:
        calls = 0

        def loader():
            nonlocal calls
            calls += 1
            return _fake_model()

        t = LocalWhisperTranscriber(model_loader=loader)
        await t.transcribe(b"a", "voice.ogg")
        await t.transcribe(b"b", "voice.ogg")

        assert calls == 1

    async def test_concurrent_first_use_loads_once(self) -> None:
        """Two voice notes arriving together must not load 1.5 GB twice."""
        calls = 0

        def loader():
            nonlocal calls
            calls += 1
            import time

            time.sleep(0.05)
            return _fake_model()

        t = LocalWhisperTranscriber(model_loader=loader)
        await asyncio.gather(
            t.transcribe(b"a", "voice.ogg"), t.transcribe(b"b", "voice.ogg")
        )

        assert calls == 1

    async def test_load_failure_becomes_runtime_error(self) -> None:
        def loader():
            raise OSError("no such model")

        t = LocalWhisperTranscriber("some/model", model_loader=loader)

        with pytest.raises(RuntimeError, match="Could not load some/model"):
            await t.transcribe(b"a", "voice.ogg")

    async def test_missing_dependency_message_is_passed_through(self) -> None:
        """The install hint must survive to the user, not be wrapped as a load error."""

        def loader():
            raise RuntimeError("Local transcription needs faster-whisper. Install it")

        t = LocalWhisperTranscriber(model_loader=loader)

        with pytest.raises(RuntimeError, match="needs faster-whisper"):
            await t.transcribe(b"a", "voice.ogg")


class TestDefaults:
    def test_leaves_a_core_for_the_bot(self) -> None:
        assert default_cpu_threads() >= 1
        assert default_cpu_threads() <= max(1, (__import__("os").cpu_count() or 2) - 1)

    def test_zero_threads_means_auto(self) -> None:
        t = LocalWhisperTranscriber(cpu_threads=0)
        assert t.cpu_threads == default_cpu_threads()

    def test_explicit_threads_win(self) -> None:
        assert LocalWhisperTranscriber(cpu_threads=2).cpu_threads == 2

    def test_beam_size_floor(self) -> None:
        assert LocalWhisperTranscriber(beam_size=0).beam_size == 1

    def test_default_model_is_the_multilingual_turbo(self) -> None:
        assert "large-v3-turbo" in DEFAULT_MODEL
        assert LocalWhisperTranscriber().model == DEFAULT_MODEL
