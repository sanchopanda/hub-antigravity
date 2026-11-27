#!/usr/bin/env python3
"""Мост: процесс сессии хаба на Antigravity (§1.3, вариант A). Ядро запускает его вместо claude (оболочка bin/claude).

Говорит с ядром как Claude Code: stream-json в обе стороны. С agy — одним долгим процессом
`agy -p --input-format stream-json --output-format stream-json`. Хуки хаба (SessionStart, UserPromptSubmit, Stop,
SessionEnd) вызывает сам, по команде из файла `--settings`. Если сессия не для agy (связной, мини-хаб, нет модели agy) —
exec настоящего claude с теми же аргументами.

Мост проверен живыми пробами на agy 1.5.18 (§5.3, §10); оставшиеся допущения (живой 429 квоты) помечены в route.py.
"""
import base64
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # python -I не добавляет папку скрипта
import deny  # noqa: E402
import handoff  # noqa: E402
import hooks  # noqa: E402
import route  # noqa: E402
import store  # noqa: E402
import translate  # noqa: E402

KEEPALIVE_S = 60     # пока agy молчит во время хода, раз в минуту строка `system/status`: ядро не считает сессию зависшей
SLASH = ("/compact", "/recap", "/clear", "/model", "/effort")
# /compact и /recap agy 1.5.18 сам раскрывает в print-режиме (живая проба 27.11): ход проходит, ответ — result
SLASH_PASS = ("/compact", "/recap")
IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def save_image(block, image_dir):
    """Картинку ядра — файлом на диск: блок image в stream-json agy не берёт («only text»), а файл агент читает
    через read_file (живая проба 27.11: ответил по цвету картинки). -> путь или None."""
    src = block.get("source") if isinstance(block.get("source"), dict) else {}
    ext = IMAGE_EXT.get(str(src.get("media_type") or ""))
    if src.get("type") != "base64" or not ext or not image_dir:
        return None
    try:
        data = base64.b64decode(str(src.get("data") or ""))
    except (ValueError, TypeError):
        return None
    if not data:
        return None
    path = os.path.join(image_dir, hashlib.sha256(data).hexdigest()[:16] + "." + ext)
    try:
        os.makedirs(image_dir, mode=0o700, exist_ok=True)
        if not os.path.exists(path):
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
    except OSError:
        return None
    return path


