// Предварительный текст только открытой сессии; законченное сообщение пишет само ядро.
export default function registerStream(hub, isAgyModel = () => true) {
  let rootNode = null, session = '', generation = 0, timer = null, busy = false, inFlight = false;
  async function refresh() {
    if (!rootNode || !session || !busy || inFlight) return;
    inFlight = true;
    const name = session, current = generation;
    try {
      const r = await fetch('/api/mod/antigravity/stream?session=' + encodeURIComponent(name), {
        headers: { 'X-Hub': '1' }, cache: 'no-store',
      });
      const body = r.ok ? await r.json() : {};
      if (current !== generation || name !== session || !rootNode) return;
      const text = typeof body.text === 'string' ? body.text : '';
      rootNode.textContent = text;
      rootNode.hidden = !text;
    } catch (_) {
      if (current === generation && rootNode) { rootNode.textContent = ''; rootNode.hidden = true; }
    } finally { inFlight = false; }
  }
  hub.addSlot('session.composer', {
    id: 'antigravity-stream',
    render(root, _snap, ctx) {
      if (rootNode !== root) { root.hidden = true; rootNode = root; }
      root.classList.add('agy-stream');
      const next = ctx?.session?.name || '';
      const active = ctx?.session?.state === 'busy' && isAgyModel(ctx?.session?.model || ctx?.session?.info?.model);
      if (session !== next || busy !== active) {
        session = next; busy = active; generation++;
        root.textContent = ''; root.hidden = true;
        if (active) refresh();
      }
      if (!timer) timer = setInterval(refresh, 500);
    },
  });
}
