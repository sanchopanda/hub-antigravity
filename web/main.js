import registerModels from "./models.js";

// Antigravity quota widget in the hub topbar: Gemini and third-party 5h/weekly remaining %,
// Moscow reset times. Falls back to "—" when agy did not respond or cached data is older than 5 minutes.
const MOSCOW_TZ = "Europe/Moscow";
const MAX_CACHE_S = 300;
const AGY_MODEL = /(^|[-_/])agy($|[-_])|^codex\/claude-(?:opus|sonnet)-5-5$/i;

const STR = {
  ru: {
    gemini5h: "Gemini 5ч",
    geminiWeek: "Gemini неделя",
    cg5h: "Claude/GPT 5ч",
    cgWeek: "Claude/GPT неделя",
    noData: "Antigravity: нет свежих данных квоты",
    updated: "обновлено",
    reset: "сброс",
  },
  en: {
    gemini5h: "Gemini 5h",
    geminiWeek: "Gemini week",
    cg5h: "Claude/GPT 5h",
    cgWeek: "Claude/GPT week",
    noData: "Antigravity: no fresh quota data",
    updated: "updated",
    reset: "reset",
  },
};

export function isAgyModel(model) {
  return AGY_MODEL.test(String(model || ""));
}

export function selectedModel(snap, hash) {
  const m = String(hash || "").match(/^#\/s\/(.+)$/);
  if (!m) {
    if (hash && hash !== "#") return "";
    const h = (snap?.sessions || []).find((x) => x.kind === "hub");
    return h?.model || h?.info?.model || "";
  }
  let name;
  try { name = decodeURIComponent(m[1]); } catch { return ""; }
  const s = (snap?.sessions || []).find((x) => x.name === name);
  return s?.model || s?.info?.model || "";
}

function moscowParts(sec) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: MOSCOW_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(sec * 1000));
  const map = {};
  for (const p of parts) map[p.type] = p.value;
  return map;
}

export function formatMoscowReset(resetAt, nowSec, _lang = "ru") {
  if (!Number.isFinite(resetAt) || resetAt <= 0) return "";
  const r = moscowParts(resetAt);
  const n = moscowParts(nowSec);
  const hhmm = `${r.hour}:${r.minute}`;
  const sameDay = r.year === n.year && r.month === n.month && r.day === n.day;
  return sameDay ? hhmm : `${r.day}.${r.month} ${hhmm}`;
}

export function formatMoscowClock(sec, _lang = "ru") {
  if (!Number.isFinite(sec) || sec <= 0) return "";
  const p = moscowParts(sec);
  return `${p.hour}:${p.minute}`;
}

export function levelOf(pct) {
  if (!Number.isFinite(pct)) return "muted";
  if (pct <= 10) return "high";
  if (pct <= 25) return "warn";
  return "ok";
}

function worstLevel(levels) {
  if (levels.includes("high")) return "high";
  if (levels.includes("warn")) return "warn";
  if (levels.includes("ok")) return "ok";
  return "muted";
}

export function bucketView(bucket, nowSec, lang = "ru") {
  if (!bucket || typeof bucket !== "object" || !Number.isFinite(bucket.pct)) {
    return { ok: false, pct: null, pctText: "—", resetText: "", level: "muted" };
  }
  const pct = Math.max(0, Math.min(100, Math.round(bucket.pct)));
  const resetAt = Number.isFinite(bucket.reset_at) && bucket.reset_at > 0 ? bucket.reset_at : null;
  const resetText = resetAt ? formatMoscowReset(resetAt, nowSec, lang) : "";
  return { ok: true, pct, pctText: `${pct}%`, resetText, level: levelOf(pct) };
}

