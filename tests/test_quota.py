"""Тесты виджета квоты Antigravity: разбор `agy -p "/usage" --output-format json`, кэш <= 5 мин, отказы и web/main.js."""
from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import MODULE  # noqa: E402

import quota  # noqa: E402

# Живой образец вывода agy 1.5.18 (`agy -p "/usage" --output-format json`) от 27.11.2026
AGY_1518_USAGE = json.dumps({
    "conversation_id": "",
    "status": "SUCCESS",
    "response": (
        "Gemini Models\tWeekly Limit Remaining\t83%\t2026-12-04T06:25:37Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t34%\t2026-11-27T11:25:37Z\n"
        "Claude and GPT models\tWeekly Limit Remaining\t100%\t1970-01-01T00:00:00Z\n"
        "Claude and GPT models\tFive Hour Limit Remaining\t100%\t1970-01-01T00:00:00Z\n"
    ),
    "duration_seconds": 0,
    "num_turns": 0,
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.826279878616333,
                            "reset_time": "2026-12-04T06:25:37Z",
                        },
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.33975958824157715,
                            "reset_time": "2026-11-27T11:25:37Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 1,
                            "reset_time": "1970-01-01T00:00:00Z",
                        },
                        {
                            "id": "3p-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 1,
                            "reset_time": "1970-01-01T00:00:00Z",
                        },
                    ],
                },
            ]
        },
    },
})


class ParseUsage(unittest.TestCase):
    def test_parses_real_agy_1_5_18_output(self):
        got = quota.parse_usage(AGY_1518_USAGE)
        self.assertIsNotNone(got)
        self.assertEqual(got["gemini"]["h5"]["pct"], 34)
        self.assertEqual(got["gemini"]["weekly"]["pct"], 83)
        self.assertIsNotNone(got["gemini"]["h5"]["reset_at"])
        self.assertIsNotNone(got["gemini"]["weekly"]["reset_at"])
        self.assertEqual(got["claude_gpt"]["h5"]["pct"], 100)
        self.assertIsNone(got["claude_gpt"]["h5"]["reset_at"])
        self.assertEqual(got["claude_gpt"]["weekly"]["pct"], 100)
        self.assertIsNone(got["claude_gpt"]["weekly"]["reset_at"])

    def test_epoch_zero_and_garbage_reset_times_become_none(self):
        for raw in ("1970-01-01T00:00:00Z", "1970-01-01T00:00:00+00:00", "", None, "not-a-date", 12345):
            self.assertIsNone(quota.parse_reset_time(raw), raw)
        # 2026-11-27T11:25:37Z
        ts = quota.parse_reset_time("2026-11-27T11:25:37Z")
        self.assertEqual(ts, datetime(2026, 11, 27, 11, 25, 37, tzinfo=timezone.utc).timestamp())

    def test_handles_prefix_logs_and_invalid_inputs(self):
        wrapped = "some diagnostic line\n" + AGY_1518_USAGE + "\n"
        self.assertEqual(quota.parse_usage(wrapped)["gemini"]["h5"]["pct"], 34)
        self.assertIsNone(quota.parse_usage(""))
        self.assertIsNone(quota.parse_usage("not json"))
        self.assertIsNone(quota.parse_usage(json.dumps({"status": "ERROR", "command": {"data": {"groups": []}}})))
        self.assertIsNone(quota.parse_usage(json.dumps({"status": "SUCCESS", "command": {"data": {"groups": []}}})))

    def test_clamps_remaining_fraction_and_rejects_bad_numbers(self):
        self.assertEqual(quota.parse_bucket({"remaining_fraction": -0.2, "reset_time": "2026-11-27T11:25:37Z"})["pct"], 0)
        self.assertEqual(quota.parse_bucket({"remaining_fraction": 1.4, "reset_time": "1970-01-01T00:00:00Z"})["pct"], 100)
        self.assertIsNone(quota.parse_bucket({"remaining_fraction": float("nan")}))
        self.assertIsNone(quota.parse_bucket({"remaining_fraction": True}))
        self.assertIsNone(quota.parse_bucket({"remaining_fraction": "0.5"}))


class FetchAndCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agy-quota-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.old = os.environ.get("HUB_MOD_ANTIGRAVITY_DATA")
        os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = self.dir
        self.addCleanup(self._restore)

    def _restore(self):
        if self.old is None:
            os.environ.pop("HUB_MOD_ANTIGRAVITY_DATA", None)
        else:
            os.environ["HUB_MOD_ANTIGRAVITY_DATA"] = self.old

    def test_fetch_handles_success_and_failures_without_raising(self):
        def ok_run(cmd, **kw):
            self.assertEqual(cmd[1:], ["-p", "/usage", "--output-format", "json"])
            return subprocess.CompletedProcess(cmd, 0, AGY_1518_USAGE, "")

        got = quota.fetch(run=ok_run, agy="/fake/agy", now=1000.0)
        self.assertTrue(got["ok"])
        self.assertEqual(got["fetched_at"], 1000.0)
        self.assertLessEqual(got["ttl_s"], 300)
        self.assertEqual(got["gemini"]["h5"]["pct"], 34)

        def fail_rc(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, "", "boom")

        self.assertFalse(quota.fetch(run=fail_rc, agy="/fake/agy", now=1000.0)["ok"])

        def fail_timeout(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 30)

        self.assertFalse(quota.fetch(run=fail_timeout, agy="/fake/agy", now=1000.0)["ok"])

        def fail_missing(cmd, **kw):
            raise FileNotFoundError("no agy")

        self.assertFalse(quota.fetch(run=fail_missing, agy="/fake/agy", now=1000.0)["ok"])

    def test_disk_cache_respects_5_minute_limit(self):
        rec = {"ok": True, "at": 1000.0, "fetched_at": 1000.0, "ttl_s": 300,
               "gemini": {"h5": {"remaining": 0.5, "pct": 50, "reset_at": None}}, "claude_gpt": None}
        quota.save_cache(rec)
        self.assertIsNotNone(quota.load_cache(now=1299.0))
        self.assertIsNone(quota.load_cache(now=1300.0))      # ровно 5 минут -> уже устарел
        self.assertIsNone(quota.load_cache(now=999.0))       # часы ушли назад -> не верим кэшу

    def test_poller_caches_refreshes_and_does_not_spam_failures(self):
        writes = []
        calls = []
        mode = {"ok": True}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if mode["ok"]:
                return subprocess.CompletedProcess(cmd, 0, AGY_1518_USAGE, "")
            return subprocess.CompletedProcess(cmd, 1, "", "offline")

        poller = quota.Poller(lambda k, v: writes.append((k, v)), run=fake_run)
        poller.prime(now=1000.0)
        self.assertEqual(len(writes), 1)
        self.assertFalse(writes[-1][1]["ok"])                # кэша не было -> сразу пишем маркер

        poller.step(now=1000.0, agy="/fake/agy")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(writes), 2)
        self.assertTrue(writes[-1][1]["ok"])

        # Внутри окна обновления agy не дёргаем
        self.assertIsNone(poller.step(now=1100.0, agy="/fake/agy"))
        self.assertEqual(len(calls), 1)

        # Пора обновляться, agy упал -> один раз пишем ok=False
        mode["ok"] = False
        poller.step(now=1270.0, agy="/fake/agy")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(writes), 3)
        self.assertFalse(writes[-1][1]["ok"])

        # Повторный сбой через минуту -> журнал не спамим
        poller.step(now=1330.0, agy="/fake/agy")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(writes), 3)

        # Новый процесс после рестарта подхватывает свежий кэш с диска без ожидания agy
        writes2 = []
        poller2 = quota.Poller(lambda k, v: writes2.append((k, v)), run=fake_run)
        poller2.prime(now=1200.0)
        self.assertEqual(len(writes2), 1)
        self.assertTrue(writes2[0][1]["ok"])
        self.assertEqual(writes2[0][1]["gemini"]["h5"]["pct"], 34)


