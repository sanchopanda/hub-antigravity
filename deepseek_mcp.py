#!/usr/bin/env python3
"""MCP-сервер (stdio) с инструментом `deepseek_ask`: Gemini внутри хода agy задаёт вопрос DeepSeek (§7).

Ключ — только из engines.json в папке настроек хаба (права 0600), в окружении, аргументах и журналах его нет; в текстах
ошибок он маскируется. Перед каждым вызовом сервер спрашивает `hub budget --json` (только чтение) и отказывает, если
денег нет, потолок дня набран, оценка больше остатка дня, свой потолок инструмента (day_usd) набран или ответа нет.
Отказ — обычный текст для модели «DeepSeek недоступен: …», ход agy не падает.

Адрес DeepSeek — из каталога модуля engines (ext/engines/engines.py), для стенда его подменяет
HUB_MOD_ANTIGRAVITY_DEEPSEEK_URL. Режим `--ask` — тот же код как команда оболочки (bin/deepseek-ask): запасной путь,
если agy не вызывает инструменты MCP в `-p` (ПРОБА, §10 п. 5).
"""
import importlib.util
import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # python -I не добавляет папку скрипта
import store  # noqa: E402

SERVER = {"name": "hub-deepseek", "version": "0.1.0"}
PROTOCOL = "2025-06-18"
LIMITS = {"prompt": 60000, "system": 8000}
# USD за 1 млн токенов, пиковые часы, промах кэша — верхняя граница оценки. Копия core/engines.PRICES (проверено 2026-11-09:
# https://api-docs.deepseek.com/quick_start/pricing); дата проверки — в строке отчёта «est_cost_usd — оценка»
PRICES = {"flash": {"in": 0.3, "out": 1.2}, "pro": {"in": 1.32, "out": 3.96}}
FALLBACK = {"base_url": "https://api.deepseek.com/anthropic", "variants": {"": "deepseek-v4-flash", "pro": "deepseek-v4-pro"}}
TOOL = {
    "name": "deepseek_ask",
    "description": ("Спросить у DeepSeek (другая модель) и получить текст ответа. Для второго мнения, проверки решения, "
                    "рутины. Платный: потолок трат в день задан хабом; при отказе вернёт причину."),
    "inputSchema": {"type": "object", "properties": {
        "prompt": {"type": "string", "description": "вопрос, до 60 тыс. знаков"},
        "system": {"type": "string", "description": "системная инструкция, до 8 тыс. знаков"},
        "model": {"type": "string", "enum": ["flash", "pro"], "description": "по умолчанию из настроек хаба (flash)"},
        "max_tokens": {"type": "integer", "description": "длина ответа; по умолчанию 2000, потолок 8000"}},
        "required": ["prompt"]},
}


class Refused(Exception):
    """Отказ с причиной для модели; ход agy от него не падает."""


def mask(text, key):
    t = str(text)
    if key:
        t = t.replace(key, "***")
    return re.sub(r"(?i)(bearer|api[-_ ]?key|token)[\"'=: ]+[A-Za-z0-9._\-]{16,}", r"\1 ***", t)


# --- ключ -----------------------------------------------------------------------------------------------------------

def load_key(path=None):
    """Ключ DeepSeek из engines.json: только если файл виден лишь владельцу (0600), иначе отказ."""
    path = path or os.path.join(store.config_dir(), "engines.json")
    try:
        st = os.stat(path)
    except OSError:
        raise Refused("нет файла с ключом (engines.json)") from None
    if st.st_mode & 0o077:
        raise Refused(f"права engines.json {oct(st.st_mode & 0o777)[2:]}, нужны 600 — ключ не читаю")
    try:
        with open(path, encoding="utf-8") as f:
            key = ((json.load(f) or {}).get("keys") or {}).get("deepseek")
    except (OSError, ValueError):
        raise Refused("engines.json не читается") from None
    if not isinstance(key, str) or not key or any(c.isspace() for c in key):
        raise Refused("ключа deepseek в engines.json нет")
    return key


# --- адрес и модели --------------------------------------------------------------------------------------------------

