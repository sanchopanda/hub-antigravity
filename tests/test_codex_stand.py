"""CodexHost настоящего ядра работает с подменным agy во временном хабе."""
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

from helpers import MODULE, UUID, Sandbox


class CodexStand(unittest.TestCase):
    def test_real_codex_host_delivers_an_agy_answer(self):
        wrapper = os.environ.get("HUB_TEST_CODEX_WRAPPER") or os.path.join(MODULE, "bin", "codex")
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        hub = os.path.join(sandbox.dir, "hub")
        with open(hub, "w", encoding="utf-8") as stream:
            stream.write(f'#!{sys.executable}\nimport json\nprint(json.dumps({{"sessions": {{"agy-s": {{"session_id": "{UUID}"}}}}}}))\n')
        os.chmod(hub, 0o755)
        env = sandbox.env(scenario="echo")
        env["PATH"] = sandbox.dir + os.pathsep + env["PATH"]
        env.update(HUB_MODULE="antigravity", HUB_MOD_ANTIGRAVITY_CODEX_WORK="1")
        hook = os.path.join(sandbox.home, ".gemini", "config", "hooks.json")
        os.makedirs(os.path.dirname(hook))
        with open(hook, "w", encoding="utf-8") as stream:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{
                "type": "command", "command": f"{sys.executable} {MODULE}/agy_guard.py"
            }]}]}}, stream)
        with patch.dict(os.environ, env, clear=True):
            from core import modrun
            from hosts import codex_rules
            from hosts.codex import CodexHost
            sys.path.insert(0, MODULE)
            self.addCleanup(sys.path.remove, MODULE)
            import main

            class Mods:
                state_dir = sandbox.state

                def answering(self, point):
                    return ["antigravity"] if point == "launch.env" else []

                def declared(self, *args, **kwargs):
                    return ["antigravity"]

                def call(self, name, point, payload, ctx=None):
                    return modrun._launch_env(main.launch(payload), {"name": "antigravity", "env": []})

                def fail(self, *args):
                    raise AssertionError(args)

            with patch.object(codex_rules, "prepare_home"), patch.object(codex_rules, "trusted", return_value=True), \
                    patch.object(modrun, "manifest_of", return_value={}):
                host = CodexHost(sandbox.state, codex=(wrapper,),
                                 env=env, all_sessions=True)
                host.modules = Mods()
                self.addCleanup(lambda: host.kill("agy-s"))
                self.assertEqual(host.spawn("agy-s", UUID, cwd=sandbox.dir, model="claude-agy", kind="work"), "spawned")
                host.type("agy-s", "Проверка", message="m1")
                found = []
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not any(x[0] == "turn.result" for x in found):
                    found.extend(host.poll())
                    time.sleep(0.02)
                result = next((x for x in found if x[0] == "turn.result"), None)
                self.assertIsNotNone(result, [x[0] for x in found])
                self.assertFalse(result[2]["is_error"])
                self.assertIn("Проверка", result[2]["text"])
                self.assertNotEqual(result[2].get("subtype"), "error")
                threads = os.path.join(sandbox.state, "sessions", "agy-s", "codex-threads.json")
                with open(threads, encoding="utf-8") as stream:
                    self.assertIn(UUID, json.load(stream))
