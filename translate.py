"""Перевод событий agy (stream-json) в события Claude Code для ядра (§1.1, §1.4). Чистый класс без ввода-вывода.

Формат событий подтверждён живыми пробами agy 1.5.18:
- `init` один раз; `step_update` на каждый переход шага или кусок текста: step_index, state ACTIVE|DONE|ERROR, step_type
  user_input|agent_response|tool|checkpoint|unknown, tool_name, text_delta (куски текста), usage, tool_info{name, parameters, output, error};
- `result` на каждый ход: status, response (за ход), error, duration_seconds / num_turns / usage — накопительные.
"""
import json


def _int(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def usage_numbers(u):
    """Поля usage agy -> {in, out, think, cache}. Живая проба agy 1.5.18: input_tokens не включает cache_read_tokens (total = in + out)."""
    u = u if isinstance(u, dict) else {}
    return {"in": _int(u.get("input_tokens")), "out": _int(u.get("output_tokens")),
            "think": _int(u.get("thinking_tokens")), "cache": _int(u.get("cache_read_tokens"))}


def minus(cur, prev):
    return {k: max(0, cur.get(k, 0) - prev.get(k, 0)) for k in ("in", "out", "think", "cache")}


def result_text(v):
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


class Turn:
    """Один ход: события agy -> строки для ядра. feed() отдаёт список готовых сообщений Claude Code."""

    def __init__(self, uuid, model, prev_cum=None):
        self.uuid, self.model = uuid, model
        self.prev_cum = dict(prev_cum or {})
        self.texts, self.flushed, self.tools, self.tool_done = {}, set(), {}, set()
        self.said, self.tool_names = False, []
        self.last_usage = {}
        self.conv = None
        self.mcp = []
        self.cum = None
        self.denied_actions = []
        self.error, self.status, self.response, self.duration, self.turns = None, "", "", None, None

    # -- служебное
    def _msg(self, kind, message, **extra):
        return {"type": kind, "message": message, "session_id": self.uuid, **extra}

    def _assistant(self, block, usage=None):
        msg = {"role": "assistant", "model": self.model, "content": [block]}
        if usage:
            msg["usage"] = {"input_tokens": usage["in"], "output_tokens": usage["out"],
                            "cache_read_input_tokens": usage["cache"], "cache_creation_input_tokens": 0}
        return self._msg("assistant", msg)

    @staticmethod
    def _ready_cut(buf):
        """Граница готового текста для потоковой отдачи при ACTIVE: завершённый абзац (\\n\\n) или строка >= 100 символов без разрыва ```-блока."""
        if not buf:
            return 0
        pos = len(buf)
        while True:
            para = buf.rfind("\n\n", 0, pos)
            if para == -1:
                break
            if buf[:para].strip() and buf[:para].count("```") % 2 == 0:
                return para + 2
            pos = para
        pos = len(buf)
        while True:
            nl = buf.rfind("\n", 0, pos)
            if nl == -1:
                return 0
            if len(buf[:nl].strip()) >= 100 and buf[:nl].count("```") % 2 == 0:
                return nl + 1
            pos = nl

    def _drain_text(self, idx):
        chunks = self.texts.get(idx)
        if not chunks or idx in self.flushed:
            return []
        buf = "".join(chunks)
        cut = self._ready_cut(buf)
        if cut <= 0:
            return []
        head, tail = buf[:cut].strip(), buf[cut:]
        self.texts[idx] = [tail] if tail else []
        if not head:
            return []
        self.said = True
        return [self._assistant({"type": "text", "text": head})]

    def _flush_text(self, idx):
        text = "".join(self.texts.pop(idx, [])).strip()
        if idx in self.flushed or not text:
            return []
        self.flushed.add(idx)
        self.said = True
        return [self._assistant({"type": "text", "text": text}, self.last_usage or None)]

    def _tool_use(self, idx, ev, info):
        name = str(info.get("name") or ev.get("tool_name") or "tool")
        tid = f"agy-{self.conv or 'c'}-{idx}"
        self.tools[idx] = tid
        self.tool_names.append(name)
        params = info.get("parameters")
        return [self._assistant({"type": "tool_use", "id": tid, "name": name,
                                 "input": params if isinstance(params, dict) else {"value": params}})]

    # -- события
    def feed(self, ev):
        """Одно событие agy -> список сообщений для ядра. Неизвестные события пропускаются (agy выходит почти каждый день)."""
        if not isinstance(ev, dict):
            return []
        kind = ev.get("event")
        if ev.get("conversation_id"):
            self.conv = ev["conversation_id"]
        # живая проба 27.11 (agy 1.5.15): поля события лежат внутри него самого — {"event":"step_update","step_update":{…}}
        payload = ev.get(kind) if isinstance(kind, str) and isinstance(ev.get(kind), dict) else ev
        if payload.get("conversation_id"):
            self.conv = payload["conversation_id"]
        if kind == "init":
            tools = (ev.get("init") or {}).get("tools") if isinstance(ev.get("init"), dict) else None
            tools = tools or []
            # живая проба agy 1.5.18: в init.tools приходит общий call_mcp_tool (не mcp_<сервер>_<имя>); префикс mcp_ оставляем на случай eager-серверов
            servers = sorted({t.split("_")[1] for t in tools if isinstance(t, str) and t.startswith("mcp_") and t.count("_") >= 2})
            self.mcp = servers
            return []
        if kind == "step_update":
            return self._step(payload)
        if kind == "result":
            return self._result(payload)
        return []

    def _step(self, ev):
        idx, state, stype = ev.get("step_index", 0), ev.get("state"), ev.get("step_type")
        if ev.get("usage"):
            self.last_usage = usage_numbers(ev["usage"])
        out = []
        if stype == "agent_response":
            # живая проба agy 1.5.18: text_delta — куски текста на ACTIVE и завершающий фрагмент на DONE;
            # готовые абзацы (и длинные завершённые строки) отдаём по мере генерации, остаток — при DONE без дублирования
            if ev.get("text_delta"):
                self.texts.setdefault(idx, []).append(str(ev["text_delta"]))
            if state == "DONE":
                out += self._flush_text(idx)
            else:
                out += self._drain_text(idx)
        elif stype == "tool":
            info = ev.get("tool_info") if isinstance(ev.get("tool_info"), dict) else {}
            if idx not in self.tools:
                out += self._tool_use(idx, ev, info)
            # живая проба 27.11 (agy 1.5.18): ошибка инструмента и отказ pre-tool хука приходят со state="ERROR", не "DONE"
            if state in ("DONE", "ERROR") and idx not in self.tool_done:
                self.tool_done.add(idx)
                err = info.get("error")
                text = (err.get("message") if isinstance(err, dict) else result_text(err)) if err else result_text(info.get("output"))
                out.append(self._msg("user", {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": self.tools[idx], "content": text, "is_error": bool(err) or state == "ERROR"}]}))
        return out

    def _result(self, ev):
        out = []
        for idx in sorted(self.texts):   # шаг текста, который так и не закрылся DONE
            out += self._flush_text(idx)
        status = str(ev.get("status") or "").upper()
        response = ev.get("response") if isinstance(ev.get("response"), str) else result_text(ev.get("response"))
        self.cum = usage_numbers(ev.get("usage"))
        self.error = ev.get("error")
        self.status = status
        self.response = response
        self.denied_actions = ev.get("denied_actions") if isinstance(ev.get("denied_actions"), list) else []
        self.duration = ev.get("duration_seconds")
        self.turns = ev.get("num_turns")
        if status in ("SUCCESS", "") and response.strip() and not self.said:
            self.said = True
            out.append(self._assistant({"type": "text", "text": response}, self.last_usage or None))
        return out

    def turn_usage(self):
        """Токены хода: накопительное из result минус прошлое накопительное (§1.4)."""
        return minus(self.cum or {}, self.prev_cum)
