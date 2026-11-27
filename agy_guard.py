#!/usr/bin/env python3
"""Хук PreToolUse для agy (~/.gemini/config/hooks.json): запреты сессий хаба там, где у Claude Code их держит флаг
--disallowedTools (§5.3). agy флага запретов не имеет: мост кладёт правила сессии в <данные модуля>/rules/<uuid>.json
(deny.translate), хук узнаёт сессию по conversationId -> convs.json и отказывает.

Вход agy — {"toolCall":{"name","args"},"conversationId",…} (docs/hooks.md). Поля читаем и в старом написании
(tool_name, tool_input) — на случай другой версии. Ручной agy владельца (беседа не из convs.json) хук не трогает.
Вывод отказа — {"decision":"deny","reason":…}; сбой хука молчит и пропускает.
"""
import json
import os
import re
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deny  # noqa: E402
import store  # noqa: E402

# pkill / killall / pgrep по образцу и в любом виде пути: hosts/common.DENY (macOS: pkill -f убивал всё с именем пользователя)
KILL = re.compile(r"(^|[\s;&|(`$])(?:/usr/bin/|/bin/)?(pkill|killall|pgrep)(?=\s|$)")
REASON = ("Не убивай процессы по образцу ({tool}): на macOS это задевает чужие процессы. Останови свой процесс по pid "
          "(kill <pid>), который запустил сам.")


def conversation(body):
    for k in ("conversationId", "conversation_id", "conversation"):
        if isinstance(body.get(k), str):
            return body[k]
    return None


def tool_call(body):
    """(имя шага, аргументы) из входа хука: {"toolCall":{…}} у agy, поля Claude Code — как запасной вариант."""
    tc = body.get("toolCall") if isinstance(body.get("toolCall"), dict) else {}
    name = str(tc.get("name") or body.get("tool_name") or body.get("toolName") or body.get("tool") or "").lower()
    args = tc.get("args")
    if args is None:
        args = body.get("tool_input") or body.get("toolInput") or body.get("input") or body.get("parameters") or {}
    return name, args


def project_denial(paths):
    """Запреты и запросы проекта проверяются при каждом вызове инструмента."""
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return "Antigravity: некорректный список правил проекта"
    for path in paths:
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            permissions = data.get("permissions") or {}
            if not isinstance(permissions, dict):
                raise ValueError("permissions")
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, AttributeError):
            return "Antigravity: не удалось прочитать правила проекта"
        if permissions.get("deny") or permissions.get("ask"):
            return "Antigravity: правила проекта deny/ask пока не поддерживаются"
    return None


def _normalize_questions(args):
    """Вопросы из ask_question -> список словарей для item/tool/requestUserInput."""
    args = args if isinstance(args, dict) else {}
    raw = args.get("questions") if isinstance(args.get("questions"), list) else (
        [args] if args.get("question") or args.get("title") else []
    )
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("question") or item.get("title") or "").strip()
        if not text:
            continue
        opts = []
        for opt in item.get("options") or []:
            if isinstance(opt, dict):
                label = str(opt.get("label") or opt.get("value") or "").strip()
                desc = str(opt.get("description") or "").strip()
            else:
                label, desc = str(opt).strip(), ""
            if label:
                opts.append({"label": label, "description": desc})
        out.append({
            "id": f"q{len(out) + 1}",
            "header": str(item.get("header") or "")[:12],
            "question": text,
            "options": opts,
        })
        if len(out) >= 4:
            break
    return out


def _forward_ask(rules, args):
    """Передать ask_question в codex_bridge.py для карточки AskUserQuestion в хабе."""
    sock_path = rules.get("ask_sock")
    if not isinstance(sock_path, str) or not sock_path:
        return {"decision": "deny", "reason": "Antigravity: инструмент ask_question этой сессии запрещён"}
    questions = _normalize_questions(args)
    if not questions:
        return {"decision": "deny", "reason": "Antigravity: в ask_question не переданы вопросы"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(4.0)
            conn.connect(sock_path)
            conn.sendall((json.dumps({"questions": questions}, ensure_ascii=False) + "\n").encode("utf-8"))
            buf = bytearray()
            while len(buf) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in buf:
                    break
        resp = json.loads(bytes(buf).splitlines()[0].decode("utf-8"))
    except (OSError, ValueError, IndexError):
        return {"decision": "deny", "reason": "Antigravity: не удалось передать карточку вопроса в мост хаба"}
    status = resp.get("status")
    text = str(resp.get("text") or "").strip()
    if status == "detached":
        hint = f" ({text})" if text else ""
        return {"decision": "deny", "reason": f"Карточка вопроса отправлена владельцу в хаб{hint}. "
                "НЕ вызывай ask_question повторно и НЕ дублируй вопрос текстом — завершай ход, ответ владельца придёт следующим сообщением."}
    if status == "wait":
        return {"decision": "deny", "reason": "Карточка вопроса открыта владельцу в хабе. "
                "НЕ вызывай ask_question повторно и НЕ дублируй вопрос текстом — остановись и дождись ответа владельца."}
    if status == "answered":
        return {"decision": "deny", "reason": f"Ответ по карточке вопроса: {text or 'без текста'}. НЕ вызывай ask_question повторно с тем же вопросом."}
    return {"decision": "deny", "reason": text or "Antigravity: не удалось показать карточку вопроса владельцу"}


def decide(body):
    """{"decision":"deny","reason":…} или None."""
    uuid = store.conv_by_id(conversation(body))
    if not uuid:
        if os.environ.get("HUB_SESSION"):
            return {"decision": "deny", "reason": "Antigravity: беседа сессии хаба ещё не распознана"}
        return None       # не сессия хаба: ручной agy владельца
    tool, args = tool_call(body)
    cwd = body.get("cwd") if isinstance(body.get("cwd"), str) else None
    rules = deny.read_rules(uuid)
    if os.environ.get("HUB_ON_CODEX") == "1" and "project_settings" not in rules:
        return {"decision": "deny", "reason": "Antigravity: нет проверенных правил сессии"}
    why = (project_denial(rules.get("project_settings")) if "project_settings" in rules else None)
    why = why or deny.subagent_hit(tool) or deny.check(rules, tool, args, cwd) or deny.guard_hit(tool, args, cwd)
    if why:
        return {"decision": "deny", "reason": why}
    if tool == deny.ASK_TOOL:
        return _forward_ask(rules, args)
    if not tool or tool in deny.SHELL_TOOLS:
        m = KILL.search(deny.command_of(args))
        if m:
            return {"decision": "deny", "reason": REASON.format(tool=m.group(2))}
    return None


def main():
    try:
        body = json.loads(sys.stdin.read() or "{}")
        out = decide(body if isinstance(body, dict) else {})
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
    except Exception:   # noqa: BLE001
        if os.environ.get("HUB_SESSION"):
            sys.stdout.write(json.dumps({"decision": "deny", "reason": "Antigravity: ошибка проверки ограничений"}))


if __name__ == "__main__":
    main()
    sys.exit(0)
