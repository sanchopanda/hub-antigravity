"""Тесты deepseek_mcp (спека §8 п. 6): протокол MCP по stdio, подменённый HTTP, подменённый `hub budget --json`, потолки,
права engines.json, ключ не в выводе. Ни DeepSeek, ни живой хаб не задействованы."""
import http.server
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import MODULE  # noqa: E402

import deepseek_mcp as ds  # noqa: E402
import store  # noqa: E402

KEY = "sk-SECRET-SENTINEL-deepseek-0123456789"


class Fake(http.server.BaseHTTPRequestHandler):
    log = []
    reply = (200, {"content": [{"type": "text", "text": "четыре"}], "usage": {"input_tokens": 12, "output_tokens": 3}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.log.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        code, data = Fake.reply
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


HUB = '''#!%(py)s
import os, sys, time, json
mode = open(os.environ["HUB_BUDGET_FILE"]).read().strip() if os.path.exists(os.environ["HUB_BUDGET_FILE"]) else "gone"
if sys.argv[1:] != ["budget", "--json"]:
    sys.exit(9)
if mode == "hang":
    time.sleep(60)
if mode in ("gone", "fail"):
    sys.exit(1)
print(mode)
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ds-test-")
        self.state = os.path.join(self.dir, "state")
        self.conf = os.path.join(self.dir, "conf")
        os.makedirs(self.conf)
        self.data = os.path.join(self.state, "moddata", "antigravity")
        os.makedirs(self.data, mode=0o700)
        self.write_keys({"deepseek": KEY, "qwen": "x"})
        self.budget_file = os.path.join(self.dir, "budget.json")
        self.hub = os.path.join(self.dir, "hub")
        with open(self.hub, "w") as f:
            f.write(HUB % {"py": sys.executable})
        os.chmod(self.hub, 0o755)
        self.budget(money_left=True, reached=False, over=False, left_today_usd=2.9)
        Fake.log, Fake.reply = [], (200, {"content": [{"type": "text", "text": "четыре"}],
                                           "usage": {"input_tokens": 12, "output_tokens": 3}})
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": self.dir, "HUB_STATE_DIR": self.state, "HUB_CONFIG_DIR": self.conf,
                    "HUB_CONFIG": os.path.join(self.dir, "hub.json"), "HUB_MOD_ANTIGRAVITY_DATA": self.data,
                    "HUB_MOD_ANTIGRAVITY_DEEPSEEK_URL": self.url, "HUB_MOD_ANTIGRAVITY_HUB": self.hub,
                    "HUB_BUDGET_FILE": self.budget_file, "HUB_MOD_ANTIGRAVITY_HUB_TIMEOUT": "3"}
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)

    def test_plain_key_is_read_from_linux_hub_configuration(self):
        from unittest.mock import patch
        os.remove(os.path.join(self.conf, "engines.json"))
        path = os.path.join(self.conf, "deepseek-key")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(KEY + "\n")
        os.chmod(path, 0o600)
        with patch.dict(os.environ, {"HUB_CONFIG_DIR": self.conf}):
            self.assertEqual(ds.load_key(), KEY)
            os.chmod(path, 0o644)
            with self.assertRaises(ds.Refused):
                ds.load_key()

    def write_keys(self, keys, mode=0o600):
        path = os.path.join(self.conf, "engines.json")
        with open(path, "w") as f:
            json.dump({"keys": keys}, f)
        os.chmod(path, mode)

    def budget(self, **kw):
        base = {"money_left": True, "money_why": "на счету 2,96 $", "reached": False, "over": False, "left_today_usd": 2.9}
        with open(self.budget_file, "w") as f:
            f.write(json.dumps({**base, **kw}))

    def hang_budget(self):
        with open(self.budget_file, "w") as f:
            f.write("hang")

    def server(self):
        """MCP-сервер процессом; -> (popen, функция запрос -> ответ)."""
        p = subprocess.Popen([sys.executable, "-I", os.path.join(MODULE, "deepseek_mcp.py")], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, text=True, encoding="utf-8")
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.seq = 0

        def rpc(method, params=None, notify=False):
            msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
            if not notify:
                self.seq += 1
                msg["id"] = self.seq
            p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            p.stdin.flush()
            return None if notify else json.loads(p.stdout.readline())
        return p, rpc

    def call(self, rpc, **args):
        r = rpc("tools/call", {"name": "deepseek_ask", "arguments": args})
        return r["result"]

    def text(self, res):
        return res["content"][0]["text"]


class Protocol(Base):
    def test_initialize_list_and_call_over_stdio(self):
        p, rpc = self.server()
        init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "agy"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "hub-deepseek")
        self.assertIn("tools", init["result"]["capabilities"])
        rpc("notifications/initialized", notify=True)            # у уведомления ответа нет — следующий ответ принадлежит списку
        tools = rpc("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["deepseek_ask"])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["prompt"])
        res = self.call(rpc, prompt="2+2?")
        self.assertEqual((self.text(res), res["isError"]), ("четыре", False))
        self.assertEqual(res["structuredContent"]["tokens_in"], 12)
        self.assertEqual(res["structuredContent"]["model"], "flash")
        self.assertEqual(rpc("ping")["result"], {})
        self.assertEqual(rpc("нет/такого")["error"]["code"], -32601)
        p.stdin.close()
        self.assertEqual(p.wait(timeout=10), 0)

    def test_unknown_tool_and_garbage_input_do_not_kill_the_server(self):
        p, rpc = self.server()
        self.assertEqual(rpc("tools/call", {"name": "другой", "arguments": {}})["error"]["code"], -32602)
        p.stdin.write("не json\n")
        p.stdin.flush()
        self.assertEqual(json.loads(p.stdout.readline())["error"]["code"], -32700)
        self.assertEqual(rpc("ping")["result"], {})


class Request(Base):
    def test_the_request_to_deepseek_has_the_expected_shape(self):
        _, rpc = self.server()
        self.call(rpc, prompt="вопрос", system="будь краток", model="pro", max_tokens=500)
        got = Fake.log[0]
        self.assertEqual(got["path"], "/v1/messages")
        self.assertEqual(got["auth"], f"Bearer {KEY}")
        self.assertEqual(got["body"], {"model": "deepseek-v4-pro", "max_tokens": 500, "system": "будь краток",
                                       "messages": [{"role": "user", "content": "вопрос"}]})

    def test_defaults_and_the_hard_cap_on_max_tokens(self):
        _, rpc = self.server()
        self.call(rpc, prompt="а")
        self.call(rpc, prompt="б", max_tokens=50000)
        self.assertEqual([r["body"]["max_tokens"] for r in Fake.log], [2000, 8000])
        self.assertEqual(Fake.log[0]["body"]["model"], "deepseek-v4-flash")

    def test_a_bad_argument_is_a_plain_refusal(self):
        _, rpc = self.server()
        for args, word in (({"prompt": ""}, "prompt"), ({"prompt": "x" * 60001}, "60000"), ({"prompt": "a", "model": "gpt"}, "model"),
                           ({"prompt": "a", "max_tokens": "много"}, "max_tokens"), ({"prompt": "a", "system": "s" * 8001}, "8000")):
            res = self.call(rpc, **args)
            self.assertIn("DeepSeek недоступен", self.text(res))
            self.assertIn(word, self.text(res))
        self.assertEqual(Fake.log, [])

    def test_the_provider_errors_are_text_without_the_key(self):
        Fake.reply = (401, {"error": {"message": f"bad key {KEY}"}})
        p, rpc = self.server()
        res = self.call(rpc, prompt="а")
        self.assertIn("HTTP 401", self.text(res))
        self.assertNotIn(KEY, json.dumps(res))
        Fake.reply = (500, None)
        self.assertIn("HTTP 500", self.text(self.call(rpc, prompt="б")))

    def test_a_dead_address_is_a_refusal_not_a_crash(self):
        self.env["HUB_MOD_ANTIGRAVITY_DEEPSEEK_URL"] = "http://127.0.0.1:9"
        _, rpc = self.server()
        self.assertIn("DeepSeek недоступен", self.text(self.call(rpc, prompt="а")))


class Money(Base):
    def refused(self, why=None, **args):
        _, rpc = self.server()
        res = self.call(rpc, **{"prompt": "вопрос", **args})
        self.assertTrue(self.text(res).startswith("DeepSeek недоступен: "), self.text(res))
        self.assertEqual(res["isError"], False)                    # ход agy не падает
        if why:
            self.assertIn(why, self.text(res))
        self.assertEqual(Fake.log, [])                              # до DeepSeek дело не дошло
        return res

    def test_no_money_left_refuses(self):
        self.budget(money_left=False, money_why="счёт пуст")
        self.refused("счёт пуст")

    def test_the_day_ceiling_reached_refuses_unless_the_owner_allowed_over(self):
        self.budget(reached=True, left_today_usd=0)
        self.refused("потолок дня")
        self.budget(reached=True, over=True, left_today_usd=0)
        _, rpc = self.server()
        self.assertEqual(self.text(self.call(rpc, prompt="а")), "четыре")     # `hub budget over` — слово владельца

    def test_an_estimate_above_what_is_left_today_refuses(self):
        self.budget(left_today_usd=0.0001)
        self.refused("больше остатка дня")

    def test_no_answer_from_hub_budget_refuses(self):
        self.budget_gone = os.remove(self.budget_file)
        self.refused("hub budget не пришёл")

    def test_a_hung_hub_budget_refuses_within_the_timeout(self):
        self.env["HUB_MOD_ANTIGRAVITY_HUB_TIMEOUT"] = "1"
        self.hang_budget()
        t = time.time()
        self.refused("hub budget не пришёл")
        self.assertLess(time.time() - t, 8)

    def test_a_missing_money_left_is_not_taken_for_yes(self):
        with open(self.budget_file, "w") as f:
            f.write(json.dumps({"reached": False}))
        self.refused("денег")

    def test_the_tools_own_day_ceiling(self):
        store.append_line(os.path.join(self.data, "deepseek.jsonl"), {"at": time.time(), "model": "flash", "in": 1, "out": 1, "usd": 0.499})
        self.refused("потолок инструмента")
        # вчерашняя трата потолок не занимает
        os.remove(os.path.join(self.data, "deepseek.jsonl"))
        store.append_line(os.path.join(self.data, "deepseek.jsonl"), {"at": time.time() - 2 * 86400, "model": "flash", "in": 1, "out": 1, "usd": 0.49})
        _, rpc = self.server()
        self.assertEqual(self.text(self.call(rpc, prompt="а")), "четыре")

    def test_a_call_is_logged_as_numbers_only(self):
        _, rpc = self.server()
        self.call(rpc, prompt="СЕКРЕТНЫЙ-ВОПРОС", system="СЕКРЕТНАЯ-СИСТЕМА")
        raw = open(os.path.join(self.data, "deepseek.jsonl")).read()
        rec = json.loads(raw)
        self.assertEqual((rec["in"], rec["out"], rec["model"]), (12, 3, "flash"))
        self.assertGreater(rec["usd"], 0)
        for secret in ("СЕКРЕТНЫЙ", "СЕКРЕТНАЯ", KEY, "четыре"):
            self.assertNotIn(secret, raw)

    def test_the_estimate_follows_the_spec(self):
        # (длина ÷ 3 + max_tokens) × пиковая цена промаха кэша, USD за 1 млн токенов
        self.assertEqual(ds.estimate("а" * 300, "", "flash", 1000), round((100 * 0.3 + 1000 * 1.2) / 1e6, 6))
        self.assertEqual(ds.estimate("а" * 3, "б" * 3, "pro", 10), round((2 * 1.32 + 10 * 3.96) / 1e6, 6))


class Key(Base):
    def test_engines_json_must_be_0600(self):
        for mode in (0o644, 0o640, 0o604, 0o666):
            self.write_keys({"deepseek": KEY}, mode)
            _, rpc = self.server()
            res = self.call(rpc, prompt="а")
            self.assertIn("права engines.json", self.text(res), oct(mode))
            self.assertNotIn(KEY, json.dumps(res))
        self.assertEqual(Fake.log, [])

    def test_no_key_and_no_file_refuse(self):
        self.write_keys({"qwen": "x"})
        _, rpc = self.server()
        self.assertIn("ключа deepseek", self.text(self.call(rpc, prompt="а")))
        os.remove(os.path.join(self.conf, "engines.json"))
        self.assertIn("нет файла", self.text(self.call(rpc, prompt="а")))

    def test_the_key_is_in_no_output_and_no_file_of_the_module(self):
        p, rpc = self.server()
        self.call(rpc, prompt="а")
        Fake.reply = (500, {"error": KEY})
        self.call(rpc, prompt="б")
        rpc("tools/list")
        p.stdin.close()
        p.wait(timeout=10)
        self.assertNotIn(KEY, p.stderr.read())
        for root, _, files in os.walk(self.data):
            for f in files:
                self.assertNotIn(KEY, open(os.path.join(root, f), errors="replace").read(), f)

    def test_the_server_is_not_given_the_key_by_environment(self):
        self.assertNotIn("deepseek", " ".join(self.env))
        self.assertFalse([v for v in self.env.values() if KEY in v])
        _, rpc = self.server()
        self.assertEqual(self.text(self.call(rpc, prompt="а")), "четыре")      # ключ найден сам, из файла


class Script(Base):
    def run_ask(self, *args, stdin=""):
        sh = os.path.join(MODULE, "bin", "deepseek-ask")
        os.makedirs(os.path.join(self.dir, "bin"), exist_ok=True)
        return subprocess.run([sh, *args], env={**self.env}, input=stdin, capture_output=True, text=True, timeout=30)

    def test_the_fallback_script_gives_the_answer_and_has_the_same_checks(self):
        r = self.run_ask("--model", "pro", "сколько", "будет", "2+2")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "четыре"), r.stderr)
        self.assertEqual(Fake.log[0]["body"]["messages"][0]["content"], "сколько будет 2+2")
        self.assertEqual(Fake.log[0]["body"]["model"], "deepseek-v4-pro")
        r = self.run_ask("-", stdin="из stdin")
        self.assertEqual(r.stdout.strip(), "четыре")
        self.assertEqual(Fake.log[1]["body"]["messages"][0]["content"], "из stdin")
        self.budget(money_left=False)
        r = self.run_ask("ещё")
        self.assertEqual(r.returncode, 3)
        self.assertIn("DeepSeek недоступен", r.stdout)
        self.assertNotIn(KEY, r.stdout + r.stderr)
        self.assertEqual(len(Fake.log), 2)


if __name__ == "__main__":
    unittest.main()
