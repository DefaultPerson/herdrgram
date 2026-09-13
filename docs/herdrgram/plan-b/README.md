# План Б: herdrgram с нуля (не активен)

Запускать только если ccgram провалит пробный прогон (docs/PLAN-adopt-ccgram.md, M0–M1).

Что есть:
- `ccbot-reuse-map.md` — результат фазы Understand: 27 модулей ccbot с вердиктами as-is/adapt/rewrite/drop, точками связи с tmux и их herdr-эквивалентами, скрытыми допущениями.
- `../research/herdr-api-notes.md` — проверенные факты API herdr 0.7.1 (методы, события, детекция, рестарт).

Что не сделано: 3 независимых дизайна → судьи → синтез DESIGN.md → живая проверка допущений → PLAN.md. Воркфлоу `herdrgram-design` (run id `wf_96146074-4eb`, скрипт в `~/.claude/projects/-home-def-projects-misc-herdrgram/…/workflows/scripts/herdrgram-design-wf_96146074-4eb.js`) остановлен на фазе Understand из-за пивота на ccgram; при возобновлении в той же сессии Claude Code (`Workflow` с `resumeFromRunId`) готовые результаты Understand берутся из кэша. На этой машине 4 CPU → 2 агента параллельно, полный прогон занял бы часы.