class ManifestAndWeb(unittest.TestCase):
    def test_manifest_has_snapshot_key_and_web_plugin(self):
        layer = os.path.realpath(os.path.expanduser("~/.hub-personal/code"))
        if layer not in sys.path:
            sys.path.insert(0, layer)
        from core import modules
        with open(os.path.join(MODULE, "module.json"), encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual(modules.validate(man, "module.json"), [])
        self.assertEqual(man["version"], "0.3.9")
        self.assertEqual(man["web"], "web/main.js")
        self.assertIn("antigravity_quota", man["snapshot"]["keys"])
        self.assertIn("antigravity_models", man["snapshot"]["keys"])
        self.assertTrue(os.path.isfile(os.path.join(MODULE, "web", "main.js")))
        self.assertTrue(os.path.isfile(os.path.join(MODULE, "web", "models.js")))
        self.assertTrue(os.path.isfile(os.path.join(MODULE, "web", "models.css")))
        self.assertTrue(os.path.isfile(os.path.join(MODULE, "web", "quota.css")))

    def test_web_main_js_formatting_moscow_epoch_zero_and_fallback(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node не установлен")
        js_path = os.path.join(MODULE, "web", "main.js")
        script = f"""
import assert from "node:assert/strict";
import {{ formatMoscowReset, quotaView, bucketView, isAgyModel, selectedModel }} from {json.dumps("file://" + js_path)};

// 2026-11-27T08:55:00Z = 11:55 MSK
const now = Date.UTC(2026, 10, 27, 8, 55, 0) / 1000;
// 2026-11-27T11:25:37Z = 14:25 MSK (тот же день)
const reset5h = Date.UTC(2026, 10, 27, 11, 25, 37) / 1000;
// 2026-12-04T06:25:37Z = 04.12 09:25 MSK (другой день)
const resetWk = Date.UTC(2026, 11, 4, 6, 25, 37) / 1000;
// Полночь по Москве: 2026-11-27T21:05:00Z = 28.11 00:05 MSK (не 24:05!)
const resetMid = Date.UTC(2026, 10, 27, 21, 5, 0) / 1000;

assert.equal(formatMoscowReset(reset5h, now, "ru"), "14:25 МСК");
assert.equal(formatMoscowReset(resetWk, now, "ru"), "04.12 09:25 МСК");
assert.equal(formatMoscowReset(resetMid, now, "ru"), "28.11 00:05 МСК");
assert.equal(formatMoscowReset(0, now, "ru"), "");
assert.equal(formatMoscowReset(null, now, "ru"), "");

// 100% + epoch 0 (reset_at=null) -> время не показывается
const b100 = bucketView({{ pct: 100, reset_at: null }}, now, "ru");
assert.equal(b100.pctText, "100%");
assert.equal(b100.resetText, "");
assert.equal(b100.level, "ok");

// Пороги подсветки: <=10 high, <=25 warn, >25 ok
assert.equal(bucketView({{ pct: 10, reset_at: reset5h }}, now).level, "high");
assert.equal(bucketView({{ pct: 25, reset_at: reset5h }}, now).level, "warn");
assert.equal(bucketView({{ pct: 34, reset_at: reset5h }}, now).level, "ok");

// Свежие данные: Gemini + Claude/GPT
const fresh = quotaView({{
  ok: true,
  fetched_at: now - 60,
  ttl_s: 300,
  gemini: {{ h5: {{ pct: 34, reset_at: reset5h }}, weekly: {{ pct: 83, reset_at: resetWk }} }},
  claude_gpt: {{ h5: {{ pct: 100, reset_at: null }}, weekly: {{ pct: 100, reset_at: null }} }},
}}, now, "ru");
assert.equal(fresh.ok, true);
assert.equal(fresh.gemini.h5.pctText, "34%");
assert.equal(fresh.gemini.h5.resetText, "14:25 МСК");
assert.equal(fresh.gemini.weekly.pctText, "83%");
assert.equal(fresh.gemini.weekly.resetText, "04.12 09:25 МСК");
assert.equal(fresh.claudeGpt.h5.pctText, "100%");
assert.equal(fresh.claudeGpt.h5.resetText, "");

// Устаревший кэш (> 5 мин) и ok:false -> прочерк, не ошибка
for (const bad of [
  null,
  {{ ok: false, fetched_at: null, ttl_s: 300 }},
  {{ ok: true, fetched_at: now - 301, ttl_s: 300, gemini: {{ h5: {{ pct: 34, reset_at: reset5h }} }} }},
]) {{
  const v = quotaView(bad, now, "ru");
  assert.equal(v.ok, false);
  assert.equal(v.gemini.h5.pctText, "—");
  assert.equal(v.gemini.weekly.pctText, "—");
  assert.equal(v.claudeGpt, null);
}}

// Подсветка активной agy-сессии
assert.equal(isAgyModel("claude-agy"), true);
assert.equal(isAgyModel("claude-deepseek"), false);
const snap = {{ sessions: [{{ name: "s1", model: "claude-agy" }}, {{ name: "hub", kind: "hub", model: "claude-deepseek" }}] }};
assert.equal(selectedModel(snap, "#/s/s1"), "claude-agy");
assert.equal(selectedModel(snap, ""), "claude-deepseek");
console.log("JS_OK");
"""
        cp = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(cp.returncode, 0, cp.stderr or cp.stdout)
        self.assertIn("JS_OK", cp.stdout)


if __name__ == "__main__":
    unittest.main()
