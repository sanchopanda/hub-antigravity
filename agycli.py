"""Команда `hub antigravity` и проверка hub doctor: логика без ядра (main.py только подключает её к точкам модуля)."""
import os
import json
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup as agy_setup  # noqa: E402
import store  # noqa: E402
import sweep  # noqa: E402

USAGE = ("hub antigravity [status] | state [--json] | unblock | sweep [--all-back] | setup [--dry|--undo] [--dir <папка agy>] | doctor\n"
         "  status — вход, блок, места, ходы и токены, вызовы DeepSeek; unblock — снять блок;\n"
         "  state — состояние для модуля выбора провайдера: квота, блок, модели и места;\n"
         "  models [--json] — актуальные модели agy с категориями;\n"
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
    bs = store.blocks(now)
    if not bs:
        lines.append("вход / квота: блока нет")
    elif len(bs) == len(store.GROUPS) and len({b.get("reason") for b in bs.values()}) == 1:
        b = next(iter(bs.values()))   # вход или квота обеих групп: один общий блок
        lines.append(f"вход / квота: блок до {fmt_time(b['until'])} — {b.get('text') or b.get('reason')}")
    else:
        for g in store.GROUPS:
            b = bs.get(g)
            label = store.GROUP_LABEL[g]
            lines.append(f"блок {label}: " + (f"до {fmt_time(b['until'])} — {b.get('text') or b.get('reason')}"
                                              if b else "нет"))
    import quota as agy_quota   # quota импортирует agycli: здесь, а не вверху файла
    rec = agy_quota.load_cache(now=now)
    if rec:
        parts = []
        for g in store.GROUPS:
            gg = rec.get(agy_quota.QKEY[g]) if isinstance(rec.get(agy_quota.QKEY[g]), dict) else {}
            wins = [f"{w} {gg[w]['pct']}%" for w in ("h5", "weekly") if isinstance(gg.get(w), dict)]
            parts.append(f"{store.GROUP_LABEL[g]}: " + (", ".join(wins) if wins else "—"))
        lines.append("квота (agy /usage): " + "; ".join(parts))
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
    lines.append("модели: " + (", ".join(f"{k} → {v or 'agy по умолчанию'}" for k, v in sorted(cfg["models"].items())) or f"отображение пусто; основная {cfg.get('primary_model') or 'не задана'} (hub.json, antigravity.primary_model)"))
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


def service_has_shell(platform=None, env=None, host=None, run=subprocess.run):
    """Проверяем окружение процесса, которое служба уже загрузила."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if host == "codex":
        value = env.get("HUB_CODEX_BIN")
        if not value:
            return False, "Оболочка Codex для Antigravity не задана в окружении службы"
        try:
            result = run([value, "--antigravity-probe"], capture_output=True, text=True, timeout=5,
                         stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return False, "Не удалось проверить оболочку Codex"
        ok = result.returncode == 0 and result.stdout.strip() == "antigravity-codex/1"
        return ok, f"Маршрут через {value}" if ok else "Оболочка Codex ещё не подключена к Antigravity"
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
    models = cfg.get("models") or {}
    primary = str(cfg.get("primary_model") or "")
    parts = []
    if primary:
        parts.append(f"основная {primary}")
    if models:
        parts.append(", ".join(f"{k}→{v or 'по умолчанию'}" for k, v in sorted(models.items())))
    out.append({"name": "antigravity: модели", "ok": bool(models or primary),
                "detail": "; ".join(parts) or "не заданы ни antigravity.primary_model, ни antigravity.models в hub.json"})
    host = store.read_json(store.hub_json_path()).get("host")
    ok, text = service_has_shell(host=host)
    out.append({"name": "antigravity: маршрут", "ok": bool(ok), "detail": text})
    real = agy_setup.real_claude_target()
    out.append({"name": "antigravity: настоящий claude", "ok": bool(real),
                "detail": real or "исполняемый файл Claude Code не найден"})
    bs = store.blocks()
    for g in store.GROUPS:
        b = bs.get(g)
        out.append({"name": f"antigravity: блок {store.GROUP_LABEL[g]}", "ok": b is None,
                    "detail": (b.get("text") or b.get("reason")) if b else "нет"})
    mode = None
    keys = os.path.join(store.config_dir(), "engines.json")
    if not os.path.exists(keys):
        keys = os.path.join(store.config_dir(), "deepseek-key")
    if os.path.exists(keys):
        mode = oct(os.stat(keys).st_mode & 0o777)[2:]
    out.append({"name": "antigravity: ключ DeepSeek", "ok": mode == "600",
                "detail": f"{os.path.basename(keys)} права {mode or 'файла нет'}"})
    return out


MODELS_TTL = 300


def _model_category(model_id):
    if str(model_id).startswith("gemini"):
        return "Gemini"
    if str(model_id).startswith("claude"):
        return "Claude"
    if str(model_id).startswith("gpt-oss"):
        return "GPT"
    return "Другие"


def _parse_models(text):
    out = []
    for line in str(text or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", parts[0]):
            continue
        agy_id, title = parts
        out.append({
            "id": "claude-agy-" + agy_id,
            "agy_id": agy_id,
            "title": title.strip() or agy_id,
            "displayName": title.strip() or agy_id,
            "category": _model_category(agy_id),
        })
    return out


def _normalize_models(rows):
    """Read caches saved by earlier module versions (actual agy ids, displayName)."""
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        agy_id = str(row.get("agy_id") or row.get("id") or "").strip()
        if not agy_id:
            continue
        wrapper = agy_id if agy_id.startswith("claude-agy-") else "claude-agy-" + agy_id
        actual = str(row.get("agy_id") or agy_id)
        title = str(row.get("title") or row.get("displayName") or actual)
        out.append({
            "id": wrapper,
            "agy_id": actual,
            "title": title,
            "displayName": title,
            "category": row.get("category") or _model_category(actual),
        })
    return out


def model_catalog(now=None, run=subprocess.run, force=False):
    """Stable session ids from the models currently offered by `agy models`, cached for five minutes."""
    now = time.time() if now is None else float(now)
    cache = os.path.join(store.data_dir(), "models.json")
    saved = store.read_json(cache)
    models = _normalize_models(saved.get("models")) if isinstance(saved, dict) else []
    fetched = saved.get("fetched_at") if isinstance(saved, dict) else None
    if not force and models and isinstance(fetched, (int, float)) \
            and 0 <= now - fetched < MODELS_TTL:
        return models
    try:
        result = run([agy_path(), "models"], capture_output=True, text=True,
                     timeout=20, stdin=subprocess.DEVNULL)
        fresh = _parse_models(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        fresh = []
    if fresh:
        store.write_json(cache, {"fetched_at": now, "models": fresh})
        return fresh
    return models


def state_payload(now=None):
    """Состояние Antigravity для оркестратора: только собственные данные модуля."""
    import quota
    now = time.time() if now is None else float(now)
    cfg = store.settings()
    cached = quota.load_cache(now=now)
    group_blocks = store.blocks(now)
    blocked = group_blocks.get("gemini")      # поле для оркестратора: «исчерпание» — это группа Gemini; блоки по группам — ниже
    gemini = (cached or {}).get("gemini") or {}
    exhausted = bool(blocked and blocked.get("reason") == "quota")
    if cached:
        exhausted = exhausted or any(
            isinstance(bucket, dict) and isinstance(bucket.get("pct"), (int, float)) and bucket["pct"] <= 0
            for bucket in (gemini.get("h5"), gemini.get("weekly"))
        )
    slots = store.slot_report(cfg["max_sessions"])
    return {
        "ok": True,
        "exhausted": exhausted,
        "blocked": blocked,
        "blocks": group_blocks,
        "primary_model": cfg.get("primary_model"),
        "fallback_model": cfg.get("fallback_model"),
        "quota": cached,
        "models": cfg["models"],
        "catalog": model_catalog(now=now),
        "max_sessions": cfg["max_sessions"],
        "slots": [{"number": number, "session": session} for number, session in slots],
    }


def handle(args, caller_owner=False, now=None):
    """Ответ точки cli: {out, err, code}."""
    args = list(args or [])
    if not args or args == ["status"]:
        return {"out": "\n".join(status(now=now)) + "\n", "err": "", "code": 0}
    if args == ["state", "--json"]:
        return {"out": json.dumps(state_payload(now=now), ensure_ascii=False) + "\n", "err": "", "code": 0}
    if args == ["models", "--json"]:
        return {"out": json.dumps({"ok": True, "models": model_catalog(now=now)}, ensure_ascii=False) + "\n",
                "err": "", "code": 0}
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
