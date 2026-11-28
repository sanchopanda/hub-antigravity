"""Тесты модуля вне моста: launch.env (маршруты), команда hub antigravity, doctor, setup во временной папке, sweep,
guard, чистые правила route / store / handoff / translate. Живого хаба, agy и ~/.gemini нет."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import HERE, MODULE, UUID  # noqa: E402

import agy_guard  # noqa: E402
import agycli  # noqa: E402
import deny as agy_deny  # noqa: E402
import handoff  # noqa: E402
import route  # noqa: E402
import setup as agy_setup  # noqa: E402
import store  # noqa: E402
import sweep  # noqa: E402
import translate  # noqa: E402


class Env(unittest.TestCase):
    """Временная папка хаба для store: HUB_STATE_DIR, HUB_CONFIG, HUB_CONFIG_DIR, папка данных модуля."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agy-mod-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.old = {k: os.environ.get(k) for k in ("HUB_STATE_DIR", "HUB_CONFIG", "HUB_CONFIG_DIR", "HUB_MOD_ANTIGRAVITY_DATA",
                                                    "HUB_MOD_ANTIGRAVITY_AGY", "CLAUDE_CONFIG_DIR",
                                                    "HUB_MOD_ANTIGRAVITY_LAYERS", "HUB_MOD_ANTIGRAVITY_CODE")}
        self.addCleanup(self.restore)
        os.environ.update(HUB_STATE_DIR=os.path.join(self.dir, "state"), HUB_CONFIG=os.path.join(self.dir, "hub.json"),
                          HUB_CONFIG_DIR=os.path.join(self.dir, "conf"), CLAUDE_CONFIG_DIR=os.path.join(self.dir, "claude"))
        os.environ.pop("HUB_MOD_ANTIGRAVITY_DATA", None)
        self.hub({"antigravity": {"models": {
            "claude-agy": "gemini-carbon", "claude-agy-argon": "default",
            "claude-opus-5-5": "claude-opus-5-5-high",
            "claude-sonnet-5-5": "claude-sonnet-5-5-high",
        }}})

    def restore(self):
        for k, v in self.old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def hub(self, cfg):
        with open(os.environ["HUB_CONFIG"], "w") as f:
            json.dump(cfg, f)


class Launch(Env):
    """main.py грузится с настоящим ядром живого слоя (core.hubmod): точки модуля — обычные функции, их зовём напрямую."""

    @classmethod
    def setUpClass(cls):
        layer = os.path.realpath(os.path.expanduser("~/.hub-personal/code"))
        sys.path.insert(0, layer)
        cls._env = os.environ.get("HUB_MODULE")
        os.environ["HUB_MODULE"] = "antigravity"
        sys.modules.pop("main", None)
        import main
        cls.main = main

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("HUB_MODULE", None) if cls._env is None else os.environ.__setitem__("HUB_MODULE", cls._env)

    def test_an_agy_model_gets_the_route_and_its_agy_model_id(self):
        got = self.main.launch({"session": "s", "model": "claude-agy"})
        env = got["env"]
        self.assertEqual((env["HUB_MOD_ANTIGRAVITY_ROUTE"], env["HUB_MOD_ANTIGRAVITY_MODEL"]), ("1", "gemini-carbon"))
        self.assertEqual(env["HUB_MOD_ANTIGRAVITY_PYTHON"], sys.executable)
        self.assertEqual(env["HUB_MOD_ANTIGRAVITY_DATA"], store.data_dir())
        # «default» — agy по умолчанию: id пустой, но маршрут есть; пустую строку ядро вырезает из env, поэтому метка
        self.assertEqual(self.main.launch({"model": "claude-agy-argon"})["env"]["HUB_MOD_ANTIGRAVITY_MODEL"], "default")
        # у модуля нет полей, которые ядро запрещает модулю (HUB_* только свои HUB_MOD_ANTIGRAVITY_*)
        for k in env:
            self.assertTrue(k.startswith("HUB_MOD_ANTIGRAVITY_"), k)
        codex_env = self.main.launch({"session": "s", "kind": "work", "model": "codex/claude-agy"})["env"]
        self.assertEqual(codex_env["HUB_MOD_ANTIGRAVITY_MODEL"], "gemini-carbon")
        self.assertEqual(codex_env["HUB_MOD_ANTIGRAVITY_CODEX_WORK"], "1")
        hub_env = self.main.launch({"session": "hub-core", "kind": "hub", "model": "codex/claude-agy"})["env"]
        self.assertEqual(hub_env["HUB_MOD_ANTIGRAVITY_CODEX_WORK"], "1")
        for name, agy_id in (("claude-opus-5-5", "claude-opus-5-5-high"),
                             ("claude-sonnet-5-5", "claude-sonnet-5-5-high")):
            got = self.main.launch({"session": "s", "kind": "work", "model": f"codex/{name}"})["env"]
            self.assertEqual(got["HUB_MOD_ANTIGRAVITY_ROUTE"], "1")
            self.assertEqual(got["HUB_MOD_ANTIGRAVITY_MODEL"], agy_id)
            self.assertEqual(got["HUB_MOD_ANTIGRAVITY_CODEX_WORK"], "1")

    def test_quota_notification_uses_a_valid_state_key(self):
        keys = []
        def check_key(key, *args):
            import re
            self.assertRegex(key, r"^[a-z0-9_.-]+$")
            keys.append(key)
            return None
        with patch.object(self.main.store, "blocks", return_value={"gemini": {"reason":"quota", "at":1}}), \
             patch.object(self.main.hubmod.state, "get", side_effect=check_key), \
             patch.object(self.main.hubmod.state, "set", side_effect=check_key), \
             patch.object(self.main.hubmod, "fact"):
            self.main.watch_once(now=2)
        self.assertEqual(keys, ["notified.gemini", "notified.gemini"])

    def test_the_default_model_of_the_hub_counts_for_a_new_session(self):
        self.assertEqual(self.main.launch({"session": "s"}, default="claude-agy")["env"]["HUB_MOD_ANTIGRAVITY_ROUTE"], "1")
        self.assertEqual(self.main.launch({"session": "s", "model": "sonnet"}, default="claude-agy"), {})   # своя модель сильнее
        self.hub({"codex": {"model": "claude-agy"},
                  "antigravity": {"models": {"claude-agy": "gemini-carbon"}}})
        got = self.main.launch_env({"session": "new", "kind": "work"})
        self.assertEqual(got["env"]["HUB_MOD_ANTIGRAVITY_MODEL"], "gemini-carbon")

    def test_other_models_get_nothing_and_so_keep_the_one_check_path(self):
        for m in ("sonnet", "opus", "claude-deepseek", "claude-qwen", "", None):
            self.assertEqual(self.main.launch({"session": "s", "model": m}), {}, m)

    def test_an_agy_model_without_an_id_goes_to_the_real_claude_and_fails_by_name(self):
        self.assertEqual(self.main.launch({"model": "claude-agy-carbon"}), {})

    def test_a_session_that_left_agy_gets_the_handoff_route_once(self):
        store.set_conv(UUID, conv="c1", session="s1")
        store.log_turn(UUID, "agy", "вопрос", "ответ", [], now=time.time() - 50)
        got = self.main.launch({"session": "s1", "model": "claude-deepseek", "resume": True})
        self.assertEqual(got["env"]["HUB_MOD_ANTIGRAVITY_ROUTE"], "handoff")
        self.assertEqual(self.main.launch({"session": "s1", "model": "sonnet", "resume": False}), {})   # новая сессия — не передача
        self.assertEqual(self.main.launch({"session": "другая", "model": "sonnet", "resume": True}), {})
        store.set_conv(UUID, handoff_at=time.time())                      # передачу сделали — маршрут снят
        self.assertEqual(self.main.launch({"session": "s1", "model": "sonnet", "resume": True}), {})

    def test_a_failure_inside_the_hook_never_breaks_other_sessions(self):
        os.environ["HUB_CONFIG"] = "/нет/такого"                          # чтение настроек: пустые, но не падение
        self.assertEqual(self.main.launch_env({"model": "sonnet"}), {})
        orig = self.main.launch
        self.main.launch = lambda *a, **k: 1 / 0
        try:
            self.assertEqual(self.main.launch_env({"model": "claude-agy"}), {})
        finally:
            self.main.launch = orig


