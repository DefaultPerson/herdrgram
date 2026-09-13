# herdrgram

Telegram ⇄ Claude Code через **herdr** (без tmux): ушёл от компа — продолжаешь ту же сессию в Telegram, вернулся — продолжаешь в herdr, пишешь с любой стороны.

Статус (2026-09-13): фаза исследования и планирования, кода нет.

- `docs/DECISION.md` — главный вывод: готовый мост уже существует (`alexei-led/ccgram`, herdr-бэкенд, MIT); рекомендация «adopt + маленький форк» вместо переписывания ccbot.
- `docs/PLAN-adopt-ccgram.md` — план внедрения ccgram с патчами (пробный прогон → форк → PR в апстрим).
- `docs/research/` — проверенные факты об API herdr 0.7.1, конспект сокет-API, обзор существующих проектов, gap-анализ ccgram.
- `docs/plan-b/` — задел на полную реализацию с нуля (на случай, если ccgram не подойдёт): карта переиспользования модулей ccbot. Дизайн/план с нуля не дописаны — воркфлоу остановлен после пивота на ccgram, его можно возобновить (см. docs/plan-b/README.md).

## Запуск (тестовый стенд, 2026-09-13)

- Форк: этот репо = `alexei-led/ccgram` v4.10.3 + патчи herdrgram (`git log upstream/main..main`). Remote `upstream` указывает на ccgram.
- Конфиг: `~/.ccgram/.env` (токен тестового бота `@your_bot`, `CCGRAM_MULTIPLEXER=herdr`, флаги патчей включены, `AUTOCLOSE_DONE_MINUTES=0`, `AUTOCLOSE_DEAD_MINUTES=2`).
- Хуки Claude Code: `uv run ccgram hook --install` уже выполнен (9 событий рядом с herdr-хуком; проверка `uv run ccgram doctor`; откат `uv run ccgram hook --uninstall`).
- Процесс: systemd user unit `~/.config/systemd/user/ccgram.service` (`ExecStart=.venv/bin/ccgram`, `WorkingDirectory=` этот репо, `Restart=on-failure`). Команды: `systemctl --user status|restart|stop ccgram`, логи `journalctl --user -u ccgram -f -o cat`. После правок кода: `systemctl --user restart ccgram`.
- Скрыть сессию от бота: `herdr tab rename <tab_id> __имя__`. Вернуть: `herdr tab rename <tab_id> имя`.

### Патчи поверх апстрима
- `CCGRAM_KILL_ON_TOPIC_CLOSE` — закрытие/удаление топика убивает панель herdr (в апстриме вручную запущенные сессии не убиваются никогда).
- `CCGRAM_DELETE_TOPIC_ON_AUTOCLOSE` — автозакрытие мёртвого топика удаляет его, а не закрывает.
- `/show` и кнопка тулбара `show` — фокус на панель сессии в herdr (`herdr agent focus`, fallback `tab focus`).
- `CCGRAM_ECHO_USER_MESSAGES=false` — не дублировать в Telegram то, что набрано в терминале.
- `CCGRAM_HERDR_NOTIFY_ON_INJECT` — тост herdr «Telegram → <сессия>» при доставке сообщения из Telegram.
- `CCGRAM_TOPIC_NAME_DECORATIONS=false` — в имени топика только чистое имя: ни эмодзи статуса, ни бейджей RC/YOLO, и смена состояния больше не переименовывает топик (переименование прилетает только когда меняется само имя, т.е. когда переименовали вкладку herdr).
- `CCGRAM_MUX_RENAME_FROM_TELEGRAM=false` — переименование топика в Telegram не уходит в herdr (`tab rename` переименовал бы вкладку всем соседним панелям); имена текут только herdr → Telegram, локально ничего не запоминается — набранное в Telegram имя живёт до следующей смены метки вкладки.
- `CCGRAM_HERDR_TOPIC_LABEL=tab` — имя топика = метка вкладки herdr как есть (включая голый номер у неназванной вкладки), ` ▸ <панель>` дописывается только там, где в одной вкладке несколько агентов.

### Известное
- Сессии, запущенные до установки хука, регистрируются в session_map при первом событии Stop; до этого в логе каждые 2 с «Removing stale window_state → Auto-detected provider» для них (апстримное поведение, безвредно).
- Топики в приватном чате создаются только после того, как чат «замечен»: создай любой топик в чате с ботом и напиши в него.
