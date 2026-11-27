"""Маршрут claude-agy для хаба на Codex app-server."""

from collections import deque
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
    def __init__(self, thread, cwd, instructions, resumed, events, mode, restrictions, settings, ask_sock=None):
        super().__init__([
            "--resume" if resumed else "--session-id", thread,
            "--model", os.environ.get("HUB_MOD_ANTIGRAVITY_NAME", "claude-agy"),
            "--permission-mode", mode,
            "--disallowedTools", "SendMessage", "ListAgents", *restrictions,
        ])
        self.cwd = cwd
        self.instr = instructions
        self.events = events
        rule_path = deny.rules_path(self.uuid)
        protected = deny.read_rules(self.uuid)
        protected["project_settings"] = settings
        if ask_sock:
            protected["ask_sock"] = ask_sock
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
        self.ask_server = self.ask_sock = self.waiting_ask = None
        self.deferred_result = self.pending_answer = None

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

    def _reply_ask(self, wait, payload):
        conn = wait.get("conn") if wait else None
        if conn is None:
            return
        wait["conn"] = None
        try:
            conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def close_ask(self):
        if self.waiting_ask:
            self._reply_ask(self.waiting_ask, {"status": "error", "text": "Antigravity: сессия закрыта"})
            self.waiting_ask = None
        self.deferred_result = self.pending_answer = None
        if self.ask_server is not None:
            try:
                self.ask_server.close()
            except OSError:
                pass
            self.ask_server = None
        if self.ask_sock:
            try:
                os.unlink(self.ask_sock)
            except OSError:
                pass
            self.ask_sock = None

    def open_ask(self, ident):
        candidate = os.path.join(store.data_dir(), f"ask-{os.getpid()}-{ident[:8]}.sock")
        if len(candidate.encode("utf-8")) > 95:
            candidate = os.path.join(tempfile.gettempdir(), f"agy-ask-{os.getpid()}-{ident[:8]}.sock")
        try:
            os.unlink(candidate)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(candidate)
        os.chmod(candidate, 0o600)
        srv.listen(4)
        self.ask_server, self.ask_sock = srv, candidate
        threading.Thread(target=self._accept_asks, args=(srv,), daemon=True).start()
        return candidate

    def _accept_asks(self, srv):
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.settimeout(1.0)
                buf = bytearray()
                while len(buf) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if b"\n" in buf:
                        break
                payload = json.loads(bytes(buf).splitlines()[0].decode("utf-8"))
            except (OSError, ValueError, IndexError):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            self.events.put(("ask", (payload, conn)))

    def close(self):
        self.close_ask()
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
        ask_sock = self.open_ask(internal)
        self.session = Session(internal, cwd, instructions, resumed, self.events, modes[policy],
                               restrictions, rules.get("settings") or [], ask_sock=ask_sock)
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

    def _ask_request(self, payload, conn):
        questions = payload.get("questions") if isinstance(payload, dict) else None
        if not self.turn or not self.external_thread or not isinstance(questions, list) or not questions:
            return self._reply_ask({"conn": conn}, {"status": "error", "text": "Antigravity: нет активного хода для вопроса"})
        if self.waiting_ask:
            return self._reply_ask({"conn": conn}, {"status": "error", "text": "Antigravity: предыдущий вопрос ещё ждёт ответа"})
        rid = f"ask-{uuid.uuid4().hex[:12]}"
        self.waiting_ask = {"id": rid, "questions": questions, "conn": conn, "deadline": time.monotonic() + 1.5}
        self.send({"id": rid, "method": "item/tool/requestUserInput",
                   "params": {"threadId": self.external_thread, "questions": questions}})

    def on_client_response(self, msg):
        wait = self.waiting_ask
        if not wait or msg.get("id") != wait["id"]:
            return
        self.waiting_ask = None
        if "error" in msg:
            err = (msg.get("error") or {}).get("message") or "ошибка карточки вопроса"
            if wait.get("conn") is not None:
                return self._reply_ask(wait, {"status": "error", "text": err})
            text = f"[Ошибка карточки вопроса]: {err}"
        else:
            answers = ((msg.get("result") or {}).get("answers") or {})
            qs = wait["questions"]
            if wait.get("conn") is not None:
                texts = [str(a) for q in qs for a in ((answers.get(q["id"]) or {}).get("answers") or []) if a]
                joined = "; ".join(texts)
                status = "detached" if re.search(r"\bq\d+\b", joined) else "answered"
                return self._reply_ask(wait, {"status": status, "text": joined})
            lines = []
            for q in qs:
                vals = [str(a) for a in ((answers.get(q["id"]) or {}).get("answers") or []) if a]
                lines.append(f"- {q['question']}: {', '.join(vals) if vals else 'нет ответа'}")
            text = "[Ответ владельца на карточку вопроса]:\n" + "\n".join(lines)
        if self.deferred_result is not None:
            self.deferred_result = None
            self.session.q.put(("core", {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}))
        else:
            self.pending_answer = text

    def request(self, req):
        if "method" not in req and "id" in req:
            return self.on_client_response(req)
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
                if self.waiting_ask:
                    self._reply_ask(self.waiting_ask, {"status": "error", "text": "прервано"})
                    self.waiting_ask = None
                self.pending_answer = None
                if self.deferred_result is not None:
                    self.deferred_result = None
                    self.notify("turn/completed", turn={"id": self.turn, "status": "interrupted"})
                    self.turn = None
                    if self.pending:
                        self.start_turn(self.pending.popleft())
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
            if self.pending_answer is not None:
                answer = self.pending_answer
                self.pending_answer = None
                self.session.q.put(("core", {"type": "user", "message": {"content": [{"type": "text", "text": answer}]}}))
                return
            if self.waiting_ask is not None:
                self.deferred_result = msg
                return
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
                wait = self.waiting_ask
                if wait and wait.get("conn") is not None:
                    left = max(0.0, wait["deadline"] - time.monotonic())
                    try:
                        kind, event = self.events.get(timeout=left)
                    except queue.Empty:
                        self._reply_ask(wait, {"status": "wait"})
                        continue
                else:
                    kind, event = self.events.get()
                if kind == "request":
                    self.request(event)
                elif kind == "ask":
                    self._ask_request(*event)
                elif kind == "bridge":
                    self.on_bridge(event)
                elif kind == "bridge_error":
                    if self.turn:
                        self.pending.clear()
                        if self.waiting_ask:
                            self._reply_ask(self.waiting_ask, {"status": "error", "text": f"Сбой моста: {event}"})
                            self.waiting_ask = None
                        self.deferred_result = self.pending_answer = None
                        self.on_bridge({"type": "result", "is_error": True, "result": f"Сбой моста Antigravity: {event}"})
                    return 1
                else:
                    return 0
        finally:
            self.close()


if __name__ == "__main__":
    sys.exit(Server().run())