class Commands(Env):
    def test_state_exposes_quota_models_slots_and_exhaustion(self):
        import quota
        from unittest.mock import patch
        fresh = {"ok": True, "at": time.time(), "fetched_at": time.time(), "ttl_s": 300,
                 "gemini": {"h5": {"pct": 0, "reset_at": None},
                            "weekly": {"pct": 50, "reset_at": None}}}
        quota.save_cache(fresh)
        catalog = [{"id": "claude-agy-gemini-4-hydrogen-high",
                    "agy_id": "gemini-4-hydrogen-high",
                    "title": "Gemini 4 Hydrogen (High)", "category": "Gemini"}]
        with patch.object(agycli, "model_catalog", return_value=catalog):
            got = agycli.state_payload()
        self.assertTrue(got["exhausted"])
        self.assertEqual(got["models"]["claude-agy"], "gemini-carbon")
        self.assertEqual(got["max_sessions"], 2)
        self.assertIn("slots", got)
        self.assertEqual(got["catalog"], catalog)

    def test_model_catalog_uses_agy_and_keeps_categories(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        rows = ("gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)\n"
                "claude-opus-5-5-high\tClaude Opus 5.5 (High)\n"
                "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n")
        run = lambda *a, **k: SimpleNamespace(returncode=0, stdout=rows)
        with patch.object(agycli, "agy_path", return_value="/fake/agy"):
            got = agycli.model_catalog(now=1000, run=run, force=True)
        self.assertEqual([x["category"] for x in got], ["Gemini", "Claude", "GPT"])
        self.assertEqual(got[0]["id"], "claude-agy-gemini-4-hydrogen-high")
        self.assertEqual(got[0]["agy_id"], "gemini-4-hydrogen-high")

    def test_linux_standard_config_is_found(self):
        os.environ.pop("HUB_CONFIG", None)
        os.environ.pop("HUB_CONFIG_DIR", None)
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.dir, "xdg")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        target = os.path.join(os.environ["XDG_CONFIG_HOME"], "hub-core")
        os.makedirs(target)
        with open(os.path.join(target, "hub.json"), "w") as f:
            json.dump({"antigravity": {"models": {"claude-agy": "gemini-test"}}}, f)
        self.assertEqual(store.settings()["models"]["claude-agy"], "gemini-test")

    def test_instance_config_is_taken_from_anchor(self):
        os.environ.pop("HUB_CONFIG", None)
        os.environ.pop("HUB_CONFIG_DIR", None)
        state = os.environ["HUB_STATE_DIR"]
        os.makedirs(state)
        target = os.path.join(self.dir, "instance-config")
        with open(os.path.join(state, "instance.json"), "w", encoding="utf-8") as stream:
            json.dump({"name": "personal", "config_dir": target}, stream)
        self.assertEqual(store.config_dir(), target)

    def test_status_shows_login_block_places_usage_and_deepseek(self):
        store.log_usage("s", "claude-agy", {"in": 100, "out": 10, "think": 5, "cache": 50}, 1000, "ok")
        store.log_deepseek("flash", 10, 5, 0.01)
        text = "\n".join(agycli.status())
        for part in ("блока нет", "1: свободно", "ходы за 5 ч: 1", "вход 100", "0.0100 $ из 0.50 $", "claude-agy → gemini-carbon",
                     "тихие часы: выключены", "исполнители на claude-agy: да"):
            self.assertIn(part, text)
        store.set_block("quota", time.time() + 600, "Antigravity: квота до 12:00")
        self.assertIn("блок до", "\n".join(agycli.status()))

    def test_unblock_clears_the_block(self):
        store.set_block("auth", time.time() + 600, "нужен вход")
        self.assertIn("блок снят", agycli.handle(["unblock"])["out"])
        self.assertIsNone(store.block())
        self.assertIn("не было", agycli.handle(["unblock"])["out"])

    def test_unknown_arguments_print_usage(self):
        r = agycli.handle(["нет"])
        self.assertEqual(r["code"], 2)
        self.assertIn("hub antigravity", r["err"])

    def test_setup_is_for_the_owner_only(self):
        r = agycli.handle(["setup", "--dir", os.path.join(self.dir, "g")], caller_owner=False)
        self.assertEqual(r["code"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "g")))

    def test_doctor_reports_each_check(self):
        os.environ["HUB_MOD_ANTIGRAVITY_AGY"] = os.path.join(self.dir, "нет-agy")
        names = {c["name"]: c for c in agycli.doctor()}
        self.assertFalse(names["antigravity: agy"]["ok"])
        self.assertTrue(names["antigravity: модели"]["ok"])
        self.assertTrue(names["antigravity: блок Gemini"]["ok"])
        self.assertTrue(names["antigravity: блок Claude/GPT"]["ok"])
        self.assertFalse(names["antigravity: ключ DeepSeek"]["ok"])      # engines.json нет
        for c in names.values():
            self.assertIsInstance(c["ok"], bool)
            self.assertLessEqual(len(c["detail"]), 500)

    def test_doctor_models_check_accepts_primary_model_without_models_map(self):
        os.environ["HUB_MOD_ANTIGRAVITY_AGY"] = os.path.join(self.dir, "нет-agy")
        self.hub({"antigravity": {}})                                    # models пусто, primary_model — из умолчаний
        c = {c["name"]: c for c in agycli.doctor()}["antigravity: модели"]
        self.assertTrue(c["ok"])
        self.assertIn("gemini-4-hydrogen-high", c["detail"])
        self.hub({"antigravity": {"primary_model": ""}})                 # нет ни models, ни primary_model
        c = {c["name"]: c for c in agycli.doctor()}["antigravity: модели"]
        self.assertFalse(c["ok"])
        self.assertIn("primary_model", c["detail"])

    def test_plist_check_reads_only(self):
        plist = os.path.join(self.dir, "x.plist")
        with open(plist, "w") as f:
            f.write(f"<dict><key>HUB_CLAUDE</key>\n<string>{agycli.SHELL}</string></dict>")
        self.assertEqual(agycli.plist_has_shell(plist)[0], True)
        with open(plist, "w") as f:
            f.write("<dict><key>HUB_X</key><string>1</string></dict>")
        self.assertEqual(agycli.plist_has_shell(plist)[0], False)
        self.assertIsNone(agycli.plist_has_shell(os.path.join(self.dir, "нет"))[0])

    def test_linux_doctor_checks_effective_wrapper(self):
        self.assertFalse(agycli.service_has_shell(platform="linux", env={})[0])
        self.assertTrue(agycli.service_has_shell(
            platform="linux", env={"HUB_CLAUDE": agycli.SHELL}
        )[0])
        wrapper = os.path.join(MODULE, "bin", "codex")
        self.assertTrue(agycli.service_has_shell(platform="linux", env={"HUB_CODEX_BIN": wrapper}, host="codex")[0])
        self.assertFalse(agycli.service_has_shell(platform="linux", env={}, host="codex")[0])


