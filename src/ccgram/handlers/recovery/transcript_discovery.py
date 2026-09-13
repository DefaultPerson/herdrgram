"""Transcript discovery for hookless providers.

Discovers and registers transcripts for providers without hook support
(Codex, Gemini). Also handles provider auto-detection from pane process
and shell ↔ agent transitions.

Key components:
  - discover_and_register_transcript: main discovery function called per topic
  - seed_session_from_native_id: register the session a backend already names
  - _detect_and_apply_provider: provider auto-detection from running process
  - _find_and_register_transcript: transcript search for hookless providers
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ... import session_query
from ...providers import (
    detect_provider_from_pane,
    detect_provider_from_runtime,
    detect_provider_from_transcript_path,
    get_cached_foreground_pgid,
    get_provider_for_window,
    should_probe_pane_title_for_provider_detection,
)
from ...session import session_manager
from ...session_map import session_map_prefix, session_map_sync
from ...telegram_client import TelegramClient
from ...multiplexer import multiplexer as tmux_manager
from ...window_state_ports import identity_state

if TYPE_CHECKING:
    from ...providers.base import AgentProvider
    from ...multiplexer.base import WindowRef as TmuxWindow

logger = structlog.get_logger()


def _session_id_already_bound(session_id: str, window_id: str) -> bool:
    """Return True if another currently bound window already uses ``session_id``."""
    # Lazy: thread_router may not be installed in some test paths; fail open
    # if it isn't available so discovery can still continue with this window.
    from ...thread_router import thread_router

    try:
        iterator = thread_router.iter_thread_bindings()
    except RuntimeError:
        return False

    for _user_id, _thread_id, bound_window_id in iterator:
        if bound_window_id == window_id:
            continue
        if identity_state.get_session_id(bound_window_id) == session_id:
            return True
    return False


# Providers whose transcript file is fully determined by a session id and a
# working directory. Claude names the file after the session id inside a
# directory derived from the cwd, so a backend that publishes the session id
# gives us everything needed to find it. Codex and Gemini name their files by
# their own scheme and are already located by the hookless scan below, so they
# are deliberately absent — an entry here is a claim that the pair is enough.
_NATIVE_SEED_TRANSCRIPTS: dict[str, Callable[[str, str], Path | None]] = {
    "claude": session_query.build_claude_transcript_path,
}


def _native_seed_cwds(
    w: "TmuxWindow", identity: identity_state.IdentityProjection | None
) -> list[str]:
    """Directories to try when deriving the transcript, best candidate first.

    The backend's own value leads: it reports the directory the agent runs in,
    which is the one the provider encoded into the transcript's path. The
    stored cwd follows as a fallback for a backend that reports none.
    """
    cwds: list[str] = []
    for cwd in (w.cwd, identity.cwd if identity else ""):
        if cwd and cwd not in cwds:
            cwds.append(cwd)
    return cwds


async def seed_session_from_native_id(
    window_id: str,
    w: "TmuxWindow | None",
    identity: identity_state.IdentityProjection | None,
) -> bool:
    """Register the session the multiplexer already names for this window.

    Only a session with a ``session_map.json`` entry is monitored, and for a
    hookful provider that entry is written by the agent's own SessionStart
    hook. A session that was already running when the hook was installed, or
    whose hook failed, therefore gets a Telegram topic that never receives a
    line: no other path creates the entry, because Stop and Notification
    events refuse to author one.

    A backend that publishes the agent's native session id knows what that
    hook would have written. Derive the transcript from the id and the working
    directory and register it the way a hookless provider is registered, which
    is the same pair of writes and the same guard against two topics claiming
    one session.

    Returns whether an entry was written. Nothing is replayed: the monitor
    starts a session it has not tracked before at the current end of the file,
    so seeding a multi-megabyte transcript delivers its next line, not its
    history.
    """
    if w is None or not w.native_session_id or not w.native_agent:
        return False
    session_id = w.native_session_id

    if (
        identity is not None
        and identity.session_id == session_id
        and identity.transcript_path is not None
    ):
        # Already tracking exactly this session — the steady-state answer on
        # every tick of every bound topic, so it must not touch the disk.
        return False

    derive = _NATIVE_SEED_TRANSCRIPTS.get(w.native_agent)
    if derive is None:
        return False

    if _session_id_already_bound(session_id, window_id):
        logger.debug(
            "Skipping native session seed: session is bound to another window",
            window_id=window_id,
            session_id=session_id,
        )
        return False

    for cwd in _native_seed_cwds(w, identity):
        transcript_path = derive(session_id, cwd)
        if transcript_path is None or not await asyncio.to_thread(
            transcript_path.is_file
        ):
            continue
        session_map_sync.register_hookless_session(
            window_id=window_id,
            session_id=session_id,
            cwd=cwd,
            transcript_path=str(transcript_path),
            provider_name=w.native_agent,
        )
        await asyncio.to_thread(
            session_map_sync.write_hookless_session_map,
            window_id=window_id,
            session_id=session_id,
            cwd=cwd,
            transcript_path=str(transcript_path),
            provider_name=w.native_agent,
        )
        logger.info(
            "Seeded session map from multiplexer native session id",
            window_id=window_id,
            session_id=session_id,
            provider=w.native_agent,
            cwd=cwd,
            transcript_path=str(transcript_path),
        )
        return True
    return False


def _is_agent_origin(
    window_id: str, identity: identity_state.IdentityProjection
) -> bool:
    """Return whether a shell transition means the bound agent exited."""
    initial_provider = (
        identity_state.get_initial_provider_name(window_id) or identity.provider_name
    )
    return identity.provider_name not in ("", "shell") and initial_provider != "shell"


async def _detect_and_apply_provider(
    window_id: str,
    identity: identity_state.IdentityProjection,
    w: "TmuxWindow",
    *,
    client: TelegramClient | None = None,
    chat_id: int = 0,
    thread_id: int = 0,
) -> bool:
    """Apply provider transitions; report when an agent-origin pane became a shell."""
    if identity_state.is_provider_manually_overridden(window_id):
        return False
    detected = await detect_provider_from_pane(
        w.pane_current_command, window_id=window_id
    )
    if not detected and should_probe_pane_title_for_provider_detection(
        w.pane_current_command
    ):
        pane_title = await tmux_manager.get_pane_title(window_id)
        detected = detect_provider_from_runtime(
            w.pane_current_command,
            pane_title=pane_title,
        )

    if detected == "shell" and _is_agent_origin(window_id, identity):
        logger.info(
            "Agent exited to shell; keeping provider for recovery",
            window_id=window_id,
            provider=identity.provider_name,
        )
        return True

    if detected and detected != identity.provider_name:
        old_provider = identity.provider_name
        session_manager.set_window_provider(window_id, detected, cwd=w.cwd or None)
        # Lazy: providers/__init__.py reaches back into transcript code
        # via provider format modules.
        from ...providers import get_provider_for_window

        new_caps = get_provider_for_window(window_id, detected)
        old_caps = (
            get_provider_for_window(window_id, old_provider) if old_provider else None
        )
        if new_caps and new_caps.capabilities.chat_first_command_path:
            identity_state.clear_transcript_path(window_id)
            # Lazy: shell.shell_prompt_orchestrator hits the recovery
            # subpackage's discovery code via send-keys callbacks.
            from ..shell.shell_prompt_orchestrator import ensure_setup

            await ensure_setup(
                window_id,
                "provider_switch",
                client=client,
                chat_id=chat_id,
                thread_id=thread_id,
            )
        elif old_caps and old_caps.capabilities.chat_first_command_path:
            # Lazy: same shell ↔ recovery cycle as above.
            from ..shell.shell_capture import clear_shell_monitor_state

            # Lazy: same shell ↔ recovery cycle as above.
            from ..shell.shell_prompt_orchestrator import (
                clear_state as clear_orchestrator,
            )

            clear_shell_monitor_state(window_id)
            clear_orchestrator(window_id)
    elif not detected and identity.transcript_path:
        inferred = detect_provider_from_transcript_path(str(identity.transcript_path))
        if inferred and inferred != identity.provider_name:
            session_manager.set_window_provider(window_id, inferred, cwd=w.cwd or None)
    return False


def _resolve_providers_to_try(
    window_id: str,
    identity: identity_state.IdentityProjection,
    w: "TmuxWindow | None",
) -> list[tuple[str, "AgentProvider"]] | None:
    """Determine which providers to probe for transcripts.

    Returns a list of (name, provider) pairs, or ``None`` to signal the
    caller should set up a shell provider.
    """
    # Lazy: hoisting forms polling/__init__ → window_tick →
    # recovery.transcript_discovery → polling_state partial-init
    # cycle (worker-order-dependent; verified during F6.2). polling_types
    # is leaf-level — Task 5 of Round 5 may hoist this once cycle test covers it.
    # Lazy: polling_types is leaf-pure; importing here at module load would touch the polling subpackage __init__
    from ..polling.polling_types import is_shell_prompt

    # Lazy: providers registry reaches back through transcripts
    from ...providers import registry

    if identity.provider_name:
        provider = get_provider_for_window(window_id, identity.provider_name)
        if provider.capabilities.chat_first_command_path:
            return []
        return [(provider.capabilities.name, provider)]

    if w and is_shell_prompt(w.pane_current_command):
        return None  # signals caller to set up shell

    return [
        (name, registry.get(name))
        for name in registry.provider_names()
        if not registry.get(name).capabilities.supports_hook and name != "shell"
    ]


async def _find_and_register_transcript(
    window_id: str,
    identity: identity_state.IdentityProjection,
    providers_to_try: list[tuple[str, "AgentProvider"]],
    pane_alive: bool,
) -> None:
    """Search for transcripts among candidate providers and register if found."""
    window_key = f"{session_map_prefix()}{window_id}"

    transcript_path_str = (
        str(identity.transcript_path) if identity.transcript_path else ""
    )

    for provider_name, provider in providers_to_try:
        max_age = 0 if pane_alive else None
        event = await asyncio.to_thread(
            provider.discover_transcript,
            identity.cwd,
            window_key,
            max_age=max_age,
        )
        if not event:
            continue

        if _session_id_already_bound(event.session_id, window_id):
            logger.debug(
                "Skipping discover result for window %s: session_id %s already bound",
                window_id,
                event.session_id,
            )
            continue

        if (
            identity.session_id == event.session_id
            and transcript_path_str == event.transcript_path
            and identity.provider_name == provider_name
        ):
            return

        session_map_sync.register_hookless_session(
            window_id=window_id,
            session_id=event.session_id,
            cwd=event.cwd,
            transcript_path=event.transcript_path,
            provider_name=provider_name,
        )
        await asyncio.to_thread(
            session_map_sync.write_hookless_session_map,
            window_id=window_id,
            session_id=event.session_id,
            cwd=event.cwd,
            transcript_path=event.transcript_path,
            provider_name=provider_name,
        )
        return


def _hook_already_resolved(
    window_id: str, identity: identity_state.IdentityProjection
) -> bool:
    """True when a hookful provider has already populated transcript_path."""
    if not identity.provider_name:
        return False
    provider = get_provider_for_window(window_id, identity.provider_name)
    return bool(provider.capabilities.supports_hook and identity.transcript_path)


def _foreground_process_restarted(
    *,
    before_pgid: int,
    after_pgid: int,
    old_identity: identity_state.IdentityProjection,
    new_identity: identity_state.IdentityProjection,
) -> bool:
    """True when the same provider is running in a new foreground process group."""
    return bool(
        before_pgid
        and after_pgid
        and before_pgid != after_pgid
        and old_identity.session_id
        and old_identity.provider_name
        and old_identity.provider_name == new_identity.provider_name
    )


async def _switch_to_shell(
    window_id: str,
    *,
    client: TelegramClient | None,
    chat_id: int,
    thread_id: int,
) -> None:
    """Provider-switch to shell and clear transcript bookkeeping."""
    session_manager.set_window_provider(window_id, "shell")
    identity_state.clear_transcript_path(window_id)
    # Lazy: same shell ↔ recovery cycle as _detect_and_apply_provider.
    from ..shell.shell_prompt_orchestrator import ensure_setup

    await ensure_setup(
        window_id,
        "provider_switch",
        client=client,
        chat_id=chat_id,
        thread_id=thread_id,
    )


async def _complete_transcript_discovery(
    window_id: str,
    identity: identity_state.IdentityProjection,
    window: "TmuxWindow | None",
    providers_to_try: list[tuple[str, "AgentProvider"]] | None,
    *,
    client: TelegramClient | None,
    chat_id: int,
    thread_id: int,
) -> bool:
    """Complete transcript discovery or signal an agent-origin shell fallback."""
    if providers_to_try is None:
        if _is_agent_origin(window_id, identity):
            return True
        await _switch_to_shell(
            window_id, client=client, chat_id=chat_id, thread_id=thread_id
        )
        return False
    if not providers_to_try:
        return False

    # Lazy: importing polling package modules above the function creates a cycle.
    from ..polling.polling_types import is_shell_prompt

    pane_alive = window is not None and not is_shell_prompt(window.pane_current_command)
    await _find_and_register_transcript(
        window_id, identity, providers_to_try, pane_alive
    )
    return False


async def _seed_and_refresh_identity(
    window_id: str,
    w: "TmuxWindow | None",
    identity: identity_state.IdentityProjection,
) -> identity_state.IdentityProjection:
    """Seed from the backend's native session id and re-read what it wrote.

    A successful seed leaves the window holding the entry its hook would have
    written, so the caller's hook-resolved check reads it as tracked and the
    hookless scan is skipped — but only if it sees the state after the write,
    not the projection taken before it.
    """
    if not await seed_session_from_native_id(window_id, w, identity):
        return identity
    refreshed = identity_state.get_identity(window_id)
    return refreshed if refreshed is not None else identity


async def _bootstrap_identity(
    window_id: str, w: "TmuxWindow | None"
) -> identity_state.IdentityProjection | None:
    """Create window state for a live window ccgram has no state for yet.

    Binding a window normally writes its state, so this is a recovery path:
    a window whose state was swept while the stale-state guard was dead, or
    one bound by a build that predates that fix, is otherwise stuck — without
    state there is no identity, and without an identity discovery returns
    before it can create one. Seeding the provider (and cwd when the backend
    exposes it) lets such a window heal on the next tick.
    """
    if w is None:
        return None
    if await session_map_sync.session_map_entry_may_exist(window_id):
        # The hook already tracks this window, so ccgram is not missing its
        # record — only the in-memory state, which the monitor rebuilds from
        # that entry within one sync. Seeding here would race it and, worse,
        # destroy it: on a state-less window set_window_provider counts as a
        # provider switch and clears the entry, and hook.py refuses to recreate
        # one from any non-SessionStart event ("SessionStart owns initial
        # creation"). The live session would go untracked until the agent is
        # restarted, with its messages no longer reaching the topic.
        return None

    detected = await detect_provider_from_pane(
        w.pane_current_command or "", window_id=window_id
    )
    if not detected or detected == "shell":
        # On a state-less window ``set_window_provider`` seeds
        # ``initial_provider_name`` from the value being written, so bootstrapping
        # a shell would stamp the window shell-origin permanently and
        # ``_is_agent_origin`` would never fire the recovery banner again — for a
        # dead topic whose agent already exited to a shell, which is exactly the
        # population this heals. A shell has no transcript to discover, so
        # skipping loses nothing.
        return None

    if await session_map_sync.session_map_entry_may_exist(window_id):
        # Re-checked after the probe. The guard above ran before an await, and
        # SessionStart writes the entry from a separate process, so it can land
        # while the probe is in flight — and the write below would delete the
        # entry it had just made. Nothing suspends between here and the write.
        return None

    session_manager.set_window_provider(window_id, detected, cwd=w.cwd or None)
    logger.info(
        "Bootstrapped window state for untracked live window",
        window_id=window_id,
        provider=detected,
        cwd=w.cwd or "",
    )
    return identity_state.get_identity(window_id)


async def discover_and_register_transcript(
    window_id: str,
    *,
    _window: "TmuxWindow | None" = None,
    client: TelegramClient | None = None,
    user_id: int = 0,
    thread_id: int = 0,
) -> bool:
    """Discover transcript state and report when an agent-origin process exited.

    Shell-origin windows may transition shell ↔ agent. Agent-origin windows
    retain their provider when the process returns to a shell so callers can
    route the topic into recovery instead of shell command handling.

    A backend that publishes the agent's own session id gets that session
    registered here first, which heals a hookful window whose hook never wrote
    one — see ``seed_session_from_native_id``.
    """
    # Lazy: thread_router proxy resolved when transcript discovery is invoked
    from ...thread_router import thread_router

    w = _window or await tmux_manager.find_window_by_id(window_id)

    identity = identity_state.get_identity(window_id)
    if identity is None:
        identity = await _bootstrap_identity(window_id, w)
    if identity is None:
        return False

    chat_id = thread_router.resolve_chat_id(user_id, thread_id) if user_id else 0

    pgid_before = get_cached_foreground_pgid(window_id)
    original_identity = identity
    process_restarted = False
    if w:
        agent_exited = await _detect_and_apply_provider(
            window_id, identity, w, client=client, chat_id=chat_id, thread_id=thread_id
        )
        if agent_exited:
            return True
        refreshed = identity_state.get_identity(window_id)
        if refreshed is None:
            return False
        identity = refreshed
        pgid_after = get_cached_foreground_pgid(window_id)
        process_restarted = _foreground_process_restarted(
            before_pgid=pgid_before,
            after_pgid=pgid_after,
            old_identity=original_identity,
            new_identity=identity,
        )

    identity = await _seed_and_refresh_identity(window_id, w, identity)

    if _hook_already_resolved(window_id, identity) and not process_restarted:
        return False

    if not identity.cwd:
        if not w or not w.cwd:
            return False
        session_manager.set_window_provider(
            window_id, identity.provider_name or "", cwd=w.cwd
        )
        refreshed = identity_state.get_identity(window_id)
        if refreshed is None:
            return False
        identity = refreshed

    providers_to_try = _resolve_providers_to_try(window_id, identity, w)
    return await _complete_transcript_discovery(
        window_id,
        identity,
        w,
        providers_to_try,
        client=client,
        chat_id=chat_id,
        thread_id=thread_id,
    )
