// Civilizer dashboard: vanilla JS, inline SVG, no dependencies. Reads the JSON API in dashboard.py and
// polls /api/version every 10 s; when the token changes (the monitor appended a turn, or the advisor wrote
// a note) the current view refetches while holding its previous render at reduced opacity.
"use strict";

// ------------------------------------------------------------------ dom helpers
const $ = (sel, root = document) => root.querySelector(sel);
const SVGNS = "http://www.w3.org/2000/svg";
function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
function setAttrs(el, attrs, isSvg) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "style") el.style.cssText += ";" + v;  // append: fill/stroke may already sit in style
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (isSvg && (k === "fill" || k === "stroke")) el.style[k] = v; // var(--p0) works in style, not reliably as attribute
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  return el;
}
const h = (tag, attrs, ...kids) => add(setAttrs(document.createElement(tag), attrs, false), kids);
const s = (tag, attrs, ...kids) => add(setAttrs(document.createElementNS(SVGNS, tag), attrs, true), kids);

let _ctx;
function textW(str, px = 11, weight = 400) {
  _ctx = _ctx || document.createElement("canvas").getContext("2d");
  _ctx.font = `${weight} ${px}px system-ui, -apple-system, "Segoe UI", sans-serif`;
  return _ctx.measureText(str).width;
}

// ------------------------------------------------------------------ state
const S = {
  tab: "overview", token: null, meta: null, players: {}, local: 0,
  chart: { metric: "gold_per_turn", vis: "visible", hidden: new Set(), range: "all", table: false },
  tl: { local: true, domain: null, off: new Set() },
  turn: { g: null, localOnly: true },
  notes: { kind: "" },
};

async function api(path) {
  const r = await fetch(path, { cache: "no-store" });
  let body;
  try { body = await r.json(); } catch (e) { throw new Error(`${path}: HTTP ${r.status}`); }
  if (!r.ok) throw new Error(body.error || `${path}: HTTP ${r.status}`);
  return body;
}

// ------------------------------------------------------------------ formatting
const AGE = { AGE_ANTIQUITY: "Antiquity", AGE_EXPLORATION: "Exploration", AGE_MODERN: "Modern" };
const ageName = a => AGE[a] || (a || "").replace(/^AGE_/, "").toLowerCase().replace(/^./, c => c.toUpperCase());
const pretty = v => String(v || "").replace(/^(DIFFICULTY|MAPSIZE|GAMESPEED|LEADER|CIVILIZATION|IDEOLOGY)_/, "").toLowerCase()
  .replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
function fmt(v, digits) {
  if (v == null || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 100000) return (v / 1000).toFixed(0) + "K";
  if (a >= 10000) return (v / 1000).toFixed(1).replace(/\.0$/, "") + "K";
  if (a >= 1000 || Number.isInteger(v)) return Math.round(v).toLocaleString("en-US");
  return v.toFixed(digits ?? 1).replace(/\.0$/, "");
}
const fmtSigned = v => v == null ? "" : (v > 0 ? "+" : v < 0 ? "−" : "±") + fmt(Math.abs(v));
const pct = v => v == null ? "—" : Math.round(v * 100) + "%";
function ago(sec) {
  if (sec == null) return "never";
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}
const sinceIso = iso => iso ? (Date.now() - new Date(iso).getTime()) / 1000 : null;

function ageAt(g) {
  const ages = S.meta?.ages || [];
  let a = null;
  for (const x of ages) if (g > x.g_offset) a = x;
  return a;
}
function gLabel(g, withG) {
  if (g == null) return "—";
  const a = ageAt(g);
  const base = a ? `${ageName(a.age)} T${g - a.g_offset}` : `g${g}`;
  return withG ? `${base} (g${g})` : base;
}
const gRange = (a, b) => b == null || a === b ? gLabel(a) : (ageAt(a) === ageAt(b) ? `${gLabel(a)}–T${b - ageAt(b).g_offset}` : `${gLabel(a)} – ${gLabel(b)}`);

const isMajor = id => id != null && id >= 0 && id < 8 && S.players[id] != null;
const pcolor = id => isMajor(id) ? `var(--p${id})` : "var(--minor)";
function pname(id, short) {
  if (id == null) return "—";
  const p = S.players[id];
  if (!p) return `Independent ${id}`;
  return p.player_id === S.local && !short ? `${p.leader} (you)` : p.leader;
}
function pciv(id) {
  const p = S.players[id];
  if (!p) return "";
  const c = p.civilizations || {};
  return c[S.meta?.game?.current_age] || Object.values(c).pop() || "";
}
const swatch = id => h("span", { class: "sw", style: `background:${pcolor(id)}` });
const lineKey = color => h("span", { class: "lk", style: `background:${color}` });

const VERDICT = {
  went_well: ["✓", "Went well"], went_awry: ["✕", "Went awry"], mixed: ["◐", "Mixed"],
  ongoing: ["…", "Ongoing"], neutral: ["–", "Neutral"],
};
function badge(v, source) {
  if (!v) return null;
  const [ic, label] = VERDICT[v] || ["?", v];
  return h("span", { class: `badge v-${v}`, title: source ? `Verdict source: ${source}` : null },
    h("span", { class: "ic" }, ic), label, source ? h("span", { class: "src" }, source === "advisor" ? "· advisor" : "· heuristic") : null);
}
function deltaSpan(d, hib, unitless) {
  if (d == null) return h("span", { class: "delta flat" }, "");
  const dir = d > 0 ? 1 : d < 0 ? -1 : 0;
  const cls = dir === 0 || hib == null ? "flat" : (dir > 0) === (hib === 1) ? "good" : "bad";
  return h("span", { class: `delta ${cls}` }, dir > 0 ? "▲ " : dir < 0 ? "▼ " : "", fmtSigned(d), unitless ? "" : "");
}
function meter(p, cls) {
  const v = p == null ? 0 : Math.max(0, Math.min(1, p));
  return h("div", { class: `meter ${cls || ""}`, role: "meter", "aria-valuenow": Math.round(v * 100), "aria-valuemin": 0, "aria-valuemax": 100 },
    h("i", { style: `width:${(v * 100).toFixed(1)}%` }));
}
const empty = (...kids) => h("div", { class: "empty" }, ...kids);
const card = (attrs, ...kids) => h("div", Object.assign({ class: "card" }, attrs || {}), ...kids);

// ------------------------------------------------------------------ tooltip
const tip = $("#tip");
function showTip(evt, ...kids) {
  tip.replaceChildren(...kids.flat().filter(Boolean));
  tip.style.display = "block";
  const pad = 14, w = tip.offsetWidth, ht = tip.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
  if (y + ht > window.innerHeight - 8) y = Math.max(8, evt.clientY - ht - pad);
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
const hideTip = () => { tip.style.display = "none"; };

// ------------------------------------------------------------------ scales
function linear(d0, d1, r0, r1) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  const f = v => r0 + (v - d0) * k;
  f.inv = px => k ? d0 + (px - r0) / k : d0;
  return f;
}
function niceTicks(lo, hi, n = 5) {
  if (hi === lo) { hi = lo + 1; }
  const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(st => st >= raw) || 10 * mag;
  const t0 = Math.floor(lo / step) * step, out = [];
  for (let v = t0; v <= hi + step * 0.5; v += step) out.push(+v.toFixed(10));
  return out;
}
// x ticks on the g axis: labelled by turn within the age, plus the age starts
function gTicks(x0, x1, widthPx) {
  const span = x1 - x0, steps = [1, 2, 5, 10, 20, 25, 50, 100];
  const step = steps.find(st => widthPx / (span / st) >= 64) || 100;
  const ticks = [];
  for (const a of S.meta.ages) {
    for (let t = step; a.g_offset + t <= a.g_offset + a.turns_max; t += step) {
      const g = a.g_offset + t;
      if (g >= x0 && g <= x1) ticks.push({ g, label: `T${t}` });
    }
  }
  return ticks;
}
function xDomainFor(range) {
  const ages = S.meta.ages, cg = S.meta.game.current_g;
  if (range && range !== "all") {
    const a = ages.find(x => x.age === range);
    if (a) return [a.g_offset + 1, a.current ? Math.max(cg, a.g_offset + 2) : a.end_g];
  }
  return [Math.min(S.meta.first_g || 1, 1), cg];
}

// ------------------------------------------------------------------ header / polling
function renderHeader() {
  const m = S.meta;
  const gl = $("#gameline");
  if (!m || !m.game) { gl.textContent = "No game in the store yet — run `civilizer monitor` or `civilizer refresh`."; return; }
  const g = m.game, me = S.players[S.local];
  gl.replaceChildren(h("b", {}, me ? me.leader : pretty(g.leader)), ` · ${pciv(S.local) || pretty(g.civilization)} · ${pretty(g.difficulty)} · `,
    `${ageName(g.current_age)} turn ${g.current_turn}${g.age_turns_max ? ` of ${g.age_turns_max}` : ""}`);
  renderMonitor(m.monitor);
}
function renderMonitor(mon) {
  const el = $("#monitor"), rf = $("#refresh");
  mon = mon || {};
  const st = mon.state || "absent";
  let color = "var(--muted)", text = "Monitor off";
  if (st === "watching" && !mon.stale) { color = "var(--good)"; text = `Monitor live · ${ago(mon.age_s)}`; }
  else if (st === "watching") { color = "var(--warning)"; text = `Monitor stale · seen ${ago(mon.age_s)}`; }
  else if (st === "game_unavailable") { color = "var(--warning)"; text = `Game not reachable · ${ago(mon.age_s)}`; }
  else if (st === "error") { color = "var(--critical)"; text = "Monitor error"; }
  else if (st === "stopped") { text = `Monitor stopped · ${ago(mon.age_s)}`; }
  el.replaceChildren(h("span", { class: "dot", style: `background:${color}` }), text);
  el.title = [`state: ${st}`, mon.pid ? `pid ${mon.pid}` : null, mon.last_g ? `last turn ${gLabel(mon.last_g)}` : null,
    mon.interval ? `poll every ${mon.interval}s` : null, mon.last_error ? `error: ${mon.last_error}` : null].filter(Boolean).join("\n");
  const lr = mon.last_refresh;
  rf.textContent = lr?.at ? `Store refreshed ${ago(sinceIso(lr.at))}` : (S.meta?.game?.updated_at ? `Store refreshed ${ago(sinceIso(S.meta.game.updated_at))}` : "");
}

