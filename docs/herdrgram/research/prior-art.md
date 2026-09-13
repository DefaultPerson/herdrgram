# Prior art (проверено 2026-09-13)

## TL;DR
Готовый мост herdr ⇄ Telegram уже существует: **alexei-led/ccgram** — это ccmux/ccbot, эволюционировавший в мультибэкендный бот (tmux / herdr / agterm), MIT. Писать herdrgram с нуля нужно только если ccgram не закроет must-have сценарии (см. gap-анализ ниже, когда будет готов).

## alexei-led/ccgram — факты, проверенные по исходникам (clone: /tmp/ccgram-src, v4.10.3, коммит 56c2b62 от 2026-09-05)
- Лицензия MIT (LICENSE кредитует six-ddc/ccmux как оригинал). Python **>=3.14** (на машине есть 3.12/3.13, uv может скачать 3.14.5). ~59k строк в src/, 4364 теста. Установка: `uv tool install ccgram`.
- Зависимости: python-telegram-bot 22.6, telegramify-markdown 1.x, libtmux, Pillow, pyte, aiohttp, structlog, click, pathspec; опционально edge-tts.
- Бэкенд herdr: `CCGRAM_MULTIPLEXER=herdr`, сокет из `$HERDR_SOCKET_PATH` или дефолтный. Поддерживает протоколы **14–20** (у нас 14, herdr 0.7.1) — `HERDR_SUPPORTED_PROTOCOLS = frozenset(range(14, 21))` (src/ccgram/multiplexer/herdr.py:95); неизвестный протокол = warning + best-effort.
- Идентичность топика: `agent.list` — единственный источник; персистится opaque `herdr-session-v1-…` (digest композита `source/agent/kind/value` из `agent_session`), а не pane/tab id → переживает рестарт herdr и перенумерацию. Перед каждым действием свежий guard по `agent.list`; отсутствующая/неоднозначная цель = fail closed. Гонка «сессия сменилась после guard» задокументирована как принятая.
- События: `herdr_events.py` держит постоянный сокет-стрим `events.subscribe`: `pane.agent_status_changed` (per-pane, требует pane_id) + `tab.closed` (глобально), с reprime после подписки; identity/действия — только через `agent.list` (polling + reconciliation).
- Автообнаружение: «Open a new herdr tab in the appropriate workspace, then start any supported agent CLI. CCGram discovers agent panes automatically; bare shell panes are not surfaced as topics» (docs/guides.md:502). Топик = одна агент-сессия; label `<Provider> ▸ <workspace> ▸ <tab> ▸ <pane>`. Workspaces/tabs с label вида `__*__` невидимы для ccgram (escape hatch для самохостинга внутри herdr).
- Хуки Claude Code (`ccgram hook --install`): SessionStart, Notification, Stop, StopFailure, SessionEnd, SubagentStart, SubagentStop, TeammateIdle, TaskCompleted (src/ccgram/hook.py:67-84); `ccgram doctor` проверяет, что хук ccgram и хук herdr сосуществуют в settings.json. Хуки опциональны — есть fallback на скрейпинг.
- Возможности (README/guides): forum topics в группе или приватном чате с topics; директория-браузер + выбор workspace herdr при `/new`; Standard/YOLO режим (`--dangerously-skip-permissions`); `CCGRAM_CLAUDE_COMMAND` для кастомной команды запуска; скриншоты + live view (автообновление 5 с); action toolbar; ответы одним тапом на нумерованные/yes-no промпты; `/send` файлов; голос (Whisper: groq/openai/совместимый) + TTS-ответы; `/last`, `/sessions`, `/sync` (чистка retired-топиков), `/unbind`, `/agent`; at-least-once доставка с watermark и «Jump to live»; worktree-топики через `herdr worktree create`; Remote Control badge 📡; mini-app дашборд (опц.).
- Конфиг: `~/.ccgram/.env` — TELEGRAM_BOT_TOKEN, ALLOWED_USERS, CCGRAM_GROUP_ID (для группы), CCGRAM_MULTIPLEXER, CCGRAM_HIDE_TOOL_CALLS, CCGRAM_HIDE_STATUS, CCGRAM_STATUS_POLL_INTERVAL, CCGRAM_PANE_LIFECYCLE_NOTIFY, CCGRAM_WHISPER_PROVIDER/GROQ_API_KEY/OPENAI_API_KEY, CCGRAM_PROVIDER, CCGRAM_ACK_REACTION и др. Запуск как systemd user service описан в docs/guides.md («Running as a Service»).
- CHANGELOG: активная работа над herdr (guarded targets, protocol 17, pane-scoped topics, sessionless agents, legacy identity recovery, split Enter) — herdr-бэкенд не заброшен.

