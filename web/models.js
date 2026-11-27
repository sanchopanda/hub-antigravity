const AGY_MODEL = /(^|[-_/])agy($|[-_])|claude-(?:opus|sonnet)-5-5/i;
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

export default function register(hub) {
  let picker, select, sessionName;
  hub.addStyle("models.css");

  function make(root) {
    picker = hub.el("span", "agy-model-picker");
    picker.append(hub.el("label", null, "Модель Antigravity"));
    select = hub.el("select");
    select.setAttribute("aria-label", "Модель Antigravity для этой сессии");
    picker.append(select);
    root.append(picker);
  }

  hub.addSlot("session.header", {
    id: "antigravity-model",
    order: -5,
    render(root, snap, ctx) {
      const session = ctx?.session;
      const active = visible(session);
      document.documentElement.classList.toggle("hub-agy-session", active);
      if (!picker || !picker.isConnected) make(root);
      picker.hidden = !active;
      if (!active || !session?.name) return;

      const {models, aliases} = catalog(snap);
      const requested = currentModel(session);
      const actual = aliases[requested] || requested.replace(/^claude-agy-/, "");
      const groups = new Map();
      for (const item of models) {
        const category = item.category || "Другие";
        if (!groups.has(category)) groups.set(category, []);
        const option = hub.el("option", null, item.title || item.id);
        option.value = item.id;
        option.title = item.agy_id || item.id;
        groups.get(category).push(option);
      }
      select.replaceChildren(...[...groups].map(([category, options]) => {
        const group = hub.el("optgroup");
        group.label = category;
        group.append(...options);
        return group;
      }));
      const selected = models.find((item) => item.agy_id === actual || item.id === requested);
      if (selected) select.value = selected.id;
      else if (!models.length) {
        const option = hub.el("option", null, "Каталог моделей ещё загружается");
        option.value = "";
        select.replaceChildren(option);
      }
      select.disabled = session.state === "busy";
      select.title = select.disabled ? "Сменится после текущего хода" : "Выбрать модель Antigravity";
      if (sessionName !== session.name) {
        sessionName = session.name;
        select.onchange = async () => {
          const chosen = select.value;
          select.disabled = true;
          const result = await hub.post("/api/session/model", {session: session.name, model: chosen});
          if (result.error) hub.banner(result.error);
          else if (result.data?.queued) hub.banner(result.data.text);
          select.disabled = false;
        };
      }
    },
  });
}
