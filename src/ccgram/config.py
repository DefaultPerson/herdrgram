"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/Claude paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > $CCGRAM_DIR/.env (default ~/.ccgram).
The module-level `config` instance is imported by nearly every other module.

Key class: Config (singleton instantiated as `config`).
"""

import structlog
import os
from pathlib import Path

from dotenv import load_dotenv

from .multiplexer.topic_mapping import TOPIC_LABEL_FULL, TOPIC_LABEL_TAB
from .utils import ccgram_dir

logger = structlog.get_logger()


def _parse_int_env(name: str, default: int) -> int:
    """Parse an integer from an env var with a clear error on bad values."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid integer: {exc}") from exc


def _resolve_toolbar_path() -> str:
    """Resolve the toolbar TOML config path: env var → ~/.ccgram → empty.

    Order:
      1. ``$CCGRAM_TOOLBAR_CONFIG`` if set (used as-is, even if missing)
      2. ``~/.ccgram/toolbar.toml`` if it exists
      3. ``""`` (use built-in defaults)
    """
    env = os.getenv("CCGRAM_TOOLBAR_CONFIG", "").strip()
    if env:
        return env
    fallback = ccgram_dir() / "toolbar.toml"
    return str(fallback) if fallback.exists() else ""


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccgram_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir
        # load_dotenv default override=False means first-loaded wins
        for env_path in (Path(".env"), self.config_dir / ".env"):
            if env_path.is_file():
                load_dotenv(env_path)
                logger.debug("Loaded env from %s", env_path.resolve())

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "ccgram")
        self.tmux_main_window_name = "__main__"
        # Own tmux window ID (set by run_bot() after auto-detect, used to skip self in list_windows)
        self.own_window_id: str | None = None

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"
        self.events_file = self.config_dir / "events.jsonl"

        # Claude Code session monitoring configuration
        _claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        self.claude_config_dir: Path = (
            Path(_claude_config_dir).expanduser()
            if _claude_config_dir
            else Path.home() / ".claude"
        )
        self.claude_projects_path = self.claude_config_dir / "projects"
        self.monitor_poll_interval = max(
            0.5, float(os.getenv("MONITOR_POLL_INTERVAL", "1.0"))
        )
        self.status_poll_interval, self.yolo_confirmation_timeout = (
            max(0.5, float(os.getenv("CCGRAM_STATUS_POLL_INTERVAL", "1.0"))),
            max(1.0, float(os.getenv("CCGRAM_YOLO_CONFIRMATION_TIMEOUT", "30.0"))),
        )

        # Multi-instance support
        group_id_str = os.getenv("CCGRAM_GROUP_ID")
        if group_id_str:
            try:
                self.group_id: int | None = int(group_id_str)
            except ValueError as e:
                raise ValueError(f"CCGRAM_GROUP_ID must be a valid integer: {e}") from e
        else:
            self.group_id = None

        # Provider selection
        self.provider_name: str = os.getenv("CCGRAM_PROVIDER", "claude")

        self._init_multiplexer()

        # Directory browser: show hidden (dot) directories
        self.show_hidden_dirs: bool = os.getenv(
            "CCGRAM_SHOW_HIDDEN_DIRS", ""
        ).lower() in ("1", "true", "yes")

        # Ack reaction: react to forwarded messages with an emoji (empty = disabled)
        self.ack_reaction: str = os.getenv("CCGRAM_ACK_REACTION", "")

        # Whisper transcription
        self.whisper_provider: str = os.getenv("CCGRAM_WHISPER_PROVIDER", "")
        self.whisper_api_key: str = os.getenv("CCGRAM_WHISPER_API_KEY", "")
        self.whisper_base_url: str = os.getenv("CCGRAM_WHISPER_BASE_URL", "")
        self.whisper_model: str = os.getenv("CCGRAM_WHISPER_MODEL", "")
        self.whisper_language: str = os.getenv("CCGRAM_WHISPER_LANGUAGE", "")

        # Voice replies (text-to-speech)
        # CCGRAM_TTS_PROVIDER: empty = disabled; "edge" = edge-tts; "openai" = OpenAI TTS
        self.tts_provider: str = os.getenv("CCGRAM_TTS_PROVIDER", "")
        self.tts_voice: str = os.getenv(
            "CCGRAM_TTS_VOICE", "en-US-EmmaMultilingualNeural"
        )
        self.tts_model: str = os.getenv("CCGRAM_TTS_MODEL", "gpt-4o-mini-tts")
        self.tts_api_key: str = os.getenv("CCGRAM_TTS_API_KEY", "")

        # LLM command generation (shell provider) and toolbar config path.
        # toolbar_config_path resolution: env var → ~/.ccgram/toolbar.toml → "".
        # Empty string means "use built-in defaults". The handler layer passes
        # this path to ``toolbar_config.load_toolbar_config()`` once at startup.
        self._init_shell_and_llm()
        self._init_live_view()
        self._init_send()
        self._init_lifecycle()
        self._init_transcript_visibility()
        self._init_topic_naming()

        # Voice confirmation is safer by default; enable only for trusted,
        # low-friction dictation workflows.
        self.voice_autosend: bool = os.getenv(
            "CCGRAM_VOICE_AUTOSEND", "false"
        ).lower() in ("1", "true", "yes")
        # Hide only the transient status presentation; replies and controls
        # remain available through their normal paths.
        self.hide_status: bool = os.getenv("CCGRAM_HIDE_STATUS", "false").lower() in (
            "1",
            "true",
            "yes",
        )

        # Global default batch mode: ephemeral tools (single rolling message deleted
        # on completion). Off by default. Per-window batch_mode takes precedence when
        # explicitly set to any value other than DEFAULT_BATCH_MODE via /verbose.
        self.ephemeral_tools: bool = os.getenv(
            "CCGRAM_EPHEMERAL_TOOLS", ""
        ).lower() in ("1", "true", "yes")

        # Color mapping for the topic state emoji prefix.
        # "system" (default): green=active, yellow=idle (system POV: green=working).
        # "user": green=idle, yellow=active (user POV: green=ready for me).
        # Invalid values fall back to "system".
        raw_status_mode = os.getenv("CCGRAM_STATUS_MODE", "").strip().lower()
        self.status_mode: str = (
            raw_status_mode if raw_status_mode in ("system", "user") else "system"
        )

        logger.debug(
            "Config initialized: dir=%s, allowed_users=%d, tmux_session=%s",
            self.config_dir,
            len(self.allowed_users),
            self.tmux_session_name,
        )

    def _init_multiplexer(self) -> None:
        """Select the terminal-multiplexer backend."""
        # tmux default; herdr and agterm opt-in.
        self.multiplexer_name: str = os.getenv("CCGRAM_MULTIPLEXER", "tmux")
        # Upstream resolves a window's status by scraping the terminal first
        # and only asks a ``native_agent_status`` backend (herdr, agterm) when
        # the scrapers came back empty, so any spinner-looking leftover on
        # screen outvotes the backend that actually knows.
        # CCGRAM_HERDR_NATIVE_STATUS_AUTHORITY=true inverts that on those
        # backends: the native state decides, and the scrapers only supply the
        # label (or the interactive prompt) for the state it reports. On tmux,
        # which has no native status, the flag changes nothing.
        self.herdr_native_status_authority: bool = os.getenv(
            "CCGRAM_HERDR_NATIVE_STATUS_AUTHORITY", ""
        ).lower() in ("1", "true", "yes")

    def _init_live_view(self) -> None:
        self.live_view_interval: int = max(
            1, _parse_int_env("CCGRAM_LIVE_VIEW_INTERVAL", 5)
        )
        self.live_view_timeout: int = max(
            1, _parse_int_env("CCGRAM_LIVE_VIEW_TIMEOUT", 300)
        )

    def _init_shell_and_llm(self) -> None:
        self.prompt_mode = os.getenv("CCGRAM_PROMPT_MODE", "wrap")
        self.prompt_marker = os.getenv("CCGRAM_PROMPT_MARKER", "ccgram")
        self.toolbar_config_path: str = _resolve_toolbar_path()
        self.llm_provider: str = os.getenv("CCGRAM_LLM_PROVIDER", "")
        self.llm_api_key: str = os.getenv("CCGRAM_LLM_API_KEY", "")
        self.llm_base_url: str = os.getenv("CCGRAM_LLM_BASE_URL", "")
        self.llm_model: str = os.getenv("CCGRAM_LLM_MODEL", "")
        try:
            self.llm_temperature: float = float(
                os.getenv("CCGRAM_LLM_TEMPERATURE", "0.1")
            )
        except ValueError as e:
            raise ValueError(
                f"CCGRAM_LLM_TEMPERATURE must be a valid number: {e}"
            ) from e

    def _init_send(self) -> None:
        self.send_search_depth: int = _parse_int_env("CCGRAM_SEND_SEARCH_DEPTH", 5)
        self.send_max_results: int = _parse_int_env("CCGRAM_SEND_MAX_RESULTS", 50)

    def _init_transcript_visibility(self) -> None:
        """Which transcript content reaches Telegram, and what the desk sees."""
        # Global default for hiding tool_use/tool_result content in Telegram.
        # Shown by default; set CCGRAM_HIDE_TOOL_CALLS=true to suppress globally.
        # Per-window override via WindowState.tool_call_visibility takes precedence.
        self.hide_tool_calls: bool = os.getenv(
            "CCGRAM_HIDE_TOOL_CALLS", "false"
        ).lower() in ("1", "true", "yes")
        self.hide_thinking: bool = os.getenv(
            "CCGRAM_HIDE_THINKING", "false"
        ).lower() in (
            "1",
            "true",
            "yes",
        )
        # Round-trip visibility between a topic and its terminal, both at the
        # upstream default. CCGRAM_ECHO_USER_MESSAGES=false drops the 👤 echo
        # of text typed in the pane itself (Telegram-originated input has its
        # own dedup path and is unaffected either way);
        # CCGRAM_HERDR_NOTIFY_ON_INJECT=true raises a desktop notification when
        # a Telegram message lands in a pane, on backends that declare
        # supports_notifications (herdr).
        self.echo_user_messages: bool = os.getenv(
            "CCGRAM_ECHO_USER_MESSAGES", "true"
        ).lower() not in ("0", "false", "no")
        self.herdr_notify_on_inject: bool = os.getenv(
            "CCGRAM_HERDR_NOTIFY_ON_INJECT", ""
        ).lower() in ("1", "true", "yes")

    def _init_topic_naming(self) -> None:
        """How a topic is titled and decorated, and which side may rename it.

        All four default to the upstream behaviour.
        """
        # Upstream prefixes every topic title with a state emoji and appends an
        # RC/YOLO badge, which means an editForumTopic call (and a "topic
        # renamed" service message) on every state change.
        # CCGRAM_TOPIC_NAME_DECORATIONS=false renders the bare display name
        # instead; a state change then produces no rename at all, while a
        # change of the display name itself is still synced.
        self.topic_name_decorations: bool = os.getenv(
            "CCGRAM_TOPIC_NAME_DECORATIONS", "true"
        ).lower() not in ("0", "false", "no")
        # Upstream pushes a Telegram topic rename back into the multiplexer.
        # On herdr that is a ``tab rename``, which relabels the tab for every
        # pane sharing it. CCGRAM_MUX_RENAME_FROM_TELEGRAM=false drops the
        # rename instead, leaving names to flow multiplexer → Telegram only.
        self.mux_rename_from_telegram: bool = os.getenv(
            "CCGRAM_MUX_RENAME_FROM_TELEGRAM", "true"
        ).lower() not in ("0", "false", "no")
        # Upstream creates every topic with Telegram's default icon, so a chat
        # of agent topics is one grey column. CCGRAM_TOPIC_RANDOM_ICON=true
        # gives each topic CCGram creates a random icon from
        # getForumTopicIconStickers and a random colour, purely to tell them
        # apart at a glance.
        self.topic_random_icon: bool = os.getenv(
            "CCGRAM_TOPIC_RANDOM_ICON", ""
        ).lower() in ("1", "true", "yes")
        # herdr topic titles. "full" (default) keeps the upstream
        # "<Provider> ▸ <workspace> ▸ <tab> ▸ <pane>" label; "tab" uses the
        # herdr tab label on its own, so a topic reads exactly like the herdr
        # sidebar. Invalid values fall back to "full".
        raw_topic_label = os.getenv("CCGRAM_HERDR_TOPIC_LABEL", "").strip().lower()
        self.herdr_topic_label: str = (
            raw_topic_label
            if raw_topic_label in (TOPIC_LABEL_FULL, TOPIC_LABEL_TAB)
            else TOPIC_LABEL_FULL
        )
        self._init_topic_on_demand()

    def _init_topic_on_demand(self) -> None:
        """Whether a discovered session gets a topic or an offer of one.

        All three default to the upstream behaviour: every eligible window
        discovered without a binding is adopted immediately, and a herdr record
        is eligible whether or not herdr has named its agent session yet.
        """
        # Upstream creates a topic the moment discovery finds an unbound
        # eligible window. CCGRAM_TOPIC_ON_DEMAND=true posts one offer to the
        # General topic instead and creates the topic only when it is accepted,
        # so a chat does not fill with topics nobody asked for.
        self.topic_on_demand: bool = os.getenv(
            "CCGRAM_TOPIC_ON_DEMAND", ""
        ).lower() in (
            "1",
            "true",
            "yes",
        )
        # How many transcript messages a newly bound topic replays before live
        # delivery takes over. Upstream replays nothing — a topic bound to a
        # session that has been running for a while starts empty and only shows
        # what happens next — so 0 is the default and turns the feature off.
        self.topic_backfill_messages: int = max(
            0, _parse_int_env("CCGRAM_TOPIC_BACKFILL_MESSAGES", 0)
        )
        # herdr reports a starting agent before it can name that agent's
        # session, and names it a few seconds later; the adapter mints a
        # terminal-derived stand-in identity for the gap so a hook-capable
        # agent stays addressable. Adopting that stand-in costs a second topic
        # when the named identity arrives, because the two carry different
        # targets. CCGRAM_HERDR_REQUIRE_NATIVE_SESSION=true withholds the
        # stand-in from discovery; it stays in the complete listing, so
        # liveness and cleanup still see it.
        self.herdr_require_native_session: bool = os.getenv(
            "CCGRAM_HERDR_REQUIRE_NATIVE_SESSION", ""
        ).lower() in ("1", "true", "yes")

    def _init_lifecycle(self) -> None:
        self.autoclose_done_minutes: int = int(
            os.getenv("AUTOCLOSE_DONE_MINUTES", "30")
        )
        self.autoclose_dead_minutes: int = int(
            os.getenv("AUTOCLOSE_DEAD_MINUTES", "10")
        )
        self.pane_lifecycle_notify: bool = os.getenv(
            "CCGRAM_PANE_LIFECYCLE_NOTIFY", ""
        ).lower() in ("1", "true", "yes")
        # Symmetric topic lifecycle, both off by default (upstream behaviour):
        # closing a topic keeps its window alive for rebinding, and autoclose
        # closes the topic instead of deleting it (deletion is irreversible).
        self.kill_on_topic_close: bool = os.getenv(
            "CCGRAM_KILL_ON_TOPIC_CLOSE", ""
        ).lower() in ("1", "true", "yes")
        self.delete_topic_on_autoclose: bool = os.getenv(
            "CCGRAM_DELETE_TOPIC_ON_AUTOCLOSE", ""
        ).lower() in ("1", "true", "yes")
        self._init_miniapp()

    def _init_miniapp(self) -> None:
        # Mini App backend (Phase 3 / Theme 6) — disabled when base URL is empty.
        # base_url is the externally reachable URL Telegram uses to open the
        # WebApp; host/port control the local aiohttp listener.
        self.miniapp_base_url: str = os.getenv("CCGRAM_MINIAPP_BASE_URL", "").strip()
        self.miniapp_host: str = os.getenv("CCGRAM_MINIAPP_HOST", "127.0.0.1")
        self.miniapp_port: int = _parse_int_env("CCGRAM_MINIAPP_PORT", 8765)

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users


config = Config()
