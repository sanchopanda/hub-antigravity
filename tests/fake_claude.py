#!/usr/bin/env python3
"""Фальшивый настоящий claude: пишет аргументы и нужные переменные в FAKE_CLAUDE_LOG и выходит (проверка оболочки §2.4)."""
import json
import os
import sys

with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "route": os.environ.get("HUB_MOD_ANTIGRAVITY_ROUTE"),
                        "model_env": os.environ.get("HUB_MOD_ANTIGRAVITY_MODEL")}, ensure_ascii=False) + "\n")
