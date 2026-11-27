"""Модуль antigravity: сессии хаба на Antigravity (agy) и DeepSeek внутри них (спека ~/.hub-personal/antigravity-research/spec.md).

Точки: launch.env (маршрут сессии: ставит переменные оболочке bin/claude), cli (`hub antigravity …`), doctor. Сам модуль
сессии не запускает: их запускает ядро через оболочку (HUB_CLAUDE в службе хаба — это включает владелец, §2.5).
Ещё одна работа в процессе: один раз сообщить владельцу о новом блоке (квота / вход) — owner.notify с dedup_key.
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("HUB_REPO") or os.getcwd())   # ядро: hub-up даёт HUB_REPO и PYTHONPATH
sys.path.insert(0, HERE)
from core import hubmod  # noqa: E402

import agycli as agy_cmd  # noqa: E402
import handoff  # noqa: E402
import quota  # noqa: E402
import setup as agy_setup  # noqa: E402
import store  # noqa: E402

POLL = 15


def default_model():
    """Модель по умолчанию хаба: верхний model или codex.model на хабе Codex."""
    data = store.read_json(store.hub_json_path())
    codex = data.get("codex") if isinstance(data.get("codex"), dict) else {}
    return data.get("model") or codex.get("model")


def launch(req, cfg=None, default=None):
    """Ответ launch.env: {} — сессия не наша; иначе {env}: маршрут для оболочки bin/claude (§2.4).

    - модель сессии с agy и id модели известен — ROUTE=1, MODEL=<id agy> ("" — agy по умолчанию);
    - модель не agy, сессия поднимается с resume, и после последнего хода Claude были ходы agy — ROUTE=handoff:
      оболочка передаст итог (§6.3);
    - иначе ничего: оболочка сразу exec настоящего claude.
    Сбой здесь не должен ломать чужие сессии: любая ошибка — {}."""
    cfg = cfg or store.settings()
    asked = req.get("model") or default
    base = {"HUB_MOD_ANTIGRAVITY_PYTHON": sys.executable, "HUB_MOD_ANTIGRAVITY_DATA": store.data_dir()}
    if store.is_agy_model(asked, cfg):
        mid = store.agy_model(asked, cfg)
        if mid is None:
            return {}       # id модели не задан: claude откажет понятной ошибкой модели, сессия не уйдёт молча на Sonnet
        # "" ядро вырезает из окружения (modrun: пустая строка удаляет переменную) — по умолчанию agy шлём метку "default"
        env = {**base, "HUB_MOD_ANTIGRAVITY_ROUTE": "1", "HUB_MOD_ANTIGRAVITY_MODEL": mid or "default",
               "HUB_MOD_ANTIGRAVITY_ROOT": HERE,
               "HUB_MOD_ANTIGRAVITY_NAME": str(asked)}
        if req.get("kind") == "work":
            env["HUB_MOD_ANTIGRAVITY_CODEX_WORK"] = "1"
        return {"env": env}
    if req.get("resume") and req.get("session"):
        uuid = next((u for u, rec in store.read_json(store.convs_path()).items()
                     if isinstance(rec, dict) and rec.get("session") == req["session"]), None)
        if uuid and handoff.pending(uuid):
            return {"env": {**base, "HUB_MOD_ANTIGRAVITY_ROUTE": "handoff"}}
    return {}


@hubmod.hook("launch.env")
def launch_env(req):
    try:
        return launch(req, default=default_model())
    except Exception as e:   # noqa: BLE001 — сбой модуля не должен ронять запуск чужих сессий
        print(f"antigravity launch.env: {type(e).__name__}", file=sys.stderr, flush=True)
        return {}


@hubmod.hook("cli")
def cli(req):
    return agy_cmd.handle(req.get("args"), caller_owner=bool((req.get("caller") or {}).get("owner")))


@hubmod.hook("doctor")
def doctor(req):
    return agy_cmd.doctor()


NOTIFIED = "notified"


def watch_once(now=None):
    """Новый блок — один owner.notify на блок (dedup_key по времени записи). Возвращает отправленное."""
    rec = store.block(now)
    if not rec:
        return None
    key = f"antigravity-{rec['reason']}:{int(rec.get('at') or 0)}"
    if hubmod.state.get(NOTIFIED) == key:
        return None
    hubmod.fact("owner.notify", {"title": "Antigravity", "text": rec.get("text") or "блок Antigravity",
                                 "id": f"antigravity-{rec['reason']}", "topic": "Antigravity", "urgency": "later"},
                dedup_key=key)
    hubmod.state.set(NOTIFIED, key)
    return key


def quota_loop():
    """Отдельный поток опроса `agy /usage` (кэш <= 5 мин), чтобы не тормозить watch_once и хуки модуля."""
    poller = quota.Poller(hubmod.state.set)
    try:
        poller.prime()
    except Exception as e:  # noqa: BLE001 — сбой квоты не должен ронять модуль
        print(f"antigravity quota prime: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    while True:
        try:
            poller.step()
        except Exception as e:  # noqa: BLE001 — один плохой проход не останавливает опрос
            print(f"antigravity quota: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(quota.REFRESH_FAIL)
            continue
        time.sleep(poller.wait_seconds())


@hubmod.on_ready
def loop():
    threading.Thread(target=quota_loop, name="antigravity-quota", daemon=True).start()
    while True:
        try:
            watch_once()
            agy_setup.sync_managed_paths()
            agy_setup.sync_skills()   # обновился слой хаба или появился проект — скиллы в конфиге agy догоняют
        except Exception as e:  # noqa: BLE001 — один плохой проход не останавливает слежение
            print(f"antigravity: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    hubmod.run()
