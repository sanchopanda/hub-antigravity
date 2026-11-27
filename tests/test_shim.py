"""Тесты оболочки bin/claude (спека §2.4, §8 п. 4): она стоит на пути всех сессий, поэтому каждая ветка проверена.
Оболочку запускаем копией модуля во временной папке, «настоящий claude» — фальшивый; живой хаб не задействован."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402
from helpers import MODULE, UUID, Sandbox  # noqa: E402

import handoff  # noqa: E402
import store  # noqa: E402


class Shim(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.mod = os.path.join(self.sb.dir, "ext", "antigravity")
        shutil.copytree(MODULE, self.mod, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.shim = os.path.join(self.mod, "bin", "claude")
        with open(os.path.join(self.mod, "bin", "real-claude"), "w") as f:
            f.write(self.sb.claude + "\n")

    def run_shim(self, args, env=None, stdin="", **kw):
        e = self.sb.env("text", **(env or {}))
        e.pop("HUB_MOD_ANTIGRAVITY_ROUTE", None) if (env or {}).get("HUB_MOD_ANTIGRAVITY_ROUTE", "x") is None else None
        return subprocess.run([self.shim, *args], env=e, input=stdin, capture_output=True, text=True, timeout=30,
                              cwd=self.sb.dir, **kw)

    def claude_calls(self):
        return helpers._lines(self.sb.claude_log)

    # -- ветка 1: не сессия на agy — одна проверка и exec настоящего claude
    def test_without_the_route_variable_it_is_the_real_claude_with_the_same_arguments(self):
        args = ["-p", "--input-format", "stream-json", "--model", "sonnet", "с пробелом", "", "--x=1"]
        r = self.run_shim(args, env={"HUB_MOD_ANTIGRAVITY_ROUTE": None})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.claude_calls()[0]["argv"], args)

    def test_this_path_needs_no_python_at_all(self):
        """Путь «не agy» — только sh: без python в PATH и без переменной python он всё равно доходит до claude."""
        os.symlink("/bin/sh", os.path.join(self.sb.dir, "sh"))
        script = os.path.join(self.sb.dir, "claude-sh")
        with open(script, "w") as f:
            f.write('#!/bin/sh\necho "$@" > "$FAKE_CLAUDE_LOG"\n')
        os.chmod(script, 0o755)
        with open(os.path.join(self.mod, "bin", "real-claude"), "w") as f:
            f.write(script + "\n")
        r = self.run_shim(["--version"], env={"HUB_MOD_ANTIGRAVITY_ROUTE": None, "PATH": "/nonexistent",
                                              "HUB_MOD_ANTIGRAVITY_PYTHON": "/нет/python"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(self.sb.claude_log).read().strip(), "--version")

    def test_the_default_real_claude_is_in_home_local_bin(self):
        os.remove(os.path.join(self.mod, "bin", "real-claude"))
        os.makedirs(os.path.join(self.sb.home, ".local", "bin"))
        shutil.copy(self.sb.claude, os.path.join(self.sb.home, ".local", "bin", "claude"))
        r = self.run_shim(["auth", "status"], env={"HUB_MOD_ANTIGRAVITY_ROUTE": None})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.claude_calls()[0]["argv"], ["auth", "status"])

    # -- ветка 2: переменная есть, но это не запуск сессии
    def test_auth_status_and_nested_claude_go_to_the_real_claude(self):
        for args in (["auth", "status", "--json"], ["-p", "привет"], ["--version"], ["-p", "--output-format", "json"]):
            os.remove(self.sb.claude_log) if os.path.exists(self.sb.claude_log) else None
            r = self.run_shim(args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.claude_calls()[0]["argv"], args, args)

    # -- ветка 3: сессия на agy — мост
    def test_a_session_on_agy_goes_to_the_bridge(self):
        r = self.run_shim(self.sb.argv())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.claude_calls(), [])
        first = json.loads(r.stdout.splitlines()[0])
        self.assertEqual((first["type"], first["subtype"], first["model"]), ("system", "init", "claude-agy"))

    def test_a_bridge_without_python_falls_back_to_claude_and_does_not_hang(self):
        r = self.run_shim(self.sb.argv(), env={"HUB_MOD_ANTIGRAVITY_PYTHON": "/нет/python", "PATH": "/nonexistent"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.claude_calls()), 1)                      # понятная ошибка модели у claude, а не тишина
        self.assertIn("claude-agy", self.claude_calls()[0]["argv"])

    # -- случаи, где мост сам отдаёт сессию настоящему claude (§2.4)
    def test_liaison_goes_to_the_real_claude(self):
        liaison = os.path.join(self.sb.dir, "hooks-settings-liaison.json")
        shutil.copy(self.sb.settings, liaison)
        args = self.sb.argv(settings=liaison)
        r = self.run_shim(args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.claude_calls()[0]["argv"], args)

    def test_a_mini_hub_with_bans_agy_cannot_hold_goes_to_the_real_claude(self):
        args = self.sb.argv(deny=("SendMessage", "ListAgents", "Read"))
        self.run_shim(args)
        self.assertEqual(self.claude_calls()[0]["argv"], args)

    def test_the_mini_hubs_bans_that_agy_holds_stay_on_agy(self):
        args = self.sb.argv(deny=("SendMessage", "ListAgents", "AskUserQuestion", "NotebookEdit", "EnterWorktree",
                                  "Bash(git commit:*)", "Bash(git push:*)", "Edit(/repo/**)"))
        r = self.run_shim(args)
        self.assertEqual(self.claude_calls(), [])
        self.assertIn("init", r.stdout)

    def test_a_form_ban_alone_does_not_make_it_a_mini_hub(self):
        r = self.run_shim(self.sb.argv(deny=("SendMessage", "ListAgents", "AskUserQuestion")))
        self.assertEqual(self.claude_calls(), [])
        self.assertIn("init", r.stdout)

    def test_no_agy_model_from_the_module_goes_to_the_real_claude(self):
        args = self.sb.argv()
        e = self.sb.env("text")
        e.pop("HUB_MOD_ANTIGRAVITY_MODEL")
        r = subprocess.run([self.shim, *args], env=e, input="", capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.claude_calls()[0]["argv"], args)             # claude откажет моделью claude-agy, а не уйдёт на Sonnet

    # -- передача итога agy -> другой движок (§6.3)
    def handoff_env(self):
        proj = os.path.join(self.sb.home, ".claude")
        return {"HUB_MOD_ANTIGRAVITY_ROUTE": "handoff", "CLAUDE_CONFIG_DIR": proj}, proj

    def agy_turn(self, at, req="что сделано?", resp="сделал тесты"):
        os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = self.sb.data
        self.addCleanup(os.environ.pop, "HUB_MOD_ANTIGRAVITY_DATA", None)
        store.log_turn(UUID, "agy", req, resp, ["run_command"], now=at)

    def transcript(self, proj, mtime):
        d = os.path.join(proj, "projects", "-tmp-x")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{UUID}.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "старый вопрос"}}) + "\n")
        os.utime(path, (mtime, mtime))

    def test_agy_turns_after_the_last_claude_turn_go_into_a_copy_of_the_instructions(self):
        env, proj = self.handoff_env()
        self.transcript(proj, time.time() - 1000)
        self.agy_turn(time.time() - 100)
        args = self.sb.argv(resume=True, model="sonnet")
        r = self.run_shim(args, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.claude_calls()[0]["argv"]
        new = got[got.index("--append-system-prompt-file") + 1]
        self.assertNotEqual(new, self.sb.instr)
        text = open(new).read()
        self.assertTrue(text.startswith("Правила хаба: пиши по-русски."))     # исходный файл — первым
        self.assertIn("Работа шла на Antigravity", text)
        self.assertIn("сделал тесты", text)
        self.assertEqual(open(self.sb.instr).read(), "Правила хаба: пиши по-русски.")   # исходный файл не тронут
        self.assertIn("--resume", got)                                     # у Claude разговор есть: resume остаётся
        self.assertEqual(got.count("--append-system-prompt-file"), 1)      # флаг один
        # второй запуск того же — передача уже сделана, аргументы как пришли
        os.remove(self.sb.claude_log)
        self.run_shim(args, env=env)
        self.assertEqual(self.claude_calls()[0]["argv"], args)

    def test_without_a_claude_transcript_resume_becomes_session_id(self):
        env, proj = self.handoff_env()
        self.agy_turn(time.time() - 100)
        r = self.run_shim(self.sb.argv(resume=True, model="sonnet"), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.claude_calls()[0]["argv"]
        self.assertNotIn("--resume", got)
        self.assertEqual(got[got.index("--session-id") + 1], UUID)

    def test_nothing_to_hand_over_leaves_the_arguments_alone(self):
        env, proj = self.handoff_env()
        self.transcript(proj, time.time())
        self.agy_turn(time.time() - 500)                                  # ходы agy старше последнего хода Claude
        args = self.sb.argv(resume=True, model="sonnet")
        self.run_shim(args, env=env)
        self.assertEqual(self.claude_calls()[0]["argv"], args)

    def test_no_instruction_flag_gets_one_with_snapshot_off(self):
        env, proj = self.handoff_env()
        self.agy_turn(time.time() - 100)
        self.run_shim(self.sb.argv(resume=True, model="sonnet", instr=False), env=env)
        got = self.claude_calls()[0]["argv"]
        self.assertIn("--append-system-prompt-file", got)
        self.assertEqual(got[got.index("--system-prompt-snapshot") + 1], "off")

    def test_a_failing_handoff_still_starts_the_real_claude_with_the_original_arguments(self):
        env, proj = self.handoff_env()
        args = self.sb.argv(resume=True, model="sonnet")
        env["HUB_MOD_ANTIGRAVITY_DATA"] = "/proc/нет/такой/папки"        # данные недоступны: rewrite упадёт
        r = self.run_shim(args, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.claude_calls()[0]["argv"], args)

    def test_a_new_session_id_is_not_a_handoff(self):
        env, proj = self.handoff_env()
        self.agy_turn(time.time() - 100)
        args = self.sb.argv(resume=False, model="sonnet")
        self.run_shim(args, env=env)
        self.assertEqual(self.claude_calls()[0]["argv"], args)


class Summary(unittest.TestCase):
    def test_summary_keeps_the_last_turns_whole_and_older_ones_by_their_heads_within_the_limit(self):
        items = [("Владелец" if i % 2 == 0 else "Ассистент", f"ход {i}: " + "слово " * 400) for i in range(40)]
        out = handoff.summarize(items)
        self.assertLessEqual(len(out), handoff.LIMIT)
        self.assertIn("ход 39:", out)
        self.assertIn(("слово " * 400).strip()[:500], out.split("ход 39:")[1])    # последний — целиком (до ITEM)
        self.assertNotIn("слово " * 100, out.split("ход 0:")[1].split("\n")[0] if "ход 0:" in out else "")   # старый — голова
        short = handoff.summarize([("Владелец", "один"), ("Ассистент", "два")])
        self.assertEqual(short, "Владелец: один\nАссистент: два")


if __name__ == "__main__":
    unittest.main()