## Другие проекты (по отчёту агента prior-art, выборочно проверить перед использованием)
- permgps/herdr-telegram-agents (Go, MIT, 19★): чистый unix-socket клиент protocol 17; иконка топика = статус агента, кнопки на варианты диалога, editMessageText, закреплённый дашборд, quiet-mode по idle клавиатуры. Нет голоса. Кандидат «fork & adapt» если ccgram не подойдёт.
- dcolinmorgan/herdr-remote (354★, лицензия NOASSERTION): menubar macOS + relay + PWA + Telegram без топиков. Идеи: `/digest`, `/trust`, `/interrupt`.
- mvallebr/herdr-telegram-plugin, tankisstank/herdr-telegram-plugin (TS): polling через CLI, 1 топик = 1 pane, `/follow`, `/last`, `/stop`; fingerprinting коллбэков.
- gaijinjoe/herdres (Python, MIT): PreToolUse-хук для AskUserQuestion/ExitPlanMode, стриминг через Telegram drafts.
- Notify-only: cokekitten/herdr-telegram-bridge, barnuri/herdr-notifications, naturalmoods/herdr-telegram-notify, elkraps/herdr-telegram-notify, hkdom/herdr-telegram-gate, blockshiftnetwork/herdr-telegram-attention, fulanto/herdr-oncall, sbulav/herdr-relay.
- Официально у herdr: своей Telegram-интеграции нет; reference-плагин ogulcancelik/herdr-plugin-examples/agent-telegram-notify на событии `pane.agent_status_changed`.

## Официальные альтернативы от Anthropic
- **Claude Code Remote Control** (`claude --remote-control` / `/rc`, `remoteControlAtStartup`): та же локальная сессия в claude.ai/code и мобильном приложении, синхронно с терминалом, permission prompts и AskUserQuestion пробрасываются, файлы/фото, push. Только claude.ai-клиенты (не Telegram), нужна подписка (не API key), 1 remote-сессия на процесс. Для сценария «ушёл от компа» это самый короткий путь без бота вообще — но без Telegram и без обзора всех herdr-сессий в одном месте.
- **Claude Code Channels** (research preview): `claude --channels plugin:telegram@claude-plugins-official` — MCP-плагин пушит сообщения из Telegram в запущенную сессию, Claude отвечает tool'ом `reply`. Ограничения: **один бот = одна сессия** (несколько сессий → разные `TELEGRAM_STATE_DIR` и отдельные боты), нет forum topics, нет permission relay/AskUserQuestion из Telegram, нет голоса, нужен Bun и флаг при запуске каждой сессии. Для 5 параллельных herdr-табов неудобно.

## Поправки к отчёту агента (проверено на установленном herdr 0.7.1)
- `herdr terminal session observe/control` (ndjson-стрим `terminal.frame`) на 0.7.1 **нет** — есть только `herdr terminal attach <terminal_id> [--takeover]`. Это функциональность более новых версий (0.9.x); закладываться на неё можно только после обновления herdr.
- `herdr api schema --json` на 0.7.1 нет (команда `api` отсутствует).
- Полный отчёт агента: docs/research/prior-art-agent-report.md (465 строк, 60 ссылок).
