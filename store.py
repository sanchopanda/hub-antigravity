"""Общие вещи модуля antigravity: папка данных, настройки, блок, слоты agy, учёт ходов (usage.jsonl), беседы agy.

Работает и внутри процесса модуля, и в мосте / хуке / MCP-сервере: эти процессы запускает не hub-up, ядра в PYTHONPATH
у них может не быть. Поэтому здесь только стандартная библиотека; папку данных даёт переменная окружения
HUB_MOD_ANTIGRAVITY_DATA (её ставит launch.env), иначе <HUB_STATE_DIR>/moddata/antigravity.
"""
import fcntl
import json
import os
import re
import time

NAME = "antigravity"
DEFAULTS = {
    "models": {},              # модель сессии -> id модели agy; "default" или "" — без флага --model
    "max_sessions": 2,         # процессов agy одновременно (§4)
    "quiet": None,             # "00:00-08:00": в это окно новый ход не начинаем; по умолчанию выключено (слово владельца)
    "executors": True,         # исполнители на claude-agy: разрешены владельцем 27.11.2026 (запреты держит deny.py + хук)
    "caps": {"turns_5h": None, "tokens_5h": None, "tokens_week": None},
    "fallback": "ladder",
    "primary_model": "gemini-4-hydrogen-high",       # модель group Gemini, пока есть запас (слово владельца 27.11: hydrogen)
    "fallback_model": "claude-sonnet-5-5-high",      # модель другой группы, когда Gemini исчерпан (§2)
    "idle_limit_s": 1200,      # agy молчит дольше — процесс снимаем, ход кончается ошибкой
    "block_default_s": 3600,   # блок по квоте без срока в ошибке
    "deepseek": {"model": "flash", "max_tokens": 2000, "max_tokens_hard": 8000, "day_usd": 0.5},
}
WINDOW_5H, WINDOW_WEEK = 5 * 3600, 7 * 86400


def state_dir():
    # хук наследует HUB_STATE_DIR из окружения agy (живая проба 27.11); запасной путь нужен для ручных запусков agy владельца
    return os.environ.get("HUB_STATE_DIR") or os.path.expanduser("~/.hub-personal")


def data_dir():
    path = os.environ.get("HUB_MOD_ANTIGRAVITY_DATA") or os.path.join(state_dir(), "moddata", NAME)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def config_dir():
    if os.environ.get("HUB_CONFIG_DIR"):
        return os.environ["HUB_CONFIG_DIR"]
    if os.environ.get("HUB_CONFIG"):
        return os.path.dirname(os.path.abspath(os.environ["HUB_CONFIG"]))
    state = os.environ.get("HUB_STATE_DIR")
    if state:
        instance = read_json(os.path.join(state, "instance.json"))
        directory = instance.get("config_dir") if isinstance(instance, dict) and instance.get("name") else None
        if isinstance(directory, str) and directory:
            return directory
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    standard = os.path.join(xdg, "hub-core")
    if os.path.isfile(os.path.join(standard, "hub.json")):
        return standard
    return os.path.join(xdg, "hub-personal")


def hub_json_path():
    if os.environ.get("HUB_CONFIG"):
        return os.environ["HUB_CONFIG"]
    std = os.path.join(config_dir(), "hub.json")
    old = os.path.join(state_dir(), "hub.json")
    return std if os.path.exists(std) or not os.path.exists(old) else old


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def settings():
    """Раздел antigravity из hub.json поверх умолчаний (вложенные словари сливаются на один уровень)."""
    raw = read_json(hub_json_path()).get(NAME)
    raw = raw if isinstance(raw, dict) else {}
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k].update(v)
        elif k in out:
            out[k] = v
    return out


def agy_model(model, cfg=None):
    """Id модели agy для модели сессии (claude-agy…); None — модуль её не знает; "" — agy по умолчанию (без --model)."""
    name = str(model or "")
    models = (cfg or settings())["models"]
    got = models.get(name)
    if got is None and name.startswith("codex/"):
        got = models.get(name[len("codex/"):])
    bare = name[len("codex/"):] if name.startswith("codex/") else name
    if got is None and bare == "claude-agy-hydrogen":
        got = "gemini-4-hydrogen-high"
    if got is None and bare.startswith("claude-agy-"):
        candidate = bare[len("claude-agy-"):]
        if candidate.startswith(("gemini-", "claude-", "gpt-oss-")):
            got = candidate
    if got is None:
        return None
    got = str(got).strip()
    return "" if got in ("", "default") else got


AGY = re.compile(r"(^|[-_/])agy($|[-_])")   # то же правило в daybudget/budget.py и poolpass/poolpass.py


