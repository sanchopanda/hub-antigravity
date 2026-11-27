"""`hub antigravity setup`: записи в настройки agy (§2.3 п. 4) с копией прежних файлов; `--undo` возвращает копии.

Пишет в папку agy (по умолчанию ~/.gemini; `--dir` или HUB_MOD_ANTIGRAVITY_GEMINI — другая, так тесты ходят во временную):
- config/mcp_config.json — сервер `hub-deepseek` (python и deepseek_mcp.py модуля, ключа в записи нет);
- config/hooks.json — PreToolUse → agy_guard.py;
- antigravity-cli/settings.json — permissions.allow: mcp(hub-deepseek/*);
- config/skills.json — каталоги скиллов хаба и проектов (решение владельца 27.11: глобально, один раз).
Рядом с модулем: bin/python (путь python для запасного скрипта deepseek-ask) и bin/real-claude (настоящий claude для оболочки).
Эти файлы общие с ручными запусками agy владельца. Данные входа (связка, токены) не читаются: трогаем только эти три файла.
Форматы hooks.json, mcp_config.json и skills.json подтверждены живыми пробами agy 1.5.18.
"""
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = "hub-deepseek"
ALLOW = f"mcp({SERVER}/*)"
FILES = {"mcp": ("config", "mcp_config.json"), "hooks": ("config", "hooks.json"),
         "settings": ("antigravity-cli", "settings.json"), "skills": ("config", "skills.json")}


def gemini_dir(explicit=None):
    return explicit or os.environ.get("HUB_MOD_ANTIGRAVITY_GEMINI") or os.path.expanduser("~/.gemini")


def manifest_path():
    return os.path.join(store.data_dir(), "setup.json")


def paths(base):
    return {k: os.path.join(base, *v) for k, v in FILES.items()}


