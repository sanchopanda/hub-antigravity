"""Стенд с настоящим ядром (спека §8 п. 7): StreamHost и Hub живого слоя на временной папке, оболочка bin/claude как
«claude» ядра, фальшивый agy в PATH стенда, хуки — настоящий hooks/hub_hook.py ядра. Живого хаба, Google и DeepSeek нет.
launch.env модуля вызывается настоящим main.launch через настоящий core.modlaunch."""
import json
import os
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402
from helpers import MODULE, UUID, Sandbox, wait_until  # noqa: E402

LAYER = os.path.realpath(os.path.expanduser("~/.hub-personal/code"))
OWNER = {"channel": "web", "owner": True}


class Stand(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, LAYER)
        cls.env_before = dict(os.environ)

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        os.environ.update(HUB_STATE_DIR=self.sb.state, HUB_CONFIG=self.sb.cfg, HUB_CONFIG_DIR=os.path.join(self.sb.dir, "conf"),
                          HUB_MODULE="antigravity", HOOK_LOG=self.sb.hook_log, FAKE_AGY_LOG=self.sb.agy_log,
                          FAKE_AGY_SCENARIO="echo", FAKE_CLAUDE_LOG=self.sb.claude_log, HUB_MOD_ANTIGRAVITY_AGY=self.sb.agy,
                          CLAUDE_CONFIG_DIR=os.path.join(self.sb.dir, "claude"))
        self.addCleanup(self.restore_env)
        from core import journal
        from core.daemon import Hub
        from hosts.streamjson import StreamHost
        self.journal = journal
        self.mod = os.path.join(self.sb.dir, "ext", "antigravity")
        shutil.copytree(MODULE, self.mod, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        with open(os.path.join(self.mod, "bin", "real-claude"), "w") as f:
            f.write(self.sb.claude + "\n")
        shim = os.path.join(self.mod, "bin", "claude")
        sys.path.insert(0, self.mod)
        self.addCleanup(sys.path.remove, self.mod)
        sys.modules.pop("main", None)
        import main as agmain
        self.agmain = agmain
        self.addCleanup(sys.modules.pop, "main", None)
        import core.modrun as modrun
        stand = self

        class Mods:   # то, что hubd даёт хосту: ответы точки launch.env модуля (core.modlaunch зовёт именно это)
            state_dir = stand.sb.state

            def answering(self, point):
                return ["antigravity"] if point == "launch.env" else []

            def call(self, name, point, payload, ctx=None):
                got = agmain.launch(payload)
                # схема и фильтр ответа — как делает ядро (оно само добавляет name в ctx, _call)
                return modrun._launch_env(got, {"name": "antigravity", "env": []}) if got else got

            def fail(self, *a):
                stand.fail(f"ядро отвергло ответ модуля: {a}")
        old = modrun.manifest_of
        modrun.manifest_of = lambda state_dir, name: {}
        self.addCleanup(setattr, modrun, "manifest_of", old)
        self.host = StreamHost(self.sb.state, claude=(shim,))
        self.hub = Hub(self.sb.state, self.host)
        self.hub.execute = lambda a: None
        self.host.modules = Mods()                                           # Hub подставляет свои при создании
        self.addCleanup(lambda: [self.host.kill(n) for n in list(self.host.procs)])

    def restore_env(self):
        for k in set(os.environ) - set(self.env_before):
            os.environ.pop(k)
        os.environ.update(self.env_before)

    def request(self, name="agy-s", model="claude-agy", uuid=UUID, resume=False, cwd=None, **kw):
        cwd = cwd or self.sb.dir
        self.hub.record("session.requested", name, {"cwd": cwd, "session_id": uuid, "model": model, **OWNER})
        return self.host.spawn(name, uuid, resume=resume, cwd=cwd, mode="bypassPermissions", model=model, kind="work", **kw)

    def say(self, text, name="agy-s"):
        got = self.hub.record("message.received", None, {"to": name, "text": text, **OWNER})
        mid = f"m{got['seq']}"
        self.host.type(name, f"[hub:{mid}] {text}", message=mid)
        return mid

    def events(self):
        return self.journal.read_tail(os.path.join(self.sb.state, "journal.jsonl"))

    def types(self):
        return [e["type"] for e in self.events()]

    def pump_until(self, cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.collect()
            if cond():
                return True
            time.sleep(0.2)
        self.fail(f"не дождался; журнал: {[(e['type'], e['entity']) for e in self.events()]}")

    def claude_calls(self, probes=False):
        """Вызовы настоящего claude. Ядро само зовёт `claude auth status` (вход) и `claude --help` (возможности) через ту же
        оболочку: они идут настоящему claude и не считаются запуском сессии."""
        calls = helpers._lines(self.sb.claude_log)
        return calls if probes else [c for c in calls if c["argv"][:2] != ["auth", "status"] and c["argv"] != ["--help"]]

    # -- сессия на claude-agy ------------------------------------------------------------------------------------------
    def test_a_session_on_agy_runs_through_the_real_core(self):
        self.assertEqual(self.request(), "spawned")
        mid = self.say("привет")
        self.pump_until(lambda: self.types().count("turn.stopped") == 1)
        evs = [e for e in self.events() if e["entity"] == "agy-s"]
        order = [e["type"] for e in evs]
        for t in ("session.info", "session.started", "turn.started", "session.said", "turn.result", "turn.stopped"):
            self.assertEqual(order.count(t), 1, (t, order))                 # без повторов и потерь
        self.assertLess(order.index("session.info"), order.index("turn.started"))
        self.assertLess(order.index("turn.started"), order.index("turn.result"))
        self.assertLess(order.index("turn.result"), order.index("turn.stopped"))
        started = next(e for e in evs if e["type"] == "turn.started")["data"]
        self.assertEqual(started["marker"], mid)                             # метка [hub:mN] дошла до хука ядра
        info = next(e for e in evs if e["type"] == "session.info")["data"]
        self.assertEqual((info["model"], info["mode"]), ("claude-agy", "bypassPermissions"))
        res = next(e for e in evs if e["type"] == "turn.result")["data"]
        self.assertFalse(res["is_error"])
        self.assertIn("привет", res["text"])
        self.assertGreater(res["tokens_in"], 0)
        self.assertIsNone(res["cost_usd"])
        self.assertEqual(self.claude_calls(), [])                            # настоящий claude сессию не запускал
        probes = [c["argv"] for c in self.claude_calls(probes=True)]
        self.assertIn(["auth", "status"], probes)                            # проверка входа ядра — у настоящего claude, не у моста
        # второй ход — тот же процесс agy, ещё по одному событию
        mid2 = self.say("второе")
        self.pump_until(lambda: self.types().count("turn.stopped") == 2)
        evs = [e["type"] for e in self.events() if e["entity"] == "agy-s"]
        self.assertEqual((evs.count("turn.started"), evs.count("turn.result"), evs.count("turn.stopped")), (2, 2, 2))
        self.assertEqual(len(self.sb.agy_runs()), 1)
        # отпускание сессии: stdin закрыт, мост и agy вышли, ядро видит конец
        self.host.release("agy-s")
        self.pump_until(lambda: "session.end_other" in self.types())
        self.pump_until(lambda: any(e["type"] == "host.done" and e["data"].get("do") == "release" for e in self.events()))

    def test_the_claude_login_of_the_pool_does_not_reach_agy_through_the_real_launch(self):
        os.environ.update(CLAUDE_CODE_OAUTH_TOKEN="SECRET-SENTINEL-pool", ANTHROPIC_API_KEY="SECRET-SENTINEL-api")
        self.request()
        self.say("а")
        self.pump_until(lambda: "turn.stopped" in self.types())
        run = self.sb.agy_runs()[0]
        self.assertEqual(run["leak"], [])
        self.assertIn("HUB_SESSION", run["env_names"])

    # -- обычные сессии идут в настоящий claude ---------------------------------------------------------------------------
    def test_a_plain_session_goes_to_the_real_claude_with_the_cores_arguments(self):
        for model in ("sonnet", "claude-deepseek", "opus"):
            os.path.exists(self.sb.claude_log) and os.remove(self.sb.claude_log)
            self.request(name=f"p-{model}", model=model, uuid=f"2222-{model}")
            self.assertTrue(wait_until(lambda: self.claude_calls()), model)
            call = self.claude_calls()[0]
            self.assertIsNone(call["route"], model)                          # переменной маршрута у сессии нет
            self.assertEqual(call["argv"][:5], ["-p", "--input-format", "stream-json", "--output-format", "stream-json"])
            self.assertEqual(call["argv"][call["argv"].index("--model") + 1], model)
        self.assertEqual(self.sb.agy_runs(), [])

    def test_an_agy_model_without_an_id_fails_at_the_real_claude_by_name(self):
        self.request(name="x", model="claude-agy-carbon", uuid="3333")
        self.assertTrue(wait_until(lambda: self.claude_calls()))
        call = self.claude_calls()[0]
        self.assertIsNone(call["route"])
        self.assertEqual(call["argv"][call["argv"].index("--model") + 1], "claude-agy-carbon")   # не Sonnet и не тишина

    def test_a_mini_hub_on_claude_agy_stays_on_agy_by_its_deny_rules(self):
        proj = os.path.join(self.sb.dir, "proj")     # папка мини-хаба вне состояния хаба: запрет правок в ней включается
        os.makedirs(proj)
        self.assertEqual(self.request(name="mh", uuid="4444", minihub=True, cwd=proj), "spawned")
        self.say("а", name="mh")
        self.pump_until(lambda: "turn.stopped" in self.types())
        self.assertEqual(self.claude_calls(), [])                            # запреты мини-хаба держит хук agy
        self.assertEqual(len(self.sb.agy_runs()), 1)
        rules = json.load(open(os.path.join(self.sb.data, "rules", "4444.json"), encoding="utf-8"))
        self.assertIn("commit", rules["commands"])
        self.assertIn("push", rules["commands"])
        self.assertTrue(rules["paths"])                                      # правки в своей папке — запрещены

    def test_the_liaison_on_claude_agy_goes_to_the_real_claude(self):
        self.request(name="li", uuid="5555", liaison=True)
        self.assertTrue(wait_until(lambda: self.claude_calls()))
        self.assertIn("hooks-settings-liaison.json", " ".join(self.claude_calls()[0]["argv"]))
        self.assertEqual(self.sb.agy_runs(), [])

    # -- передача итога ------------------------------------------------------------------------------------------------------
    def test_leaving_agy_for_another_engine_hands_the_turns_over_in_the_instruction_file(self):
        self.request()
        self.say("запомни: код 7")
        self.pump_until(lambda: "turn.stopped" in self.types())
        self.host.release("agy-s")
        self.assertTrue(wait_until(lambda: not self.host.alive("agy-s"), 15))
        self.pump_until(lambda: any(e["type"] == "host.done" for e in self.events()))
        self.hub.record("session.requested", "agy-s", {"cwd": self.sb.dir, "session_id": UUID, "model": "sonnet", **OWNER})
        self.host.procs.pop("agy-s", None)
        self.assertEqual(self.host.spawn("agy-s", UUID, resume=True, cwd=self.sb.dir, mode="bypassPermissions", model="sonnet",
                                         kind="work", profile=None), "spawned")
        self.assertTrue(wait_until(lambda: self.claude_calls()))
        argv = self.claude_calls()[0]["argv"]
        self.assertEqual(self.claude_calls()[0]["route"], "handoff")
        new = argv[argv.index("--append-system-prompt-file") + 1]
        self.assertIn("запомни: код 7", open(new).read())                  # ходы agy переданы новому движку
        self.assertIn("--session-id", argv)                                  # у Claude разговора нет: resume заменён
        self.assertNotIn("--resume", argv)

    def test_resume_from_claude_to_agy_with_minihub_rules_is_recognized_by_guard(self):
        import agy_guard
        import store as agy_store
        proj = os.path.join(self.sb.dir, "proj-res")
        os.makedirs(proj)
        cdir = os.path.join(self.sb.dir, "claude-conf")
        os.environ["CLAUDE_CONFIG_DIR"] = cdir
        cproj = os.path.join(cdir, "projects", "-proj-res")
        os.makedirs(cproj)
        uid = "66666666-2222-3333-4444-555555555555"
        with open(os.path.join(cproj, f"{uid}.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "контекст из Claude"}}) + "\n")
        self.assertEqual(self.request(name="mh-res", uuid=uid, resume=True, minihub=True, cwd=proj), "spawned")
        self.say("дальше", name="mh-res")
        self.pump_until(lambda: "turn.stopped" in self.types())
        self.assertEqual(self.claude_calls(), [])
        self.assertIn("контекст из Claude", self.sb.agy_turns()[0]["content"])
        old_data = os.environ.get("HUB_MOD_ANTIGRAVITY_DATA")
        os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = self.sb.data
        try:
            conv = agy_store.conv(uid).get("conv")
            self.assertEqual(conv, "conv-1")
            hit = agy_guard.decide({"conversationId": conv,
                                    "toolCall": {"name": "write_file", "args": {"Path": os.path.join(proj, "x.py")}}})
            self.assertEqual(hit["decision"], "deny")
        finally:
            if old_data is None:
                os.environ.pop("HUB_MOD_ANTIGRAVITY_DATA", None)
            else:
                os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = old_data


if __name__ == "__main__":
    unittest.main()
