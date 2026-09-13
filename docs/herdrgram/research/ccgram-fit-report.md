# ccgram 4.10.3 на herdr 0.7.1 — fit/gap анализ

Дата анализа: 2026-09-13
Исходники: `/tmp/ccgram-src` (ccgram v4.10.3, MIT, Python >=3.14, 205 модулей, 4364 теста)
Цель: можно ли взять ccgram как есть вместо разработки собственного «herdrgram»

**TL;DR: adopt + маленький форк.** herdr-адаптер написан и верифицирован именно против
herdr 0.7.1 / protocol 14. Авто-создание топиков для уже запущенных вручную сессий работает
без единого тапа и без установки хука. Привязка топика переживает рестарт herdr. Не хватает
двух вещей из вашего списка: убийства панели при закрытии топика и `/show`. Обе патчатся
примерно за 150 строк.

---

## 1. Авто-топик для сессии, запущенной вручную в herdr — ✅

Работает. Ноль тапов, хук не нужен, Telegram-действий не требуется.

Ключевой путь — `session_monitor.py:725`, метод `_emit_unbound_window_events`. Его докстринг
говорит прямо:

> Surfaces windows the hook never registered (no session_map entry) so they can become topics.

На каждом тике опроса (`MONITOR_POLL_INTERVAL`, дефолт 2.0 с) берётся `list_windows()`,
отбрасываются уже связанные с топиками окна, остальные уходят в `handle_new_window` →
`create_topic_in_chat` (`topic_orchestration.py:334`, лог `"Auto-created topic ..."`).

Есть и второй, самоисцеляющий путь: `_emit_known_unbound_window_events`
(`session_monitor.py:772`) на каждом опросе повторяет попытку для окон, которые уже есть
в `session_map`, но ещё не привязаны к топику. `handle_new_window` идемпотентен, спама нет.

Критерий отбора — `WindowRef.topic_eligible`, который herdr-бэкенд проставляет в
`herdr.py:_live_ref` (`herdr.py:678-693`):

```python
topic_eligible=adoptable
and is_herdr_session_target(record.target_id)
and bool(record.composite.agent.strip()),
```

То есть запись должна нести guarded target и непустой ярлык агента. Голый shell топиком
не становится. Решение принимает именно бэкенд, без capability-флагов — `topic_mapping.py:176-194`
специально объясняет, что привязка этого к флагу дважды приводила к багам.

Подтверждение в документации, `docs/guides.md:502`:

> Open a new herdr tab in the appropriate workspace, then start any supported agent CLI.
> CCGram discovers agent panes automatically; bare shell panes are not surfaced as topics
> (only active agent panes are).

**Ваши пять панелей проходят.** Живой `herdr agent list` отдаёт для каждой полный
`agent_session` вида:

```json
{"agent":"claude","kind":"id","source":"herdr:claude","value":"2043dc70-b530-4828-ab86-6e33b1290daa"}
```

для панелей `wC:p1`, `wC:p2`, `wC:p3`, `wC:p5`, `wC:p7`.

**Workspace и tab безразличны.** Отбрасываются только те, чей label матчит `^__.*__$` —
`herdr.py:100-103`, константа `_INTERNAL_LABEL_RE`. В `guides.md:415` это названо
self-hosting escape hatch. Ваш workspace `wC` с label `hm-dao-bots` адоптируется целиком.
Никакого требования «запустить через ccgram» нет: `list_windows` строится исключительно
из `agent list`, а не из внутреннего реестра созданных ccgram окон.

**Что нужно сделать один раз:**

1. Создать группу Telegram с включёнными Topics (или включить топики в приватном чате).
2. Добавить бота админом с правом Manage Topics.
3. Прописать `CCGRAM_GROUP_ID`.

Третий пункт обязателен именно на холодном старте. `collect_target_chats`
(`topic_orchestration.py:252-278`) сначала ищет уже известные чаты, и только если их нет:

```python
if not seen_chats:
    if config.group_id:
        seen_chats.add(config.group_id)
        logger.info("Cold-start: using CCGRAM_GROUP_ID=%d for auto-topic (window %s)", ...)
    else:
        logger.debug("No group chats found for auto-topic creation (window %s)", window_id)
```

Без `CCGRAM_GROUP_ID` на первом запуске топики просто не появятся.

`/sessions`, `/sync` и `reconciliation.py` для этого сценария не нужны. `/sync` — про уборку
осиротевших топиков, `reconciliation.py` — про tri-state «жив / мёртв / не смог спросить»
перед деструктивными действиями (`reconciliation.py:98-131`).

---

## 2. Что пишет `ccgram hook --install` — ✅ сосуществует, ⚠️ не обязателен

**Девять событий** (`hook.py:66-76`, кортеж `_HOOK_EVENT_TYPES`):
SessionStart, Notification, Stop, StopFailure, SessionEnd, SubagentStart, SubagentStop,
TeammateIdle, TaskCompleted.

**Одна и та же команда для всех** (`hook.py:123-127`):

```
<sys.executable> -m ccgram.main hook
```

с `"type": "command"`, `"timeout": 5`, и дополнительно `"async": true` для шести событий
из `_ASYNC_EVENTS` (`hook.py:79-89`): StopFailure, SessionEnd, SubagentStart, SubagentStop,
TeammateIdle, TaskCompleted. Путь к settings резолвится с учётом `CLAUDE_CONFIG_DIR`
(`hook.py:42-47`), по умолчанию `~/.claude/settings.json`.

