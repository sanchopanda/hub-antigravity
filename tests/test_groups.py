"""Тесты блоков квоты по группам моделей agy (Gemini и Claude/GPT — «3p»): разбор /usage, группа модели, выбор модели по квоте,
раздельные блоки в store, `hub antigravity status`/doctor/unblock, sweep и мост на фальшивом agy. Живых запросов нет."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Sandbox, wait_until  # noqa: E402

import agycli  # noqa: E402
import quota  # noqa: E402
import store  # noqa: E402
import sweep  # noqa: E402

GEMINI, THIRD = "gemini-4-hydrogen-high", "claude-sonnet-5-5-high"

# живой вывод agy 1.5.18 от 27.11.2026: Gemini 5ч 0%, Claude/GPT 5ч 91%
LIVE_USAGE = json.dumps({"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [
    {"name": "Gemini Models", "buckets": [
        {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.7361, "reset_time": "2026-12-04T06:25:37Z"},
        {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0, "reset_time": "2026-11-27T11:25:37Z"}]},
    {"name": "Claude and GPT models", "buckets": [
        {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.9564, "reset_time": "2026-12-04T10:08:08Z"},
        {"id": "3p-5h", "window": "5h", "remaining_fraction": 0.9127, "reset_time": "2026-11-27T15:08:08Z"}]}]}}})


def rec(g5=50, gw=80, t5=90, tw=95, now=None):
    """Запись кэша квоты: проценты окон двух групп; None — группы нет."""
    def grp(a, b):
        return None if a is None else {"h5": {"pct": a, "reset_at": 1795778737.0}, "weekly": {"pct": b, "reset_at": 1796365537.0}}
    now = time.time() if now is None else now
    return {"ok": True, "at": now, "fetched_at": now, "ttl_s": 300, "gemini": grp(g5, gw), "claude_gpt": grp(t5, tw)}


class Env(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agy-groups-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.old = {k: os.environ.get(k) for k in ("HUB_MOD_ANTIGRAVITY_DATA", "HUB_CONFIG", "HUB_STATE_DIR")}
        os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = os.path.join(self.dir, "data")
        os.environ["HUB_CONFIG"] = os.path.join(self.dir, "hub.json")
        os.environ["HUB_STATE_DIR"] = self.dir
        with open(os.environ["HUB_CONFIG"], "w") as f:
            json.dump({"antigravity": {"models": {"claude-agy": GEMINI}}}, f)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class ParseAndPick(unittest.TestCase):
    def test_live_usage_gives_separate_groups(self):
        got = quota.parse_usage(LIVE_USAGE)
        self.assertEqual((got["gemini"]["h5"]["pct"], got["gemini"]["weekly"]["pct"]), (0, 74))
        self.assertEqual((got["claude_gpt"]["h5"]["pct"], got["claude_gpt"]["weekly"]["pct"]), (91, 96))

    def test_group_of_model(self):
        for model, group in (("gemini-4-neon-high", "gemini"), ("gemini-4-hydrogen-xhigh", "gemini"), ("gemini-3.8-flash-low", "gemini"),
                             ("gemini-pro-orchestrator", "gemini"), ("", "gemini"),
                             ("claude-opus-5-5-high", "3p"), ("claude-sonnet-5-5-medium", "3p"), ("gpt-oss-120b-medium", "3p"),
                             ("fake-model", None)):
            self.assertEqual(quota.group_of_model(model), group, model)

    def test_room_needs_every_known_window_above_zero(self):
        self.assertTrue(quota.group_room(rec(), "gemini"))
        self.assertFalse(quota.group_room(rec(g5=0), "gemini"))
        self.assertFalse(quota.group_room(rec(gw=0), "gemini"))
        self.assertTrue(quota.group_room(rec(g5=0), "3p"))          # у групп окна свои
        self.assertIsNone(quota.group_room(rec(g5=None), "gemini"))  # группы в ответе нет — данных нет
        self.assertIsNone(quota.group_room(None, "gemini"))
        self.assertIsNone(quota.group_room({"ok": False}, "gemini"))

    def test_model_follows_the_live_quota(self):
        # Gemini жив — основная; Gemini пуст — запасная из другой группы; обе пусты — отказ с временем сброса
        self.assertEqual(quota.pick_model(rec(), GEMINI, THIRD), (GEMINI, None))
        self.assertEqual(quota.pick_model(rec(g5=0), GEMINI, THIRD), (THIRD, None))
        self.assertEqual(quota.pick_model(rec(gw=0), GEMINI, THIRD), (THIRD, None))
        model, why = quota.pick_model(rec(g5=0, t5=0), GEMINI, THIRD)
        self.assertIsNone(model)
        self.assertIn("обеих группах", why)
        self.assertRegex(why, r"Gemini до \d\d:\d\d, Claude/GPT до \d\d:\d\d")

    def test_no_data_does_not_stop_the_turn(self):
        self.assertEqual(quota.pick_model(None, GEMINI, THIRD), (GEMINI, None))
        self.assertEqual(quota.pick_model({"ok": False}, GEMINI, THIRD), (GEMINI, None))
        self.assertEqual(quota.pick_model(rec(g5=None), GEMINI, THIRD), (GEMINI, None))

    def test_a_local_block_beats_a_cache_that_still_shows_room(self):
        blocked = {"gemini": {"until": time.time() + 600}}
        self.assertEqual(quota.pick_model(rec(), GEMINI, THIRD, blocked), (THIRD, None))
        both = {**blocked, "3p": {"until": time.time() + 600}}
        self.assertIsNone(quota.pick_model(rec(), GEMINI, THIRD, both)[0])

    def test_a_3p_primary_with_a_3p_fallback_has_nowhere_to_go(self):
        model, why = quota.pick_model(rec(t5=0), THIRD, "claude-opus-5-5-high")
        self.assertIsNone(model)
        self.assertIn("Claude/GPT", why)

    def test_an_unknown_model_id_is_left_alone(self):
        self.assertEqual(quota.pick_model(rec(g5=0), "fake-model", THIRD), ("fake-model", None))

    def test_the_default_primary_is_hydrogen_and_the_fallback_is_sonnet(self):
        cfg = store.DEFAULTS
        self.assertEqual((cfg["primary_model"], cfg["fallback_model"]), (GEMINI, THIRD))


class RealErrorText(unittest.TestCase):
    def test_the_reset_term_of_a_live_429_is_read_when_units_are_written_together(self):
        import route
        # живой 429 agy 1.5.18 на Gemini (27.11.2026)
        text = "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 35m32s."
        self.assertEqual(route.reset_after(text), 35 * 60 + 32)
        self.assertEqual(route.classify(text), "quota")
        self.assertEqual(route.reset_after("Resets in 1h5m10s"), 3910)
        self.assertEqual(route.reset_after("resets in 3d 3h 59m"), 3 * 86400 + 3 * 3600 + 59 * 60)


class Blocks(Env):
    def test_a_group_block_closes_only_its_group(self):
        now = time.time()
        store.set_block("quota", now + 600, "Gemini кончился", now, group="gemini")
        self.assertIsNotNone(store.block("gemini"))
        self.assertIsNone(store.block("3p"))
        self.assertIsNotNone(store.block())                 # без группы — «хоть какой-то блок»
        self.assertFalse(store.all_blocked())
        store.set_block("quota", now + 900, "3p кончился", now, group="3p")
        self.assertEqual(store.block("gemini")["text"], "Gemini кончился")   # запись другой группы не затёрта
        self.assertEqual(store.block("3p")["text"], "3p кончился")
        self.assertTrue(store.all_blocked())

    def test_auth_closes_every_group(self):
        store.set_block("auth", time.time() + 600, "нужен вход")
        self.assertEqual(set(store.blocks()), {"gemini", "3p"})
        self.assertTrue(store.all_blocked())

    def test_one_group_can_be_cleared_alone_and_the_rest_stays(self):
        now = time.time()
        store.set_block("quota", now + 600, "a", now, group="gemini")
        store.set_block("quota", now + 600, "b", now, group="3p")
        store.clear_block("gemini")
        self.assertIsNone(store.block("gemini"))
        self.assertIsNotNone(store.block("3p"))
        store.clear_block("3p")
        self.assertFalse(os.path.exists(store.state_path()))

    def test_expired_records_are_dropped_alive_ones_stay(self):
        now = time.time()
        store.set_block("quota", now - 5, "старый", now - 3600, group="gemini")
        store.set_block("quota", now + 600, "живой", now, group="3p")
        self.assertIsNone(store.block("gemini"))
        self.assertIsNotNone(store.block_record())           # просроченная запись ещё есть
        store.clear_expired(now)
        self.assertEqual(set(json.load(open(store.state_path()))["blocks"]), {"3p"})

    def test_old_single_block_file_still_reads_as_a_block_of_every_group(self):
        store.write_json(store.state_path(), {"reason": "quota", "until": time.time() + 600, "text": "старый блок", "at": time.time()})
        self.assertEqual(set(store.blocks()), {"gemini", "3p"})
        self.assertEqual(store.block("3p")["text"], "старый блок")


class Commands(Env):
    def test_status_tells_a_gemini_block_from_a_3p_block(self):
        now = time.time()
        store.set_block("quota", now + 600, "Antigravity: квота Gemini до 12:00", now, group="gemini")
        text = "\n".join(agycli.status())
        self.assertIn("блок Gemini: до", text)
        self.assertIn("блок Claude/GPT: нет", text)
        store.clear_block()
        store.set_block("quota", now + 600, "Antigravity: квота Claude/GPT до 12:00", now, group="3p")
        text = "\n".join(agycli.status())
        self.assertIn("блок Gemini: нет", text)
        self.assertIn("блок Claude/GPT: до", text)

    def test_status_shows_one_line_when_every_group_is_closed_and_nothing_when_open(self):
        self.assertIn("блока нет", "\n".join(agycli.status()))
        store.set_block("auth", time.time() + 600, "нужен вход")
        text = "\n".join(agycli.status())
        self.assertIn("вход / квота: блок до", text)
        self.assertNotIn("блок Gemini", text)

    def test_status_prints_the_live_quota_of_both_groups(self):
        quota.save_cache(rec(g5=0, gw=74, t5=91, tw=96))
        self.assertIn("Gemini: h5 0%, weekly 74%; Claude/GPT: h5 91%, weekly 96%", "\n".join(agycli.status()))

    def test_doctor_has_a_check_per_group(self):
        now = time.time()
        store.set_block("quota", now + 600, "Antigravity: квота Gemini до 12:00", now, group="gemini")
        names = {c["name"]: c for c in agycli.doctor()}
        self.assertFalse(names["antigravity: блок Gemini"]["ok"])
        self.assertTrue(names["antigravity: блок Claude/GPT"]["ok"])

    def test_unblock_clears_every_group(self):
        now = time.time()
        store.set_block("quota", now + 600, "a", now, group="gemini")
        store.set_block("quota", now + 600, "b", now, group="3p")
        out = agycli.handle(["unblock"])["out"]
        self.assertIn("блок снят", out)
        self.assertEqual(store.blocks(), {})
        self.assertIn("не было", agycli.handle(["unblock"])["out"])


class SweepGroups(Env):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.dir, "status.json")
        self.log = os.path.join(self.dir, "changes.log")
        fake = os.path.join(self.dir, "hub")
        with open(fake, "w") as f:
            f.write(f"""#!{sys.executable}
