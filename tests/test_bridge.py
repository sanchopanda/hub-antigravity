"""Тесты моста на фальшивом agy (спека §8, пункты 1–3): перевод событий, usage, склейка сообщений, режимы, окружение,
слоты, тихие часы, блоки, control_request, хуки. Живых запросов к Google нет."""
import base64
import json
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402
from helpers import UUID, Sandbox, wait_until  # noqa: E402

import agy_guard  # noqa: E402
import route  # noqa: E402
import store  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def session(self, scenario="text", **kw):
        p = self.sb.start(scenario, **kw)
        p.read(lambda m: m.get("subtype") == "init")
        return p

    def hook_events(self):
        return [h["event"] for h in self.sb.hooks()]

    def stops(self, n=1):
        """Stop-хук идёт после result (§5.1): дождаться n штук."""
        self.assertTrue(wait_until(lambda: self.hook_events().count("Stop") >= n), self.hook_events())


class Translation(Base):
    def test_text_turn_goes_to_core_as_claude_code_events(self):
        p = self.session("text")
        p.user("[hub:m1] привет")
        res = p.wait_results(1)[0]
        types = [m["type"] for m in p.seen]
        self.assertEqual(types, ["system", "assistant", "result"])
        a = p.seen[1]
        self.assertEqual(a["message"]["content"], [{"type": "text", "text": "Привет, мир."}])
        self.assertEqual(a["message"]["usage"]["input_tokens"], 120)        # длина контекста из usage шага
        self.assertFalse(res["is_error"])
        self.assertEqual(res["result"], "Привет, мир.")
        self.assertEqual(res["usage"], {"input_tokens": 120, "output_tokens": 8, "cache_read_input_tokens": 100,
                                        "cache_creation_input_tokens": 0})
        self.assertIsNone(res["total_cost_usd"])
        self.assertGreaterEqual(res["num_turns"], 1)                         # ноль — это /compact для ядра
        init = p.seen[0]
        self.assertEqual(init["model"], "claude-agy")
        self.assertEqual(init["mcp_servers"], [])

    def test_a_tool_with_output_and_with_error(self):
        p = self.session("tool_ok")
        p.user("ls")
        p.wait_results(1)
        use = next(m for m in p.seen if m["type"] == "assistant" and m["message"]["content"][0]["type"] == "tool_use")
        block = use["message"]["content"][0]
        self.assertEqual((block["name"], block["input"]), ("run_command", {"command": "ls"}))
        res = next(m for m in p.seen if m["type"] == "user")["message"]["content"][0]
        self.assertEqual((res["tool_use_id"], res["content"], res["is_error"]), (block["id"], "a.txt\nb.txt", False))
        p.close()
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        q = sb.start("tool_err")
        q.read(lambda m: m.get("subtype") == "init")
        q.user("nope")
        q.wait_results(1)
        res = next(m for m in q.seen if m["type"] == "user")["message"]["content"][0]
        self.assertTrue(res["is_error"])
        self.assertIn("command not found", res["content"])

    def test_tool_step_with_state_error_sends_tool_result(self):
        # живая проба 27.11 (agy 1.5.18): отказ pre-tool хука и ошибка инструмента идут со state="ERROR", не "DONE"
        p = self.session("tool_state_error")
        p.user("сделай коммит")
        r = p.wait_results(1)[0]
        self.assertFalse(r["is_error"])
        use = next(m for m in p.seen if m["type"] == "assistant" and m["message"]["content"][0]["type"] == "tool_use")
        res = next(m for m in p.seen if m["type"] == "user")["message"]["content"][0]
        self.assertEqual(res["tool_use_id"], use["message"]["content"][0]["id"])
        self.assertTrue(res["is_error"])
        self.assertIn("git commit", res["content"])

    def test_an_mcp_call_is_a_step_with_its_name(self):
        p = self.session("mcp_call")
        p.user("спроси")
        p.wait_results(1)
        use = next(m for m in p.seen if m["type"] == "assistant" and m["message"]["content"][0]["type"] == "tool_use")
        self.assertEqual(use["message"]["content"][0]["name"], "mcp_hub-deepseek_deepseek_ask")
        res = next(m for m in p.seen if m["type"] == "user")["message"]["content"][0]
        self.assertEqual(json.loads(res["content"]), {"text": "4"})   # вывод инструмента — как JSON-текст

    def test_cumulative_usage_is_subtracted_per_turn(self):
        p = self.session("two_turns")
        p.user("один")
        first = p.wait_results(1)[0]
        p.user("два")
        second = p.wait_results(2)[1]
        self.assertEqual(first["usage"]["input_tokens"], 100)
        self.assertEqual(second["usage"], {"input_tokens": 160, "output_tokens": 15, "cache_read_input_tokens": 120,
                                           "cache_creation_input_tokens": 0})
        rows = store_rows(self.sb)
        self.assertEqual([(r["in"], r["out"], r["think"], r["cache"]) for r in rows], [(100, 10, 4, 50), (160, 15, 5, 120)])

    def test_unknown_events_and_checkpoints_are_skipped(self):
        p = self.session("unknown_event")
        p.user("x")
        res = p.wait_results(1)[0]
        self.assertEqual(res["result"], "ок")
        self.assertFalse([m for m in p.seen if m.get("_bad")])

    def test_text_delta_streams_paragraphs_during_active_without_duplication(self):
        import translate
        turn = translate.Turn("u1", "claude-agy")
        turn.feed({"event": "init", "conversation_id": "conv-stream"})
        # Незакрытый кусок без конца абзаца ждёт
        self.assertEqual(turn.feed({"event": "step_update", "step_index": 1, "state": "ACTIVE",
                                    "step_type": "agent_response", "text_delta": "Первый "}), [])
        # После \n\n первый абзац сразу уходит в поток ещё на ACTIVE
        got = turn.feed({"event": "step_update", "step_index": 1, "state": "ACTIVE",
                         "step_type": "agent_response", "text_delta": "абзац.\n\n```py\nprint(1)\n"})
        self.assertEqual([m["message"]["content"][0]["text"] for m in got], ["Первый абзац."])
        # Незакрытый блок кода не режется на \n\n до закрытия ```
        self.assertEqual(turn.feed({"event": "step_update", "step_index": 1, "state": "ACTIVE",
                                    "step_type": "agent_response", "text_delta": "\nx = 2\n"}), [])
        # После закрытия блока кода и \n\n отдаётся сразу
        got2 = turn.feed({"event": "step_update", "step_index": 1, "state": "ACTIVE",
                          "step_type": "agent_response", "text_delta": "```\n\nХвост"})
        self.assertEqual([m["message"]["content"][0]["text"] for m in got2], ["```py\nprint(1)\n\nx = 2\n```"])
        # При DONE отдаётся только остаток "Хвост", без дублирования
        done = turn.feed({"event": "step_update", "step_index": 1, "state": "DONE",
                          "step_type": "agent_response", "text_delta": ""})
        self.assertEqual([m["message"]["content"][0]["text"] for m in done], ["Хвост"])
        # На result дублирования нет
        res = turn.feed({"event": "result", "status": "SUCCESS",
                         "response": "Первый абзац.\n\n```py\nprint(1)\n\nx = 2\n```\n\nХвост"})
        self.assertEqual(res, [])

    def test_messages_that_come_during_a_turn_go_as_one_turn_after_it(self):
        p = self.session("slow_echo")
        p.user("[hub:m1] первое")
        p.user("[hub:m2] второе")
        p.user("[hub:m3] третье")
        p.wait_results(2)
        turns = self.sb.agy_turns()
        self.assertEqual(len(turns), 2)
        self.assertIn("[hub:m1] первое", turns[0]["content"])
        self.assertTrue(turns[1]["content"].endswith("[hub:m2] второе\n[hub:m3] третье"), turns[1]["content"])
        prompts = [h["body"] for h in self.sb.hooks() if h["event"] == "UserPromptSubmit"]
        self.assertEqual(prompts[1]["prompt"], "[hub:m2] второе\n[hub:m3] третье")   # хук ядра найдёт обе метки (also)

    def test_compact_and_recap_pass_to_agy_as_numbers_for_the_core(self):
        p = self.session("slash")
        p.user("/compact")
        r = p.wait_results(1)[0]
        self.assertFalse(r["is_error"])
        self.assertEqual(r["num_turns"], 0)                               # ноль — ядро ставит session.compacted
        p.user("/recap")
        r2 = p.wait_results(2)[1]
        self.assertFalse(r2["is_error"])
        self.assertEqual(r2["num_turns"], 0)                              # ноль — ядро берёт session.recap из result
        self.assertEqual(r2["result"], "### Краткий итог сессии")
        self.assertEqual([t["content"] for t in self.sb.agy_turns()], ["/compact", "/recap"])   # уходят как есть
        self.assertEqual(self.hook_events(), ["SessionStart"])            # это не ходы: ни UserPromptSubmit, ни Stop

    def test_slash_commands_agy_has_no_are_refused_without_a_turn(self):
        p = self.session("text")
        p.user("/clear")
        r = p.wait_results(1)[0]
        self.assertEqual((r["is_error"], r["num_turns"]), (True, 0))
        self.assertEqual(r["result"], "Antigravity: /clear недоступна")
        self.assertEqual(self.hook_events(), ["SessionStart"])            # это не ход: ни UserPromptSubmit, ни Stop
        self.assertEqual(self.sb.agy_runs(), [])                          # и agy не запускался

    def test_an_image_goes_to_agy_as_a_file_it_can_read(self):
        p = self.session("echo")
        data = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-image").decode()
        p.send({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "Какого цвета квадрат?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]}})
        p.wait_results(1)
        turn = self.sb.agy_turns()[0]["content"]
        m = re.search(r"\[Картинка: (\S+) — посмотри этот файл\]", turn)
        self.assertTrue(m, turn)
        path = m.group(1)
        self.assertTrue(os.path.isfile(path), path)
        self.assertEqual(open(path, "rb").read(), b"\x89PNG\r\n\x1a\nfake-image")
        self.assertIn("Какого цвета квадрат?", turn)
        self.assertNotIn("картинка не передана", turn)


class Hooks(Base):
    def test_hooks_run_in_claude_code_order_with_its_json(self):
        p = self.session("text")
        p.user("[hub:m7] раз\n[hub:m8] два")
        p.wait_results(1)
        p.close()
        self.assertEqual(self.hook_events(), ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"])
        start, prompt, stop, end = (h["body"] for h in self.sb.hooks())
        self.assertEqual((start["source"], start["session_id"]), ("startup", UUID))
        self.assertEqual(prompt["prompt"], "[hub:m7] раз\n[hub:m8] два")
        self.assertEqual(end["reason"], "other")
        self.assertTrue(all(h["session"] == "agy-s" and h["state"] == self.sb.state for h in self.sb.hooks()))

    def test_resume_is_told_to_the_start_hook(self):
        p = self.sb.start("text", resume=True)
        p.read(lambda m: m.get("subtype") == "init")
        p.close()
        self.assertEqual(self.sb.hooks()[0]["body"]["source"], "resume")

    def test_user_prompt_hook_comes_before_the_first_word_of_the_model(self):
        p = self.session("text")
        p.user("x")
        p.wait_results(1)
        self.assertEqual(self.hook_events()[:2], ["SessionStart", "UserPromptSubmit"])
        self.assertLess(len(self.sb.hooks()), 4)                          # Stop придёт после result, не раньше

    def test_a_failing_or_missing_hook_does_not_break_the_session(self):
        with open(self.sb.settings, "w") as f:
            json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "exit 3"}]}],
                                 "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/нет/такой"}]}]}}, f)
        p = self.session("text")
        p.user("x")
        self.assertFalse(p.wait_results(1)[0]["is_error"])