**Сосуществование с herdr-хуком — ✅.** `hook.py:556-565` не перезаписывает массив,
а дописывается в существующую группу:

```python
event_hooks = settings["hooks"][event_type]
if event_hooks:
    first_entry = event_hooks[0]
    if isinstance(first_entry, dict):
        first_entry.setdefault("hooks", []).append(hook_config)
```

Ваш текущий `~/.claude/settings.json` содержит ровно один SessionStart-вход:

```json
{"matcher": "*", "hooks": [{"type": "command",
 "command": "bash '~/.claude/hooks/herdr-agent-state.sh' session", "timeout": 10}]}
```

ccgram встанет рядом внутри той же группы с matcher `*`, herdr-хук не тронут. Matcher `*`
означает, что унаследованный фильтр никого не режет.

`ccgram doctor` это специально проверяет и требует обоих —
`doctor_cmd.py:107-132`, функция `_check_herdr_hook_coexistence`, вердикт
`"ccgram + herdr Claude hooks coexist"`. Если herdr-хука нет, доктор советует
`herdr integration install claude`.

Подтверждено в `guides.md:381`:

> The same Claude Code hook works on both backends — it resolves which window fired from
> `$HERDR_PANE_ID` (tmux uses `$TMUX_PANE`), so no herdr-specific hook step is required.

Резолвер идентичности — `self_identify.py:286-301`. Ветка herdr срабатывает по
`HERDR_PANE_ID`, затем обязательно читает `HERDR_WORKSPACE_ID`; при отсутствии любого из них
возвращается `None` и хук молча пропускает запись. На вашей 0.7.1 обе переменные
экспортируются в каждой панели, так что путь рабочий.

**Обязателен ли — ⚠️ нет.** Дискавери и топики работают без хука через
`_emit_unbound_window_events`. Без хука теряется:

- точная привязка Claude `session_id` к окну в `session_map.json`;
- события `Notification` для детекта блокирующих промптов;
- сигналы `Stop` / `SessionEnd` о завершении работы.

Доставка и статусы деградируют до скрин-скрейпинга плюс нативный herdr `agent_status`,
но не ломаются.

---

## 3. Двусторонняя видимость — ✅ с одной оговоркой

**Терминал → Telegram — ✅.** Сообщения, которые вы печатаете в панели за столом, уходят
в топик с префиксом 👤 и обрезкой на 3000 символов (`response_builder.py:34-38`):

```python
if role == "user":
    prefix = "\U0001f464 "
    if len(text) > _MAX_USER_MSG_LENGTH:
        text = text[:_MAX_USER_MSG_LENGTH] + "…"
    return [f"{prefix}{text}"]
```

⚠️ Это всегда включено. Переменной, чтобы выключить, нет — аналога `CCBOT_SHOW_USER_MESSAGES`
наоборот не предусмотрено.

**Telegram → терминал — ❌ без маркера происхождения.** Текст доставляется как
`pane send-text`, затем отдельным вызовом `Enter` через 0.5 с (`herdr.py:1044-1053`,
константа `_SEND_ENTER_DELAY_SECONDS` на `herdr.py:137-143`). Разделение сделано намеренно:
агентские TUI читают submit-клавишу, пришедшую в том же входном батче, как литеральный
перевод строки, и промпт набирается, но не отправляется.

В терминале такое сообщение выглядит как обычный ввод, никакой пометки «из Telegram» нет.
Модуль `handlers/telegram_origin.py` держит очередь `_PendingInjection` с TTL 30 секунд
(`telegram_origin.py:11, 37-51`) исключительно для того, чтобы не отправить это же сообщение
обратно в Telegram эхом.

**Что доставляется в Telegram:**

- ответы ассистента — ✅;
- thinking — ✅, с обрезкой до 500 символов (`response_builder.py:42-52`), управляется
  `CCGRAM_HIDE_THINKING` (`config.py:161`, дефолт `false` = показывать), фильтруется
  в `message_queue.py:543`;
- tool calls — ✅, `CCGRAM_HIDE_TOOL_CALLS` (`config.py:158`, дефолт `false` = показывать),
  плюс per-window переключатель `/toolcalls` и режим `CCGRAM_EPHEMERAL_TOOLS` (`config.py:184`),
  при котором плашки инструментов удаляются, как только готов ответ;
- статус-пузыри с таймером и тулбаром — `CCGRAM_HIDE_STATUS` (`config.py:175`);
- локальные слэш-команды и их вывод — распознаются отдельно (`transcript_parser.py:325-340`,
  типы `local_command` и `local_command_invoke`).

Ваш `CCBOT_SHOW_TOOL_CALLS` мапится на `CCGRAM_HIDE_TOOL_CALLS` с инверсией смысла.

---

## 4. Жизненный цикл — половина ✅, половина ❌

### Агент умер → топик — ⚠️ закрывается, не удаляется

Событие `pane.exited` / `pane.closed` транслируется в `MuxEvent(kind="window_died")`
(`herdr_events.py:45-47` и `:149-152`), приходит уведомление в топик. Затем через
`AUTOCLOSE_DEAD_MINUTES` (дефолт 10, `config.py:242-244`) вызывается `close_forum_topic`
(`topic_lifecycle.py:134`), состояние чистится и тред отвязывается с
`retirement_reason="remote_closed"`.