async function loadMeta() {
  S.meta = await api("/api/game");
  S.players = {};
  for (const p of S.meta.players || []) S.players[p.player_id] = p;
  S.local = S.meta.game?.local_player_id ?? 0;
}

async function poll() {
  try {
    const v = await api("/api/version");
    renderMonitor(Object.assign({}, S.meta?.monitor, v.monitor));
    if (S.meta) S.meta.monitor = v.monitor;
    if (S.token && v.token !== S.token) { S.token = v.token; await refresh(); }
    else S.token = v.token;
  } catch (e) {
    const el = $("#monitor");
    el.replaceChildren(h("span", { class: "dot", style: "background:var(--critical)" }), "Dashboard server unreachable");
  }
}

// ------------------------------------------------------------------ router
const VIEWS = { overview: viewOverview, story: viewStory, timeline: viewTimeline, charts: viewCharts, turn: viewTurn, advisor: viewAdvisor };
function parseHash() {
  const [tab, arg] = (location.hash.replace(/^#/, "") || "overview").split("/");
  S.tab = VIEWS[tab] ? tab : "overview";
  if (S.tab === "turn") S.turn.g = arg && /^\d+$/.test(arg) ? +arg : null;
}
let renderSeq = 0;
async function refresh() {
  const main = $("#view"), seq = ++renderSeq, scroll = window.scrollY;
  main.classList.add("loading");
  try {
    await loadMeta();
    renderHeader();
    for (const a of document.querySelectorAll("#tabs a")) a.classList.toggle("on", a.dataset.tab === S.tab);
    if (!S.meta.game) {
      main.replaceChildren(empty(h("b", {}, "The store has no game yet. "), "Start the monitor (", h("code", {}, "uv run civilizer monitor"),
        ") or run ", h("code", {}, "uv run civilizer refresh"), "; this page updates by itself."));
      return;
    }
    const node = await VIEWS[S.tab]();
    if (seq !== renderSeq) return;
    main.replaceChildren(node);
    window.scrollTo(0, scroll);
  } catch (e) {
    if (seq !== renderSeq) return;
    main.replaceChildren(h("div", { class: "error" }, h("b", {}, "Could not load this view. "), e.message));
    console.error(e);
  } finally {
    if (seq === renderSeq) main.classList.remove("loading");
  }
}
const go = hash => { if (location.hash === hash) refresh(); else location.hash = hash; };

// ------------------------------------------------------------------ overview
async function viewOverview() {
  const o = await api("/api/overview");
  const g = S.meta.game, me = S.players[S.local] || {};
  const ages = S.meta.ages;
  const cur = ages.find(a => a.current) || ages[ages.length - 1];

  const civs = ages.map(a => h("span", { class: "chip" }, ageName(a.age), " ", h("b", {}, (me.civilizations || {})[a.age] || "?")));
  const character = card({},
    h("div", { class: "row" }, swatch(S.local), h("span", { class: "leader" }, me.leader || pretty(g.leader))),
    h("div", { class: "sec", style: "margin-top:2px" }, [pciv(S.local), pretty(g.difficulty), g.map, pretty(g.map_size), pretty(g.speed)].filter(Boolean).join(" · ")),
    h("div", { class: "civpath" }, civs),
    h("div", { class: "small muted", style: "margin-top:8px" }, `Rivals: `, Object.values(S.players).filter(p => p.player_id !== S.local)
      .map((p, i) => [i ? ", " : "", p.leader, ` (${pciv(p.player_id)})`])),
    h("div", { class: "small muted", style: "margin-top:4px" }, `Build ${g.app_version || "?"} · game ${g.game}`));

  const totalMax = ages.reduce((n, a) => n + (a.turns_max || a.turns), 0);
  const progress = card({},
    h("div", { class: "sec small" }, `${ageName(g.current_age)} age`),
    h("div", { class: "row", style: "align-items:baseline;gap:8px" }, h("span", { class: "herofig" }, `Turn ${g.current_turn}`),
      g.age_turns_max ? h("span", { class: "sec" }, `of ${g.age_turns_max} · ${pct(g.current_turn / g.age_turns_max)} through the age`) : null),
    h("div", { class: "agebar", title: "Ages laid end to end; filled = played" }, ages.map(a => {
      const w = (a.turns_max || a.turns) / totalMax * 100, played = a.current ? (g.current_turn / (a.turns_max || 1)) : 1;
      return h("div", { style: `width:${w}%` }, h("i", { style: `width:${played * 100}%` }));
    })),
    h("div", { class: "agelabels" }, ages.map(a => h("div", { style: `width:${(a.turns_max || a.turns) / totalMax * 100}%` },
      `${ageName(a.age)} ${a.current ? `${g.current_turn}/${a.turns_max}` : a.turns}`))),
    h("div", { class: "tagline", style: "margin-top:12px" }, o.phases.filter(p => p.scope === "game" || p.scope === "age").map(p =>
      h("a", { href: "#story", style: "text-decoration:none" }, h("span", { class: "chip" }, p.scope === "game" ? "Game" : ageName(p.scope_key)),
        " ", badge(p.verdict, p.verdict_source === "advisor" ? "advisor" : null)))));

  const tiles = h("div", { class: "grid tiles" }, o.tiles.map(t => card({ class: "card tile" },
    h("div", { class: "lab" }, t.label),
    h("div", { class: "val" }, fmt(t.value), t.cap != null ? h("small", {}, ` / ${fmt(t.cap)} cap`) : null),
    h("div", { class: "meta" },
      h("div", {}, deltaSpan(t.delta, t.higher_is_better), t.delta_vs_g ? h("div", { class: "muted" }, `vs ${gLabel(t.delta_vs_g)}`) : null),
      h("div", { style: "text-align:right" }, t.rank && t.of > 2 ? h("div", { class: "muted" }, `#${t.rank} of ${t.of}`) : null, sparkline(t.trend))))));

  const standings = h("div", { class: "grid standings" }, ["economic", "science", "culture", "military", "score"].filter(k => o.standings[k]).map(k => {
    const st = o.standings[k], max = Math.max(st.target || 0, ...st.rows.map(r => r.value), 1);
    const leader = st.leader;
    return card({},
      h("div", { class: "cardhead" }, h("h3", {}, k === "score" ? "Score" : k[0].toUpperCase() + k.slice(1)), h("span", { class: "sp" }),
        st.rank ? h("span", { class: "chip" }, h("b", {}, `#${st.rank}`), ` of ${st.of}`) : null),
      st.target ? h("div", { style: "margin-bottom:8px" },
        h("div", { class: "row small" }, h("span", {}, h("b", {}, fmt(st.value)), ` / ${fmt(st.target)} to win`), h("span", { class: "sp" }), h("span", { class: "sec" }, pct(st.value / st.target))),
        meter(st.value / st.target, st.value >= st.target ? "met" : "")) : null,
      leader && leader.player_id !== S.local ? h("div", { class: "small sec", style: "margin-bottom:6px" }, `Leader: ${pname(leader.player_id)} with ${fmt(leader.value)}`)
        : h("div", { class: "small sec", style: "margin-bottom:6px" }, "You lead this path"),
      h("div", { class: "sbars" }, st.rows.map(r => [
        swatch(r.player_id), h("span", { class: `nm ${r.player_id === S.local ? "me" : ""}`, title: `${pname(r.player_id)} · ${pciv(r.player_id)}` }, pname(r.player_id, true)),
        h("span", { class: "track" }, h("i", { style: `width:${r.value / max * 100}%;background:${pcolor(r.player_id)}` }),
          st.target ? h("u", { style: `left:${st.target / max * 100}%`, title: `target ${fmt(st.target)}` }) : null),
        h("span", { class: "v" }, fmt(r.value))])));
  }));

  const majorWars = o.wars.filter(w => w.major), minorWars = o.wars.filter(w => !w.major);
  const warRow = w => h("li", {}, h("div", { class: "row" }, swatch(w.opponent), h("b", {}, pname(w.opponent)), w.major ? h("span", { class: "sec small" }, pciv(w.opponent)) : null,
    h("span", { class: "sp" }), h("span", { class: "small" }, `kills ${w.units_killed} · losses ${w.units_lost}`)),
    h("div", { class: "small sec" }, `${w.war_type || "War"} since ${gLabel(w.since_g)} (${w.turns} turns) · declared by ${w.declared_by === S.local ? "you" : pname(w.declared_by)}`));
  const wars = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Wars"), h("span", { class: "sp" }), h("span", { class: "chip" }, `${majorWars.length} major · ${minorWars.length} independent`)),
    o.wars.length ? h("ul", { class: "plain" }, majorWars.map(warRow),
      minorWars.length ? h("li", {}, h("details", {}, h("summary", {}, `${minorWars.length} wars with independent powers`), h("ul", { class: "plain" }, minorWars.map(warRow)))) : null)
      : empty("At peace."));

  return h("div", {},
    h("div", { class: "hero" }, character, progress),
    h("div", { class: "section" }, h("h2", {}, "Your empire now"), tiles),
    h("div", { class: "section" }, h("div", { class: "cardhead" }, h("h2", {}, "Victory standings"), h("span", { class: "hint" },
      o.visibility === "visible" ? "Rival values as shown on your screen" : "Includes omniscient rival data")), standings),
    h("div", { class: "section grid g2" }, wars, phaseSummaryCard(o.phases)));
}

function phaseSummaryCard(phases) {
  const wars = phases.filter(p => p.scope === "war"), goals = phases.filter(p => p.scope === "goal");
  return card({}, h("div", { class: "cardhead" }, h("h2", {}, "Phases & initiatives"), h("span", { class: "sp" }), h("a", { href: "#story", class: "small" }, "Read the story →")),
    h("ul", { class: "plain" }, [...phases.filter(p => p.scope !== "goal" || goals.length), ].map(p => h("li", {}, h("div", { class: "row" },
      h("span", {}, phaseTitle(p)), h("span", { class: "sp" }), h("span", { class: "small muted" }, gRange(p.g_start, p.g_end)), badge(p.verdict))))),
    !wars.length ? h("div", { class: "small muted", style: "margin-top:6px" }, "No wars against major civs recorded.") : null);
}
function phaseTitle(p) {
  if (p.scope === "game") return "Whole game";
  if (p.scope === "age") return `${ageName(p.scope_key)} age`;
  if (p.scope === "war") {
    const m = /player (\d+)/.exec(p.title || "");
    return m ? `War with ${pname(+m[1])}` : p.title;
  }
  return p.title || `Goal ${p.scope_key}`;
}

function sparkline(pts, w = 84, ht = 26) {
  if (!pts || pts.length < 2) return null;
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const x = linear(Math.min(...xs), Math.max(...xs), 2, w - 4), lo = Math.min(...ys), hi = Math.max(...ys);
  const y = linear(lo, hi === lo ? lo + 1 : hi, ht - 4, 4);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  return s("svg", { width: w, height: ht, viewBox: `0 0 ${w} ${ht}`, "aria-hidden": "true", style: "display:block;margin-left:auto" },
    s("path", { d, fill: "none", stroke: "var(--muted)", "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" }),
    s("circle", { cx: x(last[0]), cy: y(last[1]), r: 3, fill: "var(--accent)", stroke: "var(--surface)", "stroke-width": 1.5 }));
}

// ------------------------------------------------------------------ story
async function viewStory() {
  const d = await api("/api/phases");
  const ps = d.phases;
  const cards = sc => ps.filter(p => p.scope === sc).map(phaseCard);
  return h("div", {},
    h("div", { class: "cardhead" }, h("h2", {}, "The story so far"), h("span", { class: "hint" },
      "Facts are computed from the store; narratives and final verdicts are written by the advisor (Claude) as annotations.")),
    h("div", { class: "grid" }, cards("game")),
    h("div", { class: "section" }, h("h2", {}, "Ages"), h("div", { class: "grid g3" }, cards("age"))),
    h("div", { class: "section" }, h("h2", {}, "Wars"), ps.some(p => p.scope === "war") ? h("div", { class: "grid g3" }, cards("war"))
      : empty("No wars with major civilizations so far.")),
    h("div", { class: "section" }, h("h2", {}, "Goals"), ps.some(p => p.scope === "goal") ? h("div", { class: "grid g3" }, cards("goal"))
      : empty(h("b", {}, "No goals yet. "), "Tell Claude what you are aiming for (\"economic victory by Modern turn 120\"); it records goals with ",
        h("code", {}, "add_goal"), " and they appear here with progress and a verdict.")),
    d.unscoped.length ? h("div", { class: "section" }, h("h2", {}, "Other narrative notes"), card({}, d.unscoped.map(noteEl))) : null,
    h("div", { class: "section" }, h("h2", {}, "Intentions & priority changes"), notesStrip(d.notes)));
}

function phaseCard(p) {
  const f = p.facts || {};
  const narr = p.annotations.filter(a => a.kind === "narrative" || a.kind === "outcome");
  const other = p.annotations.filter(a => !(a.kind === "narrative" || a.kind === "outcome"));
  const scopeHint = p.scope === "age" ? `age ${p.scope_key}` : `${p.scope} ${p.scope_key}`;
  return card({ class: "card phase" },
    h("div", { class: "cardhead" }, h("h3", {}, phaseTitle(p)), h("span", { class: "small muted" }, gRange(p.g_start, p.g_end)), h("span", { class: "sp" }),
      badge(p.verdict, p.verdict_source)),
    p.verdict_source === "advisor" && p.heuristic_verdict !== p.verdict ? h("div", { class: "small muted" }, `Heuristic said: ${VERDICT[p.heuristic_verdict]?.[1] || p.heuristic_verdict}`) : null,
    h("div", { class: "facts" }, phaseFacts(p, f)),
    h("div", { class: "narr" },
      narr.length ? narr.map(noteEl) : empty("No advisor narrative yet — ask Claude to tell the story of this ", p.scope === "game" ? "game" : p.scope,
        " (it writes a ", h("code", {}, "narrative"), " annotation scoped to ", h("code", {}, scopeHint), ")."),
      other.length ? h("details", {}, h("summary", {}, `${other.length} more note${other.length > 1 ? "s" : ""}`), other.map(noteEl)) : null),
    h("div", { class: "row small", style: "margin-top:8px" },
      h("a", { href: "#timeline", onclick: e => { e.preventDefault(); S.tl.domain = [p.g_start, Math.max(p.g_end, p.g_start + 1)]; go("#timeline"); } }, "Timeline"),
      h("a", { href: `#turn/${p.g_end}` }, `Turn ${gLabel(p.g_end)}`)));
}

function noteEl(a) {
  return h("div", { class: "note" },
    h("div", { class: "row when" }, h("span", { class: "kind" }, a.kind.replace("_", " ")), `${gLabel(a.g)} · ${a.author}`, a.verdict ? badge(a.verdict) : null),
    h("p", {}, a.text));
}

function phaseFacts(p, f) {
  if (p.scope === "game") {
    const st = f.standings_now || {};
    return h("div", {},
      h("div", { class: "tagline" }, Object.entries(f.age_verdicts || {}).map(([a, v]) => h("span", { class: "chip" }, ageName(a), " ", badge(v)))),
      standingsChips(st));
  }
  if (p.scope === "age") {
    const m = f.metrics || {};
    const rows = Object.entries(m).filter(([, v]) => v && (v.start != null || v.end != null));
    const cat = Object.fromEntries((S.meta.catalog || []).map(c => [c.metric, c]));
    return h("div", {},
      rows.length ? h("table", { class: "t" }, h("tr", {}, h("th", {}, "Metric"), h("th", { class: "n" }, "Start"), h("th", { class: "n" }, "End"), h("th", { class: "n" }, "Change")),
        rows.map(([k, v]) => h("tr", {}, h("td", {}, cat[k]?.label || k), h("td", { class: "n" }, fmt(v.start)), h("td", { class: "n" }, fmt(v.end)),
          h("td", { class: "n" }, v.start ? deltaSpan(v.end - v.start, cat[k]?.higher_is_better) : "—"))))
        : h("div", { class: "small muted" }, "No per-turn yields recorded for this age (logs not archived)."),
      standingsChips(f.standings || {}),
      h("dl", { class: "kv", style: "margin-top:8px" },
        h("dt", {}, "Combat"), h("dd", {}, `${f.units_killed ?? 0} units destroyed · ${f.units_lost ?? 0} lost`),
        f.war_opponents?.length ? [h("dt", {}, "At war with"), h("dd", {}, f.war_opponents.map((o, i) => [i ? ", " : "", pname(o, true)]))] : null,
        h("dt", {}, "Settlements"), h("dd", {}, `${f.settlements_founded ?? 0} founded`,
          f.captured?.length ? ` · captured ${f.captured.map(c => `${c.settlement} (${gLabel(c.g)})`).join(", ")}` : "",
          f.lost?.length ? ` · lost ${f.lost.map(c => `${c.settlement} (${gLabel(c.g)})`).join(", ")}` : ""),
        f.governments?.length ? [h("dt", {}, "Government"), h("dd", {}, f.governments.map(x => `${x.subject} (${gLabel(x.g)})`).join(", "))] : null,
        f.legacies?.length ? [h("dt", {}, "Legacies"), h("dd", {}, f.legacies.map(x => x.subject).join(", "))] : null,
        f.wonders?.length ? [h("dt", {}, "Wonders"), h("dd", {}, f.wonders.map(x => x.subject || x).join(", "))] : null));
  }
  if (p.scope === "war") {
    return h("dl", { class: "kv" },
      h("dt", {}, "Opponent"), h("dd", {}, h("span", { class: "row", style: "gap:6px" }, swatch(f.opponent), pname(f.opponent), h("span", { class: "muted" }, pciv(f.opponent)))),
      h("dt", {}, "Declared by"), h("dd", {}, f.declared_by === S.local ? "You" : pname(f.declared_by), f.war_type ? ` · ${f.war_type}` : ""),
      h("dt", {}, "Duration"), h("dd", {}, `${f.turns ?? "?"} turns${f.ongoing ? " (ongoing)" : ""}`),
      h("dt", {}, "Combat"), h("dd", {}, `${f.units_killed ?? 0} destroyed · ${f.units_lost ?? 0} lost`),
      h("dt", {}, "Settlements"), h("dd", {}, `${(f.captured || []).length} captured · ${(f.lost || []).length} lost`));
  }
  if (p.scope === "goal") {
    return h("div", {}, h("div", { class: "row small" }, h("span", { class: "status" }, f.status || ""), h("span", {}, `${f.horizon || ""}-term`),
      f.overdue ? h("span", { class: "warnline" }, "overdue") : null, h("span", { class: "sp" }), pct(f.progress)), meter(f.progress, f.progress >= 1 ? "met" : ""),
      (f.metrics || []).length ? h("table", { class: "t", style: "margin-top:6px" }, (f.metrics || []).map(m => h("tr", {},
        h("td", {}, m.label || m.metric), h("td", { class: "n" }, `${m.comparator} ${fmt(m.target)}`), h("td", { class: "n" }, fmt(m.current)),
        h("td", { class: "n" }, m.met ? "✓ met" : pct(m.progress))))) : null);
  }
  return h("pre", { class: "small" }, JSON.stringify(f, null, 1));
}

function standingsChips(st) {
  const keys = Object.keys(st);
  if (!keys.length) return null;
  return h("div", { class: "tagline" }, keys.map(k => {
    const r = st[k];
    return h("span", { class: "chip", title: r.leader !== S.local ? `Leader: ${pname(r.leader)} (${fmt(r.leader_value)})` : "You lead" },
      `${k[0].toUpperCase() + k.slice(1)} `, h("b", {}, `#${r.rank}/${r.of}`), ` ${fmt(r.value)}`);
  }));
}

function notesStrip(notes) {
  if (!notes.length) return empty(h("b", {}, "No intentions or priority changes recorded yet. "),
    "When you tell Claude what you plan or change course, it records an ", h("code", {}, "intention"), " / ", h("code", {}, "priority_change"),
    " note; they line up here on the turn axis.");
  const wrap = card({});
  const draw = () => {
    const W = Math.max(400, wrap.clientWidth - 32), H = 54, [x0, x1] = xDomainFor("all");
    const x = linear(x0, x1, 8, W - 8);
    const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` },
      S.meta.ages.map(a => [s("line", { class: "agel", x1: x(a.g_offset + 0.5), x2: x(a.g_offset + 0.5), y1: 0, y2: H - 18 }),
        s("text", { class: "agelab", x: x(a.g_offset + 0.5) + 4, y: 11 }, ageName(a.age))]),
      s("line", { class: "gridl", x1: 8, x2: W - 8, y1: 32, y2: 32 }),
      notes.map(n => s("circle", { cx: x(n.g), cy: 32, r: 5, fill: n.kind === "intention" ? "var(--p0)" : "var(--p1)", stroke: "var(--surface)", "stroke-width": 2,
        style: "cursor:pointer", onpointermove: e => showTip(e, h("div", { class: "tt" }, `${gLabel(n.g)} · ${n.kind.replace("_", " ")}`), h("div", {}, n.text)),
        onpointerleave: hideTip, onclick: () => go(`#turn/${n.g}`) })));
    wrap.replaceChildren(h("div", { class: "legend" }, h("span", { class: "row small" }, h("span", { class: "dot", style: "background:var(--p0)" }), "intention"),
      h("span", { class: "row small" }, h("span", { class: "dot", style: "background:var(--p1)" }), "priority change")), svg,
      h("div", { style: "margin-top:8px" }, notes.slice().reverse().map(noteEl)));
  };
  requestAnimationFrame(draw);
  return wrap;
}

// ------------------------------------------------------------------ timeline
const KIND_LABEL = {
  age: "Age", war: "War", war_declared: "War declared", alliance: "Alliance", settlement: "Settlement founded", settlement_captured: "Settlement captured",
  tech: "Tech researched", civic: "Civic completed", tech_research: "Researching", civic_research: "Studying civic", government: "Government",
  religion: "Religion", religion_founded: "Religion founded", policy: "Policy", production: "Production", project: "Project", purchase: "Purchase",
  legacy: "Legacy", unit_killed: "Unit destroyed",
};
const LANES = [
  ["gov", "Government", ["government"]],
  ["settle", "Settlements", ["settlement", "settlement_captured"]],
  ["tech", "Technology", ["tech", "tech_research"]],
  ["civic", "Civics", ["civic", "civic_research"]],
  ["policy", "Policies & traditions", ["policy"]],
  ["legacy", "Religion & legacies", ["religion", "religion_founded", "legacy"]],
  ["prod", "Production & projects", ["production", "project", "purchase"]],
  ["war", "Wars", ["war"]],
  ["alliance", "Alliances", ["alliance"]],
  ["units", "Unit deaths", ["unit_killed"]],
];
const INTERVAL_KINDS = new Set(["government", "tech_research", "civic_research", "policy", "production", "project", "war", "alliance"]);
const laneOf = Object.fromEntries(LANES.flatMap(([k, , kinds]) => kinds.map(x => [x, k])));

function evRow(e, localMode) {
  const me = S.local, d = e.detail || {}, owner = e.players[0];
  const other = e.players.find(p => p !== me);
  const per = (pid, sub) => ({ id: `p${pid}${sub ? ":" + sub : ""}`, label: (sub ? sub + " · " : "") + pname(pid, true), color: pcolor(pid), order: pid, sw: pid });
  switch (e.kind) {
    case "government": return localMode ? { id: "gov", label: "Government" } : isMajor(owner) ? per(owner) : null;
    case "settlement": return localMode ? { id: "founded", label: "Founded", order: 0 } : isMajor(owner) ? per(owner) : null;
    case "settlement_captured": {
      const captor = d.captured_by ?? e.players[0], from = d.from_player ?? e.players[1];
      if (localMode) return captor === me ? { id: "captured", label: "Captured", order: 1 } : { id: "lost", label: "Lost", order: 2 };
      return isMajor(captor) ? per(captor) : isMajor(from) ? per(from) : null;
    }
    case "tech": case "civic": return localMode ? { id: e.kind + "done", label: "Completed", order: 1 } : isMajor(owner) ? per(owner) : null;
    case "tech_research": case "civic_research": return localMode ? { id: e.kind, label: "In progress", order: 0 } : isMajor(owner) ? per(owner, "in progress") : null;
    case "policy": {
      const sub = d.category === "tradition" ? "Traditions" : "Policies";
      return localMode ? { id: sub, label: sub, order: sub === "Policies" ? 0 : 1 } : isMajor(owner) ? per(owner, sub) : null;
    }
    case "religion": case "religion_founded": return localMode ? { id: "religion", label: "Religion" } : isMajor(owner) ? per(owner, "Religion") : null;
    case "legacy": return localMode ? { id: "legacy", label: "Legacies", order: 1 } : isMajor(owner) ? per(owner, "Legacies") : null;
    case "production": case "project": case "purchase":
      if (localMode) return { id: "city:" + (d.city || "?"), label: d.city || "Empire" };
      return isMajor(owner) ? per(owner) : null;
    case "war": {
      if (e.precision === "state") return { id: "warstate", label: "At war (age-end capture)", order: 99 };
      const [a, b] = e.players;
      if (localMode) return isMajor(other) ? { id: "war" + other, label: pname(other, true), color: pcolor(other), order: other, sw: other }
        : { id: "warminor", label: "Independents", order: 50 };
      if (isMajor(a) && isMajor(b)) return { id: `war${Math.min(a, b)}-${Math.max(a, b)}`, label: `${pname(Math.min(a, b), true)} – ${pname(Math.max(a, b), true)}`, order: Math.min(a, b) * 10 + Math.max(a, b) };
      const maj = isMajor(a) ? a : isMajor(b) ? b : null;
      return maj == null ? null : { id: `warind${maj}`, label: `${pname(maj, true)} – independents`, order: 100 + maj, sw: maj };
    }
    case "alliance": {
      const [a, b] = e.players;
      if (localMode) return { id: "al" + other, label: pname(other, true), sw: other };
      return { id: `al${a}-${b}`, label: `${pname(a, true)} – ${pname(b, true)}`, order: a * 10 + b };
    }
    case "unit_killed": {
      const victim = d.victim ?? e.players[0], killer = d.killer ?? e.players[1];
      if (localMode) return killer === me ? { id: "kills", label: "Kills", order: 0 } : { id: "losses", label: "Losses", order: 1 };
      return isMajor(victim) ? { id: `loss${victim}`, label: `Losses · ${pname(victim, true)}`, order: victim, sw: victim }
        : { id: "lossind", label: "Independents' losses", order: 99 };
    }
  }
  return null;
}
function evColor(e, localMode) {
  const me = S.local, d = e.detail || {};
  if (e.kind === "war" || e.kind === "alliance") {
    if (!localMode && e.kind === "war") { const [a, b] = e.players; return isMajor(a) && isMajor(b) ? pcolor(Math.max(a, b)) : pcolor(isMajor(a) ? b : a); }
    return pcolor(e.players.find(p => p !== me) ?? e.players[1]);
  }
  if (e.kind === "unit_killed") {
    const victim = d.victim ?? e.players[0], killer = d.killer ?? e.players[1];
    return localMode ? pcolor(killer === me ? victim : killer) : pcolor(killer);
  }
  if (e.kind === "settlement_captured") return pcolor(d.captured_by ?? e.players[0]);
  return pcolor(e.players[0]);
}

function buildLanes(events, localMode) {
  const cg = S.meta.game.current_g, lanes = new Map(LANES.map(([k, label]) => [k, { key: k, label, rows: new Map() }]));
  for (const e of events) {
    if (e.kind === "age" || e.kind === "war_declared") continue;
    const lk = laneOf[e.kind];
    if (!lk || S.tl.off.has(lk)) continue;
    if (localMode && !e.local) continue;
    if (!localMode && e.kind === "war" && e.minor && e.players.every(p => !isMajor(p))) continue;
    const r = evRow(e, localMode);
    if (!r) continue;
    const lane = lanes.get(lk);
    if (!lane.rows.has(r.id)) lane.rows.set(r.id, Object.assign({ items: [], first: e.start_g }, r));
    const interval = INTERVAL_KINDS.has(e.kind) && e.precision !== "state" && (e.end_g ?? cg) > e.start_g;
    lane.rows.get(r.id).items.push({ e, x0: e.start_g, x1: interval ? (e.end_g ?? cg) : e.start_g, interval, color: evColor(e, localMode),
      hollow: e.precision !== "exact", shape: e.kind === "settlement_captured" ? "tri" : e.kind === "legacy" || e.kind === "religion_founded" ? "diamond" : e.kind === "purchase" ? "tick" : "dot" });
  }
  const out = [];
  for (const lane of lanes.values()) {
    const rows = [...lane.rows.values()].sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || a.first - b.first || String(a.label).localeCompare(b.label));
    for (const row of rows) {  // greedy interval packing into tracks
      const ends = [];
      for (const it of row.items.filter(i => i.interval).sort((a, b) => a.x0 - b.x0)) {
        let t = ends.findIndex(end => end < it.x0);
        if (t < 0) { t = ends.length; ends.push(0); }
        ends[t] = it.x1;
        it.track = t;
      }
      row.tracks = Math.max(1, ends.length);
      row.hasIntervals = ends.length > 0;
    }
    if (rows.length) out.push({ key: lane.key, label: lane.label, rows });
  }
  return out;
}

