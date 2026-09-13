# herdr socket API — condensed from https://herdr.dev/docs/socket-api/ (fetched 2026-09-13; documents a NEWER herdr than the installed 0.7.1 — cross-check with herdr-api-notes.md "Methods available on 0.7.1")

Transport: newline-delimited JSON over unix socket. Request `{"id":"req_1","method":"ping","params":{}}`; success `{"id":"req_1","result":{"type":"pong"}}`; error `{"id":"req_1","error":{"code":"not_found","message":"pane not found"}}`.
Socket resolution: `--session <name>` > `HERDR_SOCKET_PATH` > `HERDR_SESSION=<name>` > default `~/.config/herdr/herdr.sock`.

Method areas: Server (ping, server.stop, server.reload_config, server.agent_manifests, server.reload_agent_manifests); Workspace (create/list/get/focus/rename/move/move_block/report_metadata/close); Tab (create/list/get/focus/rename/move/close); Pane (split, swap, move, zoom, layout, process_info, neighbor, edges, focus_direction, resize, list, current, get, rename, send_text, send_keys, send_input, read, graphics.*, report_agent, report_agent_session, report_metadata, clear_agent_authority, release_agent, close, wait_for_output); Agent (list, get, read, explain, send_keys, prompt, wait, rename, focus, start, view.set, view.clear); Events (events.subscribe, events.wait); Notification (notification.show); Client (client.window_title.set/clear); Worktree (list/create/open/remove); Layout (export/apply/set_split_ratio); Plugin (link/list/unlink/enable/disable/action.list/action.invoke/log.list/pane.open/pane.focus/pane.close); Integration (install/uninstall); Session (session.snapshot — NOT on 0.7.1); Popup (popup.close).

events.subscribe: `{"id":"sub_1","method":"events.subscribe","params":{"subscriptions":[{"type":"pane.agent_status_changed","pane_id":"w1:p1","agent_status":"blocked"}]}}` — first response acknowledges, later lines push events. Newer versions do NOT replay retained events (0.7.1 does).
Event types (newer docs): workspace.created/updated/metadata_updated/renamed/moved/reordered/closed/focused; tab.created/closed/focused/renamed/moved; pane.created/updated/closed/focused/moved/exited/agent_detected/output_matched/agent_status_changed/scroll_changed; layout.updated; worktree.created/opened/removed.

Agent status values: idle, working, blocked, done (idle and not yet seen), unknown.
pane.report_agent_session {pane_id, source, agent, agent_session_id} → stored `agent_session:{source,agent,kind:"id",value}` readable in pane.get/list, agent.get/list.
pane.report_agent {pane_id, source, agent, state, message?} — semantic state with lifecycle authority (affects waits/notifications).
pane.report_metadata {pane_id, source, agent, title?, display_agent?, state_labels?{idle,working,blocked,done,unknown}, tokens?{name:value|null}, ttl_ms?, seq?, applies_to_source?} — display only; text fields capped at 80 chars; tokens ≤32 keys.
workspace.report_metadata {workspace_id, source, tokens, ttl_ms} (newer).
PaneInfo fields: pane_id (`w<ws>:p<n>`), terminal_id, workspace_id, tab_id, focused, agent_status, revision, scroll?, terminal_title?, terminal_title_stripped?, foreground_cwd, agent_session?.
agent.wait / `herdr agent wait <target> --until done|blocked` is server-owned and event-driven; pins the resolved pane occupant.
agent.prompt {pane_id, prompt, wait:{until, timeout_ms}} (newer): atomically submits prompt + Enter and waits; returns `agent_blocked` without sending if the agent is already blocked. On 0.7.1 use agent.send/pane.send_input + pane.send_keys enter.
pane.read sources: visible, recent, recent-unwrapped, detection. `--format ansi` for a colored snapshot.
pane.send_keys / pane.send_input.keys accept key-combo strings: printable chars, `enter`, `esc`, `ctrl+h`, `alt+x`, `shift+tab`, `f1`, `minus`, `plus`.
pane.process_info → {pid, foreground_pgid, foreground_processes:[{pid,name,argv,cmdline,cwd}]}.
Env injection: pane.split / pane.run / layout.apply accept `env`; herdr-managed vars (HERDR_SOCKET_PATH, HERDR_ENV=1, HERDR_WORKSPACE_ID, HERDR_TAB_ID, HERDR_PANE_ID) win on conflict.
notification.show {title (≤80), body? (≤240), position?, sound?: none|done|request} → {shown, reason: shown|disabled|rate_limited|no_foreground_client|busy}.
session.snapshot (newer): one-shot bootstrap; recommended pattern: open events.subscribe first, buffer, snapshot, apply buffered events. On 0.7.1 emulate with pane.list + tab.list + workspace.list.
layout.export/apply: portable BSP tree with pane label/cwd/command/env — could be used to spawn a Claude pane with a specific argv and env in a new tab.
Plugins: `herdr-plugin.toml` with [[actions]], [[events]] (on = "worktree.created" etc. → command), [[panes]] (overlay/popup/split/tab/zoomed UI), [[link_handlers]]; plugin commands get HERDR_PLUGIN_* env and HERDR_PLUGIN_EVENT_JSON. A herdrgram plugin could expose actions like "Link this pane to Telegram" in the herdr UI.
Protocol stability: newer clients/servers negotiate; missing methods return normal errors — ignore unknown fields, handle unsupported methods gracefully. `herdr api schema --json` (newer) prints the full JSON schema.
IDs: pane `w<n>:p<n>`, tab `w<n>:t<n>`, workspace `w<n>`, terminal `term_<hex>` (unique per PTY, durable while the PTY lives).
