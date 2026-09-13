# План: внедрить ccgram на herdr (+ маленький форк)

Опирается на docs/DECISION.md и docs/research/ccgram-fit-report.md. Кода в этом репо пока нет — по замыслу «сначала планируем».

## M0. Пробный прогон ванильного ccgram (30 минут, обратимо)
Задачи:
- Завести тестового бота у @BotFather (или остановить ccbot на время теста — два поллера на одном токене дают 409 на getUpdates). Включить Topics в тестовой группе, дать боту админа с Manage Topics, узнать id группы (@RawDataBot, префикс -100).
- `uv tool install --python 3.14 ccgram` (Python 3.14.5 уже скачан uv; C-расширений у ccgram нет).
- `~/.ccgram/.env`: TELEGRAM_BOT_TOKEN, ALLOWED_USERS, CCGRAM_MULTIPLEXER=herdr, CCGRAM_GROUP_ID, HERDR_SOCKET_PATH=~/.config/herdr/herdr.sock, AUTOCLOSE_DONE_MINUTES=0, AUTOCLOSE_DEAD_MINUTES=0 (страховка: без autoclose и TTL-убийства), CCGRAM_PANE_LIFECYCLE_NOTIFY=false. Хук НЕ ставить.
- Спрятать вспомогательные вкладки от адопции: `herdr tab rename <tab_id> __имя__` (label вида `__*__` невидим для ccgram).
- `ccgram doctor` (ничего не меняет), затем `ccgram` на переднем плане.
Done when:
- в группе появились топики `Claude ▸ hm-dao-bots ▸ <tab>` для всех живых панелей без единого тапа;
- сообщение из топика попало в нужную панель и ушло на исполнение; текст, набранный в панели, появился в топике с 👤 и без дубля;
- ответ агента, tool calls, статус-пузырь с тулбаром, `/screenshot`, `/history`, `/sessions` работают;
- `python3 -c "import json;print(json.load(open('~/.claude/settings.json'))['hooks'])"` показывает только herdr-хук.
Откат: Ctrl-C, `uv tool uninstall ccgram`, `rm -rf ~/.ccgram`, удалить тестовую группу.

## M1. Неделя эксплуатации + хук
Задачи:
- `ccgram hook --install` (дописывает 9 событий рядом с herdr-хуком в том же matcher `*`; `ccgram doctor` проверяет сосуществование). Перезапустить агентов, чтобы хук подхватился.
- Перенести боевой токен: остановить ccbot (`systemctl --user stop`/tmux), прописать в `~/.ccgram/.env`. Маппинг: TELEGRAM_BOT_TOKEN и ALLOWED_USERS как есть; CCBOT_SHOW_TOOL_CALLS → CCGRAM_HIDE_TOOL_CALLS (инверсия); OPENAI_API_KEY → CCGRAM_WHISPER_PROVIDER=openai + OPENAI_API_KEY; CCGRAM_CLAUDE_COMMAND=claude --dangerously-skip-permissions (если хочется YOLO по умолчанию для сессий, создаваемых из Telegram).
- Решить про autoclose: вернуть дефолты (done 30 мин, dead 10 мин) или оставить 0.
- Вести список раздражителей: нужен ли kill панели при закрытии топика, нужен ли `/show`, мешает ли эхо своих сообщений, мешает ли `/clear`.
Done when: неделя без возврата к ccbot; список гэпов подтверждён или опровергнут практикой.

## M2. Форк (только если M1 подтвердил must-have гэпы)
Задачи:
- В этом репо: `git remote add upstream https://github.com/alexei-led/ccgram.git`, `git fetch upstream`, ветка `main` от `upstream/main` (репо herdrgram становится форком ccgram; текущие docs/ переносятся в `docs/herdrgram/`).
- `uv sync --extra dev`, `make check` — зелёный до правок (на этой машине полный прогон: 7368 passed, 48 skipped, ~2 мин).
- Установка из форка: `uv tool install --python 3.14 --from ~/projects/misc/herdrgram ccgram` (или `--editable`).
Done when: `ccgram --version` показывает версию из форка; `make check` зелёный.

