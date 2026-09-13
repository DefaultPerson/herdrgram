"""Voice message handler — download OGG audio, transcribe, present the review card.

Handles Telegram voice messages by downloading the audio, transcribing it with
the configured Whisper provider, and showing the text with a review keyboard so
the user reads what the agent is about to be told before it is told.

A placeholder message goes out before the transcription starts and becomes the
result in place. Local transcription on a CPU takes about as long as the
recording itself, and a typing indicator expires after five seconds — the
placeholder is the only feedback that survives the wait, and editing it keeps
the topic to one message per voice note either way.

Key handler:
  - handle_voice_message: main entry point for filters.VOICE
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from ...config import config
from ...thread_router import thread_router
from ...whisper import get_transcriber
from ...whisper.base import TranscriptionResult, WhisperTranscriber
from ..callback_helpers import get_thread_id
from ..messaging_pipeline.message_sender import safe_edit, safe_reply
from ..user_state import VOICE_PENDING

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

# Max voice file size: 25 MB (Telegram Bot API getFile limit)
_MAX_VOICE_SIZE = 25 * 1024 * 1024


TRANSCRIBING = "🎤 Transcribing…"
TRANSCRIBED = "🎤 Transcribed:\n\n{text}"

BUTTON_SEND = "✅ Send to agent"
BUTTON_AGAIN = "🔁 Re-record"
BUTTON_CLOSE = "✖️ Close"


def _build_voice_keyboard(message_id: int) -> InlineKeyboardMarkup:
    """Build the review keyboard for a transcribed voice message.

    Send is the one destructive-by-omission action (it reaches the agent), so it
    gets its own row; the two ways of not sending share the second.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BUTTON_SEND, callback_data=f"vc:send:{message_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    BUTTON_AGAIN, callback_data=f"vc:again:{message_id}"
                ),
                InlineKeyboardButton(
                    BUTTON_CLOSE, callback_data=f"vc:drop:{message_id}"
                ),
            ],
        ]
    )


async def _download_voice(message: Message, file_id: str) -> bytes | None:
    """Download voice audio from Telegram. Returns bytes or None on error."""
    try:
        file = await message.get_bot().get_file(file_id)
        audio_bytearray = await file.download_as_bytearray()
        return bytes(audio_bytearray)
    except TelegramError as e:
        logger.warning("Failed to download voice message: %s", e)
        await safe_reply(message, "❌ Failed to download voice message.")
        return None


async def _get_transcriber_or_reply(message: Message) -> WhisperTranscriber | None:
    """Resolve the configured transcriber and surface user-facing errors."""
    try:
        transcriber = get_transcriber()
    except (ValueError, RuntimeError) as e:
        await safe_reply(message, f"❌ {e}")
        return None

    if transcriber is None:
        await safe_reply(
            message,
            "⚠️ Voice transcription is not configured. Set CCGRAM_WHISPER_PROVIDER "
            "to enable it.\n\nSupported providers: local (faster-whisper, on this "
            "machine), openai, groq",
        )
        return None

    return transcriber


async def _report(progress: Message | None, message: Message, text: str) -> None:
    """Say *text* by editing the placeholder, or as a new reply if it is gone."""
    if progress is not None:
        await safe_edit(progress, text)
        return
    await safe_reply(message, text)


async def _transcribe_audio(
    message: Message,
    transcriber: WhisperTranscriber,
    audio_bytes: bytes,
    progress: Message | None = None,
) -> TranscriptionResult | None:
    """Transcribe audio bytes. Returns TranscriptionResult or None on error."""
    try:
        return await transcriber.transcribe(audio_bytes, "voice.ogg")
    except (ValueError, RuntimeError) as e:
        await _report(progress, message, f"❌ {e}")
        return None


async def _send_transcribed_text(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    message: Message,
) -> tuple[bool, str | None]:
    """Forward auto-sent transcription through the confirmation send path."""
    # Lazy: voice_callbacks imports the provider-aware voice send path.
    from .voice_callbacks import send_transcribed_text

    # Lazy: keep the handler import path free of the Telegram client cycle.
    from ...telegram_client import PTBTelegramClient

    return await send_transcribed_text(
        PTBTelegramClient(message.get_bot()),
        user_id,
        thread_id,
        window_id,
        text,
        message.chat.id,
    )


async def _send_confirm_message(
    message: Message,
    text: str,
    context: ContextTypes.DEFAULT_TYPE,
    progress: Message | None = None,
) -> None:
    """Show the transcription with its review keyboard, editing *progress* if given.

    The pending text is keyed by the original voice message_id, which is also
    what the callbacks carry: Send reacts to it, Re-record deletes it.
    """
    keyboard = _build_voice_keyboard(message.message_id)
    body = TRANSCRIBED.format(text=text)
    if progress is not None:
        await safe_edit(progress, body, reply_markup=keyboard)
    else:
        confirm_msg = await safe_reply(message, body, reply_markup=keyboard)
        if confirm_msg is None:
            return

    if context.user_data is not None:
        key = (message.chat.id, message.message_id)
        context.user_data.setdefault(VOICE_PENDING, {})[key] = text


async def _deliver_transcription(
    message: Message,
    text: str,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    context: ContextTypes.DEFAULT_TYPE,
    progress: Message | None = None,
) -> None:
    """Present a transcription and optionally forward it without confirmation."""
    if config.voice_autosend is not True:
        await _send_confirm_message(message, text, context, progress)
        return

    body = TRANSCRIBED.format(text=text)
    if progress is not None:
        await safe_edit(progress, body)
    elif await safe_reply(message, body) is None:
        return
    success, error = await _send_transcribed_text(
        user_id, thread_id, window_id, text, message
    )
    if not success:
        await safe_reply(message, f"❌ {error or 'Failed to send'}")


async def handle_voice_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle incoming voice messages: transcribe and present confirm keyboard."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.voice:
        return

    if not config.is_user_allowed(user.id):
        await safe_reply(message, "You are not authorized to use this bot.")
        return

    thread_id = get_thread_id(update)
    window_id = thread_router.resolve_window_for_thread(
        user.id, thread_id, message.chat.id
    )
    if not window_id:
        await safe_reply(
            message,
            "⚠ Topic not bound — send a text message first to pick a "
            "directory, then re-record.\n"
            "\U0001f4ac Voice messages aren't queued.",
        )
        return

    voice = message.voice
    if voice.file_size is not None and voice.file_size > _MAX_VOICE_SIZE:
        size_mb = voice.file_size / (1024 * 1024)
        await safe_reply(
            message,
            f"❌ Voice message too large ({size_mb:.1f} MB). Maximum 25 MB.",
        )
        return

    transcriber = await _get_transcriber_or_reply(message)
    if transcriber is None:
        return

    audio_bytes = await _download_voice(message, voice.file_id)
    if audio_bytes is None:
        return

    await message.get_bot().send_chat_action(
        chat_id=message.chat.id,
        message_thread_id=message.message_thread_id,
        action=ChatAction.TYPING,
    )
    # Posted before the model runs and edited in place afterwards. A local
    # decode outlives the typing indicator, so without this the topic is silent
    # for the length of the recording and the user records again into the gap.
    progress = await safe_reply(message, TRANSCRIBING)

    result = await _transcribe_audio(message, transcriber, audio_bytes, progress)
    if result is None:
        return

    if not result.text.strip():
        await _report(progress, message, "⚠️ Could not transcribe audio (empty result).")
        return

    await _deliver_transcription(
        message,
        result.text,
        user.id,
        thread_id,
        window_id,
        context,
        progress,
    )