class Process(Base):
    def test_permission_modes_become_agy_flags(self):
        for mode, want in (("bypassPermissions", ["--dangerously-skip-permissions"]), ("plan", ["--mode", "plan"]),
                           ("acceptEdits", ["--mode", "accept-edits"]), ("default", ["--mode", "accept-edits"])):
            self.assertEqual(route.mode_flags(mode), want)
        p = self.sb.start("text", mode="plan")
        p.read(lambda m: m.get("subtype") == "init")
        p.user("x")
        p.wait_results(1)
        argv = self.sb.agy_runs()[0]["argv"]
        # живая проба 27.11 (agy 1.5.15): `-p` без значения съедает `--input-format` как промпт — только `-p=`
        self.assertEqual(argv[:5], ["-p=", "--input-format", "stream-json", "--output-format", "stream-json"])
        self.assertIn("plan", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "fake-model")

    def test_headless_denied_actions_turn_reports_error_instead_of_silence(self):
        # живая проба 27.11: в --mode accept-edits без диалога agy возвращает status=SUCCESS, response="" и denied_actions
        p = self.sb.start("denied_action", mode="acceptEdits")
        p.read(lambda m: m.get("subtype") == "init")
        p.user("echo hi")
        r = p.wait_results(1)[0]
        self.assertTrue(r["is_error"])
        self.assertIn("acceptEdits", r["result"])
        self.assertIn("Bash", r["result"])
        self.assertFalse(route.CORE_TEXT.search(r["result"]), r["result"])
        self.assertEqual(self.sb.agy_runs()[0]["argv"][-2:], ["--mode", "accept-edits"])

    def test_default_model_means_no_model_flag(self):
        p = self.sb.start("text", env={"HUB_MOD_ANTIGRAVITY_MODEL": ""})
        p.read(lambda m: m.get("subtype") == "init")
        p.user("x")
        p.wait_results(1)
        self.assertNotIn("--model", self.sb.agy_runs()[0]["argv"])

    def test_the_claude_login_does_not_reach_agy(self):
        p = self.session("text")
        p.user("x")
        p.wait_results(1)
        run = self.sb.agy_runs()[0]
        self.assertEqual(run["leak"], [])
        names = set(run["env_names"])
        for bad in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "HUB_ACCOUNT",
                    "HUB_MOD_ANTIGRAVITY_ROUTE", "HUB_MOD_ANTIGRAVITY_MODEL", "HUB_MOD_ANTIGRAVITY_AGY"):
            self.assertNotIn(bad, names)
        self.assertTrue({"HUB_SESSION", "HUB_STATE_DIR", "PATH"} <= names)   # `hub` из agy работает от имени сессии

    def test_control_requests_never_reach_agy(self):
        p = self.session("text")
        p.user("x")
        p.wait_results(1)
        p.send({"type": "control_request", "request_id": "r1", "request": {"subtype": "mcp_status"}})
        p.send({"type": "control_request", "request_id": "r2", "request": {"subtype": "mcp_reconnect", "serverName": "s"}})
        p.send({"type": "control_response", "response": {"subtype": "success", "request_id": "zz"}})
        p.read(lambda m: m.get("type") == "control_response" and m["response"]["request_id"] == "r2")
        a1 = next(m for m in p.seen if m.get("type") == "control_response" and m["response"]["request_id"] == "r1")
        self.assertEqual((a1["response"]["subtype"], a1["response"]["response"]), ("success", {"mcpServers": []}))
        a2 = next(m for m in p.seen if m.get("type") == "control_response" and m["response"]["request_id"] == "r2")
        self.assertEqual(a2["response"]["subtype"], "error")
        self.assertTrue(a2["response"]["error"].startswith("Unsupported control request subtype"))   # так ядро видит «нет»
        self.assertFalse([r for r in helpers._lines(self.sb.agy_log) if "rejected" in r])

    def test_one_agy_process_per_session_even_for_many_turns(self):
        p = self.session("two_turns")
        p.user("1")
        p.wait_results(1)
        p.user("2")
        p.wait_results(2)
        self.assertEqual(len(self.sb.agy_runs()), 1)

    def test_agy_that_leaves_between_turns_is_started_again_with_the_conversation(self):
        p = self.session("exit_after")
        p.user("раз")
        p.wait_results(1)
        self.assertTrue(wait_until(lambda: not [s for s in store_report(self.sb) if s[1]]))   # место освободилось
        p.user("два")
        self.assertTrue(wait_until(lambda: len(self.sb.agy_runs()) == 2))
        argv = self.sb.agy_runs()[1]["argv"]
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-1")

    def test_closing_stdin_ends_the_session_and_the_process(self):
        p = self.session("text")
        p.user("x")
        p.wait_results(1)
        self.assertEqual(p.close(), 0)
        self.assertEqual(self.hook_events()[-1], "SessionEnd")

    def test_a_crash_in_the_middle_of_a_turn_ends_it_with_an_error_and_the_next_turn_resumes_the_conversation(self):
        p = self.session("crash")
        p.user("x")
        r = p.wait_results(1)[0]
        self.assertTrue(r["is_error"])
        self.assertIn("139", r["result"])
        self.assertIn("agy", r["result"])
        self.stops()                                                       # ход закрыт: ядро не зависнет в «ход идёт»
        p.user("ещё раз")
        self.assertTrue(wait_until(lambda: len(self.sb.agy_runs()) == 2))
        argv = self.sb.agy_runs()[1]["argv"]
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-1")   # беседа agy продолжена

    def test_an_interrupt_stops_the_turn_and_the_next_one_continues_the_conversation(self):
        p = self.session("hang")
        p.user("долго")
        self.assertTrue(wait_until(lambda: len(self.sb.agy_runs()) == 1))
        p.send({"type": "control_request", "request_id": "int-1", "request": {"subtype": "interrupt"}})
        r = p.wait_results(1)[0]
        self.assertEqual((r["is_error"], r["result"]), (True, "прервано"))
        self.stops()
        self.assertTrue(any(m.get("type") == "control_response" and m["response"]["request_id"] == "int-1" for m in p.seen))
        p.user("дальше")
        self.assertTrue(wait_until(lambda: len(self.sb.agy_runs()) == 2))
        argv = self.sb.agy_runs()[1]["argv"]
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-1")

    def test_a_silent_agy_is_removed_after_the_idle_limit(self):
        p = self.session("hang", env={"HUB_MOD_ANTIGRAVITY_IDLE": "1.5"})
        p.user("x")
        r = p.wait_results(1, timeout=20)[0]
        self.assertTrue(r["is_error"])
        self.assertIn("молчит", r["result"])

    def test_a_long_step_without_output_keeps_the_turn_and_sends_keepalive(self):
        p = self.session("long_step", env={"HUB_MOD_ANTIGRAVITY_KEEPALIVE": "1"})
        p.user("x")
        r = p.wait_results(1)[0]
        self.assertEqual(r["result"], "готово")
        self.assertTrue([m for m in p.seen if m.get("type") == "system" and m.get("subtype") == "status"])   # ядро видит «жив»

    def test_sigterm_closes_the_turn_and_exits(self):
        p = self.session("hang")
        p.user("x")
        self.assertTrue(wait_until(lambda: len(self.sb.agy_runs()) == 1))
        p.p.terminate()
        self.assertEqual(p.p.wait(timeout=15), 0)
        self.assertEqual(self.hook_events()[-2:], ["Stop", "SessionEnd"])
        self.assertTrue(any(m.get("type") == "result" and m["is_error"] for m in p.drain()))


