"""/kill's Reopen offer: post it, resume the session, open a topic for it."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.callback_data import CB_REOPEN
from ccgram.handlers.topics import topic_reopen
from ccgram.handlers.topics.topic_reopen import (
    BUTTON_REOPEN,
    handle_reopen_callback,
    offer_reopen,
    reopen,
)

_MODULE = "ccgram.handlers.topics.topic_reopen."
CHAT = 1244608389
USER = 4242
SESSION = "7c90b62c-da97-4da4-ad96-48862f153fb6"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(topic_reopen.config, "config_dir", tmp_path)
    topic_reopen.reset_for_testing()
    yield
    topic_reopen.reset_for_testing()


@contextmanager
def _env(*, created=(True, "ok", "proj", "@new"), names_on_first_prompt=False):
    """Patch everything the offer and the reopen reach for."""
    sent = MagicMock(message_id=77)
    provider = MagicMock()
    provider.make_launch_args.return_value = f"--resume {SESSION}"
    provider.capabilities.supports_hook = True
    provider.capabilities.names_session_on_first_prompt = names_on_first_prompt
    with (
        patch(
            _MODULE + "rate_limit_send_message", AsyncMock(return_value=sent)
        ) as send,
        patch(_MODULE + "edit_with_fallback", AsyncMock()) as edit,
        patch(_MODULE + "tmux_manager") as mux,
        patch(_MODULE + "session_map_sync") as sms,
        patch(_MODULE + "session_manager") as sm,
        patch(_MODULE + "thread_router") as tr,
        patch(_MODULE + "topic_orchestration") as orch,
        patch(_MODULE + "window_query") as wq,
        patch("ccgram.providers.registry") as registry,
        patch("ccgram.providers.resolve_launch_command", return_value="claude"),
    ):
        mux.list_windows = AsyncMock(return_value=[])
        mux.create_window = AsyncMock(return_value=created)
        sms.wait_for_session_map_entry = AsyncMock(return_value=True)
        orch.handle_new_window = AsyncMock(return_value=True)
        wq.resolve_window_alias.side_effect = lambda wid: wid
        wq.get_session_id_for_window.return_value = None
        tr.has_window.return_value = False
        registry.get.return_value = provider
        yield SimpleNamespace(
            send=send,
            edit=edit,
            mux=mux,
            sms=sms,
            sm=sm,
            tr=tr,
            orch=orch,
            wq=wq,
            provider=provider,
        )


async def _offer(env, **overrides) -> str:
    fields = {
        "name": "spreads",
        "session_id": SESSION,
        "cwd": "/home/user/proj",
        "provider": "claude",
        "approval_mode": "yolo",
    }
    fields.update(overrides)
    assert await offer_reopen(MagicMock(), CHAT, **fields)
    keyboard = env.send.await_args.kwargs.get("reply_markup")
    if keyboard is None:
        return ""
    button = keyboard.inline_keyboard[0][0]
    assert button.text == BUTTON_REOPEN
    return button.callback_data[len(CB_REOPEN) :]


class TestOffer:
    async def test_posts_note_with_a_short_token(self, tmp_path: Path) -> None:
        with _env() as env:
            token = await _offer(env)

        text = env.send.await_args.args[2]
        assert "Session ended: spreads" in text
        assert "/home/user/proj" in text
        assert len((CB_REOPEN + token).encode()) <= 64
        saved = json.loads((tmp_path / "reopen_offers.json").read_text())
        assert saved[token]["session_id"] == SESSION

    async def test_unresumable_session_gets_the_note_without_a_button(self) -> None:
        with _env() as env:
            token = await _offer(env, session_id="")

        assert token == ""
        assert "Session ended: spreads" in env.send.await_args.args[2]

    async def test_offer_survives_a_restart(self) -> None:
        with _env() as env:
            token = await _offer(env)
            topic_reopen.reset_for_testing()  # forget memory, keep the file
            assert await reopen(MagicMock(), token, USER)


class TestReopen:
    async def test_resumes_in_place_and_opens_a_topic(self) -> None:
        with _env() as env:
            token = await _offer(env)
            assert await reopen(MagicMock(), token, USER)

        env.mux.create_window.assert_awaited_once_with(
            "/home/user/proj", agent_args=f"--resume {SESSION}", launch_command="claude"
        )
        env.sms.wait_for_session_map_entry.assert_awaited_once()
        env.sm.set_window_approval_mode.assert_called_once_with("@new", "yolo")
        env.orch.clear_pending_creation.assert_any_call("@new")
        kwargs = env.orch.handle_new_window.await_args.kwargs
        assert kwargs == {"target_user_id": USER, "target_chat_id": CHAT}
        assert "Topic reopened: spreads" in env.edit.await_args.args[3]
        # One use: the token is gone once the topic exists.
        assert not await reopen(MagicMock(), token, USER)

    async def test_follows_the_id_the_session_registered_under(self) -> None:
        with _env() as env:
            env.wq.resolve_window_alias.side_effect = lambda wid: "@named"
            token = await _offer(env)
            assert await reopen(MagicMock(), token, USER)

        event = env.orch.handle_new_window.await_args.args[0]
        assert event.window_id == "@named"

    async def test_first_prompt_agent_is_not_waited_on(self) -> None:
        with _env(names_on_first_prompt=True) as env:
            token = await _offer(env, provider="codex")
            assert await reopen(MagicMock(), token, USER)

        env.sms.wait_for_session_map_entry.assert_not_awaited()
        env.orch.handle_new_window.assert_awaited_once()

    async def test_session_already_open_in_a_topic_is_not_started_twice(self) -> None:
        with _env() as env:
            env.mux.list_windows = AsyncMock(
                return_value=[MagicMock(window_id="@live", native_session_id=SESSION)]
            )
            env.tr.has_window.return_value = True
            token = await _offer(env)
            assert not await reopen(MagicMock(), token, USER)

        env.mux.create_window.assert_not_awaited()
        assert "already open" in env.edit.await_args.args[3]

    async def test_running_unbound_session_just_gets_its_topic(self) -> None:
        with _env() as env:
            env.mux.list_windows = AsyncMock(
                return_value=[MagicMock(window_id="@live", native_session_id=SESSION)]
            )
            token = await _offer(env)
            assert await reopen(MagicMock(), token, USER)

        env.mux.create_window.assert_not_awaited()
        assert env.orch.handle_new_window.await_args.args[0].window_id == "@live"

    async def test_failed_resume_keeps_the_button(self) -> None:
        with _env(created=(False, "no such dir", "", "")) as env:
            token = await _offer(env)
            assert not await reopen(MagicMock(), token, USER)

        env.orch.handle_new_window.assert_not_awaited()
        last = env.edit.await_args
        assert "no such dir" in last.args[3]
        assert last.kwargs["reply_markup"].inline_keyboard[0][0].text == BUTTON_REOPEN


class TestCallback:
    def _update(self, token: str) -> MagicMock:
        update = MagicMock()
        update.callback_query.data = f"{CB_REOPEN}{token}"
        update.callback_query.answer = AsyncMock()
        update.effective_user.id = USER
        return update

    async def test_unknown_token_says_so(self) -> None:
        with _env() as env:
            await handle_reopen_callback(self._update("nope"), MagicMock())

        env.mux.create_window.assert_not_awaited()
        assert "can no longer be reopened" in env.edit.await_args.args[3]

    async def test_press_resumes(self) -> None:
        with _env() as env:
            token = await _offer(env)
            await handle_reopen_callback(self._update(token), MagicMock())

        env.orch.handle_new_window.assert_awaited_once()
