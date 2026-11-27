"""Проверка протокола хаба с подменённым agy и временным состоянием."""
import json
import os
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

from helpers import MODULE, UUID, Proc, Sandbox


class CodexBridge(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.rules = os.path.join(self.sandbox.state, "sessions", "agy-s", "codex-rules.json")
        os.makedirs(os.path.dirname(self.rules))
        with open(self.rules, "w", encoding="utf-8") as stream:
            json.dump({"deny": []}, stream)
        self.hooks = os.path.join(self.sandbox.home, ".gemini", "config", "hooks.json")
        os.makedirs(os.path.dirname(self.hooks))
        with open(self.hooks, "w", encoding="utf-8") as stream:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{
                "type": "command", "command": f"{sys.executable} {MODULE}/agy_guard.py"
            }]}]}}, stream)
        hub = os.path.join(self.sandbox.dir, "hub")
        with open(hub, "w", encoding="utf-8") as stream:
            stream.write(f'#!{sys.executable}\nimport json\nprint(json.dumps({{"sessions": {{"agy-s": {{"session_id": "{UUID}"}}}}}}))\n')
        os.chmod(hub, 0o755)
        self.sandbox.env_before = self.sandbox.env
        self.sandbox.env = lambda *args, **kwargs: {
            **self.sandbox.env_before(*args, **kwargs), "PATH": self.sandbox.dir + os.pathsep + "/usr/bin:/bin"
        }
        env = self.sandbox.env(HUB_MOD_ANTIGRAVITY_CODEX_WORK="1")
        p = subprocess.Popen(
            [sys.executable, "-I", os.path.join(MODULE, "codex_bridge.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=self.sandbox.dir, text=True, encoding="utf-8",
        )
        self.proc = Proc(p)
        self.addCleanup(self.proc.kill)

    def send(self, ident, method, params=None):
        params = params or {}
        if method in ("thread/start", "thread/resume"):
            params = {"approvalPolicy": "never", "sandbox": "danger-full-access", **params}
        self.proc.send({"id": ident, "method": method, "params": params})
        events = self.proc.read(lambda e: e.get("id") == ident)
        return next(e for e in events if e.get("id") == ident)

    def test_turn_and_resume(self):
        self.assertIn("result", self.send(1, "initialize"))
        start = self.send(2, "thread/start", {"cwd": self.sandbox.dir, "developerInstructions": "Тест"})
        thread = start["result"]["thread"]["id"]
        turn = self.send(3, "turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Привет"}]})
        self.assertIn("turn", turn["result"])
        events = self.proc.read(lambda e: e.get("method") == "turn/completed")
        result = next(e for e in events if e.get("method") == "turn/completed")
        self.assertEqual(result["params"]["turn"]["status"], "completed")
        self.assertTrue(any(e.get("params", {}).get("item", {}).get("type") == "agentMessage" for e in events))
        resumed = self.send(4, "thread/resume", {"threadId": thread, "cwd": self.sandbox.dir})
        self.assertEqual(resumed["result"]["thread"]["id"], thread)
        self.assertEqual(len(self.sandbox.agy_turns()), 1)

    def test_invalid_input_is_refused(self):
        start = self.send(1, "thread/start", {"cwd": self.sandbox.dir})
        thread = start["result"]["thread"]["id"]
        response = self.send(2, "turn/start", {"threadId": thread, "input": [{"type": "unknown"}]})
        self.assertIn("error", response)
        self.assertEqual(self.sandbox.agy_turns(), [])

    def test_tool_activity_reaches_codex_host(self):
        self.proc.kill()
        env = self.sandbox.env(scenario="tool_ok", HUB_MOD_ANTIGRAVITY_CODEX_WORK="1")
        process = subprocess.Popen(
            [sys.executable, "-I", os.path.join(MODULE, "codex_bridge.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=self.sandbox.dir, text=True, encoding="utf-8",
        )
        self.proc = Proc(process)
        self.addCleanup(self.proc.kill)
        thread = self.send(1, "thread/start", {"cwd": self.sandbox.dir})["result"]["thread"]["id"]
        self.send(2, "turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Проверь"}]})
        events = self.proc.read(lambda e: e.get("method") == "turn/completed")
        tools = [e for e in events if e.get("params", {}).get("item", {}).get("type") == "mcpToolCall"]
        self.assertEqual([e["method"] for e in tools], ["item/started", "item/completed"])
        self.assertEqual(tools[0]["params"]["item"]["arguments"], {"command": "ls"})
        self.assertEqual(tools[1]["params"]["item"]["result"], "a.txt\nb.txt")

    def test_codex_thread_replacement_keeps_agy_branch(self):
        codex_thread = str(uuid.uuid4())
        start = self.send(1, "thread/resume", {"threadId": codex_thread, "cwd": self.sandbox.dir})
        self.assertEqual(start["result"]["thread"]["id"], codex_thread)
        path = os.path.join(self.sandbox.data, "codex-branches.json")
        with open(path, encoding="utf-8") as stream:
            internal = json.load(stream)[UUID]["internal"]
        replacement = str(uuid.uuid4())
        got = self.send(2, "thread/resume", {"threadId": replacement, "cwd": self.sandbox.dir})
        self.assertEqual(got["result"]["thread"]["id"], replacement)
        with open(path, encoding="utf-8") as stream:
            record = json.load(stream)[UUID]
        self.assertEqual(record["internal"], internal)
        self.assertEqual(record["external"], [codex_thread, replacement])

    def test_corrupt_branch_file_is_not_overwritten(self):
        path = os.path.join(self.sandbox.data, "codex-branches.json")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("broken")
        result = self.send(1, "thread/start", {"cwd": self.sandbox.dir})
        self.assertIn("error", result)
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "broken")

    def test_unknown_restrictions_fail_closed(self):
        with open(self.rules, "w", encoding="utf-8") as stream:
            json.dump({"deny": ["SomeUnknownRestriction"]}, stream)
        result = self.send(1, "thread/start", {"cwd": self.sandbox.dir})
        self.assertIn("error", result)
        self.assertEqual(self.sandbox.agy_turns(), [])

    def test_missing_rules_fail_closed(self):
        os.unlink(self.rules)
        result = self.send(1, "thread/start", {"cwd": self.sandbox.dir})
        self.assertIn("error", result)
        self.assertEqual(self.sandbox.agy_turns(), [])

    def test_missing_guard_fails_closed(self):
        os.unlink(self.hooks)
        result = self.send(1, "thread/start", {"cwd": self.sandbox.dir})
        self.assertIn("error", result)
        self.assertEqual(self.sandbox.agy_turns(), [])

    def test_unsupported_project_permission_fails_closed(self):
        settings = os.path.join(self.sandbox.dir, "settings.json")
        with open(settings, "w", encoding="utf-8") as stream:
            json.dump({"permissions": {"deny": ["Bash(rm:*)"]}}, stream)
        with open(self.rules, "w", encoding="utf-8") as stream:
            json.dump({"deny": [], "settings": [settings]}, stream)
        result = self.send(1, "thread/start", {"cwd": self.sandbox.dir})
        self.assertIn("error", result)
        self.assertEqual(self.sandbox.agy_turns(), [])

    def test_guard_detects_project_rules_changed_during_session(self):
        settings = os.path.join(self.sandbox.dir, "dynamic.json")
        with open(self.rules, "w", encoding="utf-8") as stream:
            json.dump({"deny": [], "settings": [settings]}, stream)
        self.assertIn("result", self.send(1, "thread/start", {"cwd": self.sandbox.dir}))
        path = os.path.join(self.sandbox.data, "codex-branches.json")
        with open(path, encoding="utf-8") as stream:
            internal = json.load(stream)[UUID]["internal"]
        import agy_guard
        import store
        with patch.dict(os.environ, {**self.sandbox.env(), "HUB_ON_CODEX": "1"}, clear=True):
            store.set_conv(internal, conv="dynamic-conversation")
            call = {"conversationId": "dynamic-conversation", "toolCall": {"name": "view_file"}}
            self.assertIsNone(agy_guard.decide(call))
            with open(settings, "w", encoding="utf-8") as stream:
                json.dump({"permissions": {"deny": ["Read(secret)"]}}, stream)
            self.assertEqual(agy_guard.decide(call)["decision"], "deny")

    def test_clear_starts_new_agy_branch(self):
        first = self.send(1, "thread/start", {"cwd": self.sandbox.dir})
        self.assertIn("result", first)
        path = os.path.join(self.sandbox.data, "codex-branches.json")
        with open(path, encoding="utf-8") as stream:
            before = json.load(stream)[UUID]["internal"]
        cleared = self.send(2, "thread/start", {"cwd": self.sandbox.dir, "sessionStartSource": "clear"})
        self.assertIn("result", cleared)
        with open(path, encoding="utf-8") as stream:
            after = json.load(stream)[UUID]["internal"]
        self.assertNotEqual(before, after)

    def test_minihub_restrictions_are_written_for_guard(self):
        banned_path = os.path.join(self.sandbox.dir, "protected")
        with open(self.rules, "w", encoding="utf-8") as stream:
            json.dump({"deny": [f"Edit({banned_path}/**)", "Bash(git push:*)"]}, stream)
        result = self.send(1, "thread/start", {"cwd": self.sandbox.dir})
        self.assertIn("result", result)
        path = os.path.join(self.sandbox.data, "codex-branches.json")
        with open(path, encoding="utf-8") as stream:
            internal = json.load(stream)[UUID]["internal"]
        with open(os.path.join(self.sandbox.data, "rules", internal + ".json"), encoding="utf-8") as stream:
            rules = json.load(stream)
        self.assertIn(banned_path, rules["paths"])
        self.assertIn("push", rules["commands"])
        import agy_guard
        import store
        with patch.dict(os.environ, self.sandbox.env(), clear=True):
            store.set_conv(internal, conv="test-conversation")
            result = agy_guard.decide({"conversationId": "test-conversation", "cwd": self.sandbox.dir,
                                       "toolCall": {"name": "run_command", "args": {"command": "git push"}}})
        self.assertEqual(result["decision"], "deny")

    def test_parallel_conversation_updates_are_preserved(self):
        processes = []
        for index in range(12):
            script = (
                "import sys; sys.path.insert(0, sys.argv[1]); import store; "
                "store.set_conv(sys.argv[2], conv=sys.argv[2])"
            )
            processes.append(subprocess.Popen(
                [sys.executable, "-I", "-c", script, MODULE, f"conversation-{index}"],
                env=self.sandbox.env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            ))
        for process in processes:
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
        path = os.path.join(self.sandbox.data, "convs.json")
        with open(path, encoding="utf-8") as stream:
            records = json.load(stream)
        for index in range(12):
            self.assertEqual(records[f"conversation-{index}"]["conv"], f"conversation-{index}")
