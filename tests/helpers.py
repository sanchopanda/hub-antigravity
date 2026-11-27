"""Общее для тестов модуля antigravity: временная папка хаба, фальшивый agy, запуск моста процессом, чтение его вывода."""
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.dirname(HERE)
sys.path.insert(0, MODULE)
UUID = "11111111-2222-3333-4444-555555555555"


class Sandbox:
    """Временный «хаб»: состояние, hub.json, данные модуля, хуки-регистраторы, фальшивые agy и claude. Живого не касается."""

    def __init__(self, hub_cfg=None):
        self.dir = tempfile.mkdtemp(prefix="agy-test-")
        self.state = os.path.join(self.dir, "state")
        self.data = os.path.join(self.state, "moddata", "antigravity")
        os.makedirs(self.data, mode=0o700)
        self.cfg = os.path.join(self.dir, "hub.json")
        self.hook_log = os.path.join(self.dir, "hooks.log")
        self.agy_log = os.path.join(self.dir, "agy.log")
        self.claude_log = os.path.join(self.dir, "claude.log")
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.settings = os.path.join(self.dir, "hooks-settings.json")
        self.set_hub({"antigravity": {"models": {"claude-agy": "fake-model"}}, **(hub_cfg or {})})
        py = sys.executable
        rec = f"{py} {os.path.join(HERE, 'hook_recorder.py')}"
        hooks = {ev: [{"hooks": [{"type": "command", "command": rec, "timeout": 10}]}]
                 for ev in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")}
        hooks["PreToolUse"] = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "false"}]}]
        with open(self.settings, "w") as f:
            json.dump({"hooks": hooks, "permissions": {"deny": ["Bash(pkill:*)"]}}, f)
        self.agy = self._wrapper("fake_agy.py", "agy")
        self.claude = self._wrapper("fake_claude.py", "claude")
        self.instr = os.path.join(self.dir, "instr.md")
        with open(self.instr, "w") as f:
            f.write("Правила хаба: пиши по-русски.")
        self.procs = []

    def _wrapper(self, script, name):
        path = os.path.join(self.dir, name)
        target = os.path.join(HERE, script)
        with open(path, "w") as f:
            f.write(f"#!{sys.executable}\nimport sys\nsys.argv[0] = {target!r}\n"
                    f"exec(compile(open({target!r}).read(), {target!r}, 'exec'), {{'__name__': '__main__', '__file__': {target!r}}})\n")
        os.chmod(path, 0o755)
        return path

    def set_hub(self, cfg):
        with open(self.cfg, "w") as f:
            json.dump(cfg, f)

    def env(self, scenario="text", **extra):
        e = {"PATH": "/usr/bin:/bin", "HOME": self.home, "HUB_SESSION": "agy-s", "HUB_STATE_DIR": self.state,
             "HUB_CONFIG": self.cfg, "HUB_MOD_ANTIGRAVITY_ROUTE": "1", "HUB_MOD_ANTIGRAVITY_MODEL": "fake-model",
             "HUB_MOD_ANTIGRAVITY_AGY": self.agy, "HUB_MOD_ANTIGRAVITY_DATA": self.data,
             "HUB_MOD_ANTIGRAVITY_PYTHON": sys.executable, "HOOK_LOG": self.hook_log, "FAKE_AGY_LOG": self.agy_log,
             "FAKE_AGY_SCENARIO": scenario, "FAKE_CLAUDE_LOG": self.claude_log,
             "CLAUDE_CODE_OAUTH_TOKEN": "SECRET-SENTINEL-token", "ANTHROPIC_API_KEY": "SECRET-SENTINEL-key",
             "ANTHROPIC_BASE_URL": "https://example.invalid", "HUB_ACCOUNT": "work"}
        e.update(extra)
        return {k: v for k, v in e.items() if v is not None}

    def argv(self, mode="bypassPermissions", resume=False, uuid=UUID, model="claude-agy", deny=("SendMessage", "ListAgents"),
             settings=None, instr=True):
        a = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--resume" if resume else "--session-id", uuid, "-n", "agy-s", "--settings", settings or self.settings,
             "--permission-prompt-tool", "stdio", "--permission-mode", mode]
        if instr:
            a += ["--append-system-prompt-file", self.instr, "--system-prompt-snapshot", "off"]
        a += ["--setting-sources", "project,local", "--disallowedTools", *deny, "--model", model]
        return a

    def start(self, scenario="text", env=None, bridge=True, **kw):
        e = self.env(scenario, **(env or {}))
        a = self.argv(**kw)
        cmd = [sys.executable, "-I", os.path.join(MODULE, "bridge.py"), *a]
        p = Proc(subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e,
                                  cwd=self.dir, text=True, encoding="utf-8", bufsize=1, start_new_session=True))
        self.procs.append(p)
        return p

    def hooks(self):
        return _lines(self.hook_log)

    def agy_runs(self):
        return [r for r in _lines(self.agy_log) if "argv" in r]

    def agy_turns(self):
        return [r for r in _lines(self.agy_log) if "turn" in r]

    def cleanup(self):
        for p in self.procs:
            p.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


def _lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


class Proc:
    def __init__(self, popen):
        self.p = popen
        self.q = queue.Queue()
        self.seen = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=lambda: [None for _ in popen.stderr], daemon=True).start()

    def _pump(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                self.q.put({"_bad": line})
        self.q.put(None)

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def user(self, text):
        self.send({"type": "user", "message": {"role": "user", "content": text}})

    def read(self, pred, timeout=15):
        """Читать вывод, пока pred(сообщение) не истинно; -> все сообщения с начала (накопленные self.seen)."""
        end = time.time() + timeout
        for m in self.seen:
            if pred(m):
                return self.seen
        while time.time() < end:
            try:
                m = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if m is None:
                break
            self.seen.append(m)
            if pred(m):
                return self.seen
        raise AssertionError(f"не дождался (timeout {timeout}); вывод: {json.dumps(self.seen, ensure_ascii=False)[:2000]}")

    def results(self):
        return [m for m in self.seen if m.get("type") == "result"]

    def wait_results(self, n, timeout=15):
        end = time.time() + timeout
        while len(self.results()) < n and time.time() < end:
            try:
                m = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if m is None:
                break
            self.seen.append(m)
        if len(self.results()) < n:
            raise AssertionError(f"ждал {n} result: {json.dumps(self.seen, ensure_ascii=False)[:2000]}")
        return self.results()

    def close(self, timeout=15):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        return self.p.wait(timeout=timeout)

    def drain(self):
        while True:
            try:
                m = self.q.get(timeout=1)
            except queue.Empty:
                break
            if m is None:
                break
            self.seen.append(m)
        return self.seen

    def kill(self):
        if self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except OSError:
                pass


def wait_until(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False