class Setup(Env):
    def setUp(self):
        super().setUp()
        self.g = os.path.join(self.dir, "gemini")
        os.makedirs(os.path.join(self.g, "config"))
        self.mod = os.path.join(self.dir, "mod")
        os.makedirs(os.path.join(self.mod, "bin"))
        with open(os.path.join(self.g, "config", "mcp_config.json"), "w") as f:
            json.dump({"mcpServers": {"other": {"command": "x"}}, "keep": 1}, f)
        # слои хаба и проекты — во временной папке: настоящие ~/.hub-personal и ~/code тесты не читают
        self.layers = os.path.join(self.dir, "layers")
        self.plugins = os.path.join(self.layers, "2.0.0-team", "plugins")
        for plugin, skill in (("one", "alpha"), ("two", "beta")):
            d = os.path.join(self.plugins, plugin, "skills", skill)
            os.makedirs(d)
            with open(os.path.join(d, "SKILL.md"), "w") as f:
                f.write(f"---\nname: {skill}\ndescription: {skill}\n---\n")
        with open(os.path.join(self.layers, "check.json"), "w") as f:
            json.dump({"name": "2.0.0-team"}, f)
        self.code = os.path.join(self.dir, "code")
        os.makedirs(os.path.join(self.code, "proj", ".claude", "skills", "gamma"))
        os.environ["HUB_MOD_ANTIGRAVITY_LAYERS"] = self.layers
        os.environ["HUB_MOD_ANTIGRAVITY_CODE"] = self.code

    def test_setup_writes_three_files_with_a_backup_and_keeps_foreign_entries(self):
        out = agy_setup.setup(self.g, python="/py", claude="/real/claude", here=self.mod)
        mcp = json.load(open(os.path.join(self.g, "config", "mcp_config.json")))
        self.assertEqual(mcp["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(mcp["keep"], 1)
        srv = mcp["mcpServers"]["hub-deepseek"]
        self.assertEqual((srv["command"], os.path.basename(srv["args"][0])), ("/py", "deepseek_mcp.py"))
        self.assertNotIn("key", json.dumps(srv).lower())                  # ключа в записи нет
        hooks = json.load(open(os.path.join(self.g, "config", "hooks.json")))
        self.assertIn("agy_guard.py", json.dumps(hooks["hooks"]["PreToolUse"]))
        allow = json.load(open(os.path.join(self.g, "antigravity-cli", "settings.json")))
        self.assertEqual(allow["permissions"]["allow"], ["mcp(hub-deepseek/*)"])
        baks = [f for f in os.listdir(os.path.join(self.g, "config")) if ".bak-hub-" in f]
        self.assertEqual(len(baks), 1)
        self.assertEqual(json.load(open(os.path.join(self.g, "config", baks[0]))), {"mcpServers": {"other": {"command": "x"}}, "keep": 1})
        self.assertEqual(open(os.path.join(self.mod, "bin", "real-claude")).read().strip(), "/real/claude")
        self.assertEqual(open(os.path.join(self.mod, "bin", "python")).read().strip(), "/py")
        # повтор — «уже настроен», вторая копия не появляется
        again = agy_setup.setup(self.g, python="/py", claude="/real/claude", here=self.mod)
        self.assertTrue(all("уже настроен" in line for line in again[:3]), again)
        self.assertEqual(len([f for f in os.listdir(os.path.join(self.g, "config")) if ".bak-hub-" in f]), 1)

    def test_updated_module_moves_only_existing_managed_paths(self):
        agy_setup.setup(self.g, python="/py", claude="/real/claude", here=self.mod)
        newer = os.path.join(self.dir, "new-module")
        with patch.object(agy_setup, "HERE", newer):
            changed = agy_setup.sync_managed_paths(self.g)
            self.assertEqual(len(changed), 2)
            hooks = json.load(open(os.path.join(self.g, "config", "hooks.json")))
            self.assertIn(os.path.join(newer, "agy_guard.py"), json.dumps(hooks))
            mcp = json.load(open(os.path.join(self.g, "config", "mcp_config.json")))
            self.assertEqual(mcp["mcpServers"]["hub-deepseek"]["args"], [os.path.join(newer, "deepseek_mcp.py")])
            self.assertEqual(mcp["mcpServers"]["other"], {"command": "x"})
            self.assertEqual(agy_setup.sync_managed_paths(self.g), [])

    def test_update_does_not_restore_removed_managed_hook(self):
        agy_setup.setup(self.g, python="/py", claude="/real/claude", here=self.mod)
        hooks_path = os.path.join(self.g, "config", "hooks.json")
        with open(hooks_path, "w", encoding="utf-8") as stream:
            json.dump({"hooks": {"PreToolUse": []}}, stream)
        with patch.object(agy_setup, "HERE", os.path.join(self.dir, "new-module")):
            agy_setup.sync_managed_paths(self.g)
        self.assertEqual(json.load(open(hooks_path))["hooks"]["PreToolUse"], [])

    def test_dry_run_changes_nothing(self):
        before = json.load(open(os.path.join(self.g, "config", "mcp_config.json")))
        out = agy_setup.setup(self.g, dry=True, here=self.mod)
        self.assertTrue(any("будет записан" in x for x in out))
        self.assertEqual(json.load(open(os.path.join(self.g, "config", "mcp_config.json"))), before)
        self.assertFalse(os.path.exists(os.path.join(self.g, "config", "hooks.json")))
        self.assertFalse(os.path.exists(os.path.join(self.mod, "bin", "python")))

    def test_undo_removes_only_our_entries(self):
        agy_setup.setup(self.g, python="/py", claude="/c", here=self.mod)
        with open(os.path.join(self.g, "config", "mcp_config.json")) as f:
            data = json.load(f)
        data["mcpServers"]["added-later"] = {"command": "y"}               # владелец дописал после setup
        with open(os.path.join(self.g, "config", "mcp_config.json"), "w") as f:
            json.dump(data, f)
        out = agy_setup.undo(self.g)
        mcp = json.load(open(os.path.join(self.g, "config", "mcp_config.json")))
        self.assertEqual(sorted(mcp["mcpServers"]), ["added-later", "other"])
        self.assertFalse(os.path.exists(os.path.join(self.g, "config", "hooks.json")))     # файла не было до нас — и нет
        self.assertFalse(os.path.exists(os.path.join(self.g, "antigravity-cli", "settings.json")))
        self.assertEqual(agy_setup.undo(self.g)[0].split(": ")[1], "нашей записи нет")

    def test_hub_and_project_skills_go_to_the_global_agy_config_once(self):
        agy_setup.setup(self.g, python="/py", claude="/c", here=self.mod)
        path = os.path.join(self.g, "config", "skills.json")
        data = json.load(open(path))
        entries = [e["path"] for e in data["entries"]]
        root = os.path.join(store.data_dir(), "skills")
        # скиллы плагинов слоя — копиями в папке модуля (иначе agy берёт имя из пути), потом личные, потом проекты
        self.assertEqual(entries[0], root)
        for skill in ("alpha", "beta"):
            self.assertTrue(os.path.isfile(os.path.join(root, skill, "SKILL.md")), skill)
        self.assertIn(os.path.join(self.code, "proj", ".claude", "skills"), entries)
        self.assertTrue(all(os.path.isdir(p) for p in entries), entries)
        first = open(path).read()
        again = agy_setup.setup(self.g, python="/py", claude="/c", here=self.mod)
        self.assertEqual(open(path).read(), first)                         # повтор ничего не добавляет
        self.assertIn("уже настроен", next(x for x in again if "skills.json" in x))

    def test_foreign_skill_entries_and_keys_stay_and_undo_removes_only_ours(self):
        path = os.path.join(self.g, "config", "skills.json")
        foreign = {"entries": [{"path": "/чужие/скиллы", "exclude": ["x"]}], "inherits": [{"path": "/shared/skills.json"}]}
        with open(path, "w") as f:
            json.dump(foreign, f)
        agy_setup.setup(self.g, python="/py", claude="/c", here=self.mod)
        data = json.load(open(path))
        self.assertEqual(data["entries"][0], {"path": "/чужие/скиллы", "exclude": ["x"]})
        self.assertEqual(data["inherits"], [{"path": "/shared/skills.json"}])
        self.assertGreater(len(data["entries"]), 1)
        agy_setup.undo(self.g)
        self.assertEqual(json.load(open(path)), foreign)                   # чужие записи на месте, наши сняты

    def test_undo_of_a_new_skills_file_removes_it(self):
        path = os.path.join(self.g, "config", "skills.json")
        agy_setup.setup(self.g, python="/py", claude="/c", here=self.mod)
        self.assertTrue(os.path.exists(path))
        agy_setup.undo(self.g)
        self.assertFalse(os.path.exists(path))

    def test_the_active_layer_is_from_check_json_else_the_newest_version(self):
        self.assertEqual(agy_setup.active_layer(self.layers), os.path.join(self.layers, "2.0.0-team"))
        os.remove(os.path.join(self.layers, "check.json"))
        self.assertEqual(agy_setup.active_layer(self.layers), os.path.join(self.layers, "2.0.0-team"))
        os.makedirs(os.path.join(self.layers, "10.0.0-team", "plugins"))
        self.assertEqual(agy_setup.active_layer(self.layers), os.path.join(self.layers, "10.0.0-team"))  # 10 > 2 по версии

    def test_a_new_layer_moves_the_skill_copies_without_duplicates(self):
        agy_setup.setup(self.g, python="/py", claude="/c", here=self.mod)
        root = os.path.join(store.data_dir(), "skills")
        new = os.path.join(self.layers, "2.1.0-team", "plugins", "one", "skills", "delta")
        os.makedirs(new)
        with open(os.path.join(new, "SKILL.md"), "w") as f:
            f.write("---\nname: delta\ndescription: delta\n---\n")
        with open(os.path.join(self.layers, "check.json"), "w") as f:
            json.dump({"name": "2.1.0-team"}, f)
        agy_setup.sync_skills(self.g)
        self.assertTrue(os.path.isfile(os.path.join(root, "delta", "SKILL.md")))   # скилл нового слоя скопирован
        self.assertFalse(os.path.exists(os.path.join(root, "alpha")))              # скиллов старого слоя больше нет
        entries = [e["path"] for e in json.load(open(os.path.join(self.g, "config", "skills.json")))["entries"]]
        self.assertEqual(len(entries), len(set(entries)))                          # дублей нет
        self.assertEqual(agy_setup.sync_skills(self.g)[1], False)                  # второй проход файл не трогает

    def test_after_undo_the_module_loop_does_not_bring_skills_back(self):
        path = os.path.join(self.g, "config", "skills.json")
        agy_setup.setup(self.g, python="/py", claude="/c", here=self.mod)
        agy_setup.undo(self.g)
        self.assertFalse(os.path.exists(path))                            # наших записей не было до нас — и файла нет
        self.assertEqual(agy_setup.sync_skills(self.g), (path, False))    # цикл модуля не возвращает записи
        self.assertFalse(os.path.exists(path))
        agy_setup.setup(self.g, python="/py", claude="/c", here=self.mod)  # setup снимает метку и ставит снова
        self.assertIn("entries", json.load(open(path)))

    def test_a_broken_json_is_not_overwritten(self):
        with open(os.path.join(self.g, "config", "mcp_config.json"), "w") as f:
            f.write("{не json")
        with self.assertRaises(ValueError):
            agy_setup.setup(self.g, here=self.mod)
        self.assertEqual(open(os.path.join(self.g, "config", "mcp_config.json")).read(), "{не json")

    def test_an_empty_or_blank_file_reads_as_empty_dict(self):
        # agy создаёт заготовку 0 байт — живую пробу 27.11 она роняла json.JSONDecodeError
        self.assertEqual(agy_setup.read(os.path.join(self.g, "нет.json")), {})
        path = os.path.join(self.g, "config", "mcp_config.json")
        for blank in ("", "  \n\t\n"):
            with open(path, "w") as f:
                f.write(blank)
            self.assertEqual(agy_setup.read(path), {})
        with open(path, "w") as f:
            f.write("{не json")
        with self.assertRaises(json.JSONDecodeError):
            agy_setup.read(path)

    def test_the_default_python_of_agy_settings_is_the_stable_uv_312(self):
        home = os.path.join(self.dir, "home")
        for name in ("cpython-3.12-macos-aarch64-none", "cpython-3.12.9-macos-aarch64-none",
                     "cpython-3.13.5-macos-aarch64-none"):
            p = os.path.join(home, ".local", "share", "uv", "python", name, "bin", "python3.12")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
            os.chmod(p, 0o755)
        want = os.path.join(home, ".local", "share", "uv", "python", "cpython-3.12.9-macos-aarch64-none", "bin", "python3.12")
        self.assertEqual(agy_setup.uv_python(home), want)                  # 3.12, самый новый; 3.13 не берём
        old = os.environ.get("HUB_AGY_PYTHON")
        os.environ["HUB_AGY_PYTHON"] = "/stable/py"
        try:
            self.assertEqual(agy_setup.uv_python(home), "/stable/py")
            self.assertEqual(agy_setup.merged("mcp", {}, None)["mcpServers"]["hub-deepseek"]["command"], "/stable/py")
        finally:
            if old is None:
                os.environ.pop("HUB_AGY_PYTHON", None)
            else:
                os.environ["HUB_AGY_PYTHON"] = old
        self.assertEqual(agy_setup.merged("mcp", {}, "/given")["mcpServers"]["hub-deepseek"]["command"], "/given")

    def test_setup_never_touches_the_real_gemini_folder_in_tests(self):
        self.assertTrue(self.g.startswith(tempfile.gettempdir()) or "/var/" in self.g)
        self.assertNotEqual(os.path.realpath(self.g), os.path.realpath(os.path.expanduser("~/.gemini")))


HUBSTUB = '''#!%(py)s
import json, os, sys
st = json.load(open(os.environ["STUB_STATE"]))
a = sys.argv[1:]
if a[:2] == ["status", "--json"]:
    print(json.dumps({"sessions": st["sessions"]}))
elif a[:2] == ["budget", "--json"]:
    print(json.dumps({"step": st.get("step", "work")}))
elif a[:2] == ["session", "model"]:
    st["sessions"][a[2]]["model"] = a[3]
    json.dump(st, open(os.environ["STUB_STATE"], "w"))
    open(os.environ["STUB_LOG"], "a").write(a[2] + " " + a[3] + "\\n")
    print("ok")
else:
    sys.exit(9)
'''


class Sweep(Env):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.dir, "stub.json")
        self.log = os.path.join(self.dir, "stub.log")
        self.exe = os.path.join(self.dir, "hub")
        with open(self.exe, "w") as f:
            f.write(HUBSTUB % {"py": sys.executable})
        os.chmod(self.exe, 0o755)
        os.environ.update(STUB_STATE=self.state, STUB_LOG=self.log)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("STUB_STATE", "STUB_LOG")])
        self.orig = sweep.hub
        sweep.hub = lambda: self.exe
        self.addCleanup(setattr, sweep, "hub", self.orig)
        real_pass = sweep.one_pass

        def quiet_pass(argv, *a, **k):    # печать прохода в тестах не нужна (--dry читает агрегатор команды)
            import contextlib
            if "--dry" in argv:
                return real_pass(argv, *a, **k)
            with contextlib.redirect_stdout(io.StringIO()):
                return real_pass(argv, *a, **k)
        sweep.one_pass = quiet_pass
        self.addCleanup(setattr, sweep, "one_pass", real_pass)

    def sessions(self, models, step="work", dead=()):
        with open(self.state, "w") as f:
            json.dump({"step": step, "sessions": {n: {"model": m, "state": "closed" if n in dead else "idle"} for n, m in models.items()}}, f)

    def changes(self):
        try:
            return [x.split() for x in open(self.log).read().splitlines()]
        except OSError:
            return []

    def models(self):
        return {n: s["model"] for n, s in json.load(open(self.state))["sessions"].items()}

    def test_a_block_moves_agy_sessions_where_the_ladder_leads_and_remembers_where_from(self):
        for step, to in (("work", "sonnet"), ("deepseek", "claude-deepseek"), ("conduit", "sonnet")):
            self.sessions({"a": "claude-agy", "b": "sonnet", "c": "claude-agy-argon"}, step=step)
            os.path.exists(self.log) and os.remove(self.log)
            store.write_json(sweep.removed_path(), {})
            store.set_block("quota", time.time() + 3600, "квота", time.time())
            self.assertEqual(sweep.one_pass([]), 0)
            self.assertEqual(sorted(self.changes()), [["a", to], ["c", to]], step)
            rec = sweep.load_removed()
            self.assertEqual((rec["a"]["from"], rec["a"]["to"], rec["c"]["from"]), ("claude-agy", to, "claude-agy-argon"))
            self.assertEqual(self.models()["b"], "sonnet")                # остальных не трогаем

    def test_the_wait_step_moves_nobody(self):
        self.sessions({"a": "claude-agy"}, step="wait")
        store.set_block("auth", time.time() + 3600, "вход", time.time())
        sweep.one_pass([])
        self.assertEqual(self.changes(), [])
        self.assertEqual(sweep.load_removed(), {})

    def test_after_the_block_only_sessions_still_where_sweep_put_them_return(self):
        self.sessions({"a": "claude-agy", "b": "claude-agy", "gone": "sonnet"})
        store.set_block("quota", time.time() + 3600, "квота", time.time())
        sweep.one_pass([])
        self.assertEqual(self.models(), {"a": "sonnet", "b": "sonnet", "gone": "sonnet"})
        # владелец сам перевёл b на opus; блок снят
        data = json.load(open(self.state))
        data["sessions"]["b"]["model"] = "opus"
        json.dump(data, open(self.state, "w"))
        os.remove(self.log)
        store.clear_block()
        sweep.one_pass([])
        self.assertEqual(self.changes(), [["a", "claude-agy"]])           # a вернулась, b — выбор владельца — нет
        self.assertEqual(self.models()["b"], "opus")
        self.assertEqual(sweep.load_removed(), {})                         # записей не осталось

    def test_an_expired_block_is_cleared_and_returns_sessions(self):
        self.sessions({"a": "claude-agy"})
        store.set_block("quota", time.time() + 3600, "квота", time.time())
        sweep.one_pass([])
        store.set_block("quota", time.time() - 5, "квота", time.time() - 3600)    # срок вышел
        sweep.one_pass([])
        self.assertEqual(self.models()["a"], "claude-agy")
        self.assertIsNone(store.block_record())

    def test_dry_run_prints_the_plan_and_changes_nothing(self):
        self.sessions({"a": "claude-agy"})
        store.set_block("quota", time.time() + 3600, "квота", time.time())
        out = agycli.handle(["sweep"])
        self.assertIn("would: hub session model a sonnet", out["out"])
        self.assertEqual(self.changes(), [])
        self.assertEqual(sweep.load_removed(), {})
        self.assertIn("--command", out["out"])

    def test_all_back_is_the_rollback_regardless_of_the_block(self):
        self.sessions({"a": "claude-agy", "b": "claude-agy-argon", "c": "claude-qwen", "d": "sonnet"})
        sweep.one_pass(["--all-back"])
        self.assertEqual(self.models(), {"a": "sonnet", "b": "sonnet", "c": "claude-qwen", "d": "sonnet"})

    def test_closed_sessions_are_left_alone(self):
        self.sessions({"a": "claude-agy"}, dead=("a",))
        store.set_block("quota", time.time() + 3600, "квота", time.time())
        sweep.one_pass([])
        self.assertEqual(self.changes(), [])

    def test_the_plan_is_a_pure_function(self):
        moves, keep = sweep.plan({"a": "claude-agy", "b": "opus"}, {"reason": "quota"}, {}, "claude-deepseek", "deepseek", 0)
        self.assertEqual([(m[0], m[2]) for m in moves], [("a", "claude-deepseek")])
        moves, keep = sweep.plan({"a": "claude-deepseek"}, None, {"a": {"from": "claude-agy", "to": "claude-deepseek"}}, None, "—", 0)
        self.assertEqual([(m[0], m[2]) for m in moves], [("a", "claude-agy")])
        moves, keep = sweep.plan({"a": "sonnet"}, None, {"a": {"from": "claude-agy", "to": "claude-deepseek"}}, None, "—", 0)
        self.assertEqual((moves, keep), ([], {}))


