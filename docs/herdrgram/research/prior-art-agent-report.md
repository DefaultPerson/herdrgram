# herdrgram: разведка прежних наработок (prior art)

Дата исследования: 2026-09-13. Все факты о репозиториях получены через `gh repo view` / `gh api`
и чтение исходников, а не из сниппетов поиска. Где утверждение взято из описания репозитория
и не проверено кодом, это отмечено явно.

---

## TL;DR

**Да, готовый мост herdr ↔ Telegram уже существует, и не один.**

Главное: `alexei-led/ccgram` это по сути **тот самый ccbot, переписанный под herdr**. В его файле
LICENSE (MIT) прямо стоит вторая строка копирайта: `Copyright (c) 2026 six-ddc (original project)`,
то есть это легальный хард-форк инструмента, которым пользователь уже пользовался. herdr там
первоклассный бэкенд наравне с tmux и agterm: forum topics один к одному к агент-сессии, голос
через Whisper, inline-кнопки, скриншоты терминала. Писать herdrgram с нуля смысла нет.

Второй по зрелости и единственный конкурент на чистом сокете это `permgps/herdr-telegram-agents`
на Go под MIT: иконка топика показывает статус агента, кнопка на каждый вариант диалога.

Отдельный стратегический риск для самой идеи: у Anthropic уже есть **официальный Telegram-плагин
через Channels**, который пушит сообщения прямо в живую сессию Claude Code и умеет permission relay.
Он не умеет forum topics и не управляет флотом панелей, и именно туда смещается ниша herdrgram.

Рекомендуемый первый шаг: `uv tool install ccgram`, затем `CCGRAM_MULTIPLEXER=herdr` на реальной
сессии. Если чего-то не хватает, дешевле дослать туда PR или форкнуть MIT-кодовую базу, чем
строить свой мост.

---

## Что уже существует, по убыванию релевантности

### Уровень 1: можно брать и пользоваться

**alexei-led/ccgram** — https://github.com/alexei-led/ccgram

- 264★, последний push 2026-09-05, Python 3.14+, **MIT**, релиз v4.10.3, есть PyPI и brew tap.
- Бэкенды: tmux, **herdr**, agterm. Принципиально не SDK: агент остаётся там, где он есть,
  сессия в терминале остаётся источником истины.
- herdr-часть, проверено в `src/ccgram/multiplexer/herdr.py` и `herdr_events.py`: поддержка
  socket-протоколов 14–20, `agent.list` как единственный источник identity, персистится
  непрозрачный таргет `herdr-session-v1-…`, а не `pane_id` и не `tab_id`, живой
  `events.subscribe` через постоянный сокет-стрим (`pane.agent_status_changed`, `tab.closed`),
  остальные вызовы через `$HERDR_SOCKET_PATH`. Есть обработка обеих форм имён событий,
  с точкой и с подчёркиванием.
- Telegram: forum topics, один топик равен одной guarded agent session, заголовок с префиксом
  провайдера `<Provider> ▸ <workspace> ▸ <tab> ▸ <pane>`, голосовые через Whisper, скриншоты
  терминала по запросу и автообновление раз в пять секунд, action toolbar с кнопками провайдера,
  ответ одним тапом на нумерованные и yes/no промпты, `/send` для файлов из воркспейса,
  directory browser при создании топика, хуки Claude Code для мгновенных пушей,
  очередь доставки at-least-once с кнопкой Jump to live, поддержка git worktree.
- Fails closed на отсутствующих, битых, бессессионных и legacy-привязках, дубликаты канонических
  таргетов карантинятся. Честно документирует post-guard race: доставка не атомарна.
- Ограничения: Linux, macOS и WSL2, нативный Windows не поддержан. Только табы, где реально
  запущен агентский CLI, становятся топиками, голые шеллы нет. Жёсткая привязка к
  `python-telegram-bot>=22.6,<22.7`.
- **Вердикт: use as-is.** Это буквально ccbot, живущий на herdr.

**permgps/herdr-telegram-agents** — https://github.com/permgps/herdr-telegram-agents