Это сознательное решение апстрима: CHANGELOG строка 155 —
*"Split herdr Enter, hide web_app in groups, close instead of delete on autoclose (#189)"*.

### Топик закрыт в Telegram → панель herdr — ❌ не убивается

Докстринг `topic_closed_handler` (`topic_lifecycle.py:426-433`) недвусмыслен:

> Handle topic closure — unbind thread but keep the tmux window alive.
> The window becomes "unbound" and is available for rebinding via the window picker
> when a new topic is created.

Убийство существует только для окон, созданных самим ccgram. И TTL-путь
(`topic_lifecycle.py:200`, `_kill_expired_unbound`), и путь удалённого топика
(`topic_lifecycle.py:338-341`, `_unbind_deleted_topic`) закрыты условием
`view.origin == CCGRAM_CREATED_WINDOW_ORIGIN`.

Правило зафиксировано прямым текстом в `window_state_store.py:123`:

> origin: Lifecycle origin. Manually-discovered windows are never auto-killed by ccgram.

Ваши панели, запущенные за столом командой `claude --dangerously-skip-permissions`,
попадают под это правило всегда. **Это главный разрыв с симметричным lifecycle ccbot.**

### Рестарт herdr → привязка — ✅ выживает

Конструкция идентификатора, `herdr.py:284-289`:

```python
def herdr_session_target_id(composite: HerdrSessionComposite) -> str:
    """Return the opaque versioned ID for a complete session composite."""
    prefix = b"ccgram-herdr-session-v1\0"
    digest = hashlib.sha256(prefix + canonical_session_bytes(composite)).hexdigest()
    return f"{HERDR_SESSION_TARGET_PREFIX}{digest}"
```

А `canonical_session_bytes` (`herdr.py:269-283`) сериализует ровно четыре поля и ничего больше:

```python
values = {
    "source": composite.source,
    "agent": composite.agent,
    "kind": composite.kind,
    "value": composite.value,
}
# Field order is part of the persisted target-ID protocol. A golden test
# pins it so refactors cannot silently orphan existing topic bindings.
payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
```

Ни `pane_id`, ни `tab_id`, ни `terminal_id`, ни `workspace_id` в дайджест не входят,
хотя все они присутствуют в `HerdrLiveRecord` (`herdr.py:231-241`).

Поскольку herdr при рестарте перезапускает `claude --resume <session_id>`, а `value`
в композите — это и есть Claude session id, композит после рестарта идентичен, дайджест
идентичен, **топик остаётся привязан**. Ваша поправка верна.

Флаг `ids_stable_across_restart=False` (`herdr.py:105`) относится к сырым herdr-локаторам,
а не к этому дайджесту. Докстринг модуля на `herdr.py:24-31` описывает ситуацию
пессимистичнее, чем ведёт себя код, и это стоит держать в голове как расхождение
документации с реализацией.

⚠️ **Одна оговорка.** Для агентов без опубликованного `agent_session` есть fallback-композит
(`herdr.py:305-312`):

```python
composite = HerdrSessionComposite("herdr", agent, "terminal", terminal_id)
```

Вот он рестарт не переживёт, потому что `terminal_id` перевыпускается. К вам не относится:
все пять панелей публикуют полный `agent_session`.

### `/clear` в Claude → ⚠️ ломает привязку

Меняется Claude session id, значит `value`, значит композит, значит дайджест. Старый target
перестаёт резолвиться — `guard_session_target` (`herdr.py:657-673`) кинет
`HerdrUnresolvedTargetError`. Топик уедет в состояние dead и по таймеру закроется,
а новая сессия получит новый топик.

Правильная починка лезет в модель идентичности. Обходной путь на каждый день: вместо
`/clear` открывать новую вкладку herdr.

### Рестарт бота → ✅ без дублей

Привязки персистятся в `thread_router`, на старте `bootstrap.py:355` вызывает
`adopt_unbound_windows`. Дедупликация доставки идёт через `parsed_offset` сессии
и delivery receipts (`delivery_contract.py:64-131`).

⚠️ Свежий открытый баг #245 от 2026-09-12: *"Restart replay starves live topics under group
rate penalty; auto-trigger for the existing skip barrier"* — при рестарте с большим бэклогом
живые топики могут голодать под рейт-лимитом группы.

---

## 5. Блокирующие промпты — ✅ все три механизма сразу

**Скрин-скрейпинг через pyte — основной.** `terminal_parser.py` держит список `UI_PATTERNS`
из пар regex-маркеров top/bottom. Шапка модуля (`terminal_parser.py:3-5`) перечисляет:

> Interactive UIs (AskUserQuestion, ExitPlanMode, Permission Prompt, RestoreCheckpoint)
> via regex-based UIPattern matching with top/bottom delimiters.

Конкретные записи: `ExitPlanMode` — `terminal_parser.py:62`, `AskUserQuestion` — `:74` и `:80`
(два варианта разметки), плюс определение по структуре на `:251`. Каждый паттерн содержит
кортеж регулярок, чтобы переживать переформулировки между версиями Claude Code
(`terminal_parser.py:44-47`).

**JSONL-триггер по именам инструментов** — `interactive_ui.py:56-63`:

```python
INTERACTIVE_TOOL_NAMES = frozenset({
    "AskUserQuestion",
    "ExitPlanMode",
    "request_user_input",   # Codex native tool name before normalization
})
```