class Guard(Env):
    @patch.dict(os.environ, {"HUB_ON_CODEX": ""})
    def test_only_hub_sessions_are_guarded_and_only_kill_by_pattern(self):
        store.set_conv(UUID, conv="conv-hub", session="s")
        deny = agy_guard.decide({"conversation_id": "conv-hub", "tool_name": "run_command", "tool_input": {"command": "pkill -f node"}})
        self.assertEqual(deny["decision"], "deny")
        self.assertIn("pkill", deny["reason"])
        for cmd in ("/usr/bin/killall node", "echo x; pgrep -f y", "ls && /bin/pkill z"):
            self.assertIsNotNone(agy_guard.decide({"conversation_id": "conv-hub", "tool_name": "run_command", "tool_input": {"command": cmd}}), cmd)
        for cmd in ("ls", "kill 123", "git log --grep pkillish", "echo pgrepx"):
            self.assertIsNone(agy_guard.decide({"conversation_id": "conv-hub", "tool_name": "run_command", "tool_input": {"command": cmd}}), cmd)
        # ручной agy владельца и чужие инструменты — ничего
        with patch.dict(os.environ, {"HUB_SESSION": ""}):
            self.assertIsNone(agy_guard.decide({"conversation_id": "чужая", "tool_name": "run_command", "tool_input": {"command": "pkill x"}}))
        self.assertIsNone(agy_guard.decide({"conversation_id": "conv-hub", "tool_name": "view_file", "tool_input": {"command": "pkill x"}}))

    def test_unknown_hub_conversation_is_denied(self):
        with patch.dict(os.environ, {"HUB_SESSION": "session"}):
            result = agy_guard.decide({"conversationId": "unknown", "toolCall": {"name": "run_command"}})
        self.assertEqual(result["decision"], "deny")

    def test_the_hook_process_prints_json_for_deny_and_stays_silent_otherwise(self):
        store.set_conv(UUID, conv="conv-hub", session="s")
        env = {"PATH": "/usr/bin:/bin", "HUB_STATE_DIR": os.environ["HUB_STATE_DIR"], "HUB_MOD_ANTIGRAVITY_DATA": store.data_dir()}
        run = lambda body, raw=None: subprocess.run([sys.executable, "-I", os.path.join(MODULE, "agy_guard.py")], input=raw or json.dumps(body),
                                                    env=env, capture_output=True, text=True, timeout=20)
        r = run({"conversation_id": "conv-hub", "tool_name": "run_command", "tool_input": {"command": "killall x"}})
        self.assertEqual(json.loads(r.stdout)["decision"], "deny")
        self.assertEqual(r.returncode, 0)
        r = run({"conversation_id": "conv-hub", "tool_name": "run_command", "tool_input": {"command": "ls"}})
        self.assertEqual((r.stdout, r.returncode), ("", 0))
        r = run(None, raw="мусор")
        self.assertEqual((r.stdout, r.returncode), ("", 0))              # сбой хука молчит и пропускает