## M3. Патч 1 — kill-on-topic-close (must-have, ~40-70 строк + тесты)
Задачи:
- `config.py`: флаг `CCGRAM_KILL_ON_TOPIC_CLOSE` (default false, чтобы апстрим принял).
- `handlers/topics/topic_lifecycle.py`: в `topic_closed_handler` (~:426) при флаге вызывать `multiplexer.kill_window(window_id)` после tri-state проверки присутствия (`reconciliation.window_presence`); снять гейт `view.origin == CCGRAM_CREATED_WINDOW_ORIGIN` в `_unbind_deleted_topic` (~:340) и `check_unbound_window_ttl` (~:185) под тем же флагом.
- На herdr `kill_window` закрывает только панель (`pane close`), не вкладку с соседями — это уже так (`multiplexer/herdr.py:1055-1063`).
- Тесты: закрытие топика → kill вызван при флаге и не вызван без; удалённый топик → то же; manually-discovered origin.
Done when: `uv run pytest tests/ccgram -q -k "topic_lifecycle or topic_closed"` зелёный; вручную: закрыл тестовый топик → панель в herdr исчезла (проверять на scratch-табе с `sleep`, не на живых сессиях).

## M4. Патч 2 — `/show` (must-have, ~60-90 строк)
Задачи:
- `multiplexer/base.py`: метод контракта `focus_window(window_id) -> bool` + capability `supports_focus`.
- `multiplexer/herdr.py`: `guard_session_target` → `_call_ok(["agent", "focus", record.terminal_id])` (0.7.1 ✓); tmux-бэкенд — `select-window` или `NotSupported`.
- `handlers/registry.py`: `CommandSpec("show", ...)`; `toolbar_config.py` `BUILTIN_ACTIONS`: кнопка `show`.
- Опционально: при доставке сообщения из Telegram вызывать `notification.show` («📱 из Telegram: …», sound none) и помечать привязанную панель `pane report-metadata --title` — тоже под флагами, чтобы не шуметь.
Done when: `/show` в топике переключает фокус herdr на нужную панель (`herdr pane list` → `focused: true`); тесты на guard + вызов.

## M5. Nice-to-have (по желанию, отдельными PR)
- `CCGRAM_ECHO_USER_MESSAGES=false` (~10 строк, `response_builder.py:35`).
- `delete_forum_topic` вместо `close_forum_topic` под флагом (`topic_lifecycle.py:134`).
- Persistent blacklist для `/unbind` (проверка в `session_monitor._emit_unbound_window_events`, ~50-70 строк).
- `/usage` (транскрипт или ccusage, ~80-120 строк).
- `/clear`: не чинить; вместо него новая вкладка herdr (`herdr tab create --cwd … && claude`).

## M6. Сервис и вывод ccbot из эксплуатации
Задачи:
- systemd user unit `~/.config/systemd/user/ccgram.service` (ExecStart=%h/.local/bin/ccgram, Environment=CCGRAM_MULTIPLEXER=herdr, Restart=on-failure); `scripts/restart.sh` ccgram не использовать (dev-супервизор под tmux, сам ставит хук).
- Убрать ccbot: остановить сервис/tmux-сессию, снять его SessionStart-хук, если он ещё есть в settings.json (сейчас там только herdr-хук), архивировать `~/.ccbot`.
- Шелл-обёртки `bcc*` из ccbot больше не нужны: сессия создаётся обычным `claude` в новой вкладке herdr (ccgram сам подхватит) или из Telegram через `/new` → директория → workspace herdr.
Done when: `systemctl --user status ccgram` active; после `systemctl --user restart ccgram` топики не задублировались; ccbot не запущен.

## M7. Апстрим
- PR #1: `/show` через `agent focus` (нейтральная фича, шанс принятия высокий).
- PR #2: `CCGRAM_KILL_ON_TOPIC_CLOSE` (осознанное расхождение с политикой «manually-discovered windows are never auto-killed» — подать как opt-in флаг с явным предупреждением).
- Следить за миграциями модели идентичности herdr-привязок (уже менялась дважды; после апгрейда проверять `/sync`), и за багами #245, #240.

## Риски
- Обновление herdr до 0.9.x меняет протокол (ccgram принимает 14–20 без предупреждения, выше — best-effort) и, по release notes, поведение `events.subscribe` (без реплея истории) — ccgram к этому готов (reprime после subscribe).
- Один токен на два бота (ccbot + ccgram) → 409; переключаться целиком.
- `/clear` ломает привязку (новый session id) — принятое ограничение.
- Апстрим-миграции identity могут потребовать ручной `/unbind` + ре-привязки.

## Открытые вопросы к пользователю
1. Тестовый бот для M0 или временно остановить ccbot и взять его токен?
2. Группа-форум или приватный чат с topics (ccgram поддерживает оба; в ccbot использовалась и та, и другая схема)?
3. Делать ли репо herdrgram форком ccgram (M2) сразу, или сначала неделя на ванильном?