**Нативный herdr-статус** — `handlers/polling/window_tick/observe.py:108-131`,
функция `_native_agent_status` превращает herdr `agent_status` в `StatusUpdate`,
включая ветку `"waiting for input"` для состояния blocked.

**Инлайн-кнопки — ✅.** `handlers/interactive/interactive_callbacks.py` обрабатывает
коллбэки `CB_ASK_UP`, `CB_ASK_DOWN`, `CB_ASK_LEFT`, `CB_ASK_RIGHT`, `CB_ASK_SPACE`,
`CB_ASK_TAB`, `CB_ASK_REFRESH` — то есть навигация по нативному TUI-виджету стрелками
прямо из Telegram. Есть защита от повторного и запоздалого тапа через
`_interactive_sequences` (`interactive_ui.py:78-81`) и привязка клавиатуры к конкретному
сообщению через `_interactive_contexts` (`:74-76`), чтобы скопированный callback не сработал.

⚠️ **MCP elicitation отдельно не распознаётся.** Попадёт под общий Permission Prompt
только если визуально совпадёт с существующим паттерном.

---

## 6. Desktop-side `/focus` или `/show` — ❌ нет

Ни команды, ни вызова. Единственные упоминания focus в herdr-адаптере — это флаги
`--no-focus` при создании workspace и tab (`herdr.py:1333`, `:1352`, `:1429`), то есть
ccgram намеренно *не* перетягивает фокус на себя.

`notification.show` и `pane report-metadata` не используются вообще — панели никак
не помечаются как связанные с Telegram.

При этом herdr 0.7.1 отдаёт всё необходимое (проверено по `herdr agent --help`,
`herdr tab --help`, `herdr workspace --help`):

```
herdr agent focus <target>
herdr tab focus <tab_id>
herdr workspace focus <workspace_id>
herdr notification <subcommand>
herdr pane report-metadata <pane_id> --source ID [--title TEXT] [--state-label STATUS=TEXT]
```

Патч сводится к одному `_call_ok(["agent", "focus", record.terminal_id])` после
`guard_session_target`, плюс регистрация команды в `handlers/registry.py:94-115`
и кнопка в `BUILTIN_ACTIONS` (`toolbar_config.py:148-186`).

---

## 7. Протокол 14 — ✅ полностью поддержан

`herdr.py:90-95`:

```python
# Supported herdr socket protocols (``herdr status`` → ``server.protocol``).
# 14–20 are supported. Other versions are attempted with a warning so ccgram
# remains usable across Herdr upgrades and downgrades.
HERDR_SUPPORTED_PROTOCOLS = frozenset(range(14, 21))
HERDR_PROTOCOL_VERSION = max(HERDR_SUPPORTED_PROTOCOLS)
```

14 — нижняя граница поддержки, входит в диапазон. **Гейтов вида `if protocol >= N` в коде
нет.** Единственная проверка — `herdr.py:551-562`: при незнакомом протоколе пишется warning
`"herdr protocol is unverified; continuing"` и работа продолжается. Подтверждено
в `guides.md:392`:

> ccgram accepts herdr socket protocols 14–20 without warnings. On the first call it reads
> `herdr status`; an older, newer, missing, or otherwise unknown protocol emits a warning
> and ccgram continues in best-effort mode.

Живая проверка вашего сервера: `protocol: 14`, `compatible: true`,
`capabilities: {"live_handoff": true}`, сокет `~/.config/herdr/herdr.sock`.

### Какие методы вызываются

Адаптер ходит через CLI, а не через сырой socket-RPC, за единственным исключением
(стрим событий). Полный список вызовов:

- `status --json` — `herdr.py:537`
- `agent list` — `herdr.py:600`
- `workspace list` — `herdr.py:701`, `:1203`
- `tab list` — `herdr.py:702`
- `pane list` — `herdr.py:1223`
- `pane get <pane_id>` — `herdr.py:517`
- `pane read --source visible --format text|ansi` — `herdr.py:857`
- `pane read --source recent --lines N --format text` — `herdr.py:863`
- `pane layout --pane` — `herdr.py:879`
- `pane process-info --pane` — `herdr.py:904`
- `pane send-text` / `pane send-keys` — `herdr.py:1044-1053`
- `pane close` — `herdr.py:1063`
- `pane run` — `herdr.py:1374`, `:1467`
- `tab rename` — `herdr.py:1091`
- `tab close` — `herdr.py:1392`, `:1453-1483`
- `workspace create --cwd --no-focus` — `herdr.py:1332`
- `workspace close` — `herdr.py:1394`

Сверил с `herdr pane --help`, `herdr tab --help`, `herdr workspace --help` на вашей 0.7.1 —
присутствуют **все до единой**, включая `pane run <pane_id> <command>` и
`pane read [--source visible|recent|recent-unwrapped] [--lines N] [--format text|ansi]`.

**Ничего из отсутствующего на 0.7.1 не используется:** ни `session.snapshot`,
ни `agent.prompt`, ни `pane.updated`. `pane run` дёргается как CLI-подкоманда,
которая в 0.7.1 существует.

### Стрим событий

Единственное долгоживущее socket-соединение — `herdr_events.py`. Шапка модуля, строка 8:

> Wire protocol (verified live against herdr 0.7.1): newline-delimited JSON over the unix
> socket. `events.subscribe` returns one ack line then keeps the connection open.

Там же на строках 13-14 учтено именно ваше замечание про подписки:

> Pane agent-status and pane-exit subscriptions require a `pane_id`; `tab.closed` is global.

Подписки (`herdr_events.py:42-48`): `pane.agent_status_changed`, `pane.exited` / `pane.closed`,
`tab.closed`. Каждое имя матчится и в dot-, и в underscore-форме, потому что herdr
непоследователен в именовании.

Реплей retained-истории при subscribe обработан через одноразовый sentinel `SUBSCRIBED`
(`herdr_events.py:16-19`, `:78-93`), который отдаётся **только** после валидного ack
и закрывает гонку reprime-vs-subscribe:

> After the ack, `open_socket_stream` yields a one-shot `SUBSCRIBED` sentinel so the caller
> can reprime *after* the subscription is live (events that arrive during the reprime are
> buffered by the socket and read next), closing the reprime-vs-subscribe race.

Переподписка на новые pane_id делается каждые 5 секунд (`_STREAM_REPRIME_INTERVAL`,
`herdr.py:150`), потому что уведомления о смене локатора в протоколе нет. Реконнект —
экспоненциальный бэкофф от 1 до 30 секунд (`herdr.py:146-148`).

---

## 8. Приватный чат с топиками и групповой форум — ✅ оба

`create_topic_in_chat` при `chat_id > 0` сначала вызывает `get_me()` и требует
`has_topics_enabled is True`, иначе тихо пропускает создание (`topic_orchestration.py:343-357`).

Приватные чаты с топиками запоминаются в `thread_router.private_topic_chats`
(`thread_router.py:98`, `:660-673`) и попадают в `collect_target_chats` наравне с группами
(`topic_orchestration.py:253`).

Бэкенд на это не влияет — вся логика выше уровня мультиплексора, herdr тут ничем
не отличается от tmux. Issue #210 *"Support private topics and harden topic routing"*
закрыт 2026-08-31. Настройка описана в `guides.md:43`.

---

## 9. Фичи по вашему списку

**Голос на вход — ✅.** `CCGRAM_WHISPER_PROVIDER=openai|groq`, либо любой
OpenAI-совместимый эндпоинт через `CCGRAM_WHISPER_BASE_URL`. Ключи `OPENAI_API_KEY`,
`GROQ_API_KEY` или `CCGRAM_WHISPER_API_KEY`. Дополнительно `CCGRAM_WHISPER_MODEL`,
`CCGRAM_WHISPER_LANGUAGE`, и `CCGRAM_VOICE_AUTOSEND=true`, чтобы не подтверждать
расшифровку перед отправкой (`config.py:131-135`, `:171`).

**Голос на выход (TTS) — ✅ сверх вашего списка.** `CCGRAM_TTS_PROVIDER=edge|openai`,
голос через `CCGRAM_TTS_VOICE`, ставится как `ccgram[tts]` (`config.py:139-144`,
`src/ccgram/tts/`).

**Фото и документы Telegram → агент — ✅.** Сохраняются в `.ccgram-uploads/` внутри cwd
сессии, агенту уходит фраза с относительным путём под его Read-инструмент
(`handlers/file_handler.py:3-6`). Обрабатываются и `filters.PHOTO`, и `filters.Document.ALL`.

**Файлы агент → Telegram — ✅.** Команда `/send` с glob, путём или подстрочным поиском
(`README.md:71`, `CCGRAM_SEND_SEARCH_DEPTH`, `CCGRAM_SEND_MAX_RESULTS`).

**`/screenshot` — ✅.** Рендер текста панели в PNG через Pillow с полным разбором ANSI
16/256/RGB и тройным фолбэком шрифтов JetBrains Mono → Noto Sans Mono CJK SC → Symbola
(`screenshot.py:1-12`). На herdr берёт `pane read --format ansi` (`herdr.py:853-858`).
Зарегистрирована как `CommandSpec("screenshot", ...)` (`registry.py:103`) и как кнопка
`screen` в тулбаре.

**`/history` — ✅.** `registry.py:95`, реализация `handlers/recovery/history.py:220`.

**`/esc` — ⚠️ не команда, а кнопка.** В тулбаре есть действие `esc` → клавиша Escape
(`toolbar_config.py:176`). Рядом: `ctrlc`, `mode` (Shift-Tab циклом по permission modes
с обновлением надписи Def/Edit/Plan/Full), `think` (Alt-T), `yolo` (Ctrl-Y), `enter`,
`tab`, `up`, `down`, `eof` (Ctrl-D), `susp` (Ctrl-Z), `model`, `live`, `getfile`, `last`,
`close`. Тулбар кастомизируется через `~/.ccgram/toolbar.toml` (`CCGRAM_TOOLBAR_CONFIG`).

**`/kill` — ⚠️ не команда, а дашборд.** `/sessions` даёт список сессий с кнопкой Kill
и подтверждением в два тапа (`handlers/sessions_dashboard.py:155-233`). Перед убийством
проверяется tri-state присутствие, и при неудаче ничего не отвязывается
(`sessions_dashboard.py:197-207`).

**`/unbind` — ✅.** `registry.py:99`.

**`/usage` — ❌ отсутствует полностью.** Ни команды, ни подсчёта токенов.

**Blacklist — ❌ отсутствует.** `/unbind` есть, но окно переадоптится следующим тиком
дискавери. Ближайший обходной путь — соглашение об именовании `__*__` для workspace
или tab, делающее их невидимыми для ccgram (`guides.md:415`).