def is_agy_model(model, cfg=None):
    """Модель Antigravity: имя с agy или явно заданная в antigravity.models."""
    name = str(model or "").lower()
    bare = name[len("codex/"):] if name.startswith("codex/") else name
    return bool(AGY.search(name)) or bare in (cfg or settings())["models"] or bare.startswith("claude-agy-")


# --- блок (квота / вход) -------------------------------------------------------------------------------------------
# У agy две группы моделей с отдельными окнами квоты (Gemini и Claude/GPT — "3p"). Ошибка квоты одной группы закрывает
# только её: вторая работает дальше. Ключи групп — quota.GROUPS (здесь же, чтобы state.json читался без quota).
GROUPS = ("gemini", "3p")
GROUP_LABEL = {"gemini": "Gemini", "3p": "Claude/GPT"}


def state_path():
    return os.path.join(data_dir(), "state.json")


def _records():
    """{группа: запись} из state.json, просроченные тоже. Старый формат (один блок на весь agy) — блок всех групп."""
    st = read_json(state_path())
    recs = st.get("blocks")
    if isinstance(recs, dict):
        return {g: r for g, r in recs.items() if isinstance(r, dict) and r.get("reason")}
    if st.get("reason"):
        return {g: st for g in GROUPS}
    return {}


def blocks(now=None):
    """Действующие блоки по группам: {gemini: запись, 3p: запись}. Просроченные сюда не входят."""
    now = time.time() if now is None else now
    return {g: r for g, r in _records().items() if float(r.get("until") or 0) > now}


def block(group=None, now=None):
    """Действующий блок {until, reason, text, at, group} или None. group=None — любой блок (старый смысл).
    Просроченный — None (файл не трогаем: его чистит unblock/sweep)."""
    got = blocks(now)
    if group is not None:
        return got.get(group)
    return next(iter(got.values()), None)


def all_blocked(now=None):
    """agy закрыт целиком: блок есть у всех групп (общий вход или квота обеих групп)."""
    return set(blocks(now)) >= set(GROUPS)


def set_block(reason, until, text, now=None, group=None):
    """Записать блок. group=None — на весь agy (вход), иначе только этой группе квоты. Чужие группы не трогаем."""
    now = time.time() if now is None else now
    recs = _records()
    for g in (GROUPS if group is None else (group,)):
        recs[g] = {"reason": reason, "until": float(until), "text": text, "at": now, "group": g}
    write_json(state_path(), {"blocks": recs})


def clear_block(group=None):
    """Снять блок: group=None — все (unblock), иначе только этой группы."""
    recs = _records()
    if group is not None:
        if group not in recs:
            return
        recs.pop(group)
        if recs:
            write_json(state_path(), {"blocks": recs})
            return
    try:
        os.unlink(state_path())
    except OSError:
        pass


def clear_expired(now=None):
    """Убрать просроченные записи, действующие оставить (sweep зовёт каждый проход)."""
    now = time.time() if now is None else now
    recs = _records()
    alive = {g: r for g, r in recs.items() if float(r.get("until") or 0) > now}
    if len(alive) == len(recs):
        return
    if alive:
        write_json(state_path(), {"blocks": alive})
    else:
        clear_block()


def block_record():
    """Какой-нибудь блок как записан, просроченный тоже (sweep по нему возвращает сессии)."""
    return next(iter(_records().values()), None)


# --- слоты agy (flock на время жизни процесса) ---------------------------------------------------------------------

def slots_dir():
    path = os.path.join(data_dir(), "slots")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


class Slot:
    """Занятое место: открытый файл с flock, пока жив мост. release() или конец процесса снимают замок."""

    def __init__(self, n, fd):
        self.n, self.fd = n, fd

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
            self.fd = None