- 19★, push 2026-09-07, Go, **MIT**.
- Единственный кроме ccgram, кто ходит в чистый unix-socket: ndjson, protocol 17, вызовы
  `agent.list`, `agent.read`, `agent.prompt`, `agent.send_keys`, `agent.focus`, `agent.rename`,
  `tab.create`, `agent.start`, `pane.close`, `notification.show`, плюс долгоживущий
  `events.subscribe` со своим reconnect и обработкой StreamReset.
- Telegram: топик на агента, **иконка топика отражает статус**, отдельная inline-кнопка на каждый
  пронумерованный вариант диалога, `editMessageText` вместо спама новыми сообщениями,
  закреплённый дашборд в General, приём файлов через `getFile`, дублирующий пинг в личку со
  звуком, переименование и закрытие топика транслируются в rename и mute агента, quiet-mode
  по простою клавиатуры, команды `/screen`, `/keys`, `/clear`, `/compact`, `/usage`, `/model`.
- Дыры: голоса нет, idle-detection работает на macOS и Windows, но не на Linux, стриминга ответа
  в одно редактируемое сообщение нет, только `/screen` по запросу.
- **Вердикт: fork & adapt**, либо использовать как есть.

**dcolinmorgan/herdr-remote** — https://github.com/dcolinmorgan/herdr-remote

- 354★, push 2026-09-11, Python, **лицензия NOASSERTION**, то есть GitHub её не распознал.
  Форкать юридически мутно, флаг.
- Меню-бар приложение для macOS (Herdi.app), Python relay, веб-дашборд, TUI и Telegram-бот.
  Ставится как herdr-плагин `dcolinmorgan/herdr-push`. Telegram ходит в relay через localhost,
  поэтому Cloudflare Tunnel для него не нужен, хватает исходящего доступа в интернет.
- Команды: `/start`, `/agents`, `/read`, `/reply`, `/send`, `/trust`, `/interrupt`, `/digest`.
  Forum topics не используются, навигация через пикеры с пагинацией Previous и Next.
- **Вердикт: steal ideas** (`/trust` для разблокировки, `/digest` со сводкой за день,
  Telegram без туннеля).

### Уровень 2: двусторонние, но менее зрелые

**mvallebr/herdr-telegram-plugin** — https://github.com/mvallebr/herdr-telegram-plugin

- 5★, TypeScript, **MIT**, на grammy. Один топик равен одной панели, zero-LLM в тракте.
- Привязка через herdr CLI и `spawnSync`, не через сокет: `herdr agent list`, `herdr tab list`,
  `herdr pane run <pane> <text>`, `herdr pane send-keys <pane> Escape|Enter`. Архитектура
  polling, watcher раз в пятнадцать секунд, а не события.
- Команды `/follow [minutes]`, `/last`, `/stop`, `/bind`, `/reconcile`, `/digest`.
  Inline-кнопок для permission-промптов почти нет.
- **Вердикт: steal ideas** (дельта-диффы вывода в `output-diff.ts`, инвариант одного активного
  цикла на панель в `pane-agent.ts`, `/follow` с дедлайном).

**tankisstank/herdr-telegram-plugin** — https://github.com/tankisstank/herdr-telegram-plugin

- 1★, TypeScript, **MIT**. Поддерживаемый форк предыдущего с упором на Codex.
- То же подключение через herdr CLI, но текст сабмитится carriage-return событием, а не простой
  вставкой в composer, на чём upstream спотыкался.
- Ценное: inline-кнопки Yes / All / No / No+comment, **fingerprinting коллбэков** (кнопка от
  старого промпта не может подтвердить новый, устаревшие клавиатуры гасятся), `/model` и
  `/reasoning` открывают нативный picker Codex и релеят варианты с подсветкой выбранного,
  фильтрация tool-шума из прогресса.
- **Вердикт: steal ideas.**

**gaijinjoe/herdres** — https://github.com/gaijinjoe/herdres