async function viewTimeline() {
  const d = await api(`/api/timeline?local=${S.tl.local ? 1 : 0}`);
  const root = h("div", {});
  const box = h("div", { class: "card tl", style: "padding:10px 12px" });
  const ctl = h("div", { class: "controls" },
    h("div", { class: "seg" }, [["Your empire", true], ["All players", false]].map(([l, v]) =>
      h("button", { class: S.tl.local === v ? "on" : "", onclick: () => { S.tl.local = v; refresh(); } }, l))),
    h("div", { class: "seg" }, [["all", "Whole game"], ...S.meta.ages.map(a => [a.age, ageName(a.age)])].map(([k, l]) =>
      h("button", { class: JSON.stringify(S.tl.domain || xDomainFor("all")) === JSON.stringify(xDomainFor(k)) ? "on" : "", onclick: () => { S.tl.domain = xDomainFor(k); refresh(); } }, l))),
    S.tl.domain && JSON.stringify(S.tl.domain) !== JSON.stringify(xDomainFor("all")) ? h("button", { class: "iconbtn", onclick: () => { S.tl.domain = null; refresh(); } }, "Reset zoom") : null,
    h("span", { class: "sp" }),
    h("details", {}, h("summary", {}, "Lanes"), h("div", { class: "row small" }, LANES.map(([k, l]) => h("label", { class: "row", style: "gap:4px" },
      h("input", { type: "checkbox", checked: !S.tl.off.has(k), onchange: ev => { ev.target.checked ? S.tl.off.delete(k) : S.tl.off.add(k); refresh(); } }), l)))));
  root.append(ctl, h("div", { class: "hint", style: "margin-bottom:8px" },
    "Colors follow players (wars, alliances and unit deaths: the other side). Hollow marks: turn known only within a range (end-of-age capture). ",
    "Drag across the chart to zoom; click a mark to open that turn."), box);
  const lanes = buildLanes(d.events, S.tl.local);
  const draw = () => drawTimeline(box, lanes);
  requestAnimationFrame(draw);
  onResize(draw);
  return root;
}

