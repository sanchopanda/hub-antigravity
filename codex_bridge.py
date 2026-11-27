"""Маршрут claude-agy для хаба на Codex app-server."""

from collections import deque
import fcntl
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import Bridge  # noqa: E402
import deny  # noqa: E402
import agy_guard  # noqa: E402
import store  # noqa: E402


def guard_ready():
    """Проверяет, что agy будет вызывать хук именно этой версии модуля."""
    path = Path.home() / ".gemini" / "config" / "hooks.json"
    try:
        with open(path, encoding="utf-8") as stream:
            groups = json.load(stream)["hooks"]["PreToolUse"]
        expected = (Path(__file__).resolve().parent / "agy_guard.py").resolve()
        for group in groups:
            if group.get("matcher") not in ("*", ".*"):
                continue
            for hook in group.get("hooks", []):
                if hook.get("type") != "command":
                    continue
                parts = shlex.split(hook.get("command", ""))
                if len(parts) >= 2 and Path(parts[1]).resolve() == expected and os.access(parts[0], os.X_OK):
                    return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return False


def check_project_permissions(rules):
    """Не запускать agy, пока запреты проекта не поддерживаются его хуком."""
    why = agy_guard.project_denial(rules.get("settings") or [])
    if why:
        raise ValueError(why)


def session_id(name):
    """Постоянный идентификатор сессии берётся из штатного состояния хаба."""
    executable = shutil.which("hub") or os.path.expanduser("~/.local/bin/hub")
    try:
        result = subprocess.run([executable, "status", "--json"], capture_output=True, text=True,
                                timeout=10, check=True)
        sessions = json.loads(result.stdout)["sessions"]
        return str(uuid.UUID(sessions[name]["session_id"]))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise ValueError("Не удалось определить идентификатор сессии хаба") from None


