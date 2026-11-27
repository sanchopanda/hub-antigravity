"""`hub antigravity sweep` и задание расписания: увести сессии с Antigravity на время блока и вернуть после (§3.3).

Задание: `hub schedule add --id antigravity-sweep --every 5m --command "python3 <этот файл>"`. Задание расписания идёт как
слово владельца, поэтому `hub session model` отсюда разрешён; модуль и сессии модель чужой сессии менять не вправе.
Команда `hub antigravity sweep` (точка cli модуля) только показывает план (`--dry` по умолчанию в ней).

Правила:
- блок есть (квота или вход, state.json, срок не вышел): каждую живую сессию на модели с agy — на то, куда сейчас ведёт
  лестница daybudget (`hub budget --json`, поле step): work и conduit — sonnet, deepseek — claude-deepseek. Ступень wait
  (все заняты) — никого не трогаем: уходить некуда. В removed.json пишется «откуда увели»: {сессия: {from, to, at, reason}}.
- блока нет (кончился срок или снят `hub antigravity unblock`): сессии из removed.json, чья модель всё ещё та, что
  поставил sweep, возвращаются на прежнюю; запись снимается. Ручной выбор владельца не перебивается: сменил модель сам —
  запись просто убирается.
- `--all-back` — откат модуля (§2.6): все сессии на claude-agy* — на sonnet, не глядя на блок; записи removed.json снимаются.
"""
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

DEAD = ("removed", "closed", "failed", "retired")
BACK = "sonnet"
FALLBACK_STEP = {"work": "sonnet", "conduit": "sonnet", "deepseek": "claude-deepseek"}
REMOVED = "removed.json"


def hub():
    return shutil.which("hub") or os.path.expanduser("~/.hub-personal/bin/hub")


def run(args, timeout=30):
    return subprocess.run([hub()] + args, capture_output=True, text=True, timeout=timeout)


def removed_path():
    return os.path.join(store.data_dir(), REMOVED)


def load_removed():
    got = store.read_json(removed_path())
    return got if isinstance(got, dict) else {}


def live_models(runner=run):
    """{сессия: модель сейчас} из `hub status --json` без закрытых; None — снимок не прочитался."""
    got = runner(["status", "--json"], 30)
    try:
        sessions = json.loads(got.stdout).get("sessions") or {}
    except (ValueError, AttributeError):
        return None
    out = {}
    for name, row in sessions.items():
        if not isinstance(row, dict) or row.get("state") in DEAD:
            continue
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        out[name] = str(row.get("model") or info.get("model") or "")
    return out


def ladder_target(runner=run):
    """(модель, ступень) по лестнице daybudget или (None, ступень), если уходить некуда."""
    got = runner(["budget", "--json"], 20)
    try:
        step = str(json.loads(got.stdout).get("step") or "")
    except (ValueError, AttributeError):
        return None, "нет ответа hub budget"
    return FALLBACK_STEP.get(step), step


def plan(live, block, removed, target, step, now, all_back=False):
    """[(сессия, из, в, почему)] и записи removed.json после ходов: чистая функция (стенд проверяет её без `hub`)."""
    moves, keep = [], dict(removed)
    if all_back:
        for name, cur in sorted(live.items()):
            if store.is_agy_model(cur):
                moves.append((name, cur, BACK, "откат модуля"))
        for name in list(keep):
            if name in live and not store.is_agy_model(live[name]) or any(m[0] == name for m in moves):
                keep.pop(name, None)
        return moves, keep
    if block:
        if not target:
            return [], keep
        for name, cur in sorted(live.items()):
            if store.is_agy_model(cur):
                moves.append((name, cur, target, f"Antigravity закрыт ({block.get('reason')}), лестница ведёт на {step}"))
        return moves, keep
    for name, rec in sorted(removed.items()):
        if not isinstance(rec, dict) or name not in live:
            continue
        if live[name] == rec.get("to") and rec.get("from"):
            moves.append((name, live[name], rec["from"], "Antigravity снова открыт — вернуть"))
        else:
            keep.pop(name, None)     # модель сменили руками: запись не нужна, сессию не трогаем
    return moves, keep


def one_pass(argv, runner=run, now=None):
    dry = "--dry" in argv
    all_back = "--all-back" in argv
    now = time.time() if now is None else now
    live = live_models(runner)
    if live is None:
        print("hub status --json: снимок не прочитался")
        return 1
    rec = store.block_record()
    block = store.block(now)
    if rec and not block and not all_back and not dry:
        store.clear_block()          # срок вышел: блок снимаем, сессии вернёт этот же проход
    target, step = ladder_target(runner) if block else (None, "—")
    if block and not target:
        print(f"блок есть ({block.get('reason')}), но лестница на ступени «{step}»: уходить некуда — никого не трогаю")
    removed = load_removed()
    moves, keep = plan(live, block, removed, target, step, now, all_back)
    done = 0
    for name, cur, to, why in moves:
        if dry:
            print(f"would: hub session model {name} {to}   (было {cur}) — {why}")
            continue
        out = runner(["session", "model", name, to])
        print(f"{name}: {cur} -> {to}: {(out.stdout or out.stderr).strip()[:150]}")
        if out.returncode != 0:
            continue
        done += 1
        if block and not all_back:
            keep[name] = {"from": cur, "to": to, "at": now, "reason": block.get("reason")}
        else:
            keep.pop(name, None)
    if not dry and keep != removed:
        store.write_json(removed_path(), keep)
    if not moves:
        print("менять нечего")
    return 0


def main(argv):
    return one_pass(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