class Isolation(Env):
    """Запреты ядра (--disallowedTools) -> правила agy (deny.py) -> отказ хука (§5.3)."""

    def test_the_cores_deny_list_becomes_agy_rules(self):
        rules, unmapped = agy_deny.translate(["SendMessage", "ListAgents", "AskUserQuestion", "NotebookEdit", "EnterWorktree",
                                              "Bash(git commit:*)", "Bash(git push:*)", "Edit(//Users/sasha/x/**)"])
        self.assertEqual(unmapped, [])
        self.assertEqual(rules, {"tools": ["ask_question", "edit_notebook", "notebook_edit", "notebook_execution"],
                                 "commands": ["commit", "push"],
                                 "paths": ["/Users/sasha/x"]})
        self.assertEqual(agy_deny.translate(["Edit(/Users/sasha/x/**)", "Edit(/Users/sasha/x/**)"])[0]["paths"],
                         ["/Users/sasha/x"])       # повтором — без дублей

    def test_a_deny_without_an_agy_equivalent_is_named_and_kept_off_agy(self):
        _rules, unmapped = agy_deny.translate(["Read", "Bash(rm:*)", "Task"])
        self.assertEqual(unmapped, ["Read", "Bash(rm:*)", "Task"])
        argv = ["-p", "--input-format", "stream-json", "--disallowedTools", "Read"]
        self.assertIn("Read", route.decide(argv, {"HUB_MOD_ANTIGRAVITY_ROUTE": "1", "HUB_MOD_ANTIGRAVITY_MODEL": "m"})[1])

    def test_rules_stop_the_steps_they_name_and_leave_reads_alone(self):
        rules = {"tools": ["ask_question", "edit_notebook"], "commands": ["commit", "push"], "paths": ["/Users/sasha/x"]}
        self.assertIn("ask_question", agy_deny.check(rules, "ask_question", {}))
        self.assertIn("git commit", agy_deny.check(rules, "run_command", {"CommandLine": "git -C . commit -m x"}))
        self.assertIn("git commit", agy_deny.check(rules, "git_commit", {}))
        self.assertIn("git push", agy_deny.check(rules, "run_command", {"CommandLine": "cd r && git push -u o b"}))
        self.assertIn("/Users/sasha/x", agy_deny.check(rules, "file_change", {"TargetFile": "/Users/sasha/x/a.py"}))
        self.assertIn("/Users/sasha/x", agy_deny.check(rules, "write_file", {"Path": "/Users/sasha/x/notes.txt"}))
        self.assertIn("/Users/sasha/x", agy_deny.check(rules, "run_command", {"CommandLine": "echo hi > /Users/sasha/x/a"}))
        self.assertIn("/Users/sasha/x", agy_deny.check(rules, "file_change", {"path": "a.py"}, cwd="/Users/sasha/x"))
        self.assertIsNone(agy_deny.check(rules, "view_file", {"AbsolutePath": "/Users/sasha/x/a.py"}))
        self.assertIsNone(agy_deny.check(rules, "run_command", {"CommandLine": "git log --oneline"}))
        self.assertIsNone(agy_deny.check({}, "run_command", {"CommandLine": "git commit -m x"}))

    def test_the_guard_does_not_let_a_session_lift_its_own_bans(self):
        rules_dir = agy_deny.protected_roots()[0]
        self.assertIn("хук", agy_deny.guard_hit("write_file", {"Path": os.path.join(rules_dir, f"{UUID}.json")}))
        self.assertIn("хук", agy_deny.guard_hit("run_command", {"CommandLine": f"echo '{{}}' > {rules_dir}/{UUID}.json"}))
        self.assertIn("хук", agy_deny.guard_hit("run_command", {"CommandLine": "sed -i s/x/y/ ~/.gemini/config/hooks.json"}))
        self.assertIsNone(agy_deny.guard_hit("view_file", {"AbsolutePath": os.path.join(rules_dir, f"{UUID}.json")}))
        self.assertIsNone(agy_deny.guard_hit("write_file", {"Path": "/tmp/ok.txt", "Content": "текст"}))
        self.assertIsNone(agy_deny.guard_hit("run_command", {"CommandLine": "echo hi > /tmp/ok"}))

    def test_the_guard_reads_agys_call_shape_and_the_sessions_rules(self):
        store.set_conv(UUID, conv="conv-hub", session="s")
        agy_deny.write_rules(UUID, {"tools": [], "commands": ["commit"], "paths": ["/Users/sasha/x"]})
        body = {"conversationId": "conv-hub", "stepIdx": 1,
                "toolCall": {"id": "t1", "name": "run_command", "args": {"CommandLine": "git commit -m x"}}}
        out = agy_guard.decide(body)
        self.assertEqual(out["decision"], "deny")
        self.assertIn("git commit", out["reason"])
        out = agy_guard.decide({"conversationId": "conv-hub", "toolCall": {"name": "file_change", "args": {"TargetFile": "/Users/sasha/x/a.py"}}})
        self.assertEqual(out["decision"], "deny")
        self.assertIsNone(agy_guard.decide({"conversationId": "conv-hub", "toolCall": {"name": "view_file", "args": {"AbsolutePath": "/Users/sasha/x/a.py"}}}))
        # ручной agy владельца — ничего, даже когда команда под запретом хаба
        self.assertIsNone(agy_guard.decide({"conversationId": "чужая", "toolCall": {"name": "run_command", "args": {"CommandLine": "pkill x"}}}))
        agy_deny.write_rules(UUID, {})       # запретов нет — хук молчит, файл правил снят
        self.assertFalse(os.path.exists(agy_deny.rules_path(UUID)))
        self.assertIsNone(agy_guard.decide(body))

    def test_subagent_and_workflow_tools_are_always_denied_for_hub_sessions(self):
        store.set_conv(UUID, conv="conv-hub", session="s")
        # даже без файла rules/<uuid>.json все инструменты порождения агентов и send_message для сессий хаба закрыты
        self.assertFalse(os.path.exists(agy_deny.rules_path(UUID)))
        for tool in ("invoke_subagent", "define_subagent", "browser_subagent", "run_workflow", "swarm",
                     "manage_subagents", "send_message"):
            self.assertIn(tool, agy_deny.check({}, tool, {}), tool)
            out = agy_guard.decide({"conversationId": "conv-hub", "toolCall": {"name": tool, "args": {"Prompt": "x"}}})
            self.assertIsNotNone(out, tool)
            self.assertEqual(out["decision"], "deny", tool)
            self.assertIn(tool, out["reason"], tool)
            # ручной agy владельца не трогаем
            self.assertIsNone(agy_guard.decide({"conversationId": "чужая", "toolCall": {"name": tool, "args": {}}}))

    def test_real_agy_write_and_notebook_tool_names_are_denied_in_guard(self):
        store.set_conv(UUID, conv="conv-hub", session="s")
        rules, unmapped = agy_deny.translate(["NotebookEdit", "Edit(/Users/sasha/x/**)"])
        self.assertEqual(unmapped, [])
        agy_deny.write_rules(UUID, rules)
        cases = (
            ("write_to_file", {"TargetFile": "/Users/sasha/x/a.py", "CodeContent": "x"}),
            ("replace_file_content", {"TargetFile": "/Users/sasha/x/a.py", "ReplacementContent": "y"}),
            ("multi_replace_file_content", {"TargetFile": "/Users/sasha/x/a.py", "ReplacementChunks": []}),
            ("sed_file", {"Path": "/Users/sasha/x/a.py", "Expression": "s/a/b/"}),
            ("notebook_edit", {"NotebookPath": "/Users/sasha/x/nb.ipynb"}),
            ("notebook_execution", {"NotebookPath": "/Users/sasha/x/nb.ipynb"}),
        )
        rules_dir = agy_deny.protected_roots()[0]
        for tool, args in cases:
            out = agy_guard.decide({"conversationId": "conv-hub", "cwd": "/Users/sasha/x",
                                    "toolCall": {"name": tool, "args": args}})
            self.assertIsNotNone(out, tool)
            self.assertEqual(out["decision"], "deny", tool)
            # правка самого слоя запретов любым из этих инструментов тоже запрещена
            self.assertIn("хук", agy_deny.guard_hit(tool, {"TargetFile": os.path.join(rules_dir, f"{UUID}.json")}), tool)
        # NotebookEdit запрещает все три имени блокнота и за пределами запрещённой папки
        for nb in ("edit_notebook", "notebook_edit", "notebook_execution"):
            out = agy_guard.decide({"conversationId": "conv-hub", "toolCall": {"name": nb, "args": {"Path": "/tmp/ok.ipynb"}}})
            self.assertEqual(out["decision"], "deny", nb)
            self.assertIn(nb, out["reason"], nb)
        # запись за пределы запрещённой папки при cwd внутри неё не даёт ложного отказа из-за Content
        self.assertIsNone(agy_guard.decide({
            "conversationId": "conv-hub",
            "cwd": "/Users/sasha/x",
            "toolCall": {"name": "write_to_file", "args": {"TargetFile": "/tmp/ok.txt", "CodeContent": "hello"}},
        }))