- 6★, push 2026-07-03, Python, **MIT**, только стандартная библиотека.
- Маппит workspace в топик, а с флагом `HERDR_TELEGRAM_TOPICS_PER_AGENT=1` даёт топик на агента
  с заголовком `<agent> · <folder>`.
- Подключение: herdr CLI плюс собственный shim-бинарь `herdr_turn_adapter.py`, который проксирует
  все команды в настоящий herdr и добавляет свою `pane turn`, читающую завершённые ходы из логов
  сессии агента. Отдельно `herdres_decision_hook.py` это **PreToolUse-хук Claude Code** для
  AskUserQuestion и ExitPlanMode.
- Telegram: кнопки на ожидающее решение, стриминг частичного вывода через draft-методы,
  закреплённый статус, голос через `herdres_speech.py`, отдельный бот с аватаркой на агента,
  `/send!` прерывает занятого агента, занятая панель это «queued», а не ошибка.
- **Вердикт: steal ideas**, в первую очередь PreToolUse-хук.

**luminexord/herdres** — https://github.com/luminexord/herdres

- 10★, push 2026-08-09, Python, **MIT**. Более новая инкарнация того же проекта, но переписана так,
  что за herdr она не наблюдает вообще: всё наблюдение, содержимое ходов и pending-интеракции
  отданы внешнему сервису Tendwire (`plotarmordev/tendwire`), herdres остаётся только
  Telegram-слоем.
- **Вердикт: как база нерелевантен** из-за жёсткой внешней зависимости. Забрать стоит только
  cursor-resumable bootstrap с персистентным watermark, чтобы догонять состояние после простоя бота.

**etinpres/herdr-telegram-remote** — https://github.com/etinpres/herdr-telegram-remote

- 3★, Python, **MIT**. Интерфейс бота корейский, есть README.en.md.
- herdr CLI через `subprocess` в `herdr_transport.py`, типизированная обработка `pane_not_found`.
  Принципиально никогда не откатывается на сфокусированную панель, каждая операция требует
  явного pane id.
- Топики в приватном чате, атомарная замена файла маппинга, дебаунс переименований,
  `/screen`, `/tail N`, `/screenshot` в PNG, `/yes`, `/no`, `/1`, `/2`, автоподхват установленных
  скиллов, опасные команды за токеном с TTL шестьдесят секунд.
- **Вердикт: steal ideas** (подавление пуша, пока ты активно жмёшь стрелки в TUI).

**cokekitten/herdr-telegram-bridge** — https://github.com/cokekitten/herdr-telegram-bridge

- 4★, Python, **MIT**. Плагин-хук плюс long-poll демон, модель взаимодействия это ответ на
  уведомление, а не команды.
- herdr CLI: `herdr agent prompt <pane> <text>` с проверкой статуса `working` и откатом на
  `agent send-keys enter`, `agent read --lines --format text`, `agent send-keys esc`, `agent get`,
  `pane process-info`. Дополнительно читает транскрипты сессий с диска для Claude, Codex,
  opencode, hermes, grok и kimi.
- Forum topics не использует, роутинг через нативный reply-threading Telegram. Приём фото и
  файлов, expandable quote для длинных ответов, корректный сплит по 4096 символов.
- **Вердикт: steal ideas** (детект завершения как переход `working` в `idle`, резолв транскрипта
  сессии на диске).

**vsem-azamat/herdr-telegram** — https://github.com/vsem-azamat/herdr-telegram

- 0★, Go, **MIT**. Работающего бота нет, автор честно пишет, что мост ещё не запускается.
  Есть типизированный Go SDK под protocol 17 и очень качественная документация.
- Ценность: десять инвариантов (топик привязан к стабильной агент-сессии, `pane_id` это адрес,
  а не identity, никогда не откатываться на focused pane, fail closed при неоднозначности),
  `docs/threat-model.md` и разбор гонки в `docs/spikes/herdr-expected-session.md`.
- **Вердикт: steal ideas**, прочитать до начала написания кода.

**vinceferro/herdr-tg** — https://github.com/vinceferro/herdr-tg

