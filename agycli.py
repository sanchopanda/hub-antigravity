"""Команда `hub antigravity` и проверка hub doctor: логика без ядра (main.py только подключает её к точкам модуля)."""
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup as agy_setup  # noqa: E402
import store  # noqa: E402
import sweep  # noqa: E402

USAGE = ("hub antigravity [status] | unblock | sweep [--all-back] | setup [--dry|--undo] [--dir <папка agy>] | doctor\n"
         "  status — вход, блок, места, ходы и токены, вызовы DeepSeek; unblock — снять блок;\n"
         "  sweep — что уведёт сессии с Antigravity при блоке (исполняет расписание: sweep.py);\n"
         "  setup — записи в настройки agy (с копией), --undo — снять.")
SHELL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "claude")


def agy_path():
    from shutil import which
    return os.environ.get("HUB_MOD_ANTIGRAVITY_AGY") or which("agy") or os.path.expanduser("~/.local/bin/agy")


def fmt_time(ts):
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def status(cfg=None, now=None):
    """Отчёт `hub antigravity`: строки для печати."""
    cfg = cfg or store.settings()
    now = time.time() if now is None else now
    lines = []
    b = store.block(now)
    lines.append(f"вход / квота: блок до {fmt_time(b['until'])} — {b.get('text') or b.get('reason')}" if b
                 else "вход / квота: блока нет")
    slots = store.slot_report(cfg["max_sessions"])
    lines.append("места agy: " + ", ".join(f"{n}: {name or 'свободно'}" for n, name in slots))
    for label, win in (("5 ч", store.WINDOW_5H), ("неделя", store.WINDOW_WEEK)):
        u = store.usage_sum(win, now)
        lines.append(f"ходы за {label}: {u['turns']}, токены вход {u['in']} / выход {u['out']} / мысли {u['think']} / кэш {u['cache']}")
    caps = {k: v for k, v in (cfg.get("caps") or {}).items() if v}
    lines.append("свои потолки: " + (", ".join(f"{k}={v}" for k, v in caps.items()) if caps else "выключены"))
    lines.append(f"тихие часы: {cfg.get('quiet') or 'выключены'}; исполнители на claude-agy: {'да' if cfg.get('executors') else 'нет'}")
    ds = store.deepseek_spent_today(now)
    lines.append(f"DeepSeek через deepseek_ask за сегодня: {ds:.4f} $ из {float((cfg.get('deepseek') or {}).get('day_usd') or 0):.2f} $")
    lines.append("модели: " + (", ".join(f"{k} → {v or 'agy по умолчанию'}" for k, v in sorted(cfg["models"].items())) or "не заданы (hub.json, antigravity.models)"))
    return lines


def plist_has_shell(path=None):
    """(ok, текст): HUB_CLAUDE службы хаба указывает на оболочку. Только чтение plist."""
    path = path or os.path.expanduser("~/Library/LaunchAgents/com.hub-personal.plist")
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None, "plist службы не найден (проверка HUB_CLAUDE не сделана)"
    m = re.search(r"<key>HUB_CLAUDE</key>\s*<string>([^<]*)</string>", text)
    if not m:
        return False, "HUB_CLAUDE в службе хаба не задан: сессии на claude-agy падают на ошибке модели"
    return os.path.realpath(m.group(1)) == os.path.realpath(SHELL), f"HUB_CLAUDE={m.group(1)}"


def service_has_shell(platform=None, env=None):
    """Проверяем окружение процесса, которое служба уже загрузила."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    value = env.get("HUB_CLAUDE")
    if value:
        return os.path.realpath(value) == os.path.realpath(SHELL), f"HUB_CLAUDE={value}"
    if platform == "darwin" and not env.get("HUB_MODULE"):
        return plist_has_shell()
    return False, "HUB_CLAUDE не задан в окружении запущенной службы хаба"


def doctor(cfg=None, run=subprocess.run):
    cfg = cfg or store.settings()
    out = []
    agy = agy_path()
    if os.access(agy, os.X_OK):
        try:
            ver = run([agy, "--version"], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL).stdout.strip()[:80]
        except (OSError, subprocess.SubprocessError):
            ver = "версию узнать не удалось"
        out.append({"name": "antigravity: agy", "ok": True, "detail": f"{agy}: {ver}"})
    else:
        out.append({"name": "antigravity: agy", "ok": False, "detail": "agy не установлен (шаг 4 плана: ставит владелец)"})
    out.append({"name": "antigravity: модели", "ok": bool(cfg["models"]),
                "detail": ", ".join(f"{k}→{v or 'по умолчанию'}" for k, v in sorted(cfg["models"].items())) or "hub.json antigravity.models пуст"})
    ok, text = service_has_shell()
    out.append({"name": "antigravity: HUB_CLAUDE", "ok": bool(ok), "detail": text})
    real = agy_setup.real_claude_target()
    out.append({"name": "antigravity: настоящий claude", "ok": bool(real),
                "detail": real or "исполняемый файл Claude Code не найден"})
    b = store.block()
    out.append({"name": "antigravity: блок", "ok": b is None, "detail": (b.get("text") or b.get("reason")) if b else "нет"})
    mode = None
    keys = os.path.join(store.config_dir(), "engines.json")
    if os.path.exists(keys):
        mode = oct(os.stat(keys).st_mode & 0o777)[2:]
    out.append({"name": "antigravity: ключ DeepSeek", "ok": mode == "600", "detail": f"engines.json права {mode or 'файла нет'}"})
    return out


def handle(args, caller_owner=False, now=None):
    """Ответ точки cli: {out, err, code}."""
    args = list(args or [])
    if not args or args == ["status"]:
        return {"out": "\n".join(status(now=now)) + "\n", "err": "", "code": 0}
    if args == ["unblock"]:
        had = store.block_record()
        store.clear_block()
        return {"out": ("блок снят: " + str(had.get("reason")) if had else "блока не было") + "\n", "err": "", "code": 0}
    if args[0] == "sweep":
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = sweep.one_pass(["--dry", *[a for a in args[1:] if a == "--all-back"]])
        return {"out": buf.getvalue() + "(только план; исполняет расписание: hub schedule add --id antigravity-sweep "
                                        "--every 5m --command \"python3 ~/.hub-personal/ext/antigravity/sweep.py\")\n",
                "err": "", "code": code}
    if args[0] == "setup":
        if not caller_owner:
            return {"out": "", "err": "setup пишет в общие настройки agy: запускает владелец из терминала\n", "code": 1}
        base = args[args.index("--dir") + 1] if "--dir" in args and args.index("--dir") + 1 < len(args) else None
        dry = "--dry" in args
        lines = agy_setup.undo(base, dry) if "--undo" in args else agy_setup.setup(base, dry)
        return {"out": "\n".join(lines) + "\n", "err": "", "code": 0}
    if args == ["doctor"]:
        return {"out": "\n".join(f"{'ok ' if c['ok'] else 'НЕТ'} {c['name']}: {c['detail']}" for c in doctor()) + "\n", "err": "", "code": 0}
    return {"out": "", "err": USAGE + "\n", "code": 2}
