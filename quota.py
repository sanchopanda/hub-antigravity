"""Квота Antigravity для виджета на странице хаба: `agy -p "/usage" --output-format json`, кэш ≤ 5 минут.

Только стандартная библиотека: работает и внутри процесса модуля, и в тестах без живого хаба.
Если agy не ответил или кэш устарел — отдаём ok=False (на странице прочерк, без текста ошибки).
"""
from datetime import datetime, timezone
import json
import math
import os
import subprocess
import time

import agycli
import store

KEY = "antigravity_quota"
CACHE_FILE = "quota.json"
QUOTA_TTL = 300        # кэш не дольше 5 минут (секунды)
REFRESH_OK = 270       # обновляем заранее (4,5 мин), чтобы на странице не было провала в прочерк на границе 5 мин
REFRESH_FAIL = 60      # после сбоя пробуем снова через минуту
TIMEOUT = 30           # agy /usage обычно отвечает ~8-15 с


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def parse_reset_time(raw):
    """ISO-время сброса в unix-секунды; 1970-01-01T00:00:00Z (<= 0) и мусор -> None."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.timestamp()
    return ts if math.isfinite(ts) and ts > 0 else None


def parse_bucket(bucket):
    """Одно окно квоты из agy /usage -> {remaining, pct, reset_at} или None."""
    if not isinstance(bucket, dict):
        return None
    rem = bucket.get("remaining_fraction")
    if not _num(rem):
        return None
    rem = max(0.0, min(1.0, float(rem)))
    pct = int(round(rem * 100))
    reset_at = parse_reset_time(bucket.get("reset_time"))
    return {"remaining": round(rem, 4), "pct": pct, "reset_at": reset_at}


def _group_key(group):
    """Классификация группы agy: gemini | None."""
    name = str(group.get("name") or "").lower()
    buckets = group.get("buckets") if isinstance(group.get("buckets"), list) else []
    ids = [str(b.get("id") or "").lower() for b in buckets if isinstance(b, dict)]
    if "gemini" in name or any(i.startswith("gemini") for i in ids):
        return "gemini"
    return None


def _bucket_window(bucket):
    win = str(bucket.get("window") or "").lower()
    bid = str(bucket.get("id") or "").lower()
    if win == "5h" or bid.endswith("-5h"):
        return "h5"
    if win in ("weekly", "week", "7d") or bid.endswith("-weekly"):
        return "weekly"
    return None


def _loads_json(text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def parse_usage(text):
    """Разобрать JSON вывода `agy -p "/usage" --output-format json` -> {gemini} или None."""
    obj = _loads_json(text)
    if not isinstance(obj, dict):
        return None
    status = obj.get("status")
    if status is not None and str(status).upper() != "SUCCESS":
        return None
    cmd = obj.get("command")
    data = cmd.get("data") if isinstance(cmd, dict) else None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return None
    out = {"gemini": None}
    found = False
    for g in groups:
        if not isinstance(g, dict):
            continue
        gkey = _group_key(g)
        if not gkey:
            continue
        parsed_group = {}
        for b in g.get("buckets") or []:
            if not isinstance(b, dict):
                continue
            wkey = _bucket_window(b)
            if not wkey:
                continue
            val = parse_bucket(b)
            if val is not None:
                parsed_group[wkey] = val
                found = True
        if parsed_group:
            out[gkey] = parsed_group
    return out if found else None


def empty_record(now=None, ttl=QUOTA_TTL):
    now = time.time() if now is None else float(now)
    return {"ok": False, "at": now, "fetched_at": None, "ttl_s": int(ttl), "gemini": None}


def fetch(run=subprocess.run, agy=None, timeout=TIMEOUT, now=None, ttl=QUOTA_TTL):
    """Опросить `agy -p "/usage" --output-format json`. Всегда возвращает публичный словарь для снимка."""
    now = time.time() if now is None else float(now)
    exe = agy or agycli.agy_path()
    try:
        cp = run([exe, "-p", "/usage", "--output-format", "json"],
                 capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return empty_record(now, ttl=ttl)
    if getattr(cp, "returncode", 1) != 0:
        return empty_record(now, ttl=ttl)
    parsed = parse_usage(getattr(cp, "stdout", ""))
    if not parsed:
        return empty_record(now, ttl=ttl)
    return {"ok": True, "at": now, "fetched_at": now, "ttl_s": int(ttl), "gemini": parsed["gemini"]}


def cache_path():
    return os.path.join(store.data_dir(), CACHE_FILE)


def load_cache(now=None, ttl=QUOTA_TTL):
    """Свежий кэш с диска (не старше ttl секунд) или None."""
    now = time.time() if now is None else float(now)
    rec = store.read_json(cache_path(), None)
    if not isinstance(rec, dict) or not rec.get("ok"):
        return None
    fetched = rec.get("fetched_at")
    if not _num(fetched):
        return None
    age = now - float(fetched)
    if age < 0 or age >= ttl:
        return None
    return {"ok": True, "at": now, "fetched_at": float(fetched), "ttl_s": int(ttl),
            "gemini": rec.get("gemini")}


def save_cache(rec):
    try:
        store.write_json(cache_path(), rec)
    except OSError:
        pass


class Poller:
    """Хранит состояние опроса квоты и обновляет ключ снимка с кэшем <= 5 минут."""

    def __init__(self, write_state, run=subprocess.run, ttl=QUOTA_TTL,
                 refresh_ok=REFRESH_OK, refresh_fail=REFRESH_FAIL):
        self.write_state = write_state
        self.run = run
        self.ttl = min(int(ttl), QUOTA_TTL)
        self.refresh_ok = min(int(refresh_ok), self.ttl)
        self.refresh_fail = int(refresh_fail)
        self.last = None
        self.next_at = 0.0
        self.written = False

    def _publish(self, rec):
        prev = self.last
        # Первую запись после старта процесса делаем всегда (требование ядра для snapshot.keys).
        # Повторные неудачи подряд не спамят журнал; успешный свежий опрос обновляет fetched_at.
        same_fail = (self.written and isinstance(prev, dict) and not prev.get("ok") and not rec.get("ok"))
        self.last = rec
        if not same_fail:
            self.write_state(KEY, rec)
            self.written = True
        return rec

    def prime(self, now=None):
        """Сразу после старта модуля: свежий кэш с диска -> в снимок, иначе пустой маркер до первого ответа agy."""
        now = time.time() if now is None else float(now)
        cached = load_cache(now=now, ttl=self.ttl)
        if cached:
            self.next_at = cached["fetched_at"] + self.refresh_ok
            return self._publish(cached)
        self.next_at = now
        return self._publish(empty_record(now, ttl=self.ttl))

    def step(self, now=None, agy=None):
        """Один шаг опроса: если пора — зовёт agy /usage, пишет кэш и снимок. Возвращает новую запись или None."""
        now = time.time() if now is None else float(now)
        if self.written and now < self.next_at:
            return None
        rec = fetch(run=self.run, agy=agy, now=now, ttl=self.ttl)
        if rec["ok"]:
            save_cache(rec)
            self.next_at = now + self.refresh_ok
        else:
            self.next_at = now + self.refresh_fail
        return self._publish(rec)

    def wait_seconds(self, now=None):
        now = time.time() if now is None else float(now)
        return max(1.0, min(self.refresh_fail, self.next_at - now))