- 0★, Rust, **MIT**. Реально построен только первый срез: крейт `herdr-client` с транспортом,
  handshake, ndjson-стримом под **protocol 20** и CLI `status`, `read`, `doctor`, `watch`.
  Отправки ввода ещё нет.
- Ценность: golden-фикстуры протокола 20 в `tests/fixtures/` и заметка о том, что herdr не отдаёт
  структурированный ask, значит парсить экран придётся самому.
- **Вердикт: steal ideas** (фикстуры).

### Уровень 3: только уведомления

Закрывают лишь половину задачи, «дёрни меня, когда агент заблокировался или закончил»:
`barnuri/herdr-notifications` (MIT, мульти-провайдер Telegram, Slack и Discord, кнопки из
детектора опций), `naturalmoods/herdr-telegram-notify` (MIT, проверяет перед доставкой, та ли
сессия сейчас в панели, локи через `flock`), `elkraps/herdr-telegram-notify` (MIT, шаблоны на
статус, редакция секретов, подавление при фокусе), `hkdom/herdr-telegram-gate` (MIT,
approval-gate с классификацией риска по `policy.json`), `blockshiftnetwork/herdr-telegram-attention`,
`fulanto/herdr-oncall`, `sbulav/herdr-relay`, `bufford-Tannen/herdr-telegram-notify` (push-only,
но хороший setup-визард и модель угроз для конфига).

Мобильные клиенты без Telegram, для полноты картины: `AltanS/collie` (946★, PWA через Tailscale),
`0cv/herdr-mobile-relay` (208★, Go, QR-пейринг и push), `ZingerLittleBee/Heeler` (нативный iOS),
`lntvan166/paddock` (веб-дашборд, говорит по сокету напрямую, шлёт Telegram-алерт после того,
как состояние устоялось).

Нерелевантное: `ivanarama/PromptPilot` (очередь фоновых задач, спавнит сессии, а не подключается
к живой, но у него есть режим `"executor": "herdr"` и поддержка `herdr --remote <host>` по ssh),
`gpando/herdr-telegram` (только OpenCode, push-only), `Phoobobo/herdr-bot` (никакого Telegram,
это overlay-TUI), `waltcow/herdr-telegram-adapter` (пустой stub).

---

## Официальная поверхность herdr и позиция мейнтейнера

Своей Telegram-интеграции у herdr нет. В `/docs/integrations/` перечислены только агентные
рантаймы (Pi, OMP, Claude Code, Codex, Copilot CLI, Devin, OpenCode и прочие), никакого Telegram,
Slack или push. Родные уведомления это системные тосты и BEL в родительский терминал.

При этом в официальных примерах лежит референс-плагин
`ogulcancelik/herdr-plugin-examples/agent-telegram-notify`: манифест `herdr-plugin.toml` с
`[[events]] on = "pane.agent_status_changed"` и командой `node notify.mjs`, конфигурация через
`.env` с `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`. Односторонний notify, без входящего канала.
Это благословлённая архитектура, но не продукт. Маркетплейс нерецензируемый: листинг означает лишь,
что репозиторий сам себя пометил топиком `herdr-plugin`, переиндексация раз в тридцать минут.

**Позиция мейнтейнера: молчание.** Проверено поиском по репозиторию: `commenter:ogulcancelik`
вместе с `telegram` даёт ноль результатов при 735 его комментариях в issues и PR. Слово telegram
встречается ровно в одном теле issue на весь репозиторий. Политика такая: фичреквесты
автоматически выпихиваются из Issues в Discussions. Показательный случай: issue #1623 про
`client.action.trigger` над socket API закрыт через восемь секунд после создания и конвертирован
в discussion #1624, где ответа мейнтейнера нет до сих пор. Автор там прямо пишет, что
`herdr-remote` и `collie` упираются в одну и ту же стену, потому что с телефона нет клавиатурного
аккорда, и что remote control это core herdr territory. Спустя два месяца приписка: на 0.9.0 стена
на месте.

Три открытых обсуждения, критичных для herdrgram, все без ответа мейнтейнера:

- discussion #3913 просит подтвердить, является ли endpoint generation 1 из версии 0.9
  поддерживаемым контрактом для сторонних мобильных клиентов.
- discussion #2016 просит добавить в `agent.prompt` проверку expected session ID. Это ровно та
  гонка, что отправит сообщение в панель с уже перезапущенным Claude.
- discussion #3809 фиксирует, что в 0.9 agent CLI по-прежнему работает в пределах одного сервера
  и не видит агентов на других машинах.

Ещё два открытых бага прямо бьют по качеству уведомлений: #2779 (панели Claude Code, запущенные
нативным лаунчером, не детектятся, статус остаётся `unknown`) и #3414 (панель Claude Code
рапортует `working`, стоя на промпте, если рядом крутится фоновый шелл).

Вывод: закладываться надо на прямой socket API с явными `pane_id` и быть готовым, что контракт
поедет. На plugin-контекст полагаться нельзя: `herdr plugin action invoke` принимает только
`--plugin`, без `--pane` и `--workspace`, и резолвит контекст из сфокусированной панели.

### Полезные факты об API herdr

Транспорт: newline-delimited JSON по локальному сокету, на Windows это named pipe. Путь
`~/.config/herdr/herdr.sock`, для именованных сессий `~/.config/herdr/sessions/<name>/herdr.sock`,
резолвится через `HERDR_SOCKET_PATH` или `HERDR_SESSION`. **Аутентификации нет**, защита только
правами файловой системы. Машиночитаемая схема доступна как `herdr api schema --json`.

Чтение панели: `pane.read` с параметрами `pane_id`, `source` (`visible`, `recent`,
`recent-unwrapped`, `detection`) и `lines`. Инъекция ввода: `pane.send_input`, `pane.send_text`,
`pane.send_keys`. Подписка на события: `events.subscribe` с массивом `subscriptions`, первый ответ
это ack, далее события пушатся построчно в тот же held-коннект.

Самое важное и недооценённое: вместо polling существует живой стрим,
`herdr terminal session observe <pane>` и `herdr terminal session control <pane> --takeover`.
Они печатают ndjson-записи `terminal.frame` с base64-кодированными ANSI-байтами, а `control`
принимает на stdin команды `terminal.input`, `terminal.resize`, `terminal.scroll`,
`terminal.release`. `pane.read` ограничен каденцией change-событий около десяти герц и теряет
курсор и scroll-regions.

---

## Claude Code Channels: главный конкурент, и он официальный

Это не Remote Control, а отдельный механизм: канал это MCP-сервер, который пушит события в уже
запущенную сессию Claude Code. Telegram, Discord и iMessage входят в research preview как готовые
плагины Anthropic. Исходник открыт, лицензия **Apache-2.0**, репозиторий
`anthropics/claude-plugins-official` (36188★, push 2026-09-13). Ниже разбор по файлу
`external_plugins/telegram/server.ts`, 1045 строк.

**Маппинг сессий на чаты: одна сессия на один запуск.** Канал регистрируется флагом
`claude --channels plugin:telegram@claude-plugins-official` и пушит ровно в ту сессию, которая
этот флаг получила. Роутинга между несколькими сессиями нет. Чтобы поднять несколько ботов на
одной машине, README требует разные токены и отдельный `TELEGRAM_STATE_DIR` на каждый инстанс.
В коде есть защита слота: если сессия умерла по SIGKILL или закрытием терминала, внук-процесс
`server.ts` может осиротеть и держать слот, поэтому каждая новая сессия это проверяет. Модель
ровно обратная herdrgram: не один бот на флот панелей, а один бот на одну сессию.

**Forum topics не поддерживаются вообще.** В исходнике нет ни одного упоминания
`message_thread_id`. Единственный вид тредов это нативный reply-to: инструмент `reply` принимает
`chat_id`, `text` и опциональный `reply_to` с `message_id`. Группы поддержаны через `access.groups`
с флагом `requireMention`, но в комментарии на строке 424 сказано, что часть групповых сценариев
исключена намеренно, по соображениям безопасности. Именно здесь ccgram и permgps объективно
сильнее официального решения.