export function quotaView(seg, nowSec, lang = "ru") {
  const dash = { ok: false, pct: null, pctText: "—", resetText: "", level: "muted" };
  const ttl = Number.isFinite(seg?.ttl_s) && seg.ttl_s > 0 ? Math.min(seg.ttl_s, MAX_CACHE_S) : MAX_CACHE_S;
  const fetchedAt = Number.isFinite(seg?.fetched_at) ? seg.fetched_at : null;
  const fresh = !!(seg && seg.ok === true && fetchedAt !== null && (nowSec - fetchedAt) <= ttl && (nowSec - fetchedAt) >= -60);
  if (!fresh) {
    return {
      ok: false,
      fetchedAt: null,
      gemini: { h5: dash, weekly: dash, level: "muted" },
      claudeGpt: null,
    };
  }
  const g5 = bucketView(seg.gemini?.h5, nowSec, lang);
  const gw = bucketView(seg.gemini?.weekly, nowSec, lang);
  const gemini = { h5: g5, weekly: gw, level: worstLevel([g5.level, gw.level]) };

  let claudeGpt = null;
  if (seg.claude_gpt && typeof seg.claude_gpt === "object" && (seg.claude_gpt.h5 || seg.claude_gpt.weekly)) {
    const c5 = bucketView(seg.claude_gpt.h5, nowSec, lang);
    const cw = bucketView(seg.claude_gpt.weekly, nowSec, lang);
    claudeGpt = { h5: c5, weekly: cw, level: worstLevel([c5.level, cw.level]) };
  }
  return { ok: true, fetchedAt, gemini, claudeGpt };
}

function makeRow(hub, label, item) {
  const row = hub.el("span", "agy-quota-row");
  row.append(hub.el("span", "agy-quota-label", label));
  const pct = hub.el("b", "agy-quota-pct", item.pctText);
  pct.dataset.level = item.level;
  row.append(pct);
  if (item.resetText) {
    row.append(hub.el("span", "agy-quota-reset", `· ${item.resetText}`));
  }
  return row;
}

function titleLine(label, item, s) {
  if (!item.ok) return `${label}: —`;
  return item.resetText ? `${label}: ${item.pctText} (${s.reset} ${item.resetText})` : `${label}: ${item.pctText}`;
}

export default function register(hub) {
  registerModels(hub);
  const s = STR[hub.lang] || STR.en;
  hub.addStyle("quota.css");
  hub.addSlot("topbar", {
    id: "antigravity-quota",
    order: 92,
    render(root, snap) {
      root.classList.add("agy-quota-slot");
      const nowSec = Date.now() / 1000;
      const v = quotaView(snap?.antigravity_quota, nowSec, hub.lang);
      const active = isAgyModel(selectedModel(snap, location.hash));

      const wrap = hub.el("span", "agy-quota-wrap");

      const gPill = hub.el("span", "agy-quota");
      gPill.dataset.group = "gemini";
      gPill.dataset.level = v.gemini.level;
      if (active) gPill.dataset.active = "true";
      gPill.append(
        makeRow(hub, s.gemini5h, v.gemini.h5),
        makeRow(hub, s.geminiWeek, v.gemini.weekly),
      );
      if (v.ok) {
        const lines = [
          titleLine(s.gemini5h, v.gemini.h5, s),
          titleLine(s.geminiWeek, v.gemini.weekly, s),
        ];
        if (v.claudeGpt) {
          lines.push(titleLine(s.cg5h, v.claudeGpt.h5, s), titleLine(s.cgWeek, v.claudeGpt.weekly, s));
        }
        if (v.fetchedAt) lines.push(`${s.updated}: ${formatMoscowClock(v.fetchedAt, hub.lang)}`);
        gPill.title = lines.join("\n");
      } else {
        gPill.title = s.noData;
      }
      wrap.append(gPill);

      if (v.ok && v.claudeGpt) {
        const cPill = hub.el("span", "agy-quota agy-quota-secondary");
        cPill.dataset.group = "claude-gpt";
        cPill.dataset.level = v.claudeGpt.level;
        if (active) cPill.dataset.active = "true";
        cPill.append(
          makeRow(hub, s.cg5h, v.claudeGpt.h5),
          makeRow(hub, s.cgWeek, v.claudeGpt.weekly),
        );
        const cLines = [
          titleLine(s.cg5h, v.claudeGpt.h5, s),
          titleLine(s.cgWeek, v.claudeGpt.weekly, s),
        ];
        if (v.fetchedAt) cLines.push(`${s.updated}: ${formatMoscowClock(v.fetchedAt, hub.lang)}`);
        cPill.title = cLines.join("\n");
        wrap.append(cPill);
      }

      root.replaceChildren(wrap);
    },
  });
}