def store_rows(sb):
    return helpers._lines(os.path.join(sb.data, "usage.jsonl"))


class Gates(Base):
    def refused(self, p, text):
        r = p.wait_results(1)[0]
        self.assertTrue(r["is_error"])
        self.assertIn(text, r["result"])
        self.assertEqual(self.sb.agy_runs(), [])                          # agy не запускался
        self.stops()
        self.assertEqual(self.hook_events()[1:3], ["UserPromptSubmit", "Stop"])   # но ход для ядра открыт и закрыт
        # слова, по которым ядро узнаёт вход/лимит Claude, в нашем тексте быть не должно
        self.assertFalse(route.CORE_TEXT.search(r["result"]), r["result"])
        return r

    def test_the_third_session_waits_for_a_slot_without_starting_agy(self):
        held = [store_take(self.sb, f"s{i}") for i in (1, 2)]
        self.addCleanup(lambda: [h.release() for h in held])
        p = self.session("text")
        p.user("x")
        p.read(lambda m: m.get("type") == "system" and m.get("subtype") == "status" and
               m.get("status") == "waiting")
        self.assertEqual(self.sb.agy_runs(), [])
        self.assertEqual(p.results(), [])
        held.pop().release()
        p.wait_results(1)
        self.assertIn("agy-s", [name for _, name in store_report(self.sb)])
        self.assertEqual(len(self.sb.agy_runs()), 1)

    def test_a_free_place_is_taken_and_given_back(self):
        held = [store_take(self.sb, "s1")]
        self.addCleanup(lambda: [h.release() for h in held])
        p = self.session("text")
        p.user("x")
        p.wait_results(1)
        self.assertTrue(any(name == "agy-s" for _, name in store_report(self.sb)))
        p.close()
        self.assertTrue(wait_until(lambda: all(n is None for _, n in [(1, None)] + [x for x in store_report(self.sb) if x[0] == 2])))

    def test_quiet_hours_do_not_start_a_turn(self):
        h = time.localtime().tm_hour
        self.sb.set_hub({"antigravity": {"models": {"claude-agy": "fake-model"},
                                         "quiet": f"{(h - 1) % 24:02d}:00-{(h + 1) % 24:02d}:00"}})
        p = self.session("text")
        p.user("x")
        self.refused(p, "тихие часы")

    def test_quiet_hours_are_off_by_default(self):
        p = self.session("text")
        p.user("x")
        self.assertFalse(p.wait_results(1)[0]["is_error"])

    def test_own_caps_stop_a_turn(self):
        self.sb.set_hub({"antigravity": {"models": {"claude-agy": "fake-model"}, "caps": {"turns_5h": 1}}})
        store_log(self.sb)
        p = self.session("text")
        p.user("x")
        self.refused(p, "потолок")

    def test_quota_error_ends_the_turn_blocks_and_the_next_turn_does_not_start_agy(self):
        p = self.session("quota")
        p.user("x")
        r = p.wait_results(1)[0]
        self.assertTrue(r["is_error"])
        self.assertRegex(r["result"], r"Antigravity: квота до \d\d:\d\d")
        self.assertFalse(route.CORE_TEXT.search(r["result"]))
        st = json.load(open(os.path.join(self.sb.data, "state.json")))["blocks"]
        self.assertEqual({r["reason"] for r in st.values()}, {"quota"})      # группа модели не известна — блок всех групп
        self.assertAlmostEqual(st["gemini"]["until"] - st["gemini"]["at"], 90 * 60, delta=2)   # срок из текста ошибки agy
        p.user("ещё")
        r2 = p.wait_results(2)[1]
        self.assertIn("квота", r2["result"])
        self.assertEqual(len(self.sb.agy_runs()), 1)                      # второй раз agy не запускали

    def test_auth_error_blocks_with_a_clear_text_and_not_as_a_claude_login_failure(self):
        p = self.session("auth")
        p.user("x")
        r = p.wait_results(1)[0]
        self.assertTrue(r["is_error"])
        self.assertIn("нужен вход", r["result"])
        self.assertIn("agy", r["result"])
        self.assertFalse(route.CORE_TEXT.search(r["result"]), "ядро сочло бы это отказом входа Claude")
        self.assertEqual({r["reason"] for r in json.load(open(os.path.join(self.sb.data, "state.json")))["blocks"].values()}, {"auth"})
        p.user("ещё")
        r2 = p.wait_results(2)[1]
        self.assertIn("нужен вход", r2["result"])
        self.assertEqual(len(self.sb.agy_runs()), 1)

    def test_an_expired_block_lets_turns_through(self):
        os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = self.sb.data
        self.addCleanup(os.environ.pop, "HUB_MOD_ANTIGRAVITY_DATA", None)
        store.set_block("quota", time.time() - 10, "Antigravity: квота", time.time() - 3600)
        p = self.session("text")
        p.user("x")
        self.assertFalse(p.wait_results(1)[0]["is_error"])

    def test_executors_are_on_and_the_setting_can_turn_them_off(self):
        with open(self.sb.instr, "w") as f:
            f.write("Ты исполнитель специалиста hub-dev под одну задачу.")
        self.sb.set_hub({"antigravity": {"models": {"claude-agy": "fake-model"}, "executors": False}})
        p = self.session("text")
        p.user("x")
        self.refused(p, "исполнители на claude-agy выключены")
        self.sb.set_hub({"antigravity": {"models": {"claude-agy": "fake-model"}}})
        q = self.sb.start("text")
        q.read(lambda m: m.get("subtype") == "init")
        q.user("x")
        self.assertFalse(q.wait_results(1)[0]["is_error"])

    def test_the_cores_deny_list_becomes_the_sessions_rules_file_for_the_guard(self):
        self.sb.start("text", deny=("SendMessage", "ListAgents", "AskUserQuestion", "NotebookEdit", "Bash(git commit:*)"))
        path = os.path.join(self.sb.data, "rules", f"{UUID}.json")
        self.assertTrue(wait_until(lambda: os.path.exists(path)), path)
        with open(path, encoding="utf-8") as f:
            rules = json.load(f)
        self.assertEqual(rules, {"tools": ["ask_question", "edit_notebook", "notebook_edit", "notebook_execution"],
                                 "commands": ["commit"], "paths": []})

    def test_missing_agy_is_said_plainly(self):
        p = self.sb.start("text", env={"HUB_MOD_ANTIGRAVITY_AGY": "/нет/agy"})
        p.read(lambda m: m.get("subtype") == "init")
        p.user("x")
        r = p.wait_results(1)[0]
        self.assertIn("agy не установлен", r["result"])


