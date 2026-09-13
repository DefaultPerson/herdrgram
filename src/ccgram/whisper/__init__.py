"""Whisper transcription provider abstraction.

Two shapes of provider behind one Protocol: an OpenAI-compatible HTTP API
(OpenAI, Groq, or any local server that speaks it) and ``local``, which runs
faster-whisper in this process and needs no key and no network.
"""

import os

from .base import WhisperTranscriber
from .httpx_transcriber import OpenAICompatTranscriber
from .local_transcriber import (
    DEFAULT_COMPUTE_TYPE,
    DEFAULT_DEVICE,
    DEFAULT_MODEL as DEFAULT_LOCAL_MODEL,
    LocalWhisperTranscriber,
)

LOCAL_PROVIDER = "local"

_PROVIDERS = {
    "openai": {"base_url": None, "model": "whisper-1", "api_key_env": "OPENAI_API_KEY"},
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "whisper-large-v3",
        "api_key_env": "GROQ_API_KEY",
    },
}

SUPPORTED_PROVIDERS: tuple[str, ...] = (*_PROVIDERS, LOCAL_PROVIDER)


# The local transcriber owns loaded weights, so it must outlive the call that
# built it: ``get_transcriber`` runs once per voice message, and a fresh object
# each time would reload ~1.5 GB per message. Keyed by the settings that shape
# the model, so a config change still takes effect.
_local_cache: tuple[tuple, LocalWhisperTranscriber] | None = None


def reset_local_transcriber() -> None:
    """Drop the cached local transcriber (tests, and a deliberate reload)."""
    global _local_cache
    _local_cache = None


def _build_local_transcriber() -> WhisperTranscriber:
    """Return the process-wide local transcriber, building it on first use."""
    global _local_cache
    # Lazy: config singleton resolved by factory call
    from ccgram.config import config

    key = (
        config.whisper_model or DEFAULT_LOCAL_MODEL,
        config.whisper_device or DEFAULT_DEVICE,
        config.whisper_compute_type or DEFAULT_COMPUTE_TYPE,
        config.whisper_threads,
        config.whisper_beam_size,
        config.whisper_language or None,
    )
    if _local_cache is not None and _local_cache[0] == key:
        return _local_cache[1]

    model, device, compute_type, threads, beam_size, language = key
    transcriber = LocalWhisperTranscriber(
        model,
        device=device,
        compute_type=compute_type,
        cpu_threads=threads,
        beam_size=beam_size,
        language=language,
    )
    _local_cache = (key, transcriber)
    return transcriber


def get_transcriber() -> WhisperTranscriber | None:
    """Create and return a Whisper transcriber based on config.

    Returns None if whisper_provider is not configured (empty string).
    """
    # Lazy: config singleton resolved by factory call
    from ccgram.config import config

    provider = config.whisper_provider
    if not provider:
        return None

    if provider == LOCAL_PROVIDER:
        return _build_local_transcriber()

    provider_info = _PROVIDERS.get(provider)
    if not provider_info:
        msg = f"Unknown whisper provider: {provider}"
        raise ValueError(msg)

    api_key = config.whisper_api_key
    if not api_key:
        api_key_env = provider_info["api_key_env"]
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            msg = f"No API key found: set {api_key_env} or CCGRAM_WHISPER_API_KEY"
            raise ValueError(msg)

    base_url = config.whisper_base_url or provider_info["base_url"]
    model = config.whisper_model or provider_info["model"]
    language = config.whisper_language or None

    return OpenAICompatTranscriber(
        api_key=api_key,
        model=model,
        base_url=base_url,
        language=language,
    )