def text_of(content, image_dir=None):
    """Текст сообщения ядра: строка или блоки; картинки — файлом и ссылкой на него (agy читает файл сам)."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(str(b.get("text") or ""))
        elif b.get("type") == "image":
            path = save_image(b, image_dir)
            if path:
                parts.append(f"[Картинка: {path} — посмотри этот файл]")
            else:
                parts.append("[картинка не передана: неизвестный формат]")
    return "\n".join(parts)


class Bridge:
    def __init__(self, argv, env=None, stdin=None, stdout=None, agy=None, clock=time.time, mono=time.monotonic):
        self.env = dict(os.environ if env is None else env)
        self.argv = argv
        self.p = route.parse(argv)
        self.uuid = self.p["session_id"] or "no-session"
        # запреты ядра (--disallowedTools) — в правила agy для хука agy_guard.py (§5.3); у agy флага запретов нет
        self.rules, self.unmapped = deny.translate(self.p["disallowed"])
        if self.uuid != "no-session":
            deny.write_rules(self.uuid, self.rules)
        self.session = self.env.get("HUB_SESSION") or self.p["name"] or ""
        self.model_name = self.p["model"] or "claude-agy"
        # "default" — метка модуля для «agy по умолчанию» (пустую строку ядро вырезает из окружения): флаг --model не нужен
        model = self.env.get("HUB_MOD_ANTIGRAVITY_MODEL", "")
        self.model_id = "" if model in ("", "default") else model
        self.cfg = store.settings()
        self.stdin, self.stdout = stdin or sys.stdin, stdout or sys.stdout
        self.agy_path = agy or self.env.get("HUB_MOD_ANTIGRAVITY_AGY") or shutil.which("agy", path=self.env.get("PATH")) \
            or os.path.expanduser("~/.local/bin/agy")
        self.clock, self.mono = clock, mono
        self.cwd = os.getcwd()
        self.q = queue.Queue()
        self.out_lock = threading.Lock()
        self.child, self.gen, self.slot = None, 0, None
        self.stderr_tail = deque(maxlen=20)
        self.turn, self.pending, self.eof = None, [], False
        self.cum = {}            # накопительный usage текущего процесса agy
        self.heard = self.mono()
        self.last_keepalive = self.mono()
        self.instr = self._read_instructions()
        self.interrupted = False
        self.keepalive = float(self.env.get("HUB_MOD_ANTIGRAVITY_KEEPALIVE") or KEEPALIVE_S)
        self.idle_limit = float(self.env.get("HUB_MOD_ANTIGRAVITY_IDLE") or self.cfg["idle_limit_s"])

    # -- ввод-вывод ----------------------------------------------------------------------------------
    def emit(self, obj):
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.out_lock:
            self.stdout.write(line)
            self.stdout.flush()

    def hook(self, event, **payload):
        hooks.run(self.p["settings"], event, {"session_id": self.uuid, "cwd": self.cwd, **payload}, self.env)

    def _read_instructions(self):
        if not self.p["append_file"]:
            return ""
        try:
            with open(self.p["append_file"], encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    # -- запуск и конец ------------------------------------------------------------------------------
    def run(self):
        try:
            signal.signal(signal.SIGTERM, lambda *_: self.q.put(("term",)))
        except ValueError:   # не главный поток (тест)
            pass
        threading.Thread(target=self._read_core, daemon=True).start()
        self.emit({"type": "system", "subtype": "init", "session_id": self.uuid, "model": self.model_name,
                   "permissionMode": self.p["mode"] or "bypassPermissions", "cwd": self.cwd,
                   "claude_code_version": "antigravity-bridge-0.1.0", "mcp_servers": [], "plugins": [],
                   "slash_commands": [], "skills": [], "tools": []})
        self.hook("SessionStart", source="resume" if self.p["resume"] else "startup")
        code = 0
        while True:
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                item = ("tick",)
            kind = item[0]
            if kind == "core":
                self.on_core(item[1])
            elif kind == "agy":
                if item[1] == self.gen:
                    self.on_agy(item[2])
            elif kind == "agy_eof":
                if item[1] == self.gen:
                    self.on_agy_eof(item[2])
            elif kind == "core_eof":
                self.eof = True
            elif kind == "term":
                self.eof, code, self.pending = True, 0, []
                self.kill_child()
                if self.turn:
                    self.finish_error("прервано: сессию останавливают", kind="interrupted")
            elif kind == "tick":
                self.on_tick()
            if self.eof and not self.turn:
                break
        self.shutdown()
        return code

    def shutdown(self):
        self.hook("SessionEnd", reason="other")
        self.stop_child(graceful=True)

    def _read_core(self):
        try:
            for line in self.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    self.q.put(("core", obj))
        finally:
            self.q.put(("core_eof",))

    # -- сообщения ядра ------------------------------------------------------------------------------
    def image_dir(self):
        """Куда класть картинки сессии: папка данных модуля, имя сессии без посторонних знаков."""
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.session or self.uuid)[:40].strip(".-") or "session"
        return os.path.join(store.data_dir(), "images", name)

    def on_core(self, msg):
        kind = msg.get("type")
        if kind == "user":
            text = text_of((msg.get("message") or {}).get("content"), self.image_dir())
            if not text.strip():
                return
            head = text.strip().split()[0] if text.strip() else ""
            if head in SLASH and not self.turn:
                if head in SLASH_PASS:
                    return self.start_turn([text], slash=head)
                return self.refuse_slash(head)
            if self.turn:
                self.pending.append(text)
            else:
                self.start_turn([text])
        elif kind == "control_request":
            self.on_control(msg)
        # control_response ядра нам не нужны: мы ничего у ядра не спрашиваем. В agy control_* НЕ уходит: у agy это код выхода 2

    def on_control(self, msg):
        rid, sub = msg.get("request_id"), (msg.get("request") or {}).get("subtype")

        def answer(ok, response=None, error=None):
            body = {"subtype": "success" if ok else "error", "request_id": rid}
            if ok:
                body["response"] = response or {}
            else:
                body["error"] = error
            self.emit({"type": "control_response", "response": body})
        if sub == "interrupt":
            answer(True)
            if self.turn:
                # живая проба agy 1.5.18 (§10 п. 8): SIGINT завершает весь процесс (status=ERROR, error="interrupted", rc=1) —
                # снимаем процесс; следующий ход идёт с --conversation
                self.interrupted = True
                self.kill_child()
                self.finish_error("прервано", kind="interrupted")
        elif sub == "mcp_status":
            answer(True, {"mcpServers": [{"name": n, "status": "connected"} for n in (self.turn.mcp if self.turn else [])]})
        else:
            answer(False, error=f"Unsupported control request subtype: {sub}")

    def refuse_slash(self, head):
        """Команды, которых у agy нет (/clear, /model, /effort): result без хода и с отказом; хуки не зовём — это не ход."""
        text = f"Antigravity: {head} недоступна"
        self.emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
                   "result": text, "session_id": self.uuid, "duration_ms": 0, "total_cost_usd": None,
                   "usage": {"input_tokens": 0, "output_tokens": 0}})

    # -- ход -------------------------------------------------------------------------------------------
    def gate(self):
        """Текст отказа начать ход или None (§3, §4): блок, исполнитель, тихие часы, потолки, места."""
        if self.instr and route.EXECUTOR_MARK in self.instr and not self.cfg.get("executors"):
            return "Antigravity: исполнители на claude-agy выключены (hub.json, antigravity.executors)"
        b = store.block(self.clock())
        if b:
            return b.get("text") or "Antigravity: блок"
        if store.in_quiet(self.cfg.get("quiet"), self.clock()):
            return f"Antigravity: тихие часы ({self.cfg['quiet']}), ход не начат"
        why = store.caps_hit(self.cfg, self.clock())
        if why:
            return f"Antigravity: {why}"
        if not self.child_alive() and not self.slot:
            slot, holders = store.take_slot(self.session, self.cfg["max_sessions"])
            if not slot:
                return f"Antigravity занят: {', '.join(holders) or 'все места'} (не больше {self.cfg['max_sessions']} процессов agy)"
            self.slot = slot
        if not os.access(self.agy_path, os.X_OK):
            self.release_slot()
            return "Antigravity: agy не установлен (hub antigravity doctor)"
        return None

    def start_turn(self, texts, slash=None):
        self.cfg = store.settings()      # настройки (тихие часы, потолки, исполнители) — по состоянию на начало хода
        joined = "\n".join(texts)
        if not slash:
            self.hook("UserPromptSubmit", prompt=joined)
        else:
            # у команды метка ядра стоит в конце строки (flow.py): agy получает чистую команду
            joined = re.sub(r"\s*\[hub:m\d+[^\]\n]*\]\s*$", "", joined).strip()
        self.turn = translate.Turn(self.uuid, self.model_name, self.cum)
        self.turn.request, self.turn.began, self.turn.sent = joined, self.mono(), self.clock()
        self.turn.slash = slash           # /compact или /recap: ядро ждёт result с num_turns 0, а не ход
        self.interrupted = False
        why = self.gate()
        if why:
            return self.finish_error(why, kind="refused")
        try:
            self.ensure_child()
            self.write_child(joined if slash else self.agy_prompt(joined))
        except OSError as e:
            self.kill_child()
            return self.finish_error(f"Antigravity: не запустился ({type(e).__name__})", kind="crash")
        self.heard = self.mono()

    def agy_prompt(self, joined):
        """Что уходит в agy: сообщения владельца; в новой беседе — ещё инструкции хаба (§5.4) и выжимка чужих ходов (§6)."""
        rec = store.conv(self.uuid)
        h = hashlib.sha256(self.instr.encode("utf-8")).hexdigest()[:16]
        parts = []
        if self.instr:
            if not rec.get("conv"):
                parts.append(f"<hub-instructions>\n{self.instr}\n</hub-instructions>")
            elif rec.get("instr") != h:
                parts.append(f"<hub-instructions-updated>\nИнструкции хаба обновлены. Новый полный текст:\n{self.instr}\n"
                             "</hub-instructions-updated>")
        self.turn.instr_hash = h
        items = handoff.since_agy_items(self.uuid, float(rec.get("last_agy_at") or 0))
        if items:
            parts.append(handoff.FIRST.format(body=handoff.summarize(items)).rstrip())
        parts.append(joined)
        return "\n\n".join(parts)

    def child_alive(self):
        return self.child is not None and self.child.poll() is None

    def ensure_child(self):
        if self.child_alive():
            return
        self.child = None
        rec = store.conv(self.uuid)
        argv = route.agy_argv(self.agy_path, self.p, self.model_id, rec.get("conv"))
        self.cum, self.stderr_tail = {}, deque(maxlen=20)
        self.turn.prev_cum = {}
        self.gen += 1
        gen = self.gen
        self.child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      cwd=self.cwd, env=route.clean_env(self.env), text=True, encoding="utf-8",
                                      bufsize=1, start_new_session=True)
        err = threading.Thread(target=self._read_err, args=(self.child,), daemon=True)
        err.start()
        threading.Thread(target=self._read_agy, args=(self.child, gen, err), daemon=True).start()

    def _read_agy(self, child, gen, err):
        try:
            for line in child.stdout:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue   # живая проба agy 1.5.18: предупреждения уходят в stderr, но защита от не-JSON в stdout остаётся
                self.q.put(("agy", gen, obj))
        finally:
            code = child.wait()
            err.join(timeout=2)   # хвост stderr нужен для разбора ошибки: дочитать до события выхода
            self.q.put(("agy_eof", gen, code))

    def _read_err(self, child):
        for line in child.stderr:
            self.stderr_tail.append(line.rstrip()[:300])

    def write_child(self, text):
        self.child.stdin.write(json.dumps({"event": "user", "message": {"content": text}}, ensure_ascii=False) + "\n")
        self.child.stdin.flush()

    # -- события agy -----------------------------------------------------------------------------------
    def on_agy(self, ev):
        self.heard = self.mono()
        if not self.turn:
            return   # событие вне хода (после прерывания): пропускаем
        for msg in self.turn.feed(ev):
            self.emit(msg)
        if ev.get("event") == "init" and self.turn.conv:
            store.set_conv(self.uuid, conv=self.turn.conv, session=self.session, model=self.model_name)
        if ev.get("event") == "result":
            self.finish_result()

    def finish_result(self):
        t = self.turn
        status = t.status
        text = (t.error if isinstance(t.error, str) else translate.result_text(t.error)) if t.error else ""
        if status in ("SUCCESS", ""):
            self.cum = dict(t.cum or {})
            if not t.response.strip() and not t.said and t.denied_actions:
                names = ", ".join(str(a.get("display_name") or a.get("action") or "tool")
                                  for a in t.denied_actions if isinstance(a, dict)) or "инструмент"
                mode = self.p["mode"] or "headless"
                return self.finish_error(
                    f"Antigravity: в режиме {mode} действие ({names}) требует подтверждения и отклонено без диалога",
                    kind="denied", usage=t.turn_usage())
            return self.finish(is_error=False, text=t.response, usage=t.turn_usage(), status="ok")
        if status in ("CANCELED", "INTERRUPTED") or self.interrupted:
            return self.finish_error("прервано", kind="interrupted", usage=t.turn_usage())
        verdict = route.classify(text + " " + " ".join(self.stderr_tail))
        if verdict:
            return self.finish_error(self.block_for(verdict, text), kind=verdict)
        return self.finish_error(f"Antigravity: ошибка — {route.scrub(text) or status.lower() or 'без текста'}", kind="error")

    def block_for(self, verdict, raw):
        """Записать блок по ошибке agy и вернуть текст результата хода (§3.1, §3.2)."""
        now = self.clock()
        if verdict == "auth":
            store.set_block("auth", now + 30 * 86400, "Antigravity: нужен вход. Запусти `agy` в Терминале и войди, затем `hub antigravity unblock`", now)
            return "Antigravity: нужен вход. Запусти `agy` в Терминале и войди"
        wait = route.reset_after(raw + " " + " ".join(self.stderr_tail)) or self.cfg["block_default_s"]
        until = now + wait
        hhmm = time.strftime("%H:%M", time.localtime(until))
        text = f"Antigravity: квота до {hhmm}" if route.reset_after(raw) else f"Antigravity: квота кончилась, проверю после {hhmm}"
        store.set_block("quota", until, text, now)
        return text

    def finish_error(self, text, kind="error", usage=None):
        self.finish(is_error=True, text=text, usage=usage or {}, status=kind)

    def finish(self, is_error, text, usage, status):
        t = self.turn
        ms = int((self.mono() - t.began) * 1000)
        if is_error and not t.said:
            self.emit(self._error_text_msg(t, text))
        # ядру важно «не ноль» у обычного хода (ноль — это /compact и /recap: их ответ не ход, а факт session.compacted/recap)
        turns = 0 if getattr(t, "slash", None) else 1
        self.emit({"type": "result", "subtype": "success" if not is_error else "error_during_execution",
                   "is_error": bool(is_error), "duration_ms": ms, "num_turns": turns,
                   "result": text, "session_id": self.uuid, "total_cost_usd": None,
                   "usage": {"input_tokens": usage.get("in", 0), "output_tokens": usage.get("out", 0),
                             "cache_read_input_tokens": usage.get("cache", 0), "cache_creation_input_tokens": 0}})
        # учёт: ход идёт в счётчик, даже ошибочный, но только если agy успел дать числа или ход был у agy
        if status not in ("refused",):
            store.log_usage(self.session, self.model_name, usage, ms, status, self.clock())
            store.log_turn(self.uuid, "agy", t.request, text, t.tool_names, self.clock())
        if not is_error:
            fields = {"last_agy_at": self.clock(), "instr": getattr(t, "instr_hash", "")}
            if t.conv:
                fields.update(conv=t.conv, session=self.session, model=self.model_name)
            store.set_conv(self.uuid, **fields)
        self.turn_done()

    def _error_text_msg(self, t, text):
        """Отказ как слово модели: страница показывает ленту из assistant-сообщений, а не только итог хода."""
        return {"type": "assistant", "session_id": self.uuid,
                "message": {"role": "assistant", "model": self.model_name, "content": [{"type": "text", "text": text}]}}

    def turn_done(self):
        if not getattr(self.turn, "slash", None):
            self.hook("Stop")   # у /compact и /recap нет хуков хода: это не ход
        self.turn = None
        if self.pending:
            texts, self.pending = self.pending, []
            self.start_turn(texts)
        if self.block_active() and not self.turn:
            self.stop_child()    # блок: процесс agy не держим и место отдаём

    def block_active(self):
        return store.block(self.clock()) is not None

    def on_agy_eof(self, code):
        err = " ".join(self.stderr_tail)
        self.child = None
        self.release_slot()
        if not self.turn:
            return
        if self.interrupted:
            return self.finish_error("прервано", kind="interrupted")
        verdict = route.classify(err)
        if verdict:
            return self.finish_error(self.block_for(verdict, err), kind=verdict)
        # живая проба agy 1.5.18: на ошибке/прерывании agy шлёт result ERROR и выходит с rc=1; сюда доходит только аварийный выход без result
        return self.finish_error(f"Antigravity: процесс agy вышел посреди хода (код {code}). {route.scrub(err, 200)}".strip(),
                                 kind="crash")

    def on_tick(self):
        if not self.turn or not self.child_alive():
            return
        quiet = self.mono() - self.heard
        if quiet > self.idle_limit:
            self.kill_child()
            return self.finish_error(f"Antigravity: agy молчит {int(quiet // 60) or 1} мин, процесс снят", kind="hung")
        if self.mono() - self.last_keepalive >= self.keepalive:
            self.last_keepalive = self.mono()
            self.emit({"type": "system", "subtype": "status", "status": "working", "session_id": self.uuid})

    # -- процесс agy -----------------------------------------------------------------------------------
    def kill_child(self):
        c = self.child
        if c is None:
            return
        self.gen += 1    # события старого процесса больше не слушаем
        self.child = None
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(c.pid, sig)
            except OSError:
                break
            try:
                c.wait(timeout=3)
                break
            except subprocess.TimeoutExpired:
                continue
        self.release_slot()

    def stop_child(self, graceful=False):
        c = self.child
        if c is None:
            self.release_slot()
            return
        if graceful and c.poll() is None:
            try:
                c.stdin.close()   # закрытый stdin — чистый конец сессии (код 0, док)
                c.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.kill_child()

    def release_slot(self):
        if self.slot:
            self.slot.release()
            self.slot = None


def main(argv, env=None):
    for stream in (sys.stdin, sys.stdout):   # Claude Code и ядро говорят в UTF-8 независимо от локали службы
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    env = dict(os.environ if env is None else env)
    which, _why = route.decide(argv, env)
    if which == "claude":
        real = route.real_claude()
        os.execv(real, [real] + argv)
    return Bridge(argv, env).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