def take_slot(session, limit):
    """Занять свободное место из limit; (Slot|None, [имена тех, кто занял]). Имя сессии лежит в файле места."""
    d = slots_dir()
    holders = []
    for n in range(1, max(1, int(limit)) + 1):
        path = os.path.join(d, f"{n}.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holders.append(_slot_name(fd) or f"место {n}")
            os.close(fd)
            continue
        os.ftruncate(fd, 0)
        os.write(fd, str(session or "?").encode())
        return Slot(n, fd), holders
    return None, holders


def _slot_name(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 200).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def slot_report(limit):
    """[(n, имя занявшего | None)] — для hub antigravity (замок на время проверки не берём насовсем)."""
    out = []
    d = slots_dir()
    for n in range(1, max(1, int(limit)) + 1):
        path = os.path.join(d, f"{n}.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            out.append((n, None))
        except OSError:
            out.append((n, _slot_name(fd) or "?"))
        finally:
            os.close(fd)
    return out


# --- учёт ходов -----------------------------------------------------------------------------------------------------

def append_line(path, rec):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_lines(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def usage_path():
    return os.path.join(data_dir(), "usage.jsonl")


def log_usage(session, model, tokens, duration_ms, status, now=None):
    append_line(usage_path(), {"at": time.time() if now is None else now, "session": session, "model": model,
                               "in": tokens.get("in", 0), "out": tokens.get("out", 0), "think": tokens.get("think", 0),
                               "cache": tokens.get("cache", 0), "ms": duration_ms, "status": status})


def usage_sum(window_s, now=None):
    """{turns, in, out, think, cache} за последние window_s секунд."""
    now = time.time() if now is None else now
    tot = {"turns": 0, "in": 0, "out": 0, "think": 0, "cache": 0}
    for r in read_lines(usage_path()):
        if now - float(r.get("at") or 0) <= window_s:
            tot["turns"] += 1
            for k in ("in", "out", "think", "cache"):
                tot[k] += int(r.get(k) or 0)
    return tot


def caps_hit(cfg=None, now=None):
    """Текст отказа, если свой потолок (caps) набран; None — можно. Выключенные (null) не проверяются."""
    caps = (cfg or settings()).get("caps") or {}
    h5, wk = usage_sum(WINDOW_5H, now), usage_sum(WINDOW_WEEK, now)
    if caps.get("turns_5h") and h5["turns"] >= caps["turns_5h"]:
        return f"свой потолок: {h5['turns']} ходов за 5 ч"
    if caps.get("tokens_5h") and h5["in"] + h5["out"] + h5["think"] >= caps["tokens_5h"]:
        return "свой потолок: токены за 5 ч"
    if caps.get("tokens_week") and wk["in"] + wk["out"] + wk["think"] >= caps["tokens_week"]:
        return "свой потолок: токены за неделю"
    return None


# --- беседы agy и журнал ходов ----------------------------------------------------------------------------------------

def convs_path():
    return os.path.join(data_dir(), "convs.json")


def conv(uuid):
    got = read_json(convs_path()).get(uuid)
    return got if isinstance(got, dict) else {}


def set_conv(uuid, **fields):
    path = convs_path()
    with open(path + ".lock", "a", encoding="utf-8") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("Не удалось прочитать беседы Antigravity")
        rec = data.get(uuid) if isinstance(data.get(uuid), dict) else {}
        rec.update(fields)
        data[uuid] = rec
        write_json(path, data)
    return rec


def conv_by_id(conversation_id):
    """uuid сессии хаба по conversation_id agy (для guard: сессию узнаём по беседе, а не по окружению)."""
    for uuid, rec in read_json(convs_path()).items():
        if isinstance(rec, dict) and rec.get("conv") == conversation_id:
            return uuid
    return None


SAFE_UUID = re.compile(r"[A-Za-z0-9._-]{1,80}")


def turns_path(uuid):
    if not SAFE_UUID.fullmatch(str(uuid or "")):
        raise ValueError("uuid сессии не годится для имени файла")
    return os.path.join(data_dir(), "turns", f"{uuid}.jsonl")


def log_turn(uuid, engine, request, response, tools, now=None):
    append_line(turns_path(uuid), {"at": time.time() if now is None else now, "engine": engine,
                                   "req": str(request)[:2000], "resp": str(response)[:4000], "tools": list(tools)[:50]})


def log_deepseek(model, tokens_in, tokens_out, est_usd, now=None):
    """Вызов deepseek_ask: без текстов, только числа."""
    append_line(os.path.join(data_dir(), "deepseek.jsonl"), {"at": time.time() if now is None else now, "model": model,
                                                              "in": tokens_in, "out": tokens_out, "usd": est_usd})


def deepseek_spent_today(now=None):
    """Оценка траты инструмента за локальные сутки (по часам Mac) — потолок day_usd."""
    now = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    return sum(float(r.get("usd") or 0) for r in read_lines(os.path.join(data_dir(), "deepseek.jsonl"))
               if time.strftime("%Y-%m-%d", time.localtime(float(r.get("at") or 0))) == day)


def in_quiet(quiet, now=None):
    """Попадает ли сейчас (локальное время) в окно "HH:MM-HH:MM", в том числе через полночь. None/пусто — нет."""
    m = re.fullmatch(r"(\d\d):(\d\d)-(\d\d):(\d\d)", str(quiet or "").strip())
    if not m:
        return False
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    t = time.localtime(time.time() if now is None else now)
    cur = t.tm_hour * 60 + t.tm_min
    if a == b:
        return False
    return a <= cur < b if a < b else (cur >= a or cur < b)
