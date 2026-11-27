# antigravity — сессии хаба на Antigravity (agy) и DeepSeek внутри них

Модуль для [hub by icons8](https://github.com/icons8/hub) — открытого хаба для сессий Claude Code. Без хаба
он не работает: модуль подключается к ядру через точки `launch.env`, `cli` и `doctor`.

Установка на другом хабе (нужны ядро 2.6+, `agy` и вход в аккаунт Antigravity):

```
hub module add https://github.com/sanchopanda/hub-antigravity
hub antigravity setup
hub antigravity doctor
```

Настройки читаются из `HUB_CONFIG`, `HUB_CONFIG_DIR` или системного каталога `~/.config/hub-core`.
На Linux для службы с движком Claude переменную `HUB_CLAUDE` задают в systemd drop-in, затем перечитывают
конфигурацию systemd и перезапускают службу. На хабе с `"host": "codex"` оболочка `bin/codex` переводит
сессии `claude-agy` на протокол agy, остальные вызовы передаёт в `HUB_ANTIGRAVITY_REAL_CODEX` или обычный Codex.
Если у хаба уже есть оболочка Codex, она должна передать вызовы с маршрутом `HUB_MOD_ANTIGRAVITY_ROUTE=1`
в `$HUB_MOD_ANTIGRAVITY_ROOT/codex_bridge.py`. При обновлении модуль переносит установленные записи хука
и MCP в конфигурации agy на актуальную версию.

Спека: `~/.hub-personal/antigravity-research/spec.md`. Версия 0.3.4 — модуль включён в хабе (`HUB_CLAUDE` в службе,
`agy 1.5.18`, `setup` выполнен). Скиллы agy настроены глобально (`~/.gemini/config/skills.json`), исполнители и
мини-хабы идут через мост, их запреты держит хук agy; в верхней полосе страницы хаба работает веб-виджет остатка квоты.

## Как это устроено
- Модель сессии `claude-agy` (id модели agy — `hub.json` → `antigravity.models`, «default» — agy по умолчанию).
  Только ручной выбор, лестница daybudget и ворота пула poolpass такие сессии не трогают.
  На хабе Codex модель задаётся как `codex/claude-agy`.
- Ядро запускает `bin/claude` (`HUB_CLAUDE` в службе хаба). Для обычных сессий — одна проверка и `exec` настоящего
  claude (`bin/real-claude` или `~/.local/bin/claude`). Для `claude-agy` — `bridge.py`: один долгий процесс `agy -p=`
  со stream-json, события agy переводятся в события Claude Code, хуки ядра мост зовёт сам.
- `launch.env` модуля ставит `HUB_MOD_ANTIGRAVITY_ROUTE=1|handoff`, `_MODEL`, `_PYTHON`, `_DATA`.
- Ограничитель: ≤ `max_sessions` процессов agy (`flock`; в `hub.json` сейчас 5), блок по квоте и входу (`state.json`),
  свои потолки (`caps`), тихие часы (`quiet`). Исполнители на `claude-agy` включены (`antigravity.executors`,
  по умолчанию `true`). Учёт: `moddata/antigravity/usage.jsonl`.
- Режимы разрешений: `bypassPermissions` → `--dangerously-skip-permissions`; `plan` → `--mode plan`;
  `acceptEdits`/`default` → `--mode accept-edits`; `auto` → `--mode auto`. В headless-режиме (`-p=`) при `accept-edits`
  запись внутри рабочей папки проходит, а команды и запись наружу требуют подтверждения и возвращают `denied_actions`
  (мост переводит это в понятный отказ хода). В `plan` запись блокирует сам режим, а `PreToolUse`-хук держит запреты.
- Запреты ядра (`--disallowedTools`) у agy флага не имеют: `deny.py` переводит их в правила сессии
  (`moddata/antigravity/rules/<uuid>.json`), их держит `PreToolUse`-хук `agy_guard.py` — файлы в папке мини-хаба
  (`write_file`, `write_to_file`, `edit_file`, `replace_file_content`, `multi_replace_file_content`, `sed_file`,
  блокноты и команды записи), `git commit`/`push`, блокноты (`edit_notebook`, `notebook_edit`, `notebook_execution`),
  вопрос владельцу, `pkill`/`killall`/`pgrep`. Что перевести нельзя — сессия идёт в Claude (`route.decide`).
  Постоянно для всех сессий хаба хук запрещает инструменты порождения агентов (`invoke_subagent`, `define_subagent`,
  `browser_subagent`, `run_workflow`, `swarm`, `manage_subagents`), так как подагенты работают с другим
  `conversationId` и обходят правила сессии, `send_message` (достаёт до чужих бесед `agy` по `conversationId`),
  а также запись в сам слой запретов (`rules/`, `~/.gemini/config`).
  Ручной `agy` владельца хук не трогает. Работает и на новой сессии, и после `--resume` (передача от Claude → agy и повторный запуск).
- `deepseek_mcp.py` — MCP-сервер `deepseek_ask`; запасной путь без MCP — `bin/deepseek-ask`. Ключ берётся из
  `engines.json` или файла `deepseek-key` в конфигурации хаба с правами `0600`. Деньги — `hub budget --json`,
  потолок инструмента `antigravity.deepseek.day_usd` (0,5 $).
- `sweep.py` — задание расписания: на время блока уводит сессии agy туда, куда ведёт лестница, потом возвращает.
- `handoff.py` — выжимка ходов при смене движка (`agy` → Claude/DeepSeek: оболочка; обратно: мост).
- Для Codex `codex_bridge.py` хранит собственную ветку agy по идентификатору сессии хаба. При переходе с
  Codex открывается отдельная беседа agy, при возвращении на Codex сохраняется его исходная ветка. Режимы
  с интерактивным подтверждением каждого действия и правила проекта `deny/ask` пока не поддержаны:
  адаптер отказывает в запуске. Связные сессии также не запускаются через agy.
- `quota.py` + `web/main.js` + `web/quota.css` — веб-виджет квоты Antigravity в верхней полосе страницы хаба рядом с
  балансом DeepSeek (ключ снимка `antigravity_quota`, слот `topbar`). Фоновый поток опрашивает `agy -p "/usage" --output-format json`
  с кэшем не дольше 5 минут (`moddata/antigravity/quota.json`), показывает две строки — `Gemini 5ч` и `Gemini неделя`
  (процент остатка и время сброса по Москве; при отсутствии времени сброса оно не выводится). Если `agy` не ответил
  или кэш устарел — показывает прочерк `—`, не ошибку. Лимит основного движка хаб показывает отдельно.

## Файлы
`module.json`, `main.py` (`launch.env`, `cli`, `doctor`, `owner.notify` о блоке, опрос квоты), `agycli.py` (`hub antigravity`),
`quota.py`, `web/main.js`, `web/quota.css`, `store.py`, `route.py`, `deny.py`, `translate.py`, `hooks.py`, `bridge.py`,
`handoff.py`, `agy_guard.py`, `deepseek_mcp.py`, `sweep.py`, `setup.py`, `bin/claude`, `bin/deepseek-ask`, `tests/`.

## Проверка
- Состояние в живом хабе: `hub antigravity status` и `hub antigravity doctor`.
- Тесты модуля (гонять здесь со снятыми `ANTHROPIC_*` и `CLAUDE_CODE_OAUTH_TOKEN`):
  `cd ~/.hub-personal/ext/antigravity/tests && env -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u CLAUDE_CODE_OAUTH_TOKEN python3 -W ignore -m unittest test_module test_bridge test_shim test_deepseek test_stand test_quota`
- Процесс модуля с ядром: `~/.hub-personal/bin/python3 -W ignore ~/.hub-personal/scratch/modules-back/check_antigravity.py`
- Стенды соседей: `check_daybudget.py`, `check_switch.py`, `check_poolpass.py` там же.

## Состояние, повторная установка и откат
Сейчас модуль включён и работает: `agy` стоит и залогинен, модуль подключён, `hub antigravity setup` выполнен,
`HUB_CLAUDE` прописан в `~/Library/LaunchAgents/com.hub-personal.plist`, модель `claude-agy` → `default`.

Если нужно подключить автоувод сессий при блоке квоты/входа:
```sh
hub schedule add --id antigravity-sweep --every 5m --command "python3 ~/.hub-personal/ext/antigravity/sweep.py"
```

Установка с нуля на чистую машину:
1. Установить `agy` и войти в аккаунт (`agy` в Терминале).
2. `hub module add ~/.hub-personal/ext/antigravity` → `hub antigravity setup` → прописать `antigravity.models` в `hub.json`.
3. Добавить `HUB_CLAUDE=~/.hub-personal/ext/antigravity/bin/claude` в plist службы хаба и перезапустить хаб.

Откат (порядок важен):
1. `hub antigravity sweep --all-back` (или `python3 sweep.py --all-back`) — перевести сессии с `claude-agy*` обратно.
2. Убрать `HUB_CLAUDE` из plist службы и перезапустить хаб.
3. `hub module remove antigravity` и `hub antigravity setup --undo`.

## Допущения «ПРОБА»
Оставшиеся непроверенные живьём места (живая строка ответа при реальном 429/исчерпании квоты и платный вызов `deepseek_ask`): `grep -n ПРОБА *.py` (спека §10).