function drawTimeline(box, lanes) {
  const LW = 190, W = Math.max(600, box.clientWidth - 24), PW = W - LW - 12;
  const [x0, x1] = S.tl.domain || xDomainFor("all");
  const x = linear(x0 - 0.5, x1 + 0.5, LW, LW + PW);
  const TH = 14, AXIS = 40, cg = S.meta.game.current_g;
  const rows = [];
  let y = AXIS + 26;  // ages band takes the first 26px under the axis
  for (const lane of lanes) {
    rows.push({ laneHead: lane, y0: y, y1: y + 22 });
    y += 22;
    for (const r of lane.rows) {
      const ht = r.hasIntervals ? r.tracks * TH + 4 : 18;
      rows.push({ row: r, y0: y, y1: y + ht });
      y += ht;
    }
    y += 4;
  }
  const H = y + 6;
  const clipId = "tlclip" + Math.random().toString(36).slice(2, 7);
  const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Timeline of the game" });
  svg.append(s("defs", {}, s("clipPath", { id: clipId }, s("rect", { x: LW, y: 0, width: PW, height: H }))));
  const plot = s("g", { "clip-path": `url(#${clipId})` });
  // axis
  const ax = s("g", { class: "ax" });
  for (const t of gTicks(x0, x1, PW)) {
    ax.append(s("line", { class: "gridl", x1: x(t.g), x2: x(t.g), y1: AXIS - 4, y2: H }), s("text", { x: x(t.g), y: AXIS - 8, "text-anchor": "middle" }, t.label));
  }
  plot.append(ax);
  // ages band + boundaries
  for (const a of S.meta.ages) {
    const a0 = Math.max(x0, a.g_offset + 1), a1 = Math.min(x1, a.current ? cg : a.end_g);
    if (a1 < a0) continue;
    const bx = x(a0 - 0.5), bw = x(a1 + 0.5) - bx;
    plot.append(s("rect", { class: "band", x: bx + 1, y: AXIS, width: Math.max(0, bw - 2), height: 20, rx: 4 }));
    const lab = `${ageName(a.age)}${a.current ? ` · T${cg - a.g_offset}/${a.turns_max}` : ` · ${a.turns} turns`}`;
    if (textW(lab, 11, 600) + 12 < bw) plot.append(s("text", { class: "bandlab", x: bx + 8, y: AXIS + 14 }, lab));
    if (a.g_offset + 0.5 > x0) plot.append(s("line", { class: "agel", x1: x(a.g_offset + 0.5), x2: x(a.g_offset + 0.5), y1: AXIS, y2: H }));
  }
  if (cg >= x0 && cg <= x1) plot.append(s("line", { class: "now", x1: x(cg + 0.5), x2: x(cg + 0.5), y1: AXIS - 2, y2: H }));
  const labels = s("g", {});
  labels.append(s("text", { class: "lane-lab", x: 4, y: AXIS + 14 }, "Ages"));
  for (const r of rows) {
    if (r.laneHead) {
      labels.append(s("text", { class: "lane-lab", x: 4, y: r.y0 + 15 }, r.laneHead.label));
      svg.append(s("line", { class: "sep", x1: 0, x2: W, y1: r.y0 + 0.5, y2: r.y0 + 0.5 }));
      continue;
    }
    const row = r.row, mid = (r.y0 + r.y1) / 2;
    let lab = row.label, lx = 14;
    if (row.sw != null) { labels.append(s("rect", { x: 14, y: mid - 4, width: 8, height: 8, rx: 2, fill: pcolor(row.sw) })); lx = 27; }
    while (textW(lab) > LW - lx - 8 && lab.length > 4) lab = lab.slice(0, -2);
    if (lab !== row.label) lab = lab.slice(0, -1) + "…";
    labels.append(s("text", { class: "row-lab", x: lx, y: mid + 4 }, s("title", {}, row.label), lab));
    for (const it of row.items) {
      if (it.x1 < x0 - 1 || it.x0 > x1 + 1) continue;
      it._y = r.y0;
      if (it.interval) {
        const ty = r.y0 + 2 + it.track * TH, bx = x(it.x0 - 0.5), bw = Math.max(2, x(it.x1 + 0.5) - bx - 1);
        plot.append(s("rect", { x: bx, y: ty, width: bw, height: TH - 2, rx: 3, fill: it.color, "fill-opacity": it.e.end_g == null || it.e.detail?.end_reason ? 0.22 : 0.3 }),
          s("rect", { x: bx, y: ty, width: Math.min(3, bw), height: TH - 2, rx: 1, fill: it.color }));
        const lab = barLabel(it.e);
        if (textW(lab, 10) + 10 < bw) plot.append(s("text", { class: "barlab", x: bx + 6, y: ty + TH - 5 }, lab));
      } else {
        const px = x(it.x0);
        if (it.shape === "tick") plot.append(s("line", { x1: px, x2: px, y1: r.y0 + 3, y2: r.y1 - 3, stroke: it.color, "stroke-width": 1.5, "stroke-opacity": 0.7 }));
        else if (it.shape === "tri") plot.append(s("path", { d: `M${px},${mid - 6}L${px + 6},${mid + 5}L${px - 6},${mid + 5}Z`, fill: it.color, stroke: "var(--surface)", "stroke-width": 1.5 }));
        else if (it.shape === "diamond") plot.append(s("path", { d: `M${px},${mid - 6}L${px + 6},${mid}L${px},${mid + 6}L${px - 6},${mid}Z`, fill: it.hollow ? "var(--surface)" : it.color, stroke: it.hollow ? it.color : "var(--surface)", "stroke-width": 1.5 }));
        else plot.append(s("circle", { cx: px, cy: mid, r: 4, fill: it.hollow ? "var(--surface)" : it.color, stroke: it.hollow ? it.color : "var(--surface)", "stroke-width": it.hollow ? 1.5 : 2 }));
      }
    }
  }
  if (!lanes.length) labels.append(s("text", { class: "row-lab", x: LW + 10, y: AXIS + 50 }, "No events in the selected lanes."));
  const brush = s("rect", { class: "brush", x: 0, y: AXIS, width: 0, height: H - AXIS, style: "display:none" });
  const cross = s("line", { class: "cross", x1: 0, x2: 0, y1: AXIS - 4, y2: H, style: "display:none" });
  svg.append(plot, labels, cross, brush);

  const hit = (evt) => {
    const pt = svg.getBoundingClientRect(), px = (evt.clientX - pt.left) * (W / pt.width), py = (evt.clientY - pt.top) * (H / pt.height);
    const r = rows.find(rr => rr.row && py >= rr.y0 && py < rr.y1);
    const found = r ? r.row.items.filter(it => it._y === r.y0 && (it.interval ? px >= x(it.x0 - 0.5) - 2 && px <= x(it.x1 + 0.5) + 2
      && (r.row.tracks === 1 || Math.floor((py - r.y0 - 2) / TH) === it.track) : Math.abs(x(it.x0) - px) <= 6)) : [];
    return { px, py, g: Math.round(x.inv(px)), row: r?.row, found };
  };
  let drag = null;
  svg.addEventListener("pointerdown", e => { const p = hit(e); if (p.px >= LW) { drag = { x: p.px, moved: false }; svg.setPointerCapture(e.pointerId); } });
  svg.addEventListener("pointermove", e => {
    const p = hit(e);
    if (drag) {
      if (Math.abs(p.px - drag.x) > 5) drag.moved = true;
      const a = Math.max(LW, Math.min(drag.x, p.px)), b = Math.min(LW + PW, Math.max(drag.x, p.px));
      setAttrs(brush, { x: a, width: b - a }, true);
      brush.style.display = drag.moved ? "" : "none";
      hideTip();
      return;
    }
    if (p.px < LW || p.px > LW + PW) { cross.style.display = "none"; hideTip(); return; }
    setAttrs(cross, { x1: x(p.g), x2: x(p.g) }, true);
    cross.style.display = "";
    if (!p.found.length) { showTip(e, h("div", { class: "tt" }, gLabel(p.g, true)), p.row ? h("div", { class: "sec" }, p.row.label) : null); return; }
    const shown = p.found.slice(0, 10);
    showTip(e, h("div", { class: "tt" }, p.row.label), shown.map(it => evTip(it.e)),
      p.found.length > shown.length ? h("div", { class: "muted" }, `+${p.found.length - shown.length} more`) : null);
  });
  svg.addEventListener("pointerleave", () => { cross.style.display = "none"; hideTip(); });
  svg.addEventListener("pointerup", e => {
    const p = hit(e);
    if (drag && drag.moved) {
      const a = Math.round(x.inv(Math.min(drag.x, p.px))), b = Math.round(x.inv(Math.max(drag.x, p.px)));
      drag = null;
      if (b - a >= 2) { S.tl.domain = [Math.max(1, a), Math.min(cg, b)]; refresh(); }
      return;
    }
    drag = null;
    if (p.px >= LW) go(`#turn/${p.found.length ? p.found[0].e.start_g : p.g}`);
  });
  box.replaceChildren(svg);
}