def read(path):
    """Файл настроек как словарь; нет файла или файл пустой (agy создаёт заготовку 0 байт) — {}; битый JSON — ошибка
    (не затираем чужое)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: не словарь JSON")
    return data


def uv_python(home=None):
    """Стабильный python для записей agy: HUB_AGY_PYTHON, иначе самый новый uv cpython-3.12-* (python рантайма хаба
    меняется с каждым обновлением — deepseek_ask тогда перестал бы запускаться), иначе sys.executable."""
    given = os.environ.get("HUB_AGY_PYTHON")
    if given:
        return given

    def ver(name):
        m = re.match(r"cpython-(\d+(?:\.\d+)*)-", name)
        return tuple(int(x) for x in m.group(1).split(".")) if m else ()

    base = os.path.join(home or os.path.expanduser("~"), ".local", "share", "uv", "python")
    try:
        # имена бывают и «cpython-3.12-macos-…», и «cpython-3.12.15-macos-…»: берём оба, новый по версии
        names = sorted((n for n in os.listdir(base) if re.match(r"cpython-3\.12(?:\.\d+)?-", n)), key=ver)
    except OSError:
        names = []
    for name in reversed(names):
        path = os.path.join(base, name, "bin", "python3.12")
        if os.access(path, os.X_OK):
            return path
    return sys.executable


def layers_dir(home=None):
    """Папка слоёв хаба: HUB_MOD_ANTIGRAVITY_LAYERS — другая (так тесты ходят во временную)."""
    return os.environ.get("HUB_MOD_ANTIGRAVITY_LAYERS") or os.path.join(
        home or os.path.expanduser("~"), ".hub-personal", "layers")


def _version(name):
    m = re.match(r"(\d+(?:\.\d+)*)", name)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def active_layer(layers=None):
    """Каталог действующего слоя хаба: имя из check.json (его пишет ядро при обновлении), иначе самый новый по версии."""
    layers = layers or layers_dir()
    try:
        name = store.read_json(os.path.join(layers, "check.json")).get("name")
    except Exception:  # noqa: BLE001 — битый check.json не должен мешать настройке
        name = None
    if name and os.path.isdir(os.path.join(layers, name, "plugins")):
        return os.path.join(layers, name)
    try:
        names = [n for n in os.listdir(layers) if os.path.isdir(os.path.join(layers, n, "plugins"))]
    except OSError:
        return None
    return os.path.join(layers, max(names, key=_version)) if names else None


def plugin_skills_root():
    """Куда модуль кладёт копии скиллов плагинов слоя: <данные модуля>/skills."""
    return os.path.join(store.data_dir(), "skills")


def skills_off_path():
    """Метка «скиллы в конфиг agy не ставить»: её кладёт --undo, чтобы фоновый цикл модуля не вернул записи через 15 с.
    Снимает метку setup."""
    return os.path.join(store.data_dir(), "skills-off")


def _plugin_sources():
    """{имя скилла: каталог} из plugins/*/skills слоя (только каталоги с SKILL.md)."""
    layer = active_layer()
    layer = os.path.join(layer, "plugins") if layer else None
    try:
        plugins = sorted(os.listdir(layer)) if layer else []
    except OSError:
        plugins = []
    out = {}
    for plugin in plugins:
        skills = os.path.join(layer, plugin, "skills")
        try:
            names = sorted(os.listdir(skills))
        except OSError:
            continue
        for name in names:
            src = os.path.join(skills, name)
            if os.path.isfile(os.path.join(src, "SKILL.md")):
                out[name] = src
    return out


def copy_plugin_skills(dry=False):
    """agy даёт скиллам, лежащим в каталоге с «plugins» в пути, имя из пути вместо имени из SKILL.md. Копируем скиллы
    плагинов слоя в нейтральную папку модуля (имя каталога = имя скилла) и обновляем, когда меняется SKILL.md.
    -> каталог записей skills.json или None, если скиллов плагинов нет."""
    src = _plugin_sources()
    if not src:
        return None
    root = plugin_skills_root()
    if not dry:
        try:
            have = os.listdir(root)
        except OSError:
            have = []
        for name in have:                       # скилл убрали из слоя — убираем копию
            if name not in src:
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        for name, path in src.items():
            dst = os.path.join(root, name)
            marker = os.path.join(dst, "SKILL.md")
            if not os.path.exists(marker) or os.path.getmtime(os.path.join(path, "SKILL.md")) > os.path.getmtime(marker):
                shutil.rmtree(dst, ignore_errors=True)
                os.makedirs(root, exist_ok=True)
                shutil.copytree(path, dst)
    return root


def skill_entries(home=None, dry=False):
    """Каталоги скиллов, которые видят сессии Claude: скиллы плагинов действующего слоя хаба (их копии для agy),
    личные (~/.claude/skills) и проектов (~/code/<проект>/.claude/skills). Только существующие: путь в конфиге agy не
    должен вести в никуда. Рабочие копии (worktrees) не берём — это те же скиллы проекта."""
    home = home or os.path.expanduser("~")
    out = []
    root = copy_plugin_skills(dry)
    if root:
        out.append(root)
    personal = os.path.join(home, ".claude", "skills")
    if os.path.isdir(personal):
        out.append(personal)
    code = os.environ.get("HUB_MOD_ANTIGRAVITY_CODE") or os.path.join(home, "code")
    try:
        names = sorted(os.listdir(code))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(code, name, ".claude", "skills")
        if not name.startswith(".") and os.path.isdir(path):
            out.append(path)
    return list(dict.fromkeys(out))


def merge_skills(data, own_old, own_new):
    """skills.json agy: записи из own_old (наши прошлые) заменяются на own_new, чужие записи и чужие ключи остаются.
    Повторный запуск даёт тот же файл."""
    data = json.loads(json.dumps(data))
    old = set(own_old or [])
    entries = [e for e in (data.get("entries") or []) if not (isinstance(e, dict) and e.get("path") in old)]
    have = {e.get("path") for e in entries if isinstance(e, dict)}
    for path in own_new:
        if path not in have:
            entries.append({"path": path})
            have.add(path)
    if entries:
        data["entries"] = entries
    else:
        data.pop("entries", None)
    return data


def unmerge_skills(data, own):
    data = json.loads(json.dumps(data))
    old = set(own or [])
    entries = [e for e in (data.get("entries") or []) if not (isinstance(e, dict) and e.get("path") in old)]
    if entries:
        data["entries"] = entries
    else:
        data.pop("entries", None)
    return data


def merged(kind, data, python=None):
    """Тот же словарь с нашей записью; чужие поля и записи остаются."""
    python = python or uv_python()
    data = json.loads(json.dumps(data))
    if kind == "mcp":
        data.setdefault("mcpServers", {})[SERVER] = {"command": python, "args": [os.path.join(HERE, "deepseek_mcp.py")]}
    elif kind == "hooks":
        cmd = f"{python} {os.path.join(HERE, 'agy_guard.py')}"
        groups = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
        groups[:] = [g for g in groups if "agy_guard.py" not in json.dumps(g)]
        groups.append({"matcher": "*", "hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
    elif kind == "settings":
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        if ALLOW not in allow:
            allow.append(ALLOW)
    return data


def unmerged(kind, data):
    data = json.loads(json.dumps(data))
    if kind == "mcp":
        (data.get("mcpServers") or {}).pop(SERVER, None)
    elif kind == "hooks":
        groups = (data.get("hooks") or {}).get("PreToolUse")
        if isinstance(groups, list):
            groups[:] = [g for g in groups if "agy_guard.py" not in json.dumps(g)]
    elif kind == "settings":
        allow = (data.get("permissions") or {}).get("allow")
        if isinstance(allow, list) and ALLOW in allow:
            allow.remove(ALLOW)
    return data


def prune(data):
    """Словарь без пустых вложенных словарей и списков: файл, который создали мы, после снятия записи не должен остаться «пустой оболочкой»."""
    if isinstance(data, dict):
        out = {k: prune(v) for k, v in data.items()}
        return {k: v for k, v in out.items() if v not in ({}, [])}
    if isinstance(data, list):
        return [prune(v) for v in data]
    return data


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def real_claude_target():
    """Настоящий claude: ~/.local/bin/claude (ссылка на версию), не `which` — он найдёт оболочку, когда она встанет."""
    path = os.path.expanduser("~/.local/bin/claude")
    return path if os.access(path, os.X_OK) else shutil.which("claude")


def sync_skills(base=None, dry=False, man=None, save=True, force=False):
    """Пересчитать записи скиллов в глобальном конфиге agy; пишет, только если файл изменится (новый слой хаба,
    новый проект). -> (путь, изменился ли файл). Зовёт и setup (force=True — метку --undo не смотрит), и фоновый цикл
    модуля (main.loop)."""
    path = paths(gemini_dir(base))["skills"]
    data_man = store.read_json(manifest_path()) if man is None else man
    rec = data_man.get(path, {})
    if not force and os.path.exists(skills_off_path()):   # после --undo не возвращаем записи, пока не позовут setup
        return path, False
    try:
        before = read(path)
    except ValueError:      # чужой битый файл не затираем
        return path, False
    desired = skill_entries(dry=dry)
    after = merge_skills(before, rec.get("own"), desired)
    changed = after != before
    if not changed and rec.get("own") == desired:
        return path, False
    if changed:
        if not dry:
            if "backup" not in rec and os.path.exists(path):
                bak = f"{path}.bak-hub-{time.strftime('%Y%m%d-%H%M%S')}"
                shutil.copy2(path, bak)
                rec["backup"] = bak
            write(path, after)
    if not dry:
        rec["own"] = desired
        data_man[path] = rec
        if save:
            store.write_json(manifest_path(), data_man)
    return path, changed


def setup(base=None, dry=False, python=None, claude=None, here=None):
    """-> список строк отчёта. Копия каждого файла — рядом, <файл>.bak-hub-<время>; первая копия не перезаписывается."""
    base = gemini_dir(base)
    python = python or uv_python()
    if not dry and os.path.exists(skills_off_path()):
        os.remove(skills_off_path())            # setup после --undo снова ставит скиллы
    out, man = [], store.read_json(manifest_path())
    for kind, path in paths(base).items():
        if kind == "skills":
            continue
        before = read(path)
        after = merged(kind, before, python)
        if after == before:
            out.append(f"{path}: уже настроен")
            continue
        out.append(f"{path}: {'будет записан' if dry else 'записан'} ({'новый' if not os.path.exists(path) else 'с копией'})")
        if dry:
            continue
        rec = man.setdefault(path, {})
        if "backup" not in rec:
            if os.path.exists(path):
                bak = f"{path}.bak-hub-{time.strftime('%Y%m%d-%H%M%S')}"
                shutil.copy2(path, bak)
                rec["backup"] = bak
            else:
                rec["backup"] = None
        write(path, after)
    skills_path, changed = sync_skills(base, dry, man, save=False, force=True)
    out.append(f"{skills_path}: {'уже настроен' if not changed else ('будет записан' if dry else 'записан')} "
               f"({len(skill_entries(dry=dry))} каталогов скиллов)")
    if not dry:
        store.write_json(manifest_path(), man)
        here = here or HERE
        with open(os.path.join(here, "bin", "python"), "w", encoding="utf-8") as f:
            f.write(python + "\n")
        real = claude or real_claude_target()
        if real:
            with open(os.path.join(here, "bin", "real-claude"), "w", encoding="utf-8") as f:
                f.write(real + "\n")
            out.append(f"bin/real-claude: {real}")
    return out


def undo(base=None, dry=False):
    base = gemini_dir(base)
    man = store.read_json(manifest_path())
    out = []
    for kind, path in paths(base).items():
        rec = man.get(path)
        if not rec:
            out.append(f"{path}: нашей записи нет")
            continue
        if dry:
            out.append(f"{path}: будет возвращён")
            continue
        bak = rec.get("backup")
        if kind == "skills":
            after = unmerge_skills(read(path), rec.get("own"))
            shutil.rmtree(plugin_skills_root(), ignore_errors=True)   # копии скиллов плагинов тоже наши
            os.makedirs(store.data_dir(), exist_ok=True)
            with open(skills_off_path(), "w", encoding="utf-8") as f:
                f.write("undo\n")                 # фоновый цикл модуля не вернёт записи без setup
        else:
            after = unmerged(kind, read(path))
        if bak and os.path.exists(bak):
            # чужие правки после setup не затираем: снимаем только нашу запись, а копию оставляем рядом
            if prune(after):
                write(path, after)
            elif os.path.exists(path):
                os.remove(path)
            out.append(f"{path}: наша запись снята (прежняя копия: {bak})")
        else:
            data = prune(after)
            if data:
                write(path, data)
            elif os.path.exists(path):
                os.remove(path)
            out.append(f"{path}: наша запись снята")
        man.pop(path, None)
    if not dry:
        store.write_json(manifest_path(), man)
    return out


def main(argv):
    base = None
    if "--dir" in argv:
        base = argv[argv.index("--dir") + 1]
    dry = "--dry" in argv
    lines = undo(base, dry) if "--undo" in argv else setup(base, dry)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
