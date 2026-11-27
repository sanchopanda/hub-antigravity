"""Запреты ядра (`--disallowedTools`) как правила agy (§5.3): их держит PreToolUse-хук agy_guard.py.

У Claude Code запрет — флаг ядра; у agy такого флага нет. Здесь перевод: запись ядра -> правило agy
- "tools" — имя шага agy (run_command, edit_notebook, ask_question …),
- "commands" — git commit / git push в любой записи (`git -C . commit`),
- "paths" — правки в папке мини-хаба (core/minihub.deny: Edit(/путь/**)).
Что перевести нельзя — сессия идёт в Claude (route.decide): выдуманный эквивалент опаснее отказа.
"""
import json
import os
import re

import store

# записи ядра, которым у agy нечего запрещать: этих инструментов у agy нет
NOTHING = {"SendMessage", "ListAgents", "EnterWorktree"}
# инструменты порождения агентов и оркестрации: шаги подагентов идут с другим conversationId и обходят хук (§5)
SUBAGENT_TOOLS = {
    "invoke_subagent",
    "define_subagent",
    "browser_subagent",
    "run_workflow",
    "swarm",
    "manage_subagents",
}
# межсессионная пересылка agy: живая проба 27.11 показала, что send_message достаёт до чужих бесед agy по conversationId
MESSAGING_TOOLS = {"send_message"}
NOTEBOOK_TOOLS = ("edit_notebook", "notebook_edit", "notebook_execution")
NOTEBOOK_TOOL = "edit_notebook"
# инструменты agy, которые пишут файлы: путь лежит в аргументах вызова (живая проба 27.11: write_file, edit_file;
# в /tools у agy также write_to_file, replace_file_content, multi_replace_file_content, sed_file, notebook_edit, notebook_execution)
WRITE_TOOLS = {
    "write_file",
    "write_to_file",
    "edit_file",
    "replace_file_content",
    "multi_replace_file_content",
    "sed_file",
    "file_change",
    "write_blob",
    "move",
    "delete_directory",
    *NOTEBOOK_TOOLS,
}
SHELL_TOOLS = {"run_command", "bash", "shell", "execute_command", "run_shell_command", "terminal"}
GIT_TOOL = "git_commit"       # отдельный шаг agy: git commit
ASK_TOOL = "ask_question"
PATH_KEYS = {
    "path", "filepath", "file_path", "filename", "file",
    "targetfile", "target_file", "targetpath", "target_path",
    "absolutepath", "absolute_path", "notebookpath", "notebook_path",
    "dir", "directory", "dirpath", "dir_path", "directorypath", "directory_path",
    "source", "src", "destination", "dst", "dest",
    "from", "to", "oldpath", "old_path", "newpath", "new_path",
    "source_path", "sourcepath", "dest_path", "destpath", "destination_path", "destinationpath",
}

BASH = re.compile(r"^Bash\((.+)\)$")
PATH_RULE = re.compile(r"^(?:Edit|Write|MultiEdit)\((.+)\)$")
GIT_WORD = re.compile(r"(^|[\s;&|(`$])git\b[^;&|\n]*?\b(commit|push)\b")
# команда, которая пишет файл: переадресация и инструменты, которые ядро тоже считает записью (core/minihub.deny)
WRITE_HINT = re.compile(r"[^>]>>?\s*\S|(^|[\s;&|(`$])(tee|sed\s+-i|mv|cp|rm|truncate|touch|install|patch|dd)\b")


def _add(seq, item):
    if item not in seq:
        seq.append(item)


def _rule_path(inner):
    """Внутренность Edit(...): "//Users/x/**" или "/Users/x/**" -> "/Users/x" (как сравниваем пути)."""
    p = inner.strip()
    if p.endswith("/**"):
        p = p[:-3]
    p = p.rstrip("/")
    while p.startswith("//"):
        p = p[1:]
    return p


def translate(disallowed):
    """Записи --disallowedTools -> (правила agy, что перевести не удалось). Пустой второй список — сессию держит agy."""
    rules = {"tools": [], "commands": [], "paths": []}
    unmapped = []
    for entry in disallowed or []:
        e = str(entry).strip()
        if not e:
            continue
        if e in NOTHING:
            continue
        if e == "AskUserQuestion":
            _add(rules["tools"], ASK_TOOL)
        elif e == "NotebookEdit":
            for t in NOTEBOOK_TOOLS:
                _add(rules["tools"], t)
        elif e == "Bash(git commit:*)":
            _add(rules["commands"], "commit")
        elif e == "Bash(git push:*)":
            _add(rules["commands"], "push")
        else:
            m = PATH_RULE.match(e)
            if m and m[1].startswith("/"):
                _add(rules["paths"], _rule_path(m[1]))
            else:
                unmapped.append(e)
    return {"tools": rules["tools"], "commands": rules["commands"], "paths": rules["paths"]}, unmapped


def _strings(value, depth=0):
    """Все строки в аргументах вызова (живые пробы agy 1.5.18: Path, CommandLine, Recipient, TypeName)."""
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v, depth + 1)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v, depth + 1)]
    return []