function barLabel(e) {  // war/alliance subjects are "war 0-8"; name the other side instead
  if (e.kind !== "war" && e.kind !== "alliance") return e.subject;
  const [a, b] = e.players;
  return S.tl.local ? pname(e.players.find(p => p !== S.local) ?? b, true) : `${pname(a, true)} – ${pname(b, true)}`;
}

function evTip(e) {
  const d = e.detail || {}, bits = [];
  const when = e.precision === "range" && e.start_lo != null ? `between T${e.start_lo} and T${e.start_hi} (${ageName(e.start_age)})`
    : e.precision === "state" ? `seen ${gLabel(e.start_g)}` : (e.end_g != null && e.end_g !== e.start_g ? gRange(e.start_g, e.end_g) : gLabel(e.start_g));
  bits.push(KIND_LABEL[e.kind] || e.kind, when);
  if (e.kind === "unit_killed") bits.push(`${pname(d.victim ?? e.players[0], true)} lost it to ${pname(d.killer ?? e.players[1], true)}`);
  else if (e.kind === "war") bits.push(`${e.players.map(p => pname(p, true)).join(" vs ")}${d.war_type ? ` · ${d.war_type}` : ""}${d.declared_by != null ? ` · declared by ${pname(d.declared_by, true)}` : ""}`);
  else if (e.kind === "settlement_captured") bits.push(`by ${pname(d.captured_by, true)} from ${pname(d.from_player, true)}`);
  else if (e.players.length && !e.local) bits.push(pname(e.players[0], true));
  if (d.city) bits.push(d.city);
  if (d.category && e.kind !== "policy") bits.push(d.category);
  if (d.completed === false) bits.push("not completed");
  if (e.end_g == null && INTERVAL_KINDS.has(e.kind)) bits.push("ongoing");
  return h("div", { class: "ev" }, h("b", {}, barLabel(e)), h("div", {}, bits.filter(Boolean).join(" · ")));
}