class Pure(Env):
    def test_parse_reads_the_flags_the_core_passes(self):
        argv = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--resume", UUID, "-n", "s",
                "--settings", "/x/hooks-settings.json", "--permission-mode", "plan", "--append-system-prompt-file", "/i.md",
                "--disallowedTools", "SendMessage", "ListAgents", "Edit(/r/**)", "--effort", "high", "--model", "claude-agy",
                "--strict-mcp-config", "--mcp-config", "/m.json", "/n.json", "--plugin-dir", "/p"]
        p = route.parse(argv)
        self.assertEqual((p["session_id"], p["resume"], p["name"], p["mode"], p["effort"], p["model"]),
                         (UUID, True, "s", "plan", "high", "claude-agy"))
        self.assertEqual(p["disallowed"], ["SendMessage", "ListAgents", "Edit(/r/**)"])
        self.assertEqual(p["mcp_config"], ["/m.json", "/n.json"])
        self.assertEqual(p["append_file"], "/i.md")
        self.assertTrue(route.is_stream_session(argv))
        self.assertFalse(route.is_stream_session(["auth", "status"]))
        self.assertFalse(route.is_stream_session(["-p", "x"]))

    def test_decide_covers_every_branch_of_the_spec(self):
        base = {"HUB_MOD_ANTIGRAVITY_ROUTE": "1", "HUB_MOD_ANTIGRAVITY_MODEL": "m"}
        ok = ["-p", "--input-format", "stream-json", "--settings", "/s/hooks-settings.json", "--disallowedTools", "SendMessage", "ListAgents"]
        self.assertEqual(route.decide(ok, base), ("agy", None))
        self.assertEqual(route.decide(ok, {}), ("claude", "нет переменной маршрута"))
        self.assertEqual(route.decide(["auth", "status"], base)[0], "claude")
        self.assertEqual(route.decide(["-p", "--input-format", "stream-json", "--settings", "/s/hooks-settings-liaison.json"], base)[1], "связной")
        # запреты мини-хаба, у которых есть правило agy (deny.translate), остаются на agy
        self.assertEqual(route.decide(ok + ["AskUserQuestion", "NotebookEdit", "EnterWorktree", "Bash(git commit:*)",
                                            "Bash(git push:*)", "Edit(//Users/sasha/x/**)"], base), ("agy", None))
        self.assertIn("мини-хаб", route.decide(ok + ["Read"], base)[1])
        self.assertEqual(route.decide(ok, {"HUB_MOD_ANTIGRAVITY_ROUTE": "1"})[1], "модуль не дал модель agy")

    def test_effort_values_map_to_agy(self):
        self.assertEqual(route.agy_argv("agy", {"effort": "xhigh", "mode": "plan"}, "m"),
                         ["agy", "-p=", "--input-format", "stream-json", "--output-format", "stream-json", "--model", "m", "--effort", "high",
                          "--mode", "plan"])
        self.assertNotIn("--effort", route.agy_argv("agy", {"effort": None}, ""))

    def test_clean_env_drops_claude_logins_and_the_modules_variables(self):
        env = {"PATH": "/p", "HUB_SESSION": "s", "HUB_STATE_DIR": "/st", "CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_AUTH_TOKEN": "t",
               "ANTHROPIC_MODEL": "m", "HUB_MOD_ANTIGRAVITY_ROUTE": "1", "HUB_MOD_ANTIGRAVITY_DATA": "/d", "HUB_ACCOUNT": "work",
               "HOME": "/h"}
        self.assertEqual(route.clean_env(env), {"PATH": "/p", "HUB_SESSION": "s", "HUB_STATE_DIR": "/st",
                                                "HUB_MOD_ANTIGRAVITY_DATA": "/d", "HOME": "/h"})

    def test_agy_errors_are_classified_and_the_reset_time_is_read(self):
        self.assertEqual(route.classify("RESOURCE_EXHAUSTED: quota exceeded"), "quota")
        self.assertEqual(route.classify("HTTP 429 Too Many Requests"), "quota")
        self.assertEqual(route.classify("You have exhausted your quota on this model."), "quota")
        self.assertEqual(route.classify("Your AI credits balance is too low to continue."), "quota")
        self.assertEqual(route.classify("Your organization's prepaid balance is insufficient."), "quota")
        self.assertEqual(route.classify("error: authentication required"), "auth")
        self.assertEqual(route.classify("authentication failed or timed out"), "auth")
        self.assertEqual(route.classify("Error: Please sign in to view available models."), "auth")
        self.assertIsNone(route.classify("segmentation fault"))
        self.assertEqual(route.reset_after("retry after 90 minutes"), 5400)
        self.assertEqual(route.reset_after("resets in 2 hours"), 7200)
        self.assertEqual(route.reset_after("resets in 3d 3h 59m"), 3 * 86400 + 3 * 3600 + 59 * 60)
        self.assertEqual(route.reset_after("try again in 30 seconds"), 30)
        self.assertIsNone(route.reset_after("quota exceeded"))

    def test_our_texts_never_look_like_a_claude_login_failure(self):
        for raw in ("Failed to authenticate: OAuth token revoked", "Invalid API key · Please run /login",
                    "You've hit your usage limit", "API Error: 401 authentication_error", "limit reached"):
            self.assertFalse(route.CORE_TEXT.search(route.scrub(raw)), raw)
        self.assertNotIn("sk-abcdefghijklmnop12345", route.scrub("key=sk-abcdefghijklmnop12345"))

    def test_quiet_window_including_midnight(self):
        def at(h, m=0):
            return time.mktime((2026, 11, 18, h, m, 0, 0, 0, -1))
        self.assertTrue(store.in_quiet("00:00-08:00", at(3)))
        self.assertFalse(store.in_quiet("00:00-08:00", at(8)))
        self.assertTrue(store.in_quiet("22:00-06:00", at(23)))
        self.assertTrue(store.in_quiet("22:00-06:00", at(5, 59)))
        self.assertFalse(store.in_quiet("22:00-06:00", at(12)))
        self.assertFalse(store.in_quiet(None, at(3)))
        self.assertFalse(store.in_quiet("ерунда", at(3)))

    def test_slots_are_taken_in_order_and_given_back(self):
        a, _ = store.take_slot("s1", 2)
        b, _ = store.take_slot("s2", 2)
        c, holders = store.take_slot("s3", 2)
        self.assertEqual((a.n, b.n, c), (1, 2, None))
        self.assertEqual(sorted(holders), ["s1", "s2"])
        a.release()
        d, _ = store.take_slot("s3", 2)
        self.assertEqual(d.n, 1)
        for s in (b, d):
            s.release()

    def test_a_slot_dies_with_the_process_that_holds_it(self):
        code = ("import sys; sys.path.insert(0, %r); import store, time; s, _ = store.take_slot('child', 1); "
                "print('held', flush=True); time.sleep(60)" % MODULE)
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "HUB_MOD_ANTIGRAVITY_DATA": store.data_dir()})
        self.assertEqual(p.stdout.readline().strip(), "held")
        self.assertEqual(store.slot_report(1), [(1, "child")])
        p.kill()
        p.wait()
        self.assertEqual(store.slot_report(1), [(1, None)])

    def test_the_model_to_agy_id_table(self):
        cfg = store.settings()
        self.assertEqual(store.agy_model("claude-agy", cfg), "gemini-carbon")
        self.assertEqual(store.agy_model("claude-agy-argon", cfg), "")
        self.assertEqual(store.agy_model("claude-agy-hydrogen", cfg), "gemini-4-hydrogen-high")
        self.assertEqual(store.agy_model("codex/claude-agy-gemini-4-hydrogen-high", cfg),
                         "gemini-4-hydrogen-high")
        self.assertTrue(store.is_agy_model("claude-agy-claude-opus-5-5-high", cfg))
        self.assertEqual(store.settings()["max_sessions"], 2)
        self.assertIsNone(store.settings()["quiet"])
        self.assertTrue(store.settings()["executors"])      # слово владельца 27.11.2026: исполнители на agy разрешены
        self.assertEqual(store.settings()["deepseek"]["day_usd"], 0.5)

    def test_translate_orders_usage_and_unknowns(self):
        t = translate.Turn(UUID, "claude-agy", {"in": 10, "out": 1, "think": 0, "cache": 0})
        self.assertEqual(t.feed({"event": "nothing"}), [])
        self.assertEqual(t.feed("мусор"), [])
        self.assertEqual(t.feed({"event": "step_update", "step_index": 1, "state": "DONE", "step_type": "checkpoint"}), [])
        t.feed({"event": "result", "status": "SUCCESS", "response": "ответ", "usage": {"input_tokens": 30, "output_tokens": 4}})
        self.assertEqual(t.turn_usage(), {"in": 20, "out": 3, "think": 0, "cache": 0})
        self.assertEqual(t.turn_usage()["in"] >= 0, True)
        # накопительное меньше прошлого (новый процесс): не уходим в минус
        self.assertEqual(translate.minus({"in": 5}, {"in": 9}), {"in": 0, "out": 0, "think": 0, "cache": 0})

    def test_handoff_into_agy_uses_the_claude_transcript(self):
        d = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "-p")
        os.makedirs(d)
        with open(os.path.join(d, f"{UUID}.jsonl"), "w") as f:
            for rec in ({"type": "user", "message": {"role": "user", "content": "[hub:m1] сделай X"}},
                        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "сделал X"}, {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}},
                        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ВЫВОД"}]}},
                        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "служебное"}}):
                f.write(json.dumps(rec) + "\n")
        items = handoff.since_agy_items(UUID, 0.0)
        self.assertEqual(items, [("Владелец", "[hub:m1] сделай X"), ("Ассистент", "сделал X")])      # инструменты и служебное — нет
        self.assertEqual(handoff.since_agy_items(UUID, time.time() + 10), [])                         # agy уже видел
        self.assertEqual(handoff.since_agy_items("нет-такой", 0.0), [])


if __name__ == "__main__":
    unittest.main()