def engine_spec():
    """{base_url, variants} DeepSeek из каталога модуля engines (с поправками hub.json); запасное значение — пресет."""
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines", "engines.py")
        spec = importlib.util.spec_from_file_location("hub_engines_catalog", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cat = mod.catalog({"engines": store.read_json(store.hub_json_path()).get("engines") or {}})
        got = cat.get("deepseek")
        if got and got.get("base_url") and got.get("variants"):
            return {"base_url": got["base_url"], "variants": got["variants"]}
    except Exception:   # noqa: BLE001 — каталога нет (стенд без ядра): пресет
        pass
    return FALLBACK


def endpoint():
    # ПРОБА: путь <base_url>/v1/messages у DeepSeek (Anthropic-совместимый адрес) проверяет только живой вызов — по слову владельца, до 0,01 $
    base = os.environ.get("HUB_MOD_ANTIGRAVITY_DEEPSEEK_URL") or engine_spec()["base_url"]
    return base.rstrip("/") + "/v1/messages"


def model_id(short):
    variants = engine_spec()["variants"]
    return variants.get("pro" if short == "pro" else "") or FALLBACK["variants"]["pro" if short == "pro" else ""]


# --- проверка аргументов, оценка, деньги -------------------------------------------------------------------------------

def check_args(args, cfg):
    args = args if isinstance(args, dict) else {}
    prompt, system = args.get("prompt"), args.get("system")
    if not isinstance(prompt, str) or not prompt.strip():
        raise Refused("нужен непустой prompt")
    if len(prompt) > LIMITS["prompt"]:
        raise Refused(f"prompt длиннее {LIMITS['prompt']} знаков")
    if system is not None and (not isinstance(system, str) or len(system) > LIMITS["system"]):
        raise Refused(f"system должен быть строкой до {LIMITS['system']} знаков")
    short = args.get("model") or cfg.get("model") or "flash"
    if short not in PRICES:
        raise Refused("model: flash или pro")
    hard = int(cfg.get("max_tokens_hard") or 8000)
    mt = args.get("max_tokens", cfg.get("max_tokens") or 2000)
    if isinstance(mt, bool) or not isinstance(mt, int) or mt < 1:
        raise Refused("max_tokens: целое число больше нуля")
    return prompt, system, short, min(mt, hard)


def estimate(prompt, system, short, max_tokens):
    """Верхняя оценка цены: (знаков ÷ 3 + max_tokens) по пиковой цене промаха кэша (§7.3)."""
    tin = math.ceil((len(prompt) + len(system or "")) / 3)
    p = PRICES[short]
    return round((tin * p["in"] + max_tokens * p["out"]) / 1e6, 6)


def hub_budget(timeout=None):
    """`hub budget --json` — словарь или None (не пришёл). Только чтение."""
    exe = os.environ.get("HUB_MOD_ANTIGRAVITY_HUB") or shutil.which("hub") or os.path.expanduser("~/.hub-personal/bin/hub")
    timeout = float(timeout or os.environ.get("HUB_MOD_ANTIGRAVITY_HUB_TIMEOUT") or 10)
    try:
        r = subprocess.run([exe, "budget", "--json"], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        got = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return got if isinstance(got, dict) else None


def money_gate(bj, est, tool_day_usd, tool_spent):
    """Причина отказа по ответу hub budget или None — вызывать можно (§7.3)."""
    if not isinstance(bj, dict):
        raise Refused("ответ hub budget не пришёл — деньги дня неизвестны")
    if bj.get("money_left") is not True:
        raise Refused(f"денег DeepSeek на сегодня нет ({bj.get('money_why') or 'money_left=false'})")
    over = bj.get("over") is True
    if bj.get("reached") is True and not over:
        raise Refused("потолок дня DeepSeek набран (hub budget over — только слово владельца)")
    left = bj.get("left_today_usd")
    if not over and isinstance(left, (int, float)) and not isinstance(left, bool) and est > left:
        raise Refused(f"оценка {est:.4f} $ больше остатка дня {left:.2f} $")
    if tool_spent + est > tool_day_usd:
        raise Refused(f"потолок инструмента на день ({tool_day_usd:.2f} $) набран: потрачено {tool_spent:.4f} $, оценка {est:.4f} $")


# --- вызов --------------------------------------------------------------------------------------------------------------

def _opener(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    local = host == "localhost"
    try:
        local = local or ipaddress.ip_address(host).is_private
    except ValueError:
        pass
    return urllib.request.build_opener(urllib.request.ProxyHandler({})) if local else urllib.request.build_opener()


def post(url, key, body, timeout=120):
    """POST Anthropic-совместимого запроса: -> (код, JSON). Сеть и разбор — отдельной функцией, чтобы тест подменял адрес."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "anthropic-version": "2023-06-01"})
    try:
        with _opener(url).open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"null")
        except ValueError:
            detail = None
        return e.code, detail
    except (OSError, ValueError) as e:
        return None, type(e).__name__


def ask(args, cfg=None, key=None, budget=None, now=None):
    """Одна работа инструмента: аргументы -> {text, model, tokens_in, tokens_out, est_cost_usd}; Refused — отказ с причиной."""
    cfg = cfg or store.settings()["deepseek"]
    prompt, system, short, max_tokens = check_args(args, cfg)
    est = estimate(prompt, system, short, max_tokens)
    key = key or load_key()
    money_gate((budget or hub_budget)(), est, float(cfg.get("day_usd") or 0.5), store.deepseek_spent_today(now))
    body = {"model": model_id(short), "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    status, data = post(endpoint(), key, body)
    if status != 200 or not isinstance(data, dict):
        why = f"HTTP {status}" if status else str(data)
        raise Refused(mask(f"запрос не прошёл ({why})", key))
    text = "".join(b.get("text", "") for b in data.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
    u = data.get("usage") or {}
    tin, tout = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    p = PRICES[short]
    spent = round((tin * p["in"] + tout * p["out"]) / 1e6, 6)
    store.log_deepseek(short, tin, tout, spent, now)
    return {"text": mask(text, key), "model": short, "tokens_in": tin, "tokens_out": tout, "est_cost_usd": spent}


# --- MCP stdio -----------------------------------------------------------------------------------------------------------

def call_tool(name, args):
    if name != "deepseek_ask":
        return {"jsonrpc_error": (-32602, f"нет инструмента {name}")}
    try:
        got = ask(args)
    except Refused as e:
        return {"content": [{"type": "text", "text": f"DeepSeek недоступен: {e}"}], "isError": False}
    except Exception as e:   # noqa: BLE001 — любой сбой — текст для модели, не падение хода
        return {"content": [{"type": "text", "text": f"DeepSeek недоступен: {type(e).__name__}"}], "isError": True}
    return {"content": [{"type": "text", "text": got["text"]}], "structuredContent": got, "isError": False}


def handle(msg):
    """Одно сообщение JSON-RPC -> ответ или None (уведомление)."""
    if not isinstance(msg, dict) or "method" not in msg:
        return None
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:
        return None   # уведомление (notifications/initialized и др.): ответа нет

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                   "serverInfo": SERVER})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [TOOL]})
    if method == "tools/call":
        got = call_tool(params.get("name"), params.get("arguments"))
        if "jsonrpc_error" in got:
            return err(*got["jsonrpc_error"])
        return ok(got)
    return err(-32601, f"метод {method} не поддерживается")


def serve(stdin=None, stdout=None):
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        out = handle(msg)
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()


def cli_ask(argv, out=None):
    """`deepseek-ask [--model flash|pro] [--max-tokens N] [--system ТЕКСТ] [-|ВОПРОС…]` — запасной путь без MCP.
    Код выхода: 0 ответ, 3 отказ (причина в stdout), 2 неверные аргументы."""
    out = out or sys.stdout
    args, rest, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--model", "--system", "--max-tokens") and i + 1 < len(argv):
            key = {"--model": "model", "--system": "system", "--max-tokens": "max_tokens"}[a]
            val = argv[i + 1]
            if key == "max_tokens":
                try:
                    val = int(val)
                except ValueError:
                    out.write("--max-tokens: целое число\n")
                    return 2
            args[key] = val
            i += 2
        else:
            rest.append(a)
            i += 1
    prompt = sys.stdin.read() if rest == ["-"] else " ".join(rest)
    args["prompt"] = prompt
    got = call_tool("deepseek_ask", args)
    text = got["content"][0]["text"] if "content" in got else got["jsonrpc_error"][1]
    out.write(text + "\n")
    return 0 if "structuredContent" in got else 3


if __name__ == "__main__":
    if sys.argv[1:2] == ["--ask"]:
        sys.exit(cli_ask(sys.argv[2:]))
    serve()