**Permission relay есть и сделан хорошо.** Сервер объявляет capability `claude/channel/permission`,
принимает нотификацию `notifications/claude/channel/permission_request`, рендерит карточку с
inline-кнопками Allow, Deny и See more, рассылает её во все allowlist-DM. Callback data имеет вид
`perm:allow:<id>` с пятисимвольным request_id, ответ уходит обратно как
`notifications/claude/channel/permission` с полем `behavior` равным `allow` или `deny`. Текстовый
ответ yes или no при наличии ожидающего запроса тоже засчитывается как решение, а не пересылается
в чат как обычная реплика. Полные детали запроса хранятся отдельно для раскрытия по See more.

**Медиа.** Входящие фото складываются на диск, и в системную подсказку зашита инструкция прочитать
файл по атрибуту `image_path`, прочие вложения тянутся инструментом `download_attachment` по
`file_id`. Исходящие файлы до 50 МБ, картинки уходят как photo с инлайн-превью, остальное как
document. Есть `react` с фиксированным белым списком эмодзи Telegram и `edit_message`.
**Голосовых сообщений нет**, обработчика `message:voice` в коде не существует.

**Доступ и защита.** Политики `pairing`, `allowlist` и `disabled`, бутстрап через шестисимвольный
код спаривания и затем `/telegram:access policy allowlist`. В подсказку сервера зашито требование
отказывать, если кто-то в Telegram просит одобрить спаривание или добавить себя в allowlist,
с прямым указанием, что именно так выглядит prompt injection.

**Требования и ограничения.** Нужен Bun. Аутентификация только через claude.ai или ключ Console API,
на Amazon Bedrock, Google Agent Platform и Microsoft Foundry не работает. Team и Enterprise
заблокированы, пока владелец не включит `channelsEnabled` в managed settings, список разрешённых
плагинов подменяется через `allowedChannelPlugins`. Флаг `--channels` принимает только плагины из
поддерживаемого Anthropic allowlist, свой канал грузится лишь через
`--dangerously-load-development-channels`. Оба флага не показываются в `claude --help`, это research
preview, синтаксис и контракт протокола могут поменяться. События приходят только пока сессия
открыта, для режима «всегда на связи» её надо держать в фоне. В неинтерактивном режиме с `-p`
вопросы с выбором варианта и подтверждение plan mode отключаются. В терминале виден входящий текст,
но не текст ответа Claude.

**Контраст с Remote Control.** Remote Control драйвит локальную сессию из приложения claude.ai и
с мобильных клиентов, запускается через `/remote-control`, `/rc`, `claude --remote-control` или
server-mode `claude remote-control` с флагами `--spawn`, `--capacity`, `--permission-mode`.
Код исполняется локально, наружу идёт только исходящий HTTPS. Но публичного API для сторонних
клиентов нет, поэтому Telegram-бота поверх него не построить. Плюс ограничения: нужна подписка
Pro, Max, Team или Enterprise, ключи API не поддерживаются, транскрипт хранится на серверах
Anthropic, организациям с Zero Data Retention включить нельзя.

**Практический вывод.** Channels закрывают сценарий «поговорить с одной сессией Claude с телефона»
и делают это с permission relay из коробки. Они не закрывают флот, не дают forum topics и не
работают с Codex, Pi, OMP и прочими не-Claude агентами в панелях herdr. Ниша herdrgram смещается
именно туда: один бот на весь флот панелей, независимо от того, какой агент внутри.

---

## Идеи, которые стоит забрать

1. **Identity через `agent.list`, а не через `pane_id`** (`alexei-led/ccgram`, обоснование в
   `vsem-azamat/herdr-telegram`). Панели переиспользуются, и топик, привязанный к `pane_id`,
   однажды отправит сообщение в чужую свежую сессию. ccgram хранит непрозрачный таргет
   `herdr-session-v1-…` и перечитывает свежий снапшот перед каждым действием, а на пропавшей,
   битой или legacy-привязке падает закрыто.

