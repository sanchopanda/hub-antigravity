#!/usr/bin/env python3
"""Фальшивый agy для тестов: никакого Google. Читает аргументы и stdin, отдаёт записанный поток из streams/<сценарий>.jsonl.

Сценарий — FAKE_AGY_SCENARIO. Строки файла — события agy как есть; служебные ключи с «_»:
  {"_turn": N}              — дальше события ответа на N-е сообщение (до него — то, что agy пишет при запуске);
  {"_sleep": S}             — пауза;  {"_hang": true} — молчать «навсегда»;
  {"_exit": K, "_stderr": T} — записать T в stderr и выйти с кодом K посреди хода;
  "__ECHO__" в тексте       — подставляется содержимое пришедшего сообщения.
Аргументы, имена переменных окружения и сообщения пишутся в FAKE_AGY_LOG (по строке на событие). Строка stdin с
event control_request / control_response — код выхода 2, как у agy (док). Это тест формата по документации Google;
настоящих записей нет (спека §10) — места допущений в мосте помечены «ПРОБА».
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def log(rec):
    path = os.environ.get("FAKE_AGY_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load():
    name = os.environ.get("FAKE_AGY_SCENARIO", "text")
    pre, turns, cur = [], {}, None
    with open(os.path.join(HERE, "streams", f"{name}.jsonl"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ev = json.loads(line)
            if "_turn" in ev:
                cur = turns.setdefault(int(ev["_turn"]), [])
            else:
                (pre if cur is None else cur).append(ev)
    return pre, turns


def play(events, echo):
    for ev in events:
        if "_sleep" in ev:
            time.sleep(float(ev["_sleep"]))
        elif ev.get("_hang"):
            time.sleep(3600)
        elif "_exit" in ev:
            sys.stderr.write(str(ev.get("_stderr", "")) + "\n")
            sys.stderr.flush()
            sys.exit(int(ev["_exit"]))
        else:
            sys.stdout.write(json.dumps(ev, ensure_ascii=False).replace("__ECHO__", json.dumps(echo)[1:-1]) + "\n")
            sys.stdout.flush()


def main():
    env = os.environ
    log({"argv": sys.argv[1:], "env_names": sorted(env), "cwd": os.getcwd(),
         "leak": [k for k, v in env.items() if "SECRET-SENTINEL" in v]})
    pre, turns = load()
    play(pre, "")
    n = 0
    for line in sys.stdin:
        try:
            ev = json.loads(line)
        except ValueError:
            sys.exit(1)
        if ev.get("event") in ("control_request", "control_response"):
            log({"rejected": ev.get("event")})
            sys.exit(2)
        if ev.get("event") != "user":
            sys.stderr.write(f'warning: ignoring unsupported stream input message event "{ev.get("event")}"\n')
            continue
        n += 1
        content = (ev.get("message") or {}).get("content")
        log({"turn": n, "content": content})
        play(turns.get(n) or [], content if isinstance(content, str) else json.dumps(content))
    sys.exit(0)


if __name__ == "__main__":
    main()