let _resizeFn = null, _resizeT = null;
function onResize(fn) { _resizeFn = fn; }
window.addEventListener("resize", () => { clearTimeout(_resizeT); _resizeT = setTimeout(() => { if (_resizeFn && document.body.contains($("#view").firstChild)) _resizeFn(); }, 150); });

// ------------------------------------------------------------------ charts
async function viewCharts() {
  const c = S.chart, cat = S.meta.catalog || [];
  const d = await api(`/api/metrics?metric=${encodeURIComponent(c.metric)}&vis=${c.vis}`);
  const groups = {};
  for (const m of cat) (groups[m.victory || "other"] = groups[m.victory || "other"] || []).push(m);
  const sel = h("select", { onchange: ev => { c.metric = ev.target.value; refresh(); } },
    Object.entries(groups).map(([gname, ms]) => h("optgroup", { label: gname[0].toUpperCase() + gname.slice(1) },
      ms.map(m => h("option", { value: m.metric, selected: m.metric === c.metric }, m.label)))));
  const players = Object.values(S.players);
  const series = players.map(p => ({ id: p.player_id, name: pname(p.player_id), color: pcolor(p.player_id), points: d.series[String(p.player_id)] || [] }))
    .filter(sr => sr.points.length);
  const hiddenOmni = Object.values(d.omniscient_hidden || {}).reduce((a, b) => a + b, 0);
  const legend = h("div", { class: "legend" }, series.map(sr => h("button", {
    class: c.hidden.has(sr.id) ? "off" : "", title: "Click to hide/show; double-click to isolate",
    onclick: () => { c.hidden.has(sr.id) ? c.hidden.delete(sr.id) : c.hidden.add(sr.id); refresh(); },
    ondblclick: () => { c.hidden = new Set(series.map(x => x.id).filter(id => id !== sr.id)); refresh(); },
  }, lineKey(sr.color), sr.name)),
    h("button", { onclick: () => { c.hidden = new Set(); refresh(); } }, "Show all"),
    h("button", { onclick: () => { c.hidden = new Set(series.map(x => x.id).filter(id => id !== S.local)); refresh(); } }, "Only you"));
  const box = h("div", { class: "chart" });
  const shown = series.filter(sr => !c.hidden.has(sr.id));
  const draw = () => lineChart(box, shown, xDomainFor(c.range), d.catalog);
  requestAnimationFrame(draw);
  onResize(draw);
  const table = c.table ? metricTable(shown) : null;
  return h("div", {},
    h("div", { class: "controls" }, sel,
      h("div", { class: "seg", title: "Omniscient values come from game logs and cover rivals you could not see" },
        [["visible", "As seen on screen"], ["all", "Include omniscient"]].map(([k, l]) => h("button", { class: c.vis === k ? "on" : "", onclick: () => { c.vis = k; refresh(); } }, l))),
      h("div", { class: "seg" }, [["all", "Whole game"], ...S.meta.ages.map(a => [a.age, ageName(a.age)])].map(([k, l]) =>
        h("button", { class: c.range === k ? "on" : "", onclick: () => { c.range = k; refresh(); } }, l))),
      h("span", { class: "sp" }),
      h("button", { class: "iconbtn", onclick: () => { c.table = !c.table; refresh(); } }, c.table ? "Hide table" : "Show table")),
    card({},
      h("div", { class: "cardhead" }, h("h2", {}, d.catalog?.label || c.metric), h("span", { class: "sec small" }, d.catalog?.unit ? `(${d.catalog.unit.replace("_", " ")})` : ""),
        h("span", { class: "sp" }), c.vis === "visible" && hiddenOmni ? h("span", { class: "hint" }, `${hiddenOmni.toLocaleString()} omniscient rival values hidden`) : null),
      d.catalog?.description ? h("div", { class: "small sec", style: "margin:-6px 0 6px" }, d.catalog.description) : null,
      series.length > 1 ? legend : null, box,
      h("div", { class: "hint", style: "margin-top:6px" }, "One color per player for the whole game. Click the chart to open that turn.")),
    table ? card({ style: "margin-top:14px" }, table) : null);
}

function bisect(points, g) {  // nearest index by g
  let lo = 0, hi = points.length - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (points[m][0] < g) lo = m + 1; else hi = m; }
  if (lo > 0 && Math.abs(points[lo - 1][0] - g) <= Math.abs(points[lo][0] - g)) lo--;
  return lo;
}