2. **Живой стрим вместо polling** (`tlamadon/herdr-hq`, `nikok6/herdr-mirror`). Команды
   `herdr terminal session observe <pane>` и `control <pane> --takeover` отдают ndjson-записи
   `terminal.frame` с base64 ANSI. Ни один Telegram-мост их не использует, это реальная
   техническая щель и главный кандидат в дифференциаторы.

3. **Три подтверждённые в коде ловушки API herdr.** `agent.prompt` отбивается ошибкой
   `agent_blocked` на заблокированной панели, отвечать надо через `pane.send_text` плюс
   `pane.send_keys`, и там же `agent read --source recent` падает с `agent_not_idle`
   (`barnuri/herdr-notifications`). Статус `done` herdr рапортует только для несфокусированных
   панелей, поэтому завершение надо ловить как переход `working` в `idle`
   (`cokekitten/herdr-telegram-bridge`).

4. **Иконка forum topic как индикатор статуса агента** (`permgps/herdr-telegram-agents`). Самый
   дешёвый способ видеть состояние всего флота, не открывая топики. Оттуда же закреплённый
   дашборд в General и правка одного сообщения через `editMessageText` вместо спама новыми.

5. **PreToolUse-хук Claude Code как источник структурированных вопросов** (`gaijinjoe/herdres`)
   для AskUserQuestion и ExitPlanMode. Надёжнее парсинга экрана, потому что herdr структурированный
   ask не отдаёт, это отдельно подтверждено в заметках `vinceferro/herdr-tg`.

6. **Fingerprinting inline-коллбэков** (`tankisstank/herdr-telegram-plugin`): кнопка от протухшего
   промпта не должна подтверждать новый, устаревшие клавиатуры гасятся. Плюс сабмит через
   carriage-return, а не простую вставку текста в composer.

7. **TOCTOU-ревалидация перед нажатием Enter** (`hkdom/herdr-telegram-gate`): между тапом Approve
   и отправкой клавиши панель перечитывается и переклассифицируется. Там же в каталоге `research/`
   лежит корпус реальных blocked-экранов Claude, Codex, Gemini и Kimi, готовые фикстуры для тестов
   парсера.

8. **Подавление пуша по активности в самом TUI** (`etinpres/herdr-telegram-remote`): если ты прямо
   сейчас жмёшь стрелки в терминале, уведомление не отправляется. Аккуратнее, чем детект простоя
   клавиатуры у permgps, который к тому же не работает на Linux.

9. **Очередь доставки at-least-once с кнопкой Jump to live** (`alexei-led/ccgram`) на случай,
   когда бот лежал, а агент работал. Порог сто элементов или пять минут возраста старейшего,
   с подтверждением и пометкой о пропущенном диапазоне.

10. **Карточка permission с Allow, Deny и See more плюс приём текстового yes или no как решения**
    (`anthropics/claude-plugins-official`, Apache-2.0, копировать законно). Оттуда же стоит взять
    дословно инструкцию против prompt injection: отказывать, когда изменить список доступа просят
    сообщением из чата.

11. **Проверка «та же ли сессия сейчас в панели» перед доставкой ответа**
    (`naturalmoods/herdr-telegram-notify`): перед отправкой делается `herdr agent get <pane>`, и
    доставка отменяется, если панель теперь хостит другую агент-сессию. Там же файловые локи через
    `flock` на очередь и на маппинг топиков.

12. **Дельта-диффы вывода и инвариант одного активного цикла на панель**
    (`mvallebr/herdr-telegram-plugin`, файлы `output-diff.ts` и `pane-agent.ts`), чтобы не слать
    в топик один и тот же экран дважды.

13. **Cursor-resumable bootstrap с персистентным watermark** (`luminexord/herdres`), чтобы после
    простоя бота догнать состояние, а не начинать с чистого листа.

14. **Спавн новой сессии с телефона** (`TerrifiedBug/omp-telegram`): `tab.create`, затем взять
    `root_pane.pane_id` и вызвать `agent.start`. Оттуда же демон, который продолжает поллить,
    когда все сессии закрыты, это критично для сценария «ушёл от стола».