**Сверх вашего списка:** `/live` (живой вид панели с автообновлением,
`CCGRAM_LIVE_VIEW_INTERVAL`, `CCGRAM_LIVE_VIEW_TIMEOUT`), `/split`, `/panes`, `/resume`,
`/rollback`, `/recall`, `/restore`, `/last`, `/agent` (смена провайдера на лету),
`/toolbar`, `/sync`, `/upgrade`, `/commands`, `/verbose`, `/toolcalls`, inline-запросы
и Mini App дашборд (`CCGRAM_MINIAPP_BASE_URL`). Поддерживаемые провайдеры помимо Claude:
Codex, Gemini, Pi, Antigravity, shell.

---

## 10. Эксплуатация

### Установка

```bash
uv tool install --python 3.14 ccgram
```

Требуется Python >=3.14 (`pyproject.toml`, `requires-python = ">=3.14"`). У вас
`cpython-3.14.5` **уже скачан** в `~/.local/share/uv/python/cpython-3.14-linux-x86_64-gnu/` —
докачивать нечего.

Собственных C-расширений у ccgram нет. Зависимости: `python-telegram-bot[socks,rate-limiter]`,
`python-dotenv`, `httpx`, `libtmux`, `Pillow`, `telegramify-markdown`, `aiofiles`, `structlog`,
`click`, `pyte`, `pathspec`, `aiohttp`. Из них pyte чисто питоновский, Pillow и aiohttp везут
готовые cp314 manylinux-колёса. Риск сборки из исходников практически нулевой.

`uv tool install` создаёт изолированное окружение в `~/.local/share/uv/tools/ccgram`,
системный Python не трогается.

### Конфиг

Приоритет загрузки `.env` (`config.py:5`): локальный `./.env` в cwd, затем `$CCGRAM_DIR/.env`,
по умолчанию `~/.ccgram/.env`. Тулбар — `~/.ccgram/toolbar.toml`.

### Миграция с `~/.ccbot/.env`

- `TELEGRAM_BOT_TOKEN` — имя совпадает, переносится как есть;
- `ALLOWED_USERS` — имя совпадает, переносится как есть;
- `CCBOT_SHOW_TOOL_CALLS` → `CCGRAM_HIDE_TOOL_CALLS` (**инверсия смысла**);
- `CCBOT_SHOW_USER_MESSAGES` → аналога нет, поведение всегда включено;
- добавить `CCGRAM_MULTIPLEXER=herdr` (`config.py:207`, дефолт `tmux`);
- добавить `CCGRAM_GROUP_ID`;
- опционально `HERDR_SOCKET_PATH`, `CCGRAM_CLAUDE_COMMAND`, `CLAUDE_CONFIG_DIR`.

### Запуск как сервис

⚠️ `scripts/restart.sh` **вам не подойдёт.** Это dev-супервизор, прибитый к tmux через
`CCGRAM_DEV_TMUX_SESSION` и окно `__main__`, и он на старте сам выполняет
`uv run ccgram hook --install`.

Правильный путь — обычный systemd `--user` юнит:

```ini
[Unit]
Description=ccgram
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/ccgram
Environment=CCGRAM_MULTIPLEXER=herdr
Environment=HERDR_SOCKET_PATH=%h/.config/herdr/herdr.sock
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

### Мульти-инстанс — ✅

Отдельный `CCGRAM_DIR` и свой `CCGRAM_GROUP_ID` на каждый инстанс. Инстанс молча игнорирует
апдейты чужих групп (`guides.md:451-477`, `config.py:108-113`). Без `CCGRAM_GROUP_ID`
один инстанс обслуживает все группы.

---

## 11. Здоровье кода и риски

**Тесты.** 101 тестовая функция непосредственно по herdr в семи файлах:

- `tests/ccgram/test_herdr_backend.py` — 87
- `tests/ccgram/test_herdr_identity_audit.py` — 3
- `tests/integration/test_shell_herdr.py` — 3
- `tests/ccgram/test_herdr_events.py` — 2
- `tests/ccgram/test_herdr_boundary_audit.py` — 2
- `tests/ccgram/test_herdr_legacy_model_audit.py` — 2
- `tests/integration/test_herdr_contract.py` — 2

Из 4364 тестов суммарно по проекту.

**TODO / FIXME / XXX / HACK** в `herdr.py`, `herdr_events.py`, `reconciliation.py`,
`topic_mapping.py`, `self_identify.py` — **ноль**.

**CHANGELOG.** 18 herdr-записей. Свежие: документирование формы трансляции событий (стр. 12),
стабилизация Telegram-доставки и устаревшего herdr-мониторинга (138), pane-scoped топики (143),
восстановление legacy-привязок (150, PR #190), разделение Enter на herdr (155, PR #189),
forward-compatible протоколы (201), сохранение идентичности окна между обновлениями (250),
топики для sessionless-агентов (262), hardening target boundaries (311-318).

**Мейнтейнер очень отзывчив.** Открытых issue — 12, самая свежая от 2026-09-12.
Закрытые #208-#237 в период 2026-08-30…2026-09-05 закрывались в течение дней после открытия.

**⚠️ Главный риск — churn модели идентичности.** За несколько релизов она менялась минимум
дважды: tab-identity model в 4.0.0 (PR #115), затем переход на guarded session targets
с пометкой старых привязок как `legacy_herdr` и **обязательной ручной ре-привязкой**
(`guides.md:75`):

> Existing Herdr tab/pane/terminal bindings are marked `legacy_herdr` and blocked.
> Use `/unbind` to archive the CCGram binding without closing the Herdr session.
> CCGram never guesses a migration target from a name or reusable locator.

Вероятность ещё одной такой миграции ненулевая, и она каждый раз потребует ручного
вмешательства.

**⚠️ Два открытых бага, которые вас заденут:**

- **#245** (2026-09-12) — голодание живых топиков при реплее после рестарта под групповым
  рейт-лимитом;
- **#240** (2026-09-07) — *"Topic probe suspension is permanent: 3 transient network errors
  disable ghost detection for a window for the process lifetime"*, то есть детект удалённых
  топиков молча умирает до перезапуска процесса.

Остальные открытые issue (#241-#244) касаются tmux-специфики и детекта провайдера,
на herdr не влияют.

---

## (a) GAP LIST

### Must-have, чинится быстро

**1. Топик закрыт или удалён → herdr-панель не убивается.**
Разрыв с симметричным lifecycle ccbot. Причина — гейт `view.origin == CCGRAM_CREATED_WINDOW_ORIGIN`
в двух местах (`topic_lifecycle.py:338-341` и `:200`), плюс сам `topic_closed_handler`
(`:426`) вообще не пытается убивать.
Патч: флаг `CCGRAM_KILL_ON_TOPIC_CLOSE`, снятие гейта под ним, вызов `kill_window`
в `topic_closed_handler`. **Оценка 40-70 строк в одном файле плюс тесты.**

**2. Нет `/show` для прыжка к панели на десктопе.**
Патч: метод `focus_window(target)` в `HerdrManager` — внутри `guard_session_target`
плюс `_call_ok(["agent", "focus", record.terminal_id])`; регистрация
`CommandSpec("show", ...)` в `registry.py`; кнопка в `BUILTIN_ACTIONS`.
Всё, что нужно со стороны herdr, в 0.7.1 уже есть. **Оценка 60-90 строк.**

### Nice-to-have

**3. `/clear` рождает новый топик.**
Правильная починка лезет в модель идентичности: нужен ре-биндинг по смене session id
при сохранении pane. Это 150+ строк в чувствительном месте и прямой конфликт с будущими
апстрим-миграциями. **Рекомендую не чинить**, а не пользоваться `/clear` — открывать
новую вкладку herdr.

**4. `/usage` отсутствует.**
Новая команда, читающая транскрипт или `ccusage`. Полностью изолировано.
**Оценка 80-120 строк.**

**5. Нет blacklist.**
`/unbind` есть, но окно переадоптится следующим тиком. Нужен persistent deny-list,
проверяемый в `_emit_unbound_window_events` (`session_monitor.py:725`).
**Оценка 50-70 строк.** Костыль на сегодня: переименовать вкладку в `__foo__`.

**6. Нельзя выключить эхо своих сообщений из терминала.**
**Оценка 10 строк**: флаг в `config.py` плюс проверка там, где строится `role="user"`.

**7. Мёртвый топик закрывается, а не удаляется.**
Осознанное решение апстрима (PR #189). Если нужно как в ccbot — заменить `close_forum_topic`
на `delete_forum_topic` в `topic_lifecycle.py:134`. **1 строка под флагом.**

---

## (b) Рекомендация: adopt + small fork

**Строить herdrgram с нуля не стоит.**

Вы получаете 59k строк, 4364 теста и, главное, herdr-адаптер, верифицированный против
ровно вашей версии 0.7.1 / protocol 14, с честно решёнными неочевидными вещами:

- разделение `send-text` и `Enter` под особенности агентских TUI (`herdr.py:137-143`);
- reprime подписок каждые 5 секунд из-за отсутствия уведомления о смене локатора
  (`herdr.py:150`);
- sentinel против гонки subscribe / reprime (`herdr_events.py:16-19`);
- карантин неоднозначных и битых записей `agent.list` (`herdr.py:600-620`);
- tri-state liveness перед любым деструктивным действием (`reconciliation.py:98-131`).

Плюс всё, что вокруг Telegram и к herdr отношения не имеет: рейт-лимитер, бэклог,
delivery receipts, entity formatting, интерактивные UI с инлайн-клавиатурой, работа
с топиками в приватных чатах. На повторение этого уйдут месяцы.

Два must-have гэпа — это примерно 150 строк в двух файлах, оба в местах, которые апстрим
трогает редко.

**Практический план.** Сначала неделя на ванильном ccgram: посмотрите, насколько вас реально
беспокоит отсутствие убийства панели и `/show`. Возможно, автозакрытия топика хватит.
Если нет — форк с двумя патчами и PR в апстрим. Мейнтейнер отзывчив, а `/show`
через `agent focus` выглядит как фича, которую примут.

---

## (c) Процедура безопасного теста, ~15 минут

### Предусловие, важное

У ccgram и ccbot один и тот же `TELEGRAM_BOT_TOKEN`. Два поллера на одном токене дадут
постоянные конфликты HTTP 409 на `getUpdates`. Либо **остановите ccbot на время теста**,
либо заведите второго тестового бота у @BotFather и возьмите его токен. Второй вариант
безопаснее.

Заранее подготовьте: группу Telegram с включёнными Topics, бота в админах с правом
Manage Topics, ID группы через @RawDataBot.

### Шаг 1. Изолированная установка

```bash
uv tool install --python 3.14 ccgram
ccgram --version
```

### Шаг 2. Конфиг

Не копируйте `~/.ccbot/.env` целиком, чтобы не притащить `CCBOT_*`:

```bash
mkdir -p ~/.ccgram
cat > ~/.ccgram/.env <<'EOF'
TELEGRAM_BOT_TOKEN=<токен тестового бота, или ccbot если ccbot остановлен>
ALLOWED_USERS=<ваш telegram user id>