function lineChart(box, series, [x0, x1], catalog) {
  const W = Math.max(480, box.clientWidth), H = 340, M = { l: 56, r: 16, t: 26, b: 30 };
  let pts = series.map(sr => ({ ...sr, points: sr.points.filter(p => p[0] >= x0 && p[0] <= x1) })).filter(sr => sr.points.length);
  if (S.chart.range === "all" && pts.length) x0 = Math.max(x0, Math.min(...pts.map(sr => sr.points[0][0])) - 1);  // skip turns with no data
  if (!pts.length) { box.replaceChildren(empty("No values for this metric in the selected range", S.chart.vis === "visible" ? " — try \"Include omniscient\"." : ".")); return; }
  const ys = pts.flatMap(sr => sr.points.map(p => p[1]));
  let lo = Math.min(0, ...ys), hi = Math.max(...ys);
  if (hi === lo) hi = lo + 1;
  const yt = niceTicks(lo, hi, 5);
  lo = Math.min(lo, yt[0]); hi = Math.max(hi, yt[yt.length - 1]);
  const x = linear(x0, x1, M.l, W - M.r), y = linear(lo, hi, H - M.b, M.t);
  const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${catalog?.label || "metric"} by player` });
  const ax = s("g", { class: "ax" });
  for (const v of yt) ax.append(s("line", { class: "gridl", x1: M.l, x2: W - M.r, y1: y(v), y2: y(v) }), s("text", { x: M.l - 8, y: y(v) + 4, "text-anchor": "end" }, fmt(v)));
  ax.append(s("line", { x1: M.l, x2: W - M.r, y1: y(lo), y2: y(lo) }));
  for (const t of gTicks(x0, x1, W - M.l - M.r)) ax.append(s("text", { x: x(t.g), y: H - M.b + 16, "text-anchor": "middle" }, t.label));
  svg.append(ax);
  for (const a of S.meta.ages) {
    const bx = a.g_offset + 0.5;
    if (bx > x0 && bx < x1) svg.append(s("line", { class: "agel", x1: x(bx), x2: x(bx), y1: M.t - 14, y2: H - M.b }));
    const lx = Math.max(x0, a.g_offset + 1);
    if (lx <= x1 && (a.current ? S.meta.game.current_g : a.end_g) >= x0) svg.append(s("text", { class: "agelab", x: x(lx) + 4, y: M.t - 6 }, ageName(a.age)));
  }
  // lines: break where turns are missing (gap > 3) so unobserved stretches are not interpolated
  const order = pts.slice().sort((a, b) => (a.id === S.local) - (b.id === S.local));  // local player drawn on top
  for (const sr of order) {
    let d = "", prev = null;
    for (const p of sr.points) {
      d += `${prev == null || p[0] - prev > 3 ? "M" : "L"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`;
      prev = p[0];
    }
    svg.append(s("path", { d, fill: "none", stroke: sr.color, "stroke-width": sr.id === S.local ? 2.5 : 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    sr.points.forEach((p, i) => {  // isolated points would otherwise be invisible
      const isolated = (i === 0 || p[0] - sr.points[i - 1][0] > 3) && (i === sr.points.length - 1 || sr.points[i + 1][0] - p[0] > 3);
      if (isolated) svg.append(s("circle", { cx: x(p[0]), cy: y(p[1]), r: 2.5, fill: sr.color }));
    });
  }
  // end label for the local player only (labels for every line would collide)
  const mine = pts.find(sr => sr.id === S.local);
  if (mine) {
    const p = mine.points[mine.points.length - 1];
    if (p[0] >= x1 - 2) svg.append(s("circle", { cx: x(p[0]), cy: y(p[1]), r: 4, fill: mine.color, stroke: "var(--surface)", "stroke-width": 2 }));
  }
  const cross = s("line", { class: "cross", y1: M.t, y2: H - M.b, style: "display:none" });
  const dots = s("g", { style: "pointer-events:none" });
  const overlay = s("rect", { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b, fill: "transparent", style: "cursor:crosshair" });
  svg.append(cross, dots, overlay);
  const at = evt => {
    const r = svg.getBoundingClientRect();
    return Math.round(x.inv((evt.clientX - r.left) * (W / r.width)));
  };
  overlay.addEventListener("pointermove", evt => {
    const g = Math.max(x0, Math.min(x1, at(evt)));
    const rows = [];
    dots.replaceChildren();
    for (const sr of pts) {
      const p = sr.points[bisect(sr.points, g)];
      if (!p || Math.abs(p[0] - g) > 2) continue;
      rows.push({ sr, p });
      dots.append(s("circle", { cx: x(p[0]), cy: y(p[1]), r: 4, fill: sr.color, stroke: "var(--surface)", "stroke-width": 2 }));
    }
    setAttrs(cross, { x1: x(g), x2: x(g) }, true);
    cross.style.display = "";
    rows.sort((a, b) => b.p[1] - a.p[1]);
    showTip(evt, h("div", { class: "tt" }, gLabel(g, true)), rows.length ? rows.map(({ sr, p }) => h("div", { class: "tr" }, lineKey(sr.color), h("b", {}, fmt(p[1])),
      h("span", {}, sr.name, p[0] !== g ? ` (${gLabel(p[0])})` : "", p[2] === "o" ? " · omniscient" : ""))) : h("div", { class: "muted" }, "no values this turn"));
  });
  overlay.addEventListener("pointerleave", () => { cross.style.display = "none"; dots.replaceChildren(); hideTip(); });
  overlay.addEventListener("click", evt => go(`#turn/${Math.max(x0, Math.min(x1, at(evt)))}`));
  box.replaceChildren(svg);
}

function metricTable(series) {
  const gs = [...new Set(series.flatMap(sr => sr.points.map(p => p[0])))].sort((a, b) => b - a);
  const idx = series.map(sr => new Map(sr.points.map(p => [p[0], p])));
  return h("div", { class: "scroll" }, h("table", { class: "t" },
    h("tr", {}, h("th", {}, "Turn"), series.map(sr => h("th", { class: "n" }, h("span", { class: "row", style: "justify-content:flex-end;gap:5px" }, lineKey(sr.color), pname(sr.id, true))))),
    gs.map(g => h("tr", {}, h("td", {}, h("a", { href: `#turn/${g}` }, gLabel(g))), idx.map(m => {
      const p = m.get(g);
      return h("td", { class: "n" }, p ? fmt(p[1]) + (p[2] === "o" ? "*" : "") : "");
    })))), h("div", { class: "hint" }, "* omniscient (from game logs; not on your screen)"));
}

// ------------------------------------------------------------------ turn by turn
async function viewTurn() {
  const t = await api(`/api/turn/${S.turn.g ?? "latest"}`);
  const cg = t.current_g, first = S.meta.first_g || 1;
  const setG = g => go(`#turn/${Math.max(first, Math.min(cg, g))}`);
  const slider = h("input", { type: "range", min: first, max: cg, value: t.g, "aria-label": "Turn",
    oninput: ev => { lab.textContent = gLabel(+ev.target.value, true); }, onchange: ev => setG(+ev.target.value) });
  const lab = h("span", { class: "turnlabel" }, gLabel(t.g, true));
  const nav = card({}, h("div", { class: "turnnav" },
    h("button", { class: "iconbtn", onclick: () => setG(t.g - 1), title: "Previous turn" }, "◀"), lab,
    h("button", { class: "iconbtn", onclick: () => setG(t.g + 1), title: "Next turn" }, "▶"), slider,
    h("button", { class: "iconbtn", disabled: t.prev_observed == null, onclick: () => setG(t.prev_observed) }, "◀ snapshot"),
    h("button", { class: "iconbtn", disabled: t.next_observed == null, onclick: () => setG(t.next_observed) }, "snapshot ▶"),
    h("button", { class: "iconbtn", onclick: () => setG(cg) }, "Latest")),
    h("div", { class: "small sec", style: "margin-top:6px" }, t.observed ? `Live snapshot captured ${new Date(t.captured_at).toLocaleString()}`
      : t.summary_g ? `No live snapshot this turn; empire summary below is from ${gLabel(t.summary_g)}.` : "No live snapshot at or before this turn; only logs and captures."));

  const evs = t.events.filter(e => !S.turn.localOnly || e.local || e.kind === "age");
  const exact = evs.filter(e => e.precision === "exact"), fuzzy = evs.filter(e => e.precision !== "exact");
  const groups = {};
  for (const e of exact) (groups[e.kind] = groups[e.kind] || []).push(e);
  const evRowEl = e => h("li", {}, h("div", { class: "row", style: "gap:6px" }, h("span", { class: "kind" }, (e.phase === "end" ? "ended · " : "") + (KIND_LABEL[e.kind] || e.kind)),
    h("b", {}, e.subject), e.players.slice(0, 3).map(p => h("span", { class: "row small sec", style: "gap:4px" }, swatch(p), pname(p, true)))), evDetail(e));
  const events = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Events"), h("span", { class: "sp" }),
    h("div", { class: "seg" }, [["Yours", true], ["Everyone", false]].map(([l, v]) => h("button", { class: S.turn.localOnly === v ? "on" : "", onclick: () => { S.turn.localOnly = v; refresh(); } }, l)))),
    exact.length ? h("ul", { class: "plain" }, Object.values(groups).flat().map(evRowEl)) : empty("Nothing recorded at exactly this turn."),
    fuzzy.length ? h("details", {}, h("summary", {}, `${fuzzy.length} more first seen in an end-of-age capture (exact turn unknown)`), h("ul", { class: "plain" }, fuzzy.map(evRowEl))) : null);

  const deltas = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Your numbers"), h("span", { class: "sp" }), t.prev_g != null ? h("span", { class: "hint" }, `Δ vs ${gLabel(t.prev_g)}`) : null),
    t.deltas.length ? h("table", { class: "t" }, h("tr", {}, h("th", {}, "Metric"), h("th", { class: "n" }, "Value"), h("th", { class: "n" }, "Change")),
      t.deltas.map(d => h("tr", {}, h("td", {}, d.label), h("td", { class: "n" }, fmt(d.value)), h("td", { class: "n" }, d.delta ? deltaSpan(d.delta, d.higher_is_better) : h("span", { class: "muted" }, d.prev == null ? "new" : "–")))))
      : empty("No metrics recorded for this turn."));

  const notes = card({}, h("h2", {}, "Advisor notes"), t.annotations.length ? t.annotations.map(noteEl)
    : empty("No notes for this turn. Notes Claude records while advising (intentions, warnings, advice) show up on the turn they were written."));
  return h("div", {}, nav, h("div", { class: "grid g2 section", style: "align-items:start" }, h("div", { class: "grid" }, summaryCard(t), notes), h("div", { class: "grid" }, events, deltas)));
}

function evDetail(e) {
  const d = e.detail || {}, bits = [];
  if (e.kind === "unit_killed") bits.push(`${pname(d.victim, true)} lost a ${e.subject} to ${pname(d.killer, true)}${d.x != null ? ` at (${d.x}, ${d.y})` : ""}`);
  if (d.city) bits.push(d.city);
  if (d.category && e.kind !== "policy") bits.push(d.category);
  if (e.kind === "policy" && d.category) bits.push(d.category);
  if (d.war_type) bits.push(d.war_type);
  if (d.declared_by != null) bits.push(`declared by ${pname(d.declared_by, true)}`);
  if (e.kind === "settlement_captured") bits.push(`captured by ${pname(d.captured_by, true)} from ${pname(d.from_player, true)}`);
  if (e.end_g != null && e.end_g !== e.start_g) bits.push(gRange(e.start_g, e.end_g));
  else if (e.end_g == null && INTERVAL_KINDS.has(e.kind)) bits.push(`since ${gLabel(e.start_g)}, ongoing`);
  if (e.precision === "range") bits.push(`between T${e.start_lo} and T${e.start_hi}`);
  return bits.length ? h("div", { class: "small sec" }, bits.join(" · ")) : null;
}