15. **Мультимашинность через ssh** (`ivanarama/PromptPilot`): `herdr --remote <host>` как обходной
    путь, пока сам herdr в 0.9 не видит агентов на других машинах.

16. **Резолв транскрипта сессии с диска** (`cokekitten/herdr-telegram-bridge`) для получения
    «последнего осмысленного ответа» вместо скрейпа экрана, с поддержкой форматов Claude, Codex,
    opencode и других.

### Юридические флаги

Безопасны для форка: ccgram, permgps, mvallebr, tankisstank, gaijinjoe, hkdom, barnuri, cokekitten,
naturalmoods, elkraps, etinpres, vsem-azamat, vinceferro, все под MIT, плюс официальный плагин
Anthropic под Apache-2.0.

Проблемные: `dcolinmorgan/herdr-remote` отдаёт NOASSERTION, лицензия не распознана;
`Lynxy039/herdr-telegram-pi` под AGPL-3.0-or-later и заразит проект; у
`waltcow/herdr-telegram-adapter` и `Phoobobo/herdr-bot` лицензии нет вообще, а значит по умолчанию
все права защищены и форкать нельзя.

---

## Sources

- https://github.com/alexei-led/ccgram
- https://github.com/permgps/herdr-telegram-agents
- https://github.com/dcolinmorgan/herdr-remote
- https://github.com/mvallebr/herdr-telegram-plugin
- https://github.com/tankisstank/herdr-telegram-plugin
- https://github.com/gaijinjoe/herdres
- https://github.com/luminexord/herdres
- https://github.com/vsem-azamat/herdr-telegram
- https://github.com/vinceferro/herdr-tg
- https://github.com/cokekitten/herdr-telegram-bridge
- https://github.com/barnuri/herdr-notifications
- https://github.com/naturalmoods/herdr-telegram-notify
- https://github.com/elkraps/herdr-telegram-notify
- https://github.com/bufford-Tannen/herdr-telegram-notify
- https://github.com/hkdom/herdr-telegram-gate
- https://github.com/etinpres/herdr-telegram-remote
- https://github.com/blockshiftnetwork/herdr-telegram-attention
- https://github.com/fulanto/herdr-oncall
- https://github.com/sbulav/herdr-relay
- https://github.com/lntvan166/paddock
- https://github.com/TerrifiedBug/omp-telegram
- https://github.com/ivanarama/PromptPilot
- https://github.com/Lynxy039/herdr-telegram-pi
- https://github.com/waltcow/herdr-telegram-adapter
- https://github.com/Phoobobo/herdr-bot
- https://github.com/gpando/herdr-telegram
- https://github.com/lsisoft/herdr-telegram-slack-bridge
- https://github.com/Jyzus/herdr-telegram-bridge
- https://github.com/six-ddc/ccbot
- https://github.com/AltanS/collie
- https://github.com/0cv/herdr-mobile-relay
- https://github.com/ZingerLittleBee/Heeler
- https://github.com/tlamadon/herdr-hq
- https://github.com/nikok6/herdr-mirror
- https://github.com/ogulcancelik/herdr-plugin-examples
- https://github.com/yigitkonur/awesome-herdr
- https://github.com/herdrdev/herdr (issues 1623, 2779, 3164, 3414; discussions 1624, 1866, 2016, 2677, 3643, 3809, 3913)
- https://herdr.dev/docs/plugins/
- https://herdr.dev/docs/marketplace/
- https://herdr.dev/docs/agents/
- https://herdr.dev/docs/socket-api/
- https://herdr.dev/docs/cli-reference/
- https://herdr.dev/docs/agent-automation/
- https://herdr.dev/docs/integrations/
- https://code.claude.com/docs/en/channels
- https://code.claude.com/docs/en/channels-reference
- https://code.claude.com/docs/en/remote-control
- https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/telegram (файлы `server.ts`, `README.md`, `ACCESS.md`, `LICENSE`)
- https://dev.to/alexeiled/ccgram-v4-control-ai-coding-agents-from-telegram-now-with-herdr-support-3h2k