def _path_strings(value, depth=0):
    """Значения полей-путей из аргументов шага; если таких полей нет — все строки (запасной вариант)."""
    found = []

    def walk(v, d):
        if d > 4:
            return
        if isinstance(v, dict):
            for k, val in v.items():
                if str(k).lower() in PATH_KEYS:
                    found.extend(_strings(val, d + 1))
                else:
                    walk(val, d + 1)
        elif isinstance(v, list):
            for item in v:
                walk(item, d + 1)

    walk(value, depth)
    return found if found else _strings(value, depth)


def command_of(args):
    """Командная строка шага run_command (у agy поле CommandLine, у вариаций — другие имена)."""
    if isinstance(args, str):
        return args
    if isinstance(args, dict):
        for k in ("CommandLine", "command", "command_line", "cmd"):
            if isinstance(args.get(k), str):
                return args[k]
    return ""


def _under(paths, values, cwd=None):
    """Есть ли среди строк путь внутри запрещённой папки (или сама папка). Относительный путь — от cwd агента."""
    for v in values:
        p = os.path.expanduser(str(v).strip())
        if not p:
            continue
        if cwd and not os.path.isabs(p):
            p = os.path.join(cwd, p)
        p = os.path.normpath(p)
        for root in paths:
            r = os.path.normpath(root)
            if p == r or p.startswith(r + os.sep):
                return root
    return None


def subagent_hit(tool):
    """Постоянные запреты сессий хаба: подагенты обходят хук и правила, а send_message достаёт до чужих бесед agy."""
    tool = str(tool or "").lower()
    if tool in SUBAGENT_TOOLS:
        return f"Antigravity: инструмент {tool} в сессиях хаба запрещён — подагенты обходят правила сессии"
    if tool in MESSAGING_TOOLS:
        return f"Antigravity: инструмент {tool} в сессиях хаба запрещён — он достаёт до чужих бесед agy; для связи используй hub send"
    return None


def check(rules, tool, args, cwd=None):
    """Текст отказа для шага agy по правилам сессии (и постоянным запретам сессий хаба) или None."""
    rules = rules if isinstance(rules, dict) else {}
    tool = str(tool or "").lower()
    why = subagent_hit(tool)
    if why:
        return why
    tools = set(rules.get("tools") or [])
    commands = set(rules.get("commands") or [])
    paths = list(rules.get("paths") or [])
    if tool and tool in tools:
        return f"Antigravity: инструмент {tool} этой сессии запрещён"
    if tool == GIT_TOOL and "commit" in commands:
        return "Antigravity: git commit этой сессии запрещён"
    if tool in WRITE_TOOLS and paths:
        root = _under(paths, _path_strings(args), cwd)
        if root:
            return f"Antigravity: правка в {root} этой сессии запрещена"
    if tool in SHELL_TOOLS:
        cmd = command_of(args)
        if commands:
            m = GIT_WORD.search(cmd)
            if m and m[2] in commands:
                return f"Antigravity: git {m[2]} этой сессии запрещён"
        if paths and cmd and WRITE_HINT.search(cmd):
            # в команде путь — только тот, что выглядит путём: голое слово (echo hi) путём не считаем, иначе запрет бьёт
            # по любой команде мини-хаба в его папке
            tokens = [t.strip("\"'") for t in re.split(r"[\s;&|()`<>]+", cmd) if re.match(r"(/|~|\.\.?/)", t)]
            root = _under(paths, tokens, cwd)
            if root:
                return f"Antigravity: запись в {root} этой сессии запрещена"
    return None


def protected_roots():
    """Где лежит сам слой запретов: правила сессий и глобальный конфиг agy (hooks.json, settings.json)."""
    return [os.path.join(store.data_dir(), "rules"), os.path.expanduser("~/.gemini/config")]


def guard_hit(tool, args, cwd=None):
    """Запись в слой запретов — отказ: иначе сессия снимет свои запреты правкой файла. Чтение не трогаем."""
    tool = str(tool or "").lower()
    if tool in WRITE_TOOLS:
        tokens = _path_strings(args)
    elif tool in SHELL_TOOLS:
        tokens = [t.strip("\"'") for t in re.split(r"[\s;&|()`<>]+", command_of(args))
                  if re.match(r"(/|~|\.\.?/)", t)]
    else:
        return None
    root = _under(protected_roots(), tokens, cwd)
    if root:
        return f"Antigravity: запись в {root} запрещена — там правила хука"
    return None


def rules_path(uuid):
    return os.path.join(store.data_dir(), "rules", f"{uuid}.json")


def write_rules(uuid, rules):
    """Правила сессии для хука: <данные модуля>/rules/<uuid>.json (хук узнаёт сессию по conversationId -> convs.json)."""
    path = rules_path(uuid)
    if not any((rules or {}).get(k) for k in ("tools", "commands", "paths")):
        try:
            os.unlink(path)      # запретов нет — хук по этой сессии молчит
        except OSError:
            pass
        return None
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def read_rules(uuid):
    data = store.read_json(rules_path(uuid)) if uuid else {}
    return data if isinstance(data, dict) else {}