class Instructions(Base):
    def test_first_message_carries_the_hub_instructions_once(self):
        p = self.session("echo")
        p.user("[hub:m1] привет")
        p.wait_results(1)
        first = self.sb.agy_turns()[0]["content"]
        self.assertTrue(first.startswith("<hub-instructions>\nПравила хаба: пиши по-русски."), first)
        self.assertTrue(first.endswith("[hub:m1] привет"))
        p.user("второе")
        p.wait_results(2)
        self.assertEqual(self.sb.agy_turns()[1]["content"], "второе")      # во второй раз — без инструкций
        p.close()
        with open(self.sb.instr, "w") as f:
            f.write("Правила хаба: новые.")
        q = self.sb.start("echo", resume=True)
        q.read(lambda m: m.get("subtype") == "init")
        q.user("после перезапуска")
        q.wait_results(1)
        third = self.sb.agy_turns()[-1]["content"]
        self.assertIn("Инструкции хаба обновлены", third)
        self.assertIn("Правила хаба: новые.", third)
        run = self.sb.agy_runs()[-1]["argv"]
        self.assertEqual(run[run.index("--conversation") + 1], "conv-1")   # та же беседа agy

    def test_an_unchanged_file_on_resume_adds_nothing(self):
        p = self.session("echo")
        p.user("а")
        p.wait_results(1)
        p.close()
        q = self.sb.start("echo", resume=True)
        q.read(lambda m: m.get("subtype") == "init")
        q.user("б")
        q.wait_results(1)
        self.assertEqual(self.sb.agy_turns()[-1]["content"], "б")

    def test_resume_from_claude_records_conv_writes_rules_and_guard_matches(self):
        # передача от Claude -> agy (--resume без записи в convs.json): мост пишет rules, шлёт выжимку, кладёт conv в convs.json
        claude_dir = os.path.join(self.sb.dir, "claude-resume")
        proj = os.path.join(claude_dir, "projects", "-ws")
        os.makedirs(proj)
        with open(os.path.join(proj, f"{UUID}.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "Кодовое слово: КОМЕТА"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Запомнил."}]}}) + "\n")
        p = self.sb.start("echo", resume=True, env={"CLAUDE_CONFIG_DIR": claude_dir},
                          deny=("SendMessage", "ListAgents", "AskUserQuestion", "Bash(git commit:*)", f"Edit(/{self.sb.dir.lstrip('/')}/**)"))
        p.read(lambda m: m.get("subtype") == "init")
        p.user("продолжай")
        p.wait_results(1)
        turn = self.sb.agy_turns()[0]["content"]
        self.assertIn("<hub-handoff>", turn)
        self.assertIn("КОМЕТА", turn)
        self.assertNotIn("--conversation", self.sb.agy_runs()[0]["argv"])    # первая беседа agy
        old_data = os.environ.get("HUB_MOD_ANTIGRAVITY_DATA")
        os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = self.sb.data
        try:
            self.assertEqual(store.conv(UUID).get("conv"), "conv-1")
            out = agy_guard.decide({"conversationId": "conv-1",
                                    "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m x"}}})
            self.assertEqual(out["decision"], "deny")
            self.assertIn("git commit", out["reason"])
            out2 = agy_guard.decide({"conversationId": "conv-1",
                                     "toolCall": {"name": "write_file", "args": {"Path": os.path.join(self.sb.dir, "a.txt")}}})
            self.assertEqual(out2["decision"], "deny")
        finally:
            if old_data is None:
                os.environ.pop("HUB_MOD_ANTIGRAVITY_DATA", None)
            else:
                os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = old_data
        p.close()
        # повторный --resume: conv уже в convs.json, agy получает --conversation и те же правила
        q = self.sb.start("echo", resume=True, env={"CLAUDE_CONFIG_DIR": claude_dir},
                          deny=("SendMessage", "ListAgents", "AskUserQuestion", "Bash(git commit:*)", f"Edit(/{self.sb.dir.lstrip('/')}/**)"))
        q.read(lambda m: m.get("subtype") == "init")
        q.user("ещё")
        q.wait_results(1)
        run2 = self.sb.agy_runs()[-1]["argv"]
        self.assertEqual(run2[run2.index("--conversation") + 1], "conv-1")
        q.close()


def store_take(sb, name):
    os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = sb.data
    slot, _ = store.take_slot(name, 2)
    return slot


def store_report(sb):
    os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = sb.data
    return store.slot_report(2)


def store_log(sb):
    os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = sb.data
    store.log_usage("s", "claude-agy", {"in": 1, "out": 1}, 10, "ok")


if __name__ == "__main__":
    unittest.main()