function summaryCard(t) {
  const sm = t.summary;
  if (!sm) return card({}, h("h2", {}, "Empire"), empty("No live snapshot covers this turn."));
  const prog = r => r ? h("div", {}, h("div", { class: "row small" }, h("b", {}, r.name), h("span", { class: "sp" }),
    h("span", { class: "sec" }, `${fmt(r.progress)} / ${fmt(r.cost)}${r.turns_left != null ? ` · ${r.turns_left} turns` : ""}`)), meter(r.cost ? r.progress / r.cost : 0)) : h("span", { class: "muted" }, "—");
  const chips = xs => xs?.length ? h("div", { class: "tagline", style: "margin-top:0" }, xs.map(x => h("span", { class: "chip" }, x))) : h("span", { class: "muted" }, "—");
  const st = sm.settlements || [];
  return card({}, h("div", { class: "cardhead" }, h("h2", {}, "Empire"), h("span", { class: "sec small" }, t.summary_g !== t.g ? `as of ${gLabel(t.summary_g)}` : "")),
    h("dl", { class: "kv" },
      h("dt", {}, "Researching"), h("dd", {}, prog(sm.researching)),
      h("dt", {}, "Civic"), h("dd", {}, prog(sm.civic)),
      h("dt", {}, "Government"), h("dd", {}, sm.government || "—", sm.ideology ? ` · ${pretty(sm.ideology)}` : ""),
      h("dt", {}, "Policies"), h("dd", {}, chips(sm.policies)),
      h("dt", {}, "Traditions"), h("dd", {}, chips(sm.traditions)),
      sm.crisis_policies?.length ? [h("dt", {}, "Crisis"), h("dd", {}, chips(sm.crisis_policies))] : null,
      sm.religion ? [h("dt", {}, "Religion"), h("dd", {}, sm.religion.name || "—")] : null,
      sm.golden_age?.turns_left ? [h("dt", {}, "Golden age"), h("dd", {}, `${sm.golden_age.turns_left} turns left`)] : null,
      h("dt", {}, "At war with"), h("dd", {}, sm.at_war_with?.length ? sm.at_war_with.map((p, i) => [i ? ", " : "", pname(p, true)]) : "nobody"),
      sm.age_progress ? [h("dt", {}, "Age progress"), h("dd", {}, `${sm.age_progress.current} / ${sm.age_progress.max}`)] : null,
      sm.notifications_unit_lost?.length ? [h("dt", {}, "Units lost"), h("dd", {}, sm.notifications_unit_lost.map(n => h("div", { class: "small" }, n)))] : null),
    st.length ? h("details", { open: true }, h("summary", {}, `${st.length} settlements · ${st.filter(x => x.type === "city").length} cities`),
      h("table", { class: "t" }, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Type"), h("th", { class: "n" }, "Pop")),
        st.slice().sort((a, b) => b.population - a.population).map(x => h("tr", {}, h("td", {}, x.name, x.capital ? " ★" : ""), h("td", { class: "sec" }, x.type), h("td", { class: "n" }, x.population))))) : null);
}

// ------------------------------------------------------------------ advisor
async function viewAdvisor() {
  const a = await api("/api/advisor");
  const voc = a.vocab || {};
  const ap = a.advisor_profile, pp = a.player_profile;
  const histList = (rows, fmtRow) => rows.length > 1 ? h("details", {}, h("summary", {}, `${rows.length - 1} earlier version${rows.length > 2 ? "s" : ""}`),
    h("ul", { class: "plain" }, rows.slice(1).map(r => h("li", { class: "small" }, h("b", {}, `v${r.version}`), ` from ${gLabel(r.valid_from_g)} · `, fmtRow(r))))) : null;
  const advisorCard = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Advisor personality"), h("span", { class: "sp" }), ap ? h("span", { class: "chip" }, `v${ap.version} since ${gLabel(ap.valid_from_g)}`) : null),
    ap ? h("div", {}, h("div", { class: "leader" }, pretty(ap.personality)), h("div", { class: "sec small" }, voc.personality?.[ap.personality] || ""),
      ap.voice ? h("p", {}, ap.voice) : null,
      h("div", { class: "tagline" }, (ap.priorities || []).map((p, i) => h("span", { class: "chip", title: voc.advisor_priority?.[p] || "" }, h("b", {}, i + 1), " ", p.replace(/_/g, " ")))),
      ap.quirks ? h("p", { class: "small sec" }, ap.quirks) : null,
      histList(a.advisor_history, r => `${pretty(r.personality)} (${(r.priorities || []).join(", ")})`))
      : empty(h("b", {}, "No advisor personality yet. "), "Ask Claude to pick one — e.g. \"be a sarcastic advisor\" or \"talk like a terse general\". Options: ",
        Object.keys(voc.personality || {}).join(", "), "."));
  const playerCard = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Your playstyle"), h("span", { class: "sp" }), pp ? h("span", { class: "chip" }, `v${pp.version} since ${gLabel(pp.valid_from_g)}`) : null),
    pp ? h("div", {}, h("div", { class: "tagline", style: "margin-top:0" }, (pp.playstyle || []).map(p => h("span", { class: "chip", title: voc.playstyle?.[p] || "" }, h("b", {}, p)))),
      h("div", { class: "small sec", style: "margin-top:6px" }, `Risk tolerance: ${pp.risk_tolerance}`), pp.notes ? h("p", {}, pp.notes) : null,
      histList(a.player_history, r => `${(r.playstyle || []).join(", ")} · risk ${r.risk_tolerance}`))
      : empty(h("b", {}, "No player profile yet. "), "Tell Claude how you like to play (aggressive, builder, whimsical…) and it will weigh its advice accordingly."));

  const plan = a.plan || [];
  const planEl = h("div", { class: "grid" }, a.plan_error ? h("div", { class: "error" }, "Goal evaluation failed: ", a.plan_error) : null,
    plan.length ? plan.map(strategyCard) : empty(h("b", {}, "No strategy or goals yet. "), "Tell Claude what you are going for (\"economic victory\", \"take Paris by Modern turn 100\"); it records a strategy, long-term goals, and short-term steps linked to metrics, and their progress shows here."));

  const kinds = [...new Set(a.annotations.map(n => n.kind))];
  const shown = a.annotations.filter(n => !S.notes.kind || n.kind === S.notes.kind);
  const log = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Annotations"), h("span", { class: "sp" }),
    kinds.length > 1 ? h("div", { class: "seg" }, [["", "All"], ...kinds.map(k => [k, k.replace("_", " ")])].map(([k, l]) =>
      h("button", { class: S.notes.kind === k ? "on" : "", onclick: () => { S.notes.kind = k; refresh(); } }, l))) : null),
    shown.length ? h("div", { class: "scroll", style: "max-height:600px" }, h("table", { class: "t" },
      h("tr", {}, h("th", {}, "Turn"), h("th", {}, "Kind"), h("th", {}, "Note"), h("th", {}, "About")),
      shown.map(n => h("tr", {}, h("td", { style: "white-space:nowrap" }, h("a", { href: `#turn/${n.g}` }, gLabel(n.g)), h("div", { class: "small muted" }, n.author)),
        h("td", {}, h("span", { class: "kind" }, n.kind.replace("_", " ")), n.verdict ? h("div", { style: "margin-top:3px" }, badge(n.verdict)) : null),
        h("td", { style: "white-space:pre-wrap" }, n.text),
        h("td", { class: "small sec" }, n.scope ? `${n.scope} ${n.scope === "age" ? ageName(n.scope_key) : n.scope_key}` : n.goal_id ? `goal ${n.goal_id}` : n.strategy_id ? `strategy ${n.strategy_id}` : "")))))
      : empty("No annotations yet. Claude writes them as it advises: your intentions, priority changes, its warnings and advice, and its narrative of how each phase went."));
  return h("div", {}, h("div", { class: "grid g2" }, advisorCard, playerCard),
    h("div", { class: "section" }, h("h2", {}, "Plan"), planEl), h("div", { class: "section" }, log));
}

function strategyCard(st) {
  return card({}, h("div", { class: "cardhead" }, h("h2", {}, st.name), h("span", { class: "status" }, st.status), h("span", { class: "sp" }),
    (st.victory_types || []).map(v => h("span", { class: "chip" }, v)),
    st.started_g ? h("span", { class: "small muted" }, `since ${gLabel(st.started_g)}${st.ended_g ? ` until ${gLabel(st.ended_g)}` : ""}`) : null),
    st.description ? h("p", { class: "sec", style: "margin-top:0" }, st.description) : null,
    (st.goals || []).length ? st.goals.map(goalEl) : empty("No goals under this strategy yet."));
}
function goalEl(g) {
  const p = g.progress;
  return h("div", { class: `goal ${g.horizon === "short" ? "short" : ""}` },
    h("div", { class: "gt" }, h("b", {}, g.title), h("span", { class: "status" }, `${g.horizon}-term · ${g.status} · P${g.priority}`),
      g.deadline_g ? h("span", { class: "small muted" }, `by ${gLabel(g.deadline_g)}`) : null, g.overdue ? h("span", { class: "warnline small" }, "overdue") : null,
      h("span", { class: "sp" }), h("span", { class: "small" }, pct(p))),
    h("div", { style: "max-width:520px;margin-top:4px" }, meter(p, p >= 1 ? "met" : g.overdue ? "over" : "")),
    g.description ? h("div", { class: "small sec", style: "margin-top:4px" }, g.description) : null,
    g.resolution ? h("div", { class: "small", style: "margin-top:4px" }, `Resolution: ${g.resolution}`) : null,
    (g.metrics || []).length ? h("div", { class: "gm" }, g.metrics.map(m => [
      h("span", {}, m.label, m.player_id !== S.local ? h("span", { class: "muted" }, ` (${pname(m.player_id, true)})`) : null),
      h("span", { class: "num sec" }, `${m.comparator === "rank<=" ? `rank ${fmt(m.current)}${m.of ? `/${m.of}` : ""}` : fmt(m.current)} · target ${m.comparator} ${fmt(m.target)}${m.baseline != null && m.comparator.startsWith("gain") ? ` (from ${fmt(m.baseline)})` : ""}`),
      h("span", {}, m.met ? h("span", { class: "small" }, "✓ met") : meter(m.progress))])) : h("div", { class: "small muted", style: "margin-top:4px" }, "No linked metrics — progress is judged by the advisor."),
    (g.children || []).map(goalEl));
}

// ------------------------------------------------------------------ boot
function initTheme() {
  const btn = $("#theme"), order = ["", "light", "dark"];
  const label = () => { btn.textContent = { "": "◐ System", light: "☀ Light", dark: "☾ Dark" }[document.documentElement.dataset.theme || ""]; };
  label();
  btn.addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme || "", next = order[(order.indexOf(cur) + 1) % order.length];
    if (next) document.documentElement.dataset.theme = next; else delete document.documentElement.dataset.theme;
    try { next ? localStorage.setItem("civilizer-theme", next) : localStorage.removeItem("civilizer-theme"); } catch (e) {}
    label();
    if (_resizeFn) _resizeFn();
  });
}
window.addEventListener("hashchange", () => { parseHash(); _resizeFn = null; refresh(); });
initTheme();
parseHash();
refresh().then(poll);
setInterval(poll, 10000);