import json, sys
a = sys.argv[1:]
state = {self.state!r}
if a[:2] == ["status", "--json"]:
    print(open(state).read())
elif a[:2] == ["budget", "--json"]:
    print(json.dumps({{"step": "work"}}))
elif a[:2] == ["session", "model"]:
    open({self.log!r}, "a").write(a[2] + " " + a[3] + "\\n")
    d = json.load(open(state)); d["sessions"][a[2]]["model"] = a[3]; json.dump(d, open(state, "w"))
    print("ok")
""")
        os.chmod(fake, 0o755)
        self._hub = sweep.hub
        sweep.hub = lambda: fake
        self.addCleanup(setattr, sweep, "hub", self._hub)
        json.dump({"step": "work", "sessions": {"a": {"model": "claude-agy", "state": "idle"}}}, open(self.state, "w"))

    def changes(self):
        try:
            return open(self.log).read().split()
        except OSError:
            return []

    def test_a_block_of_one_group_moves_nobody(self):
        now = time.time()
        for group in ("gemini", "3p"):
            store.clear_block()
            store.set_block("quota", now + 3600, "квота", now, group=group)
            self.assertEqual(sweep.one_pass([]), 0)
            self.assertEqual(self.changes(), [], group)       # вторая группа жива: мост уведёт ход сам
        self.assertEqual(sweep.load_removed(), {})

    def test_both_groups_blocked_or_a_login_block_moves_sessions_to_where_the_ladder_leads(self):
        now = time.time()
        store.set_block("quota", now + 3600, "a", now, group="gemini")
        store.set_block("quota", now + 3600, "b", now, group="3p")
        sweep.one_pass([])
        self.assertEqual(self.changes(), ["a", "sonnet"])

    def test_the_session_returns_as_soon_as_one_group_opens(self):
        now = time.time()
        store.set_block("auth", now + 3600, "вход", now)
        sweep.one_pass([])
        self.assertEqual(self.changes(), ["a", "sonnet"])
        os.remove(self.log)
        store.clear_block("3p")                                # 3p снова жива — agy открыт
        sweep.one_pass([])
        self.assertEqual(self.changes(), ["a", "claude-agy"])

    def test_one_expired_group_is_dropped_and_the_live_one_stays(self):
        now = time.time()
        store.set_block("quota", now - 5, "старый", now - 3600, group="gemini")
        store.set_block("quota", now + 3600, "живой", now, group="3p")
        sweep.one_pass([])
        self.assertEqual(set(json.load(open(store.state_path()))["blocks"]), {"3p"})


class BridgeGroups(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox({"antigravity": {"models": {"claude-agy": GEMINI}, "fallback_model": THIRD}})
        self.addCleanup(self.sb.cleanup)

    def cache(self, **kw):
        with open(os.path.join(self.sb.data, "quota.json"), "w") as f:
            json.dump(rec(**kw), f)

    def session(self, scenario="text", by_model=None):
        env = {"HUB_MOD_ANTIGRAVITY_MODEL": GEMINI}
        if by_model:
            env["FAKE_AGY_BY_MODEL"] = json.dumps(by_model)
        p = self.sb.start(scenario, env=env)
        p.read(lambda m: m.get("subtype") == "init")
        return p

    def models_run(self):
        out = []
        for r in self.sb.agy_runs():
            a = r["argv"]
            out.append(a[a.index("--model") + 1] if "--model" in a else None)
        return out

    def blocks(self):
        return json.load(open(os.path.join(self.sb.data, "state.json")))["blocks"]

    def test_empty_gemini_quota_sends_the_turn_to_the_fallback_model(self):
        self.cache(g5=0)
        p = self.session("text")
        p.user("привет")
        r = p.wait_results(1)[0]
        self.assertFalse(r["is_error"], r)
        self.assertEqual(self.models_run(), [THIRD])
        self.assertFalse(os.path.exists(os.path.join(self.sb.data, "state.json")))   # блока нет: ход прошёл

    def test_live_gemini_quota_keeps_the_primary_model(self):
        self.cache()
        p = self.session("text")
        p.user("привет")
        self.assertFalse(p.wait_results(1)[0]["is_error"])
        self.assertEqual(self.models_run(), [GEMINI])

    def test_both_groups_empty_refuse_with_the_nearest_reset_and_start_no_agy(self):
        self.cache(g5=0, t5=0)
        p = self.session("text")
        p.user("привет")
        r = p.wait_results(1)[0]
        self.assertTrue(r["is_error"])
        self.assertIn("обеих группах", r["result"])
        self.assertRegex(r["result"], r"Gemini до \d\d:\d\d")
        self.assertEqual(self.sb.agy_runs(), [])

    def test_a_gemini_quota_error_blocks_only_gemini_and_the_next_turn_goes_on_the_fallback_in_the_same_conversation(self):
        self.cache()                                         # кэш ещё не знает, что Gemini пуст
        p = self.session("quota", by_model={THIRD: "text"})
        p.user("раз")
        r = p.wait_results(1)[0]
        self.assertTrue(r["is_error"])
        self.assertIn("Gemini", r["result"])
        self.assertIn("Claude/GPT", r["result"])             # подсказка: следующий ход пойдёт на другой группе
        self.assertEqual(set(self.blocks()), {"gemini"})
        self.assertEqual(self.blocks()["gemini"]["group"], "gemini")
        p.user("два")
        r2 = p.wait_results(2)[1]
        self.assertFalse(r2["is_error"], r2)
        self.assertEqual(self.models_run(), [GEMINI, THIRD])
        runs = self.sb.agy_runs()
        self.assertNotIn("--conversation", runs[0]["argv"])
        a = runs[1]["argv"]
        self.assertEqual(a[a.index("--conversation") + 1], "conv-1")     # беседа продолжена, а не начата заново

    def test_a_3p_quota_error_blocks_only_3p(self):
        self.cache()
        sb = self.sb
        sb.set_hub({"antigravity": {"models": {"claude-agy": THIRD}}})
        p = sb.start("quota", env={"HUB_MOD_ANTIGRAVITY_MODEL": THIRD})
        p.read(lambda m: m.get("subtype") == "init")
        p.user("раз")
        r = p.wait_results(1)[0]
        self.assertTrue(r["is_error"])
        self.assertIn("Claude/GPT", r["result"])
        self.assertEqual(set(self.blocks()), {"3p"})

    def test_a_block_of_the_other_group_does_not_stop_a_turn(self):
        now = time.time()
        store.write_json(os.path.join(self.sb.data, "state.json"),
                         {"blocks": {"3p": {"reason": "quota", "until": now + 600, "text": "3p закрыт", "at": now, "group": "3p"}}})
        self.cache()
        p = self.session("text")
        p.user("привет")
        self.assertFalse(p.wait_results(1)[0]["is_error"])
        self.assertEqual(self.models_run(), [GEMINI])

    def test_a_gemini_block_alone_moves_the_turn_to_the_fallback(self):
        now = time.time()
        store.write_json(os.path.join(self.sb.data, "state.json"),
                         {"blocks": {"gemini": {"reason": "quota", "until": now + 600, "text": "g закрыт", "at": now, "group": "gemini"}}})
        self.cache()                                          # кэш показывает запас, но блок свежее
        p = self.session("text")
        p.user("привет")
        self.assertFalse(p.wait_results(1)[0]["is_error"])
        self.assertEqual(self.models_run(), [THIRD])

    def test_an_auth_error_blocks_both_groups(self):
        self.cache()
        p = self.session("auth")
        p.user("привет")
        self.assertIn("нужен вход", p.wait_results(1)[0]["result"])
        self.assertEqual(set(self.blocks()), {"gemini", "3p"})


if __name__ == "__main__":
    unittest.main()