CCGRAM_MULTIPLEXER=herdr
CCGRAM_GROUP_ID=<id тестовой группы>
CCGRAM_CLAUDE_COMMAND=claude
HERDR_SOCKET_PATH=~/.config/herdr/herdr.sock

# КЛЮЧЕВОЕ ДЛЯ БЕЗОПАСНОСТИ: отключает весь autoclose и TTL-убийство
AUTOCLOSE_DONE_MINUTES=0
AUTOCLOSE_DEAD_MINUTES=0

CCGRAM_PANE_LIFECYCLE_NOTIFY=false
MONITOR_POLL_INTERVAL=2.0
EOF
chmod 600 ~/.ccgram/.env
```

Нули в `AUTOCLOSE_*` — главная страховка. При них не запускается ни autoclose топиков,
ни `check_unbound_window_ttl`, который проверяет `if timeout <= 0: return` в самом начале
(`topic_lifecycle.py:161-164`). Плюс ваши панели помечены как manually-discovered
и не убиваются в принципе (`window_state_store.py:123`).

### Шаг 3. Доктор — ничего не меняет

```bash
ccgram doctor
```

Ожидается: `herdr` найден в PATH, сокет достижим, протокол 14 принят, и отдельным пунктом
жалоба, что ccgram-хук в `settings.json` отсутствует. Это ожидаемо и нормально —
хук ставить не будем.

### Шаг 4. Спрятать панель, в которой идёт этот анализ (опционально)

ccgram увидит и адоптирует в том числе `wC:p7`. Если не хотите для неё топик:

```bash
herdr tab rename wC:t7 __main__
```

Вернуть потом: `herdr tab rename wC:t7 ccbotnew`.

### Шаг 5. Запуск на переднем плане, без хуков

```bash
ccgram
```

Через 2-5 секунд в группе должны появиться топики с именами вида
`Claude ▸ hm-dao-bots ▸ 1`, `Claude ▸ hm-dao-bots ▸ cbot`, `Claude ▸ hm-dao-bots ▸ ccbotnew`
(формат из `topic_mapping.py:157-173`) со статусным эмодзи в начале. В логе будут строки
`Auto-created topic ... for window herdr-session-v1-...`.

### Шаг 6. Что проверить

- Напишите в топик — текст должен появиться в соответствующей панели herdr и уйти
  на исполнение.
- Напечатайте что-нибудь в панели за столом — в топике появится 👤 с вашим текстом,
  без дубля от предыдущего пункта.
- Дождитесь ответа агента, посмотрите на tool calls и статус-пузырь с тулбаром.
- Нажмите кнопку Screen в тулбаре или отправьте `/screenshot`.
- Отправьте `/history`.
- Отправьте `/sessions` — увидите дашборд. **Kill не нажимайте.**
- **Не нажимайте кнопку Esc в тулбаре**, если в панели идёт нужная вам работа.

### Шаг 7. Откат

```bash
# Ctrl-C в терминале с ccgram
uv tool uninstall ccgram
rm -rf ~/.ccgram
```

Топики в Telegram удалите руками, либо просто удалите тестовую группу.

`~/.claude/settings.json` останется нетронутым, потому что `ccgram hook --install`
вы не запускали. Если всё же запустите на каком-то этапе — откат штатный:

```bash
ccgram hook --uninstall    # удаляет только ccgram-записи
ccgram hook --status
```

`_uninstall_json_hooks` (`hook.py:418-461`) фильтрует по предикату
`_is_any_ccgram_hook_command`. Herdr-запись `bash '~/.claude/hooks/herdr-agent-state.sh' session`
под него не подпадает и останется. Проверить:

```bash
python3 -c "import json;print(json.load(open('~/.claude/settings.json'))['hooks'])"
```

Ожидаемый результат — ровно тот же единственный SessionStart-вход с matcher `*`
и командой herdr, что и до теста.

**Живые herdr-панели после отката не пострадают:** ccgram за весь тест не вызывает
ни `pane close`, ни `tab close` для окон, которые он не создавал сам.

---

## Приложение: что было проверено на живой системе

Все обращения к herdr были read-only:

- `herdr --version` → 0.7.1
- `herdr status --json` → protocol 14, compatible true, live_handoff true
- `herdr agent list` → 5 записей, у всех полный `agent_session` с Claude session uuid
- `herdr workspace list` → один workspace `wC`, label `hm-dao-bots`
- `herdr tab list` → 5 вкладок: `1`, `2`, `3`, `cbot`, `ccbotnew`
- `herdr pane --help`, `herdr tab --help`, `herdr workspace --help`, `herdr agent --help`
- `cat ~/.claude/settings.json` (только чтение)
- `uv python list` → cpython-3.14.5 уже установлен

ccgram не запускался, хуки не устанавливались, `~/.claude/settings.json` не изменялся,
в Telegram ничего не отправлялось, живые herdr-панели не затрагивались.
