"""Чистые правила модуля antigravity: разбор аргументов claude от ядра, «идёт ли сессия в agy», флаги agy,
чистка окружения, разбор ошибок agy. Без ввода-вывода: тесты гоняют эти функции напрямую."""
import os
import re

import deny
import store

# аргументы Claude Code, у которых есть значение: ядро ставит их именно так (hosts/streamjson.command, core/catalog.launch_flags)
VALUE_FLAGS = {"--session-id", "--resume", "-n", "--name", "--settings", "--permission-prompt-tool", "--permission-mode",
               "--append-system-prompt-file", "--system-prompt-snapshot", "--setting-sources", "--effort", "--model",
               "--input-format", "--output-format", "--append-system-prompt", "--system-prompt"}
# у этих флагов значений может быть несколько подряд, пока не встретится следующий флаг
LIST_FLAGS = {"--disallowedTools", "--allowedTools", "--mcp-config", "--plugin-dir", "--add-dir"}
BOOL_FLAGS = {"-p", "--print", "--verbose", "--strict-mcp-config", "--dangerously-skip-permissions"}
# что ядро запрещает любой сессии (NO_TOOLS) и что разрешено дополнительно, не меняя класса сессии (форма владельцу)
PLAIN_DENY = {"SendMessage", "ListAgents", "AskUserQuestion"}
EXECUTOR_MARK = "Ты исполнитель специалиста"   # core/executors.EXECUTOR_NOTE: так начинается заметка исполнителя


def parse(argv):
    """Аргументы claude -> словарь. Незнакомый флаг без значения ничего не ломает: он просто запоминается."""
    out = {"session_id": None, "resume": False, "name": None, "settings": None, "mode": None, "append_file": None,
           "disallowed": [], "mcp_config": [], "effort": None, "model": None, "print": False, "input_format": None,
           "output_format": None, "rest": []}
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        key, eq, inline = a.partition("=") if a.startswith("--") else (a, "", "")
        if key in VALUE_FLAGS:
            if eq:
                val, i = inline, i + 1
            else:
                val, i = (argv[i + 1] if i + 1 < n else None), i + 2
            if key in ("--session-id", "--resume"):
                out["session_id"], out["resume"] = val, key == "--resume"
            elif key in ("-n", "--name"):
                out["name"] = val
            elif key == "--settings":
                out["settings"] = val
            elif key == "--permission-mode":
                out["mode"] = val
            elif key == "--append-system-prompt-file":
                out["append_file"] = val
            elif key == "--effort":
                out["effort"] = val
            elif key == "--model":
                out["model"] = val
            elif key == "--input-format":
                out["input_format"] = val
            elif key == "--output-format":
                out["output_format"] = val
            continue
        if key in LIST_FLAGS:
            vals, i = ([inline], i + 1) if eq else ([], i + 1)
            while not eq and i < n and not argv[i].startswith("-"):
                vals.append(argv[i])
                i += 1
            if key == "--disallowedTools":
                out["disallowed"] += vals
            elif key == "--mcp-config":
                out["mcp_config"] += vals
            continue
        if a in ("-p", "--print"):
            out["print"] = True
        else:
            out["rest"].append(a)
        i += 1
    return out


def is_stream_session(argv):
    """Это поднятие сессии ядром (-p + --input-format stream-json), а не `claude auth status` и не вложенный вызов."""
    p = parse(argv)
    return bool(p["print"] and p["input_format"] == "stream-json")


def decide(argv, env=None):
    """Куда идёт запуск: ("agy", None) или ("claude", причина). Оболочка уже проверила переменную маршрута и -p;
    здесь остальные случаи §2.4 (связной, мини-хаб, нет модели agy) — мост передаёт их настоящему claude."""
    env = os.environ if env is None else env
    if env.get("HUB_MOD_ANTIGRAVITY_ROUTE") != "1":
        return "claude", "нет переменной маршрута"
    if not is_stream_session(argv):
        return "claude", "не сессия хаба"
    p = parse(argv)
    if p["settings"] and os.path.basename(p["settings"]).startswith("hooks-settings-liaison"):
        return "claude", "связной"
    _rules, unmapped = deny.translate(p["disallowed"])
    if unmapped:
        return "claude", "мини-хаб (запреты, которых agy не держит: " + ", ".join(unmapped[:3]) + ")"
    if env.get("HUB_MOD_ANTIGRAVITY_MODEL") is None:
        return "claude", "модуль не дал модель agy"
    return "agy", None


# --- флаги agy ---------------------------------------------------------------------------------------------------

# живая проба agy 1.5.18: --effort принимает low|medium|high|xhigh|max, но xhigh есть только у Neon/Hydrogen -> безопасный high
EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "max"}


def mode_flags(mode):
    """Режим разрешений ядра -> флаги agy (§5.3)."""
    m = str(mode or "")
    if m == "plan":
        return ["--mode", "plan"]
    if m in ("acceptEdits", "default"):
        return ["--mode", "accept-edits"]
    if m == "auto":
        return ["--mode", "auto"]
    return ["--dangerously-skip-permissions"]   # bypassPermissions — режим хаба по умолчанию


