#!/usr/bin/env python3
"""Хук-регистратор: пишет в HOOK_LOG событие, JSON со stdin и имена HUB_SESSION / HUB_STATE_DIR (проверка §5.1)."""
import json
import os
import sys

body = json.loads(sys.stdin.read() or "{}")
with open(os.environ["HOOK_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"event": body.get("hook_event_name"), "body": body, "session": os.environ.get("HUB_SESSION"),
                        "state": os.environ.get("HUB_STATE_DIR")}, ensure_ascii=False) + "\n")
