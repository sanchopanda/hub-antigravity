"""Хуки ядра для сессии на agy: мост сам вызывает команды из файла `--settings` (§5.1) — хуки agy для этого не нужны.

Ядро пишет в файл `hooks.<СОБЫТИЕ>[].hooks[].command`; здесь берутся только четыре события хаба (SessionStart,
UserPromptSubmit, Stop, SessionEnd). PreToolUse-подсказки про pkill и sleep — для инструмента Bash у Claude Code, у agy
их держит agy_guard.py. Сбой хука мост не роняет: хук хаба и у Claude Code ничего не ломает.
"""
import json
import subprocess

import store

EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
TIMEOUT = 10   # секунд, как у Claude Code (hosts/common.py: "timeout": 10)


def commands(settings_path, event):
    """Команды хука события из файла настроек ядра; нет файла или события — пустой список."""
    if event not in EVENTS or not settings_path:
        return []
    cfg = store.read_json(settings_path)
    out = []
    for group in (cfg.get("hooks") or {}).get(event) or []:
        for h in (group or {}).get("hooks") or []:
            if isinstance(h, dict) and h.get("type", "command") == "command" and isinstance(h.get("command"), str):
                out.append((h["command"], float(h.get("timeout") or TIMEOUT)))
    return out


def run(settings_path, event, payload, env, runner=subprocess.run):
    """Вызвать хуки события с JSON на stdin. -> число вызванных; ошибки глотаем (хук не ломает сессию)."""
    n = 0
    body = json.dumps({"hook_event_name": event, **payload}, ensure_ascii=False).encode("utf-8")
    for cmd, timeout in commands(settings_path, event):
        try:
            runner(cmd, shell=True, input=body, env=env, timeout=min(timeout, TIMEOUT), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)
            n += 1
        except (OSError, subprocess.SubprocessError):
            continue
    return n
