"""Передача итога при смене движка (§6): разговоры Claude Code и agy в разных форматах, поэтому не переносим, а даём выжимку.

- agy -> Claude/DeepSeek: оболочка (bin/claude, маршрут handoff) подменяет файл `--append-system-prompt-file` копией с блоком
  «последние ходы на Antigravity» и, если у Claude нет транскрипта этой сессии, меняет `--resume` на `--session-id`.
- Claude/DeepSeek -> agy: мост кладёт в первое сообщение agy выжимку ходов из транскрипта Claude, которых agy не видел.
Код и файлы не копируем: движок читает их сам в рабочей папке. Размер выжимки — до LIMIT знаков.
"""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # python -I не добавляет папку скрипта
import route  # noqa: E402
import store  # noqa: E402

LIMIT = 12000          # знаков в выжимке
WHOLE = 8000           # из них — последние ходы целиком
ITEM = 2000            # один ход целиком не длиннее
HEAD = 160             # у старших ходов — только начало


def summarize(items, limit=LIMIT):
    """[(кто, текст)] от старого к новому -> строка до limit знаков: последние ходы целиком (до WHOLE знаков), старшие — началами."""
    lines, used, whole_stop = [], 0, len(items)
    for i in range(len(items) - 1, -1, -1):
        who, text = items[i]
        text = str(text).strip()
        cut = text if len(text) <= ITEM else text[:ITEM] + "…"
        if used + len(cut) > WHOLE:
            break
        used += len(cut) + len(who) + 3
        whole_stop = i
    out = []
    for who, text in items[:whole_stop]:
        head = " ".join(str(text).split())
        out.append(f"{who}: {head[:HEAD]}{'…' if len(head) > HEAD else ''}")
    for who, text in items[whole_stop:]:
        text = str(text).strip()
        out.append(f"{who}: {text if len(text) <= ITEM else text[:ITEM] + '…'}")
    # общий потолок: если вышло длиннее, режем самые старые строки целиком, потом хвост
    while len("\n".join(out)) > limit and len(out) > 1:
        out.pop(0)
    got = "\n".join(out)
    return got if len(got) <= limit else got[-limit:]


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def transcript(uuid):
    """Путь транскрипта Claude Code этой сессии или None (папка проекта по slug не вычисляется — ищем по uuid)."""
    if not store.SAFE_UUID.fullmatch(str(uuid or "")):
        return None
    got = glob.glob(os.path.join(glob.escape(claude_dir()), "projects", "*", f"{uuid}.jsonl"))
    return max(got, key=os.path.getmtime) if got else None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def transcript_items(path, since=0.0):
    """Текст запросов и ответов из транскрипта Claude Code: [(кто, текст)]. Инструменты и их вывод — нет."""
    out = []
    for rec in store.read_lines(path):
        if not isinstance(rec, dict) or rec.get("isSidechain") or rec.get("isMeta"):
            continue
        kind = rec.get("type")
        if kind not in ("user", "assistant"):
            continue
        text = _text((rec.get("message") or {}).get("content")).strip()
        if not text or text.startswith(("<command-", "<local-command", "<system-reminder", "Caveat:")):
            continue
        out.append(("Владелец" if kind == "user" else "Ассистент", text))
    return out


def since_agy_items(uuid, since):
    """Ходы других движков, которых agy не видел: из транскрипта Claude Code (только текст)."""
    path = transcript(uuid)
    if not path:
        return []
    if since and os.path.getmtime(path) <= since:
        return []
    items = transcript_items(path)
    return items


def agy_items(uuid, since=0.0):
    """Ходы agy из нашего журнала после since: [(кто, текст)]."""
    out = []
    for t in store.read_lines(store.turns_path(uuid)):
        if t.get("engine") != "agy" or float(t.get("at") or 0) <= since:
            continue
        out.append(("Владелец", t.get("req") or ""))
        out.append(("Ассистент", t.get("resp") or ""))
    return out


def pending(uuid):
    """Нужна ли передача agy -> другой движок: ходы agy новее последнего хода Claude и последней передачи. -> items."""
    rec = store.conv(uuid)
    path = transcript(uuid)
    last_claude = os.path.getmtime(path) if path else 0.0
    return agy_items(uuid, max(last_claude, float(rec.get("handoff_at") or 0)))


BLOCK = ("\n\n## Работа шла на Antigravity (модель Gemini)\n"
         "Эту сессию вёл другой движок; его разговор тебе не виден. Вот последние ходы (текст запросов и ответов, без кода "
         "и файлов — их читай в рабочей папке сам):\n\n{body}\n")
FIRST = ("<hub-handoff>\nЭту сессию до тебя вёл другой движок (Claude или DeepSeek). Вот ходы, которых ты не видел "
         "(только текст; код и файлы читай в рабочей папке):\n\n{body}\n</hub-handoff>\n\n")


def rewrite(argv, now=None):
    """Аргументы claude после передачи итога: -> (новые аргументы, что сделано). Ничего не нужно — (argv, None)."""
    p = route.parse(argv)
    uuid = p["session_id"]
    if not (p["resume"] and uuid and store.SAFE_UUID.fullmatch(uuid)):
        return argv, None
    items = pending(uuid)
    if not items:
        return argv, None
    now = time.time() if now is None else now
    body = summarize(items)
    base = ""
    if p["append_file"]:
        try:
            with open(p["append_file"], encoding="utf-8") as f:
                base = f.read()
        except OSError:
            base = ""
    folder = os.path.join(store.data_dir(), "handoff")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    path = os.path.join(folder, f"{uuid}-{int(now)}.md")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(base + BLOCK.format(body=body))
    out, did = list(argv), ["instructions"]
    if p["append_file"]:
        for i, a in enumerate(out):
            if a == "--append-system-prompt-file" and i + 1 < len(out):
                out[i + 1] = path
            elif a.startswith("--append-system-prompt-file="):
                out[i] = f"--append-system-prompt-file={path}"
    else:
        out += ["--append-system-prompt-file", path, "--system-prompt-snapshot", "off"]
    if not transcript(uuid):   # Claude Code упал бы «No conversation found»: разговора у него нет, начинаем с этим же uuid
        out = ["--session-id" if a == "--resume" else a for a in out]
        did.append("session-id")
    store.set_conv(uuid, handoff_at=now)
    return out, did


def main(argv):
    real = route.real_claude()
    try:
        argv, _ = rewrite(argv)
    except Exception as e:   # noqa: BLE001 — оболочка не имеет права уронить сессию: без передачи, но с тем же claude
        try:
            sys.stderr.write(f"antigravity handoff: {type(e).__name__}\n")
        except OSError:
            pass
    os.execv(real, [real] + argv)


if __name__ == "__main__":
    main(sys.argv[1:])
