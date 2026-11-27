const AGY_MODEL = /(^|[-_/])agy($|[-_])|claude-(?:opus|sonnet)-5-5/i;
const EFFORT_ORDER = ["Low", "Medium", "High", "XHigh"];

function currentModel(session) {
  return String(session?.model || session?.info?.model || "").replace(/^codex\//, "");
}

function catalog(snap) {
  const value = snap?.mods?.antigravity?.antigravity_models?.value;
  return {
    models: Array.isArray(value?.models) ? value.models : [],
    aliases: value?.aliases && typeof value.aliases === "object" ? value.aliases : {},
  };
}

function visible(session) {
  return AGY_MODEL.test(currentModel(session));
}

export function splitModelTitle(title, fallbackId = "") {
  const raw = String(title || fallbackId || "").trim();
  const match = raw.match(/^(.*?)\s*\(([^()]+)\)\s*$/);
  if (!match) return {base: raw, effort: ""};
  return {base: match[1].trim(), effort: match[2].trim()};
}

function effortSortKey(effort) {
  const idx = EFFORT_ORDER.indexOf(effort);
  return idx >= 0 ? idx : EFFORT_ORDER.length;
}

export function modelGroups(models) {
  const entries = new Map();
  for (const item of Array.isArray(models) ? models : []) {
    if (!item || !item.id) continue;
    const {base, effort} = splitModelTitle(item.title || item.displayName, item.agy_id || item.id);
    const category = item.category || "Другие";
    const key = `${category}\0${base}`;
    if (!entries.has(key)) {
      entries.set(key, {key, base, category, variants: []});
    }
    const group = entries.get(key);
    if (!group.variants.some((v) => v.effort === effort && v.id === item.id)) {
      group.variants.push({...item, base, effort});
    }
  }
  const byCategory = new Map();
  for (const entry of entries.values()) {
    entry.variants.sort((a, b) => effortSortKey(a.effort) - effortSortKey(b.effort));
    if (!byCategory.has(entry.category)) byCategory.set(entry.category, []);
    byCategory.get(entry.category).push(entry);
  }
  return byCategory;
}

export function pickVariant(entry, preferredEffort) {
  const variants = entry?.variants || [];
  if (!variants.length) return null;
  if (preferredEffort) {
    const exact = variants.find((v) => v.effort === preferredEffort);
    if (exact) return exact;
  }
  return variants.find((v) => v.effort === "High")
    || variants.find((v) => v.effort === "Medium")
    || variants[0];
}

export default function register(hub) {
  hub.addStyle("models.css");

  function make(root) {
    const picker = hub.el("span", "agy-model-picker");
    const modelSelect = hub.el("select", "agy-model-name");
    modelSelect.setAttribute("aria-label", "Модель Antigravity для этой сессии");
    const effortSelect = hub.el("select", "agy-model-effort");
    effortSelect.setAttribute("aria-label", "Уровень рассуждений Antigravity для этой сессии");
    picker.append(modelSelect, effortSelect);
    root.append(picker);
    return {
      picker,
      modelSelect,
      effortSelect,
      sessionName: null,
      entries: new Map(),
      busy: false,
      modelSig: null,
      effortSig: null,
      lastRequested: null,
      activeEffort: "",
    };
  }

  async function applyModel(controls, sessionName, chosenId) {
    if (!chosenId || !sessionName) return;
    controls.busy = true;
    controls.modelSelect.disabled = true;
    controls.effortSelect.disabled = true;
    try {
      const result = await hub.post("/api/session/model", {session: sessionName, model: chosenId});
      if (result.error) hub.banner(result.error);
      else if (result.data?.queued) hub.banner(result.data.text);
    } finally {
      controls.busy = false;
    }
  }

  hub.addSlot("session.header", {
    id: "antigravity-model",
    order: -19,
    render(root, snap, ctx) {
      const session = ctx?.session;
      const active = visible(session);
      document.documentElement.classList.toggle("hub-agy-session", active);
      let node = root.querySelector(":scope > .agy-model-picker");
      if (!node) {
        const created = make(root);
        node = created.picker;
        node._agyControls = created;
      }
      const controls = node._agyControls;
      const {picker, modelSelect, effortSelect} = controls;
      picker.hidden = !active;
      if (!active || !session?.name) return;

      const {models, aliases} = catalog(snap);
      const requested = currentModel(session);
      const actual = aliases[requested] || requested.replace(/^claude-agy-/, "");
      const byCategory = modelGroups(models);

      const entries = new Map();
      const modelSig = models.length
        ? models.map((item) => `${item?.id || ""}\0${item?.title || item?.displayName || ""}\0${item?.category || ""}`).join("\n")
        : "__empty__";
      for (const items of byCategory.values()) {
        for (const item of items) entries.set(item.key, item);
      }
      controls.entries = entries;

      const modelListChanged = controls.modelSig !== modelSig;
      if (modelListChanged) {
        controls.modelSig = modelSig;
        if (models.length) {
          modelSelect.replaceChildren(...[...byCategory].map(([category, items]) => {
            const group = hub.el("optgroup");
            group.label = category;
            for (const item of items) {
              const option = hub.el("option", null, item.base);
              option.value = item.key;
              group.append(option);
            }
            return group;
          }));
        } else {
          const option = hub.el("option", null, "Каталог моделей ещё загружается");
          option.value = "";
          modelSelect.replaceChildren(option);
        }
      }

      const selectedItem = models.find((item) => item.agy_id === actual || item.id === requested);
      let activeEntry = null;
      let activeEffort = "";
      if (selectedItem) {
        const {base, effort} = splitModelTitle(
          selectedItem.title || selectedItem.displayName,
          selectedItem.agy_id || selectedItem.id,
        );
        const key = `${selectedItem.category || "Другие"}\0${base}`;
        activeEntry = entries.get(key) || null;
        activeEffort = effort;
      }

      const modelChanged = controls.lastRequested !== requested || controls.sessionName !== session.name;
      controls.lastRequested = requested;

      if (activeEntry) {
        if (modelListChanged || modelChanged) modelSelect.value = activeEntry.key;
        const variants = activeEntry.variants;
        const showEffort = variants.some((v) => v.effort);
        effortSelect.hidden = !showEffort;
        if (showEffort) {
          const effortSig = variants.map((v) => `${v.id}\0${v.effort || ""}`).join("\n");
          const effortListChanged = controls.effortSig !== effortSig;
          if (effortListChanged) {
            controls.effortSig = effortSig;
            effortSelect.replaceChildren(...variants.map((v) => {
              const option = hub.el("option", null, v.effort || "Стандарт");
              option.value = v.id;
              option.title = v.agy_id || v.id;
              return option;
            }));
          }
          const chosenVariant = variants.find((v) => v.id === selectedItem.id) || variants[0];
          if (chosenVariant && (effortListChanged || modelChanged)) {
            effortSelect.value = chosenVariant.id;
          }
        } else {
          controls.effortSig = "";
        }
      } else {
        controls.effortSig = "";
        effortSelect.hidden = true;
      }

      const busyTurn = session.state === "busy";
      const onlyOneEffort = !activeEntry || activeEntry.variants.length <= 1;
      controls.activeEffort = activeEffort;
      modelSelect.disabled = controls.busy || !models.length;
      effortSelect.disabled = controls.busy || onlyOneEffort;
      modelSelect.title = busyTurn ? "Сменится после текущего хода" : "Выбрать модель Antigravity";
      effortSelect.title = busyTurn
        ? "Сменится после текущего хода"
        : onlyOneEffort ? "У этой модели один уровень" : "Выбрать усилие Antigravity";

      if (controls.sessionName !== session.name) {
        controls.sessionName = session.name;
        modelSelect.onchange = async () => {
          const entry = controls.entries.get(modelSelect.value);
          const target = pickVariant(entry, controls.activeEffort);
          if (!target) return;
          controls.activeEffort = target.effort || controls.activeEffort;
          await applyModel(controls, session.name, target.id);
          modelSelect.disabled = !controls.entries.size;
          effortSelect.disabled = !entry || entry.variants.length <= 1;
        };
        effortSelect.onchange = async () => {
          await applyModel(controls, session.name, effortSelect.value);
          modelSelect.disabled = !controls.entries.size;
          effortSelect.disabled = false;
        };
      }
    },
  });
}