def agy_argv(agy, p, model_id, conv_id=None):
    """Командная строка agy для сессии: один долгий процесс со stream-json в обе стороны (§1.3 вариант A)."""
    # `-p=` — пустой промпт: без него agy берёт `--input-format` как промпт (живая проба 27.11, agy 1.5.15)
    cmd = [agy, "-p=", "--input-format", "stream-json", "--output-format", "stream-json"]
    if conv_id:
        cmd += ["--conversation", conv_id]
    if model_id:
        cmd += ["--model", model_id]
    if p.get("effort") in EFFORT:
        cmd += ["--effort", EFFORT[p["effort"]]]
    return cmd + mode_flags(p.get("mode"))


STRIP = re.compile(r"^(ANTHROPIC_|HUB_MOD_ANTIGRAVITY_|CLAUDE_CODE_OAUTH_|CLAUDE_CODE_API_|HUB_ACCOUNT)")


def clean_env(env):
    """Окружение agy: без входов Claude и переменных модуля (§5.3). HUB_SESSION, HUB_STATE_DIR и PATH остаются;
    HUB_MOD_ANTIGRAVITY_DATA остаётся тоже — по нему хук agy_guard.py находит ту же папку данных, что и мост."""
    return {k: v for k, v in env.items()
            if (not STRIP.match(k) or k == "HUB_MOD_ANTIGRAVITY_DATA") and k != "CLAUDE_CODE_OAUTH_TOKEN"}


# --- ошибки agy -----------------------------------------------------------------------------------------------------

# Ошибка входа подтверждена живой пробой agy 1.5.18 (stdout result.error: "authentication failed or timed out",
# stderr: "Error: authentication required. Run 'agy' to log in, then retry.", agy models: "Error: Please sign in…").
# Тексты квоты/кредитов дополнены по CHANGELOG.md agy 1.5.18 (живой 429 без исчерпания квоты владельца не воспроизводился — ПРОБА).
QUOTA_WORDS = re.compile(
    r"resource_exhausted|\b429\b|quota|rate.?limit|too many requests|spend.?cap|"
    r"(?:credits?|prepaid)\b.{0,40}?(?:exhaust|insufficient|too low|depleted)|balance is (?:too low|insufficient)",
    re.I,
)
AUTH_WORDS = re.compile(
    r"authentication (?:required|failed)|not (?:logged|signed) in|unauthenticated|\b401\b|"
    r"login required|sign.?in required|please sign in|run ['\"]?agy['\"]? to log in",
    re.I,
)
# ядро распознаёт отказ входа Claude и лимит Claude по этим словам в тексте результата (core/reducer AUTH_TEXT, LIMIT_TEXT):
# наш текст не должен их содержать, иначе ядро сочтёт отказ входом аккаунта Claude и переведёт аккаунт
CORE_TEXT = re.compile(r"failed to authenticate|oauth token|token (has been )?revoked|invalid api key|authentication_error|"
                       r"api error: 401\b|please run /login|hit your .{0,40}limit|usage limit reached|limit reached", re.I)
RESET_HEAD = re.compile(r"(?:reset|retry|try again|until)\w*[^0-9]{0,40}?((?:\d+\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b\s*)+)", re.I)
RESET_PART = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I)


def classify(text, code=None):
    """"auth" | "quota" | None по тексту ошибки / stderr agy и коду выхода."""
    t = str(text or "")
    if AUTH_WORDS.search(t):
        return "auth"
    if QUOTA_WORDS.search(t):
        return "quota"
    return None


def reset_after(text):
    """Секунды до сброса, если agy назвал срок («retry after 90 seconds», «resets in 2 hours», «resets in 3d 3h 59m»); иначе None."""
    m = RESET_HEAD.search(str(text or ""))
    if not m:
        return None
    total = 0
    for num, unit in RESET_PART.findall(m[1]):
        u = unit.lower()
        mult = 86400 if u.startswith("d") else 3600 if u.startswith("h") else 60 if u.startswith("m") else 1
        total += int(num) * mult
    return total or None


def scrub(text, limit=300):
    """Текст agy для результата хода: без слов, по которым ядро узнаёт вход/лимит Claude, и без ключей."""
    t = " ".join(str(text or "").split())
    t = re.sub(r"(?i)(key|token|secret|authorization|bearer)[\"'=: ]+[A-Za-z0-9._\-]{12,}", r"\1=***", t)
    t = CORE_TEXT.sub("…", t)
    return t[:limit]


def real_claude(here=None):
    """Путь настоящего claude: файл real-claude рядом с оболочкой, иначе ~/.local/bin/claude (`which` найдёт саму оболочку)."""
    here = here or os.path.dirname(os.path.abspath(__file__))
    for base in (os.path.join(here, "bin"), here):
        try:
            with open(os.path.join(base, "real-claude"), encoding="utf-8") as f:
                path = f.readline().strip()
            if path and os.access(path, os.X_OK):
                return path
        except OSError:
            pass
    return os.path.expanduser("~/.local/bin/claude")