def session_branch(identity, external, clear=False):
    """Сохраняет ветку agy отдельно от идентификаторов веток Codex."""
    folder = Path(store.data_dir())
    path = folder / "codex-branches.json"
    with open(folder / "codex-branches.lock", "a", encoding="utf-8") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            raise ValueError("Файл веток Antigravity не читается") from None
        if not isinstance(data, dict):
            raise ValueError("Файл веток Antigravity повреждён")
        record = data.get(identity)
        if clear or not isinstance(record, dict):
            record = {"internal": str(uuid.uuid4()), "external": []}
        try:
            internal = str(uuid.UUID(record["internal"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("Идентификатор ветки Antigravity повреждён") from None
        if not isinstance(record.get("external"), list):
            raise ValueError("Список веток Antigravity повреждён")
        if external not in record["external"]:
            record["external"].append(external)
        data[identity] = record
        store.write_json(str(path), data)
        return internal


class Session(Bridge):
    def __init__(self, thread, cwd, instructions, resumed, events, mode, restrictions, settings):
        super().__init__([
            "--resume" if resumed else "--session-id", thread,
            "--model", os.environ.get("HUB_MOD_ANTIGRAVITY_NAME", "claude-agy"),
            "--permission-mode", mode,
            "--disallowedTools", "SendMessage", "ListAgents", "AskUserQuestion", *restrictions,
        ])
        self.cwd = cwd
        self.instr = instructions
        self.events = events
        rule_path = deny.rules_path(self.uuid)
        protected = deny.read_rules(self.uuid)
        protected["project_settings"] = settings
        store.write_json(rule_path, protected)

    def _read_core(self):
        # Сервер app-server сам кладёт разобранные сообщения в очередь моста.
        pass

    def emit(self, obj):
        self.events.put(("bridge", obj))


class Server:
    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.events = queue.Queue()
        self.pending = deque()
        self.session = self.worker = self.turn = None
        self.external_thread = None
        self.tools = {}

    def send(self, obj):
        print(json.dumps(obj, ensure_ascii=False), file=self.stdout, flush=True)

    def reply(self, request, result):
        if "id" in request:
            self.send({"id": request["id"], "result": result})

    def error(self, request, text, code=-32602):
        if "id" in request:
            self.send({"id": request["id"], "error": {"code": code, "message": text}})

    def notify(self, method, **params):
        self.send({"method": method, "params": {"threadId": self.external_thread, **params}})

    def item(self, method, kind, **fields):
        self.notify("item/" + method, turnId=self.turn,
                    item={"id": uuid.uuid4().hex, "type": kind, **fields})

    def close(self):
        if self.worker is not None:
            self.session.q.put(("term",))
            self.worker.join()
        self.session = self.worker = self.turn = self.external_thread = None

    def start_session(self, request, params, resumed):
        if self.turn:
            return self.error(request, "A turn is in progress")
        if os.environ.get("HUB_MOD_ANTIGRAVITY_CODEX_WORK") != "1":
            return self.error(request, "Antigravity в хабе Codex доступен для обычных рабочих сессий")
        cwd = params.get("cwd") or os.getcwd()
        if not isinstance(cwd, str) or not os.path.isdir(cwd):
            return self.error(request, "Working directory does not exist")
        try:
            thread = str(uuid.UUID(params.get("threadId"))) if resumed else str(uuid.uuid4())
        except (TypeError, ValueError, AttributeError):
            return self.error(request, "no rollout found")
        self.close()
        instructions = params.get("developerInstructions") or ""
        name = os.environ.get("HUB_SESSION")
        if not name:
            return self.error(request, "Не указана сессия хаба")
        try:
            if not guard_ready():
                raise ValueError("Хук ограничений agy не установлен или указывает на другую версию")
            rules_path = Path(os.environ["HUB_STATE_DIR"]) / "sessions" / name / "codex-rules.json"
            with open(rules_path, encoding="utf-8") as stream:
                rules = json.load(stream)
            restrictions = rules["deny"]
            if not isinstance(restrictions, list) or not all(isinstance(x, str) for x in restrictions):
                raise ValueError("Некорректные ограничения Codex")
            if rules.get("liaison"):
                raise ValueError("Связная сессия не поддерживается")
            check_project_permissions(rules)
            _, unmapped = deny.translate(restrictions)
            if unmapped:
                raise ValueError("Ограничения не поддерживаются: " + ", ".join(unmapped))
            policy = (params.get("approvalPolicy"), params.get("sandbox"))
            modes = {
                ("never", "danger-full-access"): "bypassPermissions",
                ("on-request", "workspace-write"): "acceptEdits",
                ("never", "read-only"): "plan",
                ("on-request", "read-only"): "plan",
            }
            if policy not in modes:
                raise ValueError("Режим разрешений Codex не поддерживается")
            identity = session_id(name)
            internal = session_branch(identity, thread, clear=params.get("sessionStartSource") == "clear")
        except (OSError, ValueError, KeyError) as exc:
            return self.error(request, f"Не удалось сохранить ветку: {exc}")
        self.external_thread = thread
        self.session = Session(internal, cwd, instructions, resumed, self.events, modes[policy],
                               restrictions, rules.get("settings") or [])
        self.worker = threading.Thread(target=self.run_session, daemon=True)
        self.worker.start()
        self.reply(request, {"thread": {"id": thread}, "cwd": cwd, "model": self.session.model_name,
                             "modelProvider": "antigravity"})

    def run_session(self):
        try:
            self.session.run()
        except Exception as exc:
            self.events.put(("bridge_error", type(exc).__name__))

    @staticmethod
    def input_items(params):
        content = []
        context = params.get("additionalContext") or {}
        if not isinstance(context, dict):
            raise ValueError("Некорректный дополнительный контекст")
        for item in context.values():
            if isinstance(item, dict) and isinstance(item.get("value"), str):
                content.append({"type": "text", "text": item["value"]})
        for item in params.get("input") or []:
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                content.append({"type": "text", "text": item["text"]})
            elif item.get("type") == "localImage" and isinstance(item.get("path"), str):
                content.append({"type": "text", "text": "[Картинка: " + item["path"] + "]"})
            else:
                raise ValueError("Antigravity принимает текст и локальные изображения")
        if not any(item["text"].strip() for item in content):
            raise ValueError("Пустой ввод")
        return content

    def start_turn(self, content, request=None):
        self.turn = uuid.uuid4().hex
        self.tools.clear()
        turn = {"id": self.turn, "status": "inProgress"}
        if request is not None:
            self.reply(request, {"turn": turn})
        self.notify("turn/started", turn=turn)
        self.item("started", "userMessage", content=content)
        self.session.q.put(("core", {"type": "user", "message": {"content": content}}))

    def request(self, req):
        method, p = req.get("method"), req.get("params") or {}
        if method == "initialize":
            self.reply(req, {"userAgent": "hub-antigravity/0.3.0"})
        elif method == "skills/extraRoots/set":
            self.reply(req, {})
        elif method in ("thread/start", "thread/resume"):
            self.start_session(req, p, method == "thread/resume")
        elif method in ("turn/start", "turn/steer"):
            if not self.session or p.get("threadId") != self.external_thread:
                return self.error(req, "Thread not found")
            try:
                content = self.input_items(p)
            except ValueError as exc:
                return self.error(req, str(exc))
            if method == "turn/steer":
                if not self.turn or p.get("expectedTurnId") != self.turn:
                    return self.error(req, "No matching active turn")
                self.pending.append(content)
                return self.reply(req, {"turnId": self.turn})
            if self.turn:
                return self.error(req, "A turn is in progress")
            self.start_turn(content, req)
        elif method == "turn/interrupt":
            if self.turn and p.get("turnId") == self.turn:
                ctl = {"type": "control_request", "request_id": "interrupt", "request": {"subtype": "interrupt"}}
                self.session.q.put(("core", ctl))
            self.reply(req, {})
        elif method and "id" in req:
            self.error(req, f"Antigravity: {method} не поддерживается", -32601)

    def on_bridge(self, msg):
        if not self.turn:
            return
        kind = msg.get("type")
        if kind in ("assistant", "user"):
            for part in (msg.get("message") or {}).get("content") or []:
                if part.get("type") == "text" and part.get("text"):
                    self.item("completed", "agentMessage", text=part["text"])
                elif part.get("type") == "tool_use":
                    ident = part.get("id") or uuid.uuid4().hex
                    item = {"id": ident, "type": "mcpToolCall", "server": "antigravity",
                            "tool": part.get("name") or "tool", "arguments": part.get("input") or {}}
                    self.tools[ident] = item
                    self.notify("item/started", turnId=self.turn, item=item)
                elif part.get("type") == "tool_result":
                    ident = part.get("tool_use_id")
                    item = self.tools.pop(ident, None)
                    if item:
                        item = {**item, "status": "failed" if part.get("is_error") else "completed"}
                        item["error" if part.get("is_error") else "result"] = part.get("content")
                        self.notify("item/completed", turnId=self.turn, item=item)
        elif kind == "result":
            turn = {"id": self.turn, "status": "failed" if msg.get("is_error") else "completed",
                    "durationMs": msg.get("duration_ms")}
            if msg.get("is_error"):
                turn["error"] = {"message": msg.get("result") or "Ошибка Antigravity"}
            self.notify("turn/completed", turn=turn)
            self.turn = None
            if self.pending:
                self.start_turn(self.pending.popleft())

    def read(self):
        try:
            for line in self.stdin:
                try:
                    request = json.loads(line)
                except ValueError:
                    continue
                if isinstance(request, dict):
                    self.events.put(("request", request))
        finally:
            self.events.put(("eof", None))

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self.events.put(("eof", None)))
        threading.Thread(target=self.read, daemon=True).start()
        try:
            while True:
                kind, event = self.events.get()
                if kind == "request":
                    self.request(event)
                elif kind == "bridge":
                    self.on_bridge(event)
                elif kind == "bridge_error":
                    if self.turn:
                        self.pending.clear()
                        self.on_bridge({"type": "result", "is_error": True, "result": f"Сбой моста Antigravity: {event}"})
                    return 1
                else:
                    return 0
        finally:
            self.close()


if __name__ == "__main__":
    sys.exit(Server().run())
