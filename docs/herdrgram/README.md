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
- Процесс: вкладка herdr `__ccgram__` (label вида `__*__` невидим для адопции), команда `cd ~/projects/misc/herdrgram && uv run ccgram 2>&1 | tee -a /tmp/herdrgram-bot.log`.
- Перезапуск после правок: Ctrl-C в той вкладке и та же команда (или `herdr pane run <pane_id> "..."`).
- Скрыть сессию от бота: `herdr tab rename <tab_id> __имя__`. Вернуть: `herdr tab rename <tab_id> имя`.

### Патчи поверх апстрима
- `CCGRAM_KILL_ON_TOPIC_CLOSE` — закрытие/удаление топика убивает панель herdr (в апстриме вручную запущенные сессии не убиваются никогда).
- `CCGRAM_DELETE_TOPIC_ON_AUTOCLOSE` — автозакрытие мёртвого топика удаляет его, а не закрывает.
- `/show` и кнопка тулбара `show` — фокус на панель сессии в herdr (`herdr agent focus`, fallback `tab focus`).
- `CCGRAM_ECHO_USER_MESSAGES=false` — не дублировать в Telegram то, что набрано в терминале.
- `CCGRAM_HERDR_NOTIFY_ON_INJECT` — тост herdr «Telegram → <сессия>» при доставке сообщения из Telegram.

### Известное
- Сессии, запущенные до установки хука, регистрируются в session_map при первом событии Stop; до этого в логе каждые 2 с «Removing stale window_state → Auto-detected provider» для них (апстримное поведение, безвредно).
- Топики в приватном чате создаются только после того, как чат «замечен»: создай любой топик в чате с ботом и напиши в него.
