// Civilizer dashboard: vanilla JS on the Foolscap design system. Figures are Observable Plot specs rendered
// through Foolscap's fcChart (foolscap/foolscap-plot.js), which themes them from the --fc-* tokens and
// re-renders them on resize and on the paper/blueprint switch. Reads the JSON API in dashboard.py and polls
// /api/version every 10 s; when the token changes (the monitor appended a turn, or the advisor wrote a note)
// the current view refetches while holding its previous render at reduced opacity.
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

// Rough width of mono caption text, for deciding whether a label fits inside a bar.
const textW = (str, px = 11) => String(str).length * px * 0.6;

// ------------------------------------------------------------------ state
const S = {
  tab: "home", game: new URLSearchParams(location.search).get("game"), token: null, meta: null, players: {}, local: 0,
  chart: { metric: "gold_per_turn", vis: "visible", hidden: new Set(), range: "all", table: false },
  tl: { local: true, domain: null, off: new Set() },
  turn: { g: null, localOnly: true },
  notes: { kind: "" },
};

// Every game endpoint takes ?game=<seed>; without it the server picks the most recently refreshed game.
const withGame = path => S.game && path !== "/api/games" ? path + (path.includes("?") ? "&" : "?") + "game=" + encodeURIComponent(S.game) : path;
const gameHref = (game, tab = "overview") => `/?game=${encodeURIComponent(game)}#${tab}`;
async function api(path) {
  path = withGame(path);
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

const MAJORS = 12;  // one chart colour per major player id 0..11 (--fc-color-chart-1..12)
const isMajor = id => id != null && id >= 0 && id < MAJORS && S.players[id] != null;
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
// Plot needs resolved colours rather than var() references: take them from fcTokens().
const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const pcolorT = (id, t) => isMajor(id) ? t.series[id] : cssVar("--fc-color-text-faint");
const swatch = id => h("span", { class: "sw", style: `background:${pcolor(id)}` });
const lineKey = color => h("span", { class: "lk", style: `background:${color}` });

const OUTCOME = {
  success: ["✓", "Success"], partial: ["◐", "Partial"], failure: ["✕", "Failure"],
  changed_plans: ["↷", "Changed plans"], ongoing: ["…", "Ongoing"],
};
function badge(v, source) {
  if (!v) return null;
  const [ic, label] = OUTCOME[v] || ["?", v];
  return h("span", { class: `badge v-${v}`, title: source ? `Outcome source: ${source}` : null },
    h("span", { class: "ic" }, ic), label, source ? h("span", { class: "src" }, source === "advisor" ? "· advisor" : "· heuristic") : null);
}
// Game outcome (dashboard._outcome): "over" once any player has won a victory, else in progress. "unknown" means no
// snapshot has captured victories yet; nobody is known to have won, so it reads as in progress.
const victoryName = v => ({ VICTORY_SCORE: "Score", VICTORY_DOMINATION: "Domination" })[v]
  || pretty(String(v || "").replace(/^VICTORY_|_MODERN$|^MODERN_/g, "")) + " victory";
function outcomeText(o) {
  if (o?.status !== "over") return o?.status === "in_progress" ? "In progress" : "In progress · no victory recorded yet";
  const wins = o.victories.map(v => `${v.players.map(p => p.leader || `player ${p.id}`).join(" & ") || "Unknown"} won the ${victoryName(v.victory)}`);
  return `Game over · ${wins.join("; ")}`;
}
const ICON = {  // 16px stroke icons in the ink of their context: a pennant for a finished game, an hourglass for one in play
  over: "M4 15V1.5M4 2h8.5l-2.2 3 2.2 3H4",
  in_progress: "M4 1.5h8M4 14.5h8M5 1.5c0 4 6 3.5 6 6.5s-6 2.5-6 6.5M11 1.5c0 4-6 3.5-6 6.5s6 2.5 6 6.5",
};
function statusIcon(o) {
  const over = o?.status === "over", text = outcomeText(o);
  return h("span", { class: `status-ic ${over ? "over" : "live"}`, title: text, role: "img", "aria-label": text },
    s("svg", { viewBox: "0 0 16 16", width: 16, height: 16, "aria-hidden": "true" },
      s("path", { d: ICON[over ? "over" : "in_progress"], fill: "none", stroke: "currentColor", "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" })));
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
// Plot x scale over the game-wide turn index g, ticked and labelled by turn within each age.
function gScale(x0, x1, widthPx, extra) {
  return Object.assign({ domain: [x0, x1], ticks: gTicks(x0, x1, widthPx).map(tk => tk.g), label: null,
    tickFormat: g => { const a = ageAt(g); return a ? `T${g - a.g_offset}` : `g${g}`; } }, extra);
}
const ageBounds = (x0, x1) => S.meta.ages.map(a => a.g_offset + 0.5).filter(b => b > x0 && b < x1);
function ageStarts(x0, x1) {
  const cg = S.meta.game.current_g;
  return S.meta.ages.filter(a => Math.max(x0, a.g_offset + 1) <= x1 && (a.current ? cg : a.end_g) >= x0)
    .map(a => ({ g: Math.max(x0, a.g_offset + 1), label: ageName(a.age) }));
}
// Figures are numbered per view, Foolscap style: "Fig. 2 — ...".
let figNo = 0;
const figure = (el, ...caption) => h("figure", { class: "fc-figure" }, el,
  h("figcaption", { class: "fc-figcaption" }, h("span", { class: "fc-figno" }, `Fig. ${++figNo}`), ...caption));
// Where the pointer is on a rendered Plot svg, in its own coordinates.
function svgPoint(svg, evt) {
  const r = svg.getBoundingClientRect(), w = +svg.getAttribute("width"), ht = +svg.getAttribute("height");
  return { px: (evt.clientX - r.left) * (w / r.width), py: (evt.clientY - r.top) * (ht / r.height) };
}
const svgEl = (tag, attrs) => s(tag, attrs);

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
  if (S.tab === "home") { gl.textContent = ""; renderMonitor(m?.monitor); return; }
  if (!m || !m.game) { gl.textContent = `Game ${S.game} is not in the store.`; return; }
  const g = m.game, me = S.players[S.local];
  gl.replaceChildren(statusIcon(g.outcome), h("b", {}, me ? me.leader : pretty(g.leader)),
    h("span", { class: "wide" }, ` · ${pciv(S.local) || pretty(g.civilization)} · ${pretty(g.difficulty)}`), " · ",
    `${ageName(g.current_age)} T${g.current_turn}`, h("span", { class: "wide" }, g.age_turns_max ? `/${g.age_turns_max}` : ""));
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
  // the header shows only the dot; the words live in the tooltip
  el.replaceChildren(h("span", { class: "dot", style: `background:${color}` }));
  el.title = [text, `state: ${st}`, mon.pid ? `pid ${mon.pid}` : null, mon.last_g ? `last turn ${gLabel(mon.last_g)}` : null,
    mon.interval ? `poll every ${mon.interval}s` : null, mon.last_error ? `error: ${mon.last_error}` : null].filter(Boolean).join("\n");
  el.setAttribute("aria-label", text);
  const at = mon.last_refresh?.at || S.meta?.game?.updated_at;
  const sec = at ? sinceIso(at) : null;
  rf.textContent = sec == null ? "" : sec < 3600 ? "<1h" : `${Math.floor(sec / 3600)}h`;
  rf.title = sec == null ? "" : `Store refreshed ${ago(sec)}`;
  rf.hidden = sec == null;
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
    el.replaceChildren(h("span", { class: "dot", style: "background:var(--critical)" }));
    el.title = "Dashboard server unreachable";
    el.setAttribute("aria-label", el.title);
  }
}

// ------------------------------------------------------------------ router
const VIEWS = { home: viewHome, overview: viewOverview, story: viewStory, timeline: viewTimeline, charts: viewCharts, turn: viewTurn, advisor: viewAdvisor };
// Two levels: the Games index (no ?game=) and one game's analyses (?game=<seed>#overview, #story, ...).
function parseHash() {
  const [tab, arg] = (location.hash.replace(/^#/, "") || (S.game ? "overview" : "home")).split("/");
  S.tab = S.game && VIEWS[tab] ? tab : "home";
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
    $("#tabs").hidden = $("#crumb").hidden = S.tab === "home";
    if (!S.meta.game && S.tab !== "home") {
      main.replaceChildren(S.game
        ? empty(h("b", {}, `Game ${S.game} is not in the store. `), h("a", { href: "/#home" }, "Pick a game"), ".")
        : empty(h("b", {}, "The store has no game yet. "), h("a", { href: "/#home" }, "See how to start one"), "; this page updates by itself."));
      return;
    }
    figNo = 0;
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

// ------------------------------------------------------------------ home
async function viewHome() {
  const d = await api("/api/games");
  const mon = d.monitor || {};
  const list = d.games.length
    ? h("ol", { class: "gamelist" }, d.games.map(gameRow))
    : empty(h("b", {}, "No games recorded yet. "), "Follow the steps below; a game shows up here after its first turn.");
  return h("div", {},
    h("div", { class: "cardhead pagehead" }, h("h1", {}, "Games"), h("span", { class: "lab muted" }, `${d.games.length} recorded · pick one to analyse`)),
    list,
    h("div", { class: "section" }, newGameCard(mon)));
}
function gameRow(g) {
  const civs = (g.ages || []).map(a => (g.civilizations || {})[a]).filter((c, i, xs) => c && c !== xs[i - 1]);
  return h("li", {}, h("a", { class: "gamerow", href: gameHref(g.game) },
    statusIcon(g.outcome),
    h("div", { class: "gr-body" },
      h("div", { class: "gr-title" }, h("span", { class: "gr-leader" }, g.leader_name || pretty(g.leader)),
        h("span", { class: "sec" }, civs.length ? civs.join(" → ") : pretty(g.civilization)),
        g.watching ? h("span", { class: "pill" }, h("span", { class: "dot", style: "background:var(--good)" }), "Monitor live") : null),
      h("div", { class: "sec" }, outcomeText(g.outcome), ` · ${ageName(g.current_age)} turn ${g.current_turn ?? "—"}${g.age_turns_max ? ` of ${g.age_turns_max}` : ""}`,
        ` · ${g.turns_observed} turn${g.turns_observed === 1 ? "" : "s"} recorded`),
      h("div", { class: "small muted" }, [pretty(g.difficulty), g.map, pretty(g.map_size), pretty(g.speed), `seed ${g.game}`, `updated ${ago(sinceIso(g.updated_at))}`]
        .filter(Boolean).join(" · "))),
    h("span", { class: "gr-go lab", "aria-hidden": "true" }, "Open →")));
}
function newGameCard(mon) {
  const live = mon.state === "watching" && !mon.stale, waiting = mon.state === "game_unavailable" && !mon.stale;
  const status = live ? "The monitor is running and the game is reachable. Start or load a game; it shows up in the list after you end a turn."
    : waiting ? "The monitor is running but can't reach the game yet. Launch Civ VII (step 1)."
    : "The monitor isn't running, so no turns are being recorded. Start it with step 2.";
  const cmd = c => h("pre", { class: "cmd" }, h("code", {}, c));
  return card({ class: "card fc-plate" }, h("h2", {}, "Start a new game with Civilizer"),
    h("div", { class: "empty", style: "margin-bottom:12px" }, h("b", {}, live ? "Ready. " : "Not recording. "), status),
    h("ol", { class: "steps" },
      h("li", {}, h("b", {}, "Launch Civ VII. "), "Civilizer reads the game through its UI debugger on port 9444, which is on by default. If ",
        h("code", {}, "AppOptions.txt"), " has an uncommented ", h("code", {}, "UIDebugger 0"), ", set it to 1 and restart the game."),
      h("li", {}, h("b", {}, "Start Civilizer. "), "In a terminal, run:", cmd("cd ~/src/civilizer && uv run civilizer up"),
        "This serves this dashboard and starts the monitor. The monitor waits at the main menu. If a monitor or dashboard is already running, ",
        h("code", {}, "up"), " reuses it."),
      h("li", {}, h("b", {}, "Start the game. "), "Pick New Game in Civ VII. The game appears in the list after you end your first turn. ",
        "Until the first autosave, leader and difficulty come from the live game and the map name is unknown."),
      h("li", {}, h("b", {}, "Talk to the advisor (optional). "), "Open Claude Code in the repo:", cmd("cd ~/src/civilizer && claude"),
        "Tell it how you like to play and what you're going for. It records a profile, strategy and goals, and those appear on the Advisor tab.")),
    h("p", { class: "small muted", style: "margin-top:10px" }, "If you played without the monitor, run ", h("code", {}, "uv run civilizer archive-logs"),
      " before you relaunch Civ VII, because the game wipes its logs at launch. ", h("code", {}, "uv run civilizer refresh"),
      " rebuilds the store from what's archived."));
}

// ------------------------------------------------------------------ overview
async function viewOverview() {
  const o = await api("/api/overview");
  const g = S.meta.game, me = S.players[S.local] || {};
  const ages = S.meta.ages;
  const cur = ages.find(a => a.current) || ages[ages.length - 1];

  const civs = ages.map(a => h("span", { class: "fc-chip chip" }, ageName(a.age), " ", h("b", {}, (me.civilizations || {})[a.age] || "?")));
  const character = card({},
    h("div", { class: "row" }, swatch(S.local), h("span", { class: "leader" }, me.leader || pretty(g.leader))),
    h("div", { class: "sec", style: "margin-top:2px" }, [pciv(S.local), pretty(g.difficulty), g.map, pretty(g.map_size), pretty(g.speed)].filter(Boolean).join(" · ")),
    h("div", { class: "civpath" }, civs),
    h("div", { class: "small muted", style: "margin-top:8px" }, `Rivals: `, Object.values(S.players).filter(p => p.player_id !== S.local)
      .map((p, i) => [i ? ", " : "", p.leader, ` (${pciv(p.player_id)})`])),
    h("div", { class: "small muted", style: "margin-top:4px" }, `Build ${g.app_version || "?"} · game ${g.game}`));

  const totalMax = ages.reduce((n, a) => n + (a.turns_max || a.turns), 0);
  const progress = card({ class: "card fc-plate" },  // the one plated element on this view
    h("div", { class: "row outcome" }, statusIcon(g.outcome), h("span", { class: "lab" }, outcomeText(g.outcome))),
    h("div", { class: "sec small" }, `${ageName(g.current_age)} age`),
    h("div", { class: "row", style: "align-items:baseline;gap:8px" }, h("span", { class: "herofig" }, `Turn ${g.current_turn}`),
      g.age_turns_max ? h("span", { class: "sec" }, `of ${g.age_turns_max} · ${pct(g.current_turn / g.age_turns_max)} through the age`) : null),
    h("div", { class: "agebar", title: "Ages laid end to end; filled = played" }, ages.map(a => {
      const w = (a.turns_max || a.turns) / totalMax * 100, played = a.current ? (g.current_turn / (a.turns_max || 1)) : 1;
      return h("div", { style: `width:${w}%` }, h("i", { style: `width:${played * 100}%` }));
    })),
    h("div", { class: "agelabels" }, ages.map(a => h("div", { style: `width:${(a.turns_max || a.turns) / totalMax * 100}%` },
      `${ageName(a.age)} ${a.current ? `${g.current_turn}/${a.turns_max}` : a.turns}`))),
    o.blurb ? h("p", { class: "blurb", style: "margin:12px 0 0" }, o.blurb) : null,
    h("div", { class: "tagline", style: "margin-top:12px" }, o.phases.filter(p => p.scope === "game" || p.scope === "age").map(p =>
      h("a", { href: "#story", style: "text-decoration:none" }, h("span", { class: "fc-chip chip" }, p.scope === "game" ? "Game" : ageName(p.scope_key)),
        " ", badge(p.outcome, p.outcome_source === "advisor" ? "advisor" : null)))));

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
        st.rank ? h("span", { class: "fc-chip chip" }, h("b", {}, `#${st.rank}`), ` of ${st.of}`) : null),
      st.target ? h("div", { style: "margin-bottom:8px" },
        h("div", { class: "row small" }, h("span", {}, h("b", {}, fmt(st.value)), ` / ${fmt(st.target)} to win`), h("span", { class: "sp" }), h("span", { class: "sec" }, pct(st.value / st.target))),
        meter(st.value / st.target, st.value >= st.target ? "met" : "")) : null,
      leader && leader.player_id !== S.local ? h("div", { class: "small sec", style: "margin-bottom:6px" }, `Leader: ${pname(leader.player_id)} with ${fmt(leader.value)}`)
        : h("div", { class: "small sec", style: "margin-bottom:6px" }, "You lead this path"),
      standingsBars(st, max));
  }));

  const majorWars = o.wars.filter(w => w.major), minorWars = o.wars.filter(w => !w.major);
  const warRow = w => h("li", {}, h("div", { class: "row" }, swatch(w.opponent), h("b", {}, pname(w.opponent)), w.major ? h("span", { class: "sec small" }, pciv(w.opponent)) : null,
    h("span", { class: "sp" }), h("span", { class: "small" }, `kills ${w.units_killed} · losses ${w.units_lost}${w.war_support != null ? ` · support ${fmtSigned(w.war_support)}` : ""}`)),
    h("div", { class: "small sec" }, `${w.war_type || "War"} since ${gLabel(w.since_g)} (${w.turns} turns) · declared by ${w.declared_by === S.local ? "you" : pname(w.declared_by)}`));
  const wars = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Wars"), h("span", { class: "sp" }), h("span", { class: "fc-chip chip" }, `${majorWars.length} major · ${minorWars.length} independent`)),
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

function standingsBars(st, max) {
  const el = h("div", { class: "chart" });
  const short = n => n.length > 18 ? n.slice(0, 17) + "…" : n;
  const rows = st.rows.map(r => ({ ...r, name: short(pname(r.player_id, true)) + (r.player_id === S.local ? " ◂" : "") }));
  fcChart(el, t => ({
    height: rows.length * 20 + 24, marginLeft: 140, marginRight: 48, marginTop: 4, marginBottom: 20,
    color: { type: "identity" },
    x: { domain: [0, max], ticks: 3, tickFormat: fmt, label: null },
    y: { domain: rows.map(r => r.name), label: null, grid: false, tickSize: 0 },
    marks: [
      Plot.barX(rows, { x: "value", y: "name", fill: r => pcolorT(r.player_id, t), insetTop: 4, insetBottom: 4 }),
      st.target ? Plot.ruleX([st.target], { stroke: t.ink, strokeDasharray: "3,2" }) : null,
      Plot.text(rows, { x: "value", y: "name", text: r => fmt(r.value), textAnchor: "start", dx: 4, fill: t.axis }),
      Plot.tip(rows, Plot.pointerY({ x: "value", y: "name",
        title: r => `${pname(r.player_id)} · ${pciv(r.player_id)}\n${fmt(r.value)}${st.target ? ` of ${fmt(st.target)} to win` : ""}` })),
    ],
  }));
  return el;
}

function phaseSummaryCard(phases) {
  const wars = phases.filter(p => p.scope === "war"), goals = phases.filter(p => p.scope === "goal");
  return card({}, h("div", { class: "cardhead" }, h("h2", {}, "Phases & initiatives"), h("span", { class: "sp" }), h("a", { href: "#story", class: "small" }, "Read the story →")),
    h("ul", { class: "plain" }, [...phases.filter(p => p.scope !== "goal" || goals.length), ].map(p => h("li", {}, h("div", { class: "row" },
      h("span", {}, phaseTitle(p)), h("span", { class: "sp" }), h("span", { class: "small muted" }, gRange(p.g_start, p.g_end)), badge(p.outcome))))),
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

function sparkline(pts) {
  if (!pts || pts.length < 2) return null;
  const el = h("div", { class: "spark", "aria-hidden": "true" }), last = pts[pts.length - 1];
  fcChart(el, t => ({
    width: 84, height: 26, margin: 4, axis: null, x: { grid: false }, y: { grid: false },
    marks: [Plot.lineY(pts, { x: d => d[0], y: d => d[1], stroke: t.axis, strokeWidth: 1.5 }),
      Plot.dot([last], { x: d => d[0], y: d => d[1], r: 2.5, fill: t.ink })],
  }));
  return el;
}

// ------------------------------------------------------------------ story
async function viewStory() {
  const d = await api("/api/phases");
  const ps = d.phases;
  const cards = sc => ps.filter(p => p.scope === sc).map(phaseCard);
  return h("div", {},
    h("div", { class: "cardhead" }, h("h2", {}, "The story so far"), h("span", { class: "hint" },
      "Facts are computed from the store; narratives and final outcomes are written by the advisor (Claude) as annotations.")),
    h("div", { class: "grid" }, cards("game")),
    h("div", { class: "section" }, h("h2", {}, "Ages"), h("div", { class: "grid g3" }, cards("age"))),
    h("div", { class: "section" }, h("h2", {}, "Wars"), ps.some(p => p.scope === "war") ? h("div", { class: "grid g3" }, cards("war"))
      : empty("No wars with major civilizations so far.")),
    h("div", { class: "section" }, h("h2", {}, "Goals"), ps.some(p => p.scope === "goal") ? h("div", { class: "grid g3" }, cards("goal"))
      : empty(h("b", {}, "No goals yet. "), "Tell Claude what you are aiming for (\"economic victory by Modern turn 120\"); it records goals with ",
        h("code", {}, "add_goal"), " and they appear here with progress and an outcome.")),
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
      badge(p.outcome, p.outcome_source)),
    p.outcome_source === "advisor" && p.heuristic_outcome !== p.outcome ? h("div", { class: "small muted" }, `Heuristic said: ${OUTCOME[p.heuristic_outcome]?.[1] || p.heuristic_outcome}`) : null,
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
    h("div", { class: "row when" }, h("span", { class: "kind" }, a.kind.replace("_", " ")), `${gLabel(a.g)} · ${a.author}`, a.outcome ? badge(a.outcome) : null),
    a.kind === "narrative" ? narrativeEl(a.text) : h("p", {}, a.text));
}

// A narrative's first paragraph is its title when it is a single short line; "IV. Subtitle (T109–T147)" lines are
// chapter headings (docs/NARRATIVES.md).
function narrativeEl(text) {
  const paras = text.split(/\n\n+/), out = [];
  paras.forEach((t, i) => {
    if (i === 0 && paras.length > 1 && !t.includes("\n") && t.length <= 120) out.push(h("h4", { class: "narr-title" }, t));
    else if (/^[IVXL]+\. .{1,100}$/.test(t)) out.push(h("h5", { class: "narr-chapter" }, t));
    else out.push(h("p", {}, t));
  });
  return h("div", { class: "narrative" }, out);
}

function phaseFacts(p, f) {
  if (p.scope === "game") {
    const st = f.standings_now || {};
    return h("div", {},
      h("div", { class: "tagline" }, Object.entries(f.age_outcomes || {}).map(([a, v]) => h("span", { class: "fc-chip chip" }, ageName(a), " ", badge(v)))),
      standingsChips(st));
  }
  if (p.scope === "age") {
    const m = f.metrics || {};
    const rows = Object.entries(m).filter(([, v]) => v && (v.start != null || v.end != null));
    const cat = Object.fromEntries((S.meta.catalog || []).map(c => [c.metric, c]));
    return h("div", {},
      rows.length ? h("table", { class: "fc-table" }, h("tr", {}, h("th", {}, "Metric"), h("th", { class: "n" }, "Start"), h("th", { class: "n" }, "End"), h("th", { class: "n" }, "Change")),
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
    const cb = f.casus_belli || {}, cs = f.combatants, ws = f.war_support, nb = f.notable || {}, ally = cb.joined_ally_war;
    const who = x => x?.id === S.local ? "you" : x?.name || pname(x?.id);
    const side = s => [...(s.majors || []).map(who), ...(s.city_states || []).map(c => c.name || `#${c.id}`)].join(", ");
    const peaks = (nb.peak_kill_turns || []).map(k => `${k.killed} in ${gLabel(k.g)}`).join(", ");
    return h("dl", { class: "kv" },
      h("dt", {}, "Opponent"), h("dd", {}, h("span", { class: "row", style: "gap:6px" }, swatch(f.opponent), pname(f.opponent), h("span", { class: "muted" }, pciv(f.opponent)))),
      h("dt", {}, "Declared by"), h("dd", {}, f.declared_by === S.local ? "You" : pname(f.declared_by), f.war_type ? ` · ${f.war_type}` : ""),
      ally ? [h("dt", {}, "Ally's war"), h("dd", {}, `${who(ally.ally)} at war with them since ${gLabel(ally.ally_war_start_g)} (declared by ${who(ally.declared_by)})`)] : null,
      cb.run_up?.length ? [h("dt", {}, "Run-up"), h("dd", { class: "small" }, cb.run_up.map(r => `${gLabel(r.g)} ${r.what} (${r.by})`).join(" · "))] : null,
      cs ? [h("dt", {}, "Sides"), h("dd", { class: "small" }, side(cs.us), h("span", { class: "muted" }, " vs "), side(cs.them))] : null,
      h("dt", {}, "Duration"), h("dd", {}, `${f.turns ?? "?"} turns${f.ongoing ? " (ongoing)" : ""}`),
      h("dt", {}, "Combat"), h("dd", {}, `${f.units_killed ?? 0} destroyed · ${f.units_lost ?? 0} lost`, peaks ? h("span", { class: "muted" }, ` · peak ${peaks}`) : ""),
      ws ? [h("dt", {}, "War support"), h("dd", { class: "row", style: "gap:6px" }, sparkline(ws.series), `${fmtSigned(ws.last)} now · range ${fmtSigned(ws.min)} to ${fmtSigned(ws.max)}`)] : null,
      h("dt", {}, "Settlements"), nb.settlements?.length
        ? h("dd", { class: "small" }, nb.settlements.map(s => `${gLabel(s.g)} ${s.event} ${s.settlement}${s.capital ? " (capital)" : ""}${s.to ? ` to ${s.to}` : ""}`).join(" · "))
        : h("dd", {}, `${(f.captured || []).length} captured · ${(f.lost || []).length} lost`),
      nb.upsets?.length ? [h("dt", {}, "Upsets"), h("dd", {}, nb.upsets.map(u => h("div", { class: "warnline small" },
        `${gLabel(u.g)} ${u.what.replace("_", " ")}: ${u.settlement || ""}${u.settlement && u.player ? ", " : ""}${u.player || ""}`)))] : null);
  }
  if (p.scope === "goal") {
    return h("div", {}, h("div", { class: "row small" }, h("span", { class: "status" }, f.status || ""), h("span", {}, `${f.horizon || ""}-term`),
      f.overdue ? h("span", { class: "warnline" }, "overdue") : null, h("span", { class: "sp" }), pct(f.progress)), meter(f.progress, f.progress >= 1 ? "met" : ""),
      (f.metrics || []).length ? h("table", { class: "fc-table", style: "margin-top:6px" }, (f.metrics || []).map(m => h("tr", {},
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
    return h("span", { class: "fc-chip chip", title: r.leader !== S.local ? `Leader: ${pname(r.leader)} (${fmt(r.leader_value)})` : "You lead" },
      `${k[0].toUpperCase() + k.slice(1)} `, h("b", {}, `#${r.rank}/${r.of}`), ` ${fmt(r.value)}`);
  }));
}

function notesStrip(notes) {
  if (!notes.length) return empty(h("b", {}, "No intentions or priority changes recorded yet. "),
    "When you tell Claude what you plan or change course, it records an ", h("code", {}, "intention"), " / ", h("code", {}, "priority_change"),
    " note; they line up here on the turn axis.");
  const box = h("div", { class: "chart" }), [x0, x1] = xDomainFor("all");
  const kindColor = (n, t) => n.kind === "intention" ? t.series[0] : t.series[1];
  fcChart(box, t => ({
    height: 64, marginLeft: 8, marginRight: 8, marginTop: 20, marginBottom: 20,
    color: { type: "identity" },
    x: gScale(x0, x1, box.clientWidth - 16), y: { axis: null, grid: false, domain: [0, 1] },
    marks: [
      Plot.ruleX(ageBounds(x0, x1), { stroke: t.axis }),
      Plot.text(ageStarts(x0, x1), { x: "g", text: "label", frameAnchor: "top", textAnchor: "start", dx: 4, dy: -14, fill: t.ink }),
      Plot.ruleY([0.5], { stroke: t.grid }),
      Plot.dot(notes, { x: "g", y: () => 0.5, r: 5, fill: n => kindColor(n, t), stroke: cssVar("--fc-color-surface-raised"), strokeWidth: 1.5 }),
      Plot.tip(notes, Plot.pointerX({ x: "g", y: () => 0.5, title: n => `${gLabel(n.g)} · ${n.kind.replace("_", " ")}\n${n.text}` })),
    ],
  }), { onRender: plot => plot.addEventListener("click", () => plot.value && go(`#turn/${plot.value.g}`)) });
  return card({},
    h("div", { class: "legend" }, h("span", { class: "row small" }, h("span", { class: "dot", style: "background:var(--p0)" }), "intention"),
      h("span", { class: "row small" }, h("span", { class: "dot", style: "background:var(--p1)" }), "priority change")),
    figure(box, "Intentions and priority changes on the turn axis. Click a mark to open that turn."),
    h("div", { style: "margin-top:8px" }, notes.slice().reverse().map(noteEl)));
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
  const per = (pid, sub) => ({ id: `p${pid}${sub ? ":" + sub : ""}`, label: (sub ? sub + " · " : "") + pname(pid, true), order: pid, sw: pid });
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
      if (localMode) return isMajor(other) ? { id: "war" + other, label: pname(other, true), order: other, sw: other }
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
function evPlayer(e, localMode) {  // whose colour a timeline mark wears
  const me = S.local, d = e.detail || {};
  if (e.kind === "war" || e.kind === "alliance") {
    if (!localMode && e.kind === "war") { const [a, b] = e.players; return isMajor(a) && isMajor(b) ? (Math.max(a, b)) : (isMajor(a) ? b : a); }
    return (e.players.find(p => p !== me) ?? e.players[1]);
  }
  if (e.kind === "unit_killed") {
    const victim = d.victim ?? e.players[0], killer = d.killer ?? e.players[1];
    return localMode ? (killer === me ? victim : killer) : (killer);
  }
  if (e.kind === "settlement_captured") return (d.captured_by ?? e.players[0]);
  return (e.players[0]);
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
    lane.rows.get(r.id).items.push({ e, x0: e.start_g, x1: interval ? (e.end_g ?? cg) : e.start_g, interval, pid: evPlayer(e, localMode),
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
  const box = h("div", { class: "chart" });
  const ctl = h("div", { class: "controls" },
    h("div", { class: "seg" }, [["Your empire", true], ["All players", false]].map(([l, v]) =>
      h("button", { class: S.tl.local === v ? "fc-chip fc-chip-selected" : "fc-chip", onclick: () => { S.tl.local = v; refresh(); } }, l))),
    h("div", { class: "seg" }, [["all", "Whole game"], ...S.meta.ages.map(a => [a.age, ageName(a.age)])].map(([k, l]) =>
      h("button", { class: JSON.stringify(S.tl.domain || xDomainFor("all")) === JSON.stringify(xDomainFor(k)) ? "fc-chip fc-chip-selected" : "fc-chip", onclick: () => { S.tl.domain = xDomainFor(k); refresh(); } }, l))),
    S.tl.domain && JSON.stringify(S.tl.domain) !== JSON.stringify(xDomainFor("all")) ? h("button", { class: "fc-btn fc-btn-secondary iconbtn", onclick: () => { S.tl.domain = null; refresh(); } }, "Reset zoom") : null,
    h("span", { class: "sp" }),
    h("details", {}, h("summary", {}, "Lanes"), h("div", { class: "row small" }, LANES.map(([k, l]) => h("label", { class: "row", style: "gap:4px" },
      h("input", { type: "checkbox", checked: !S.tl.off.has(k), onchange: ev => { ev.target.checked ? S.tl.off.delete(k) : S.tl.off.add(k); refresh(); } }), l)))));
  root.append(ctl, h("div", { class: "card tl" }, figure(box, "The game by lane. Colours follow players (wars, alliances and unit deaths: the other side); ",
    "hollow marks: turn known only within a range (end-of-age capture). Drag across the chart to zoom; click a mark to open that turn.")));
  drawTimeline(box, buildLanes(d.events, S.tl.local));
  return root;
}

function drawTimeline(box, lanes) {
  const ML = 190, TH = 16, [x0, x1] = S.tl.domain || xDomainFor("all"), cg = S.meta.game.current_g;
  // One y band per lane head and per interval track; point rows get a single band.
  const bands = [{ key: "ages", head: true, label: "Ages" }];
  const items = [];
  for (const lane of lanes) {
    bands.push({ key: "L:" + lane.key, head: true, label: lane.label });
    for (const row of lane.rows) {
      const n = row.hasIntervals ? row.tracks : 1, base = `R:${lane.key}:${row.id}:`;
      for (let k = 0; k < n; k++) bands.push({ key: base + k, row, label: k ? "" : row.label, sw: k ? null : row.sw });
      for (const it of row.items) {
        if (it.x1 < x0 - 1 || it.x0 > x1 + 1) continue;
        items.push(Object.assign(it, { key: base + (it.interval ? it.track : 0), row }));
      }
    }
  }
  const H = bands.length * TH + 30;
  const ages = S.meta.ages.map(a => ({ a0: Math.max(x0, a.g_offset + 1), a1: Math.min(x1, a.current ? cg : a.end_g),
    label: `${ageName(a.age)}${a.current ? ` · T${cg - a.g_offset}/${a.turns_max}` : ` · ${a.turns} turns`}` })).filter(a => a.a1 >= a.a0);
  const ivs = items.filter(it => it.interval), pts = items.filter(it => !it.interval);
  const fit = (lab, turns, width) => textW(lab, 10) + 12 < turns * (width - ML - 12) / (x1 - x0 + 1);
  const trim = lab => lab.length > 26 ? lab.slice(0, 25) + "…" : lab;
  let state = null;  // scales and svg of the current render, for pointer handling
  fcChart(box, t => {
    const W = box.clientWidth || 900, paper = cssVar("--fc-color-surface-raised");
    const shape = sym => ({ fill: it => it.hollow ? paper : pcolorT(it.pid, t), stroke: it => it.hollow ? pcolorT(it.pid, t) : paper,
      strokeWidth: 1.5, r: 4.5, symbol: sym });
    return {
      height: H, marginLeft: ML, marginRight: 12, marginTop: 26, marginBottom: 4,
      color: { type: "identity" },
      x: gScale(x0 - 0.5, x1 + 0.5, W - ML - 12, { axis: "top" }),
      y: { domain: bands.map(b => b.key), axis: null, grid: false, padding: 0 },
      marks: [
        Plot.barX(ages, { x1: a => a.a0 - 0.5, x2: a => a.a1 + 0.5, y: () => "ages", fill: cssVar("--fc-color-surface-sunken"), insetLeft: 1, insetRight: 1, insetTop: 1, insetBottom: 1 }),
        Plot.text(ages.filter(a => fit(a.label, a.a1 - a.a0 + 1, W)), { x: a => a.a0 - 0.5, y: () => "ages", text: "label", textAnchor: "start", dx: 6, fill: t.ink }),
        Plot.ruleX(ageBounds(x0, x1), { stroke: t.axis }),
        cg >= x0 && cg <= x1 ? Plot.ruleX([cg + 0.5], { stroke: t.ink, strokeDasharray: "4,3" }) : null,
        Plot.barX(ivs, { x1: it => it.x0 - 0.5, x2: it => it.x1 + 0.5, y: "key", fill: it => pcolorT(it.pid, t),
          fillOpacity: it => it.e.end_g == null || it.e.detail?.end_reason ? 0.22 : 0.32, insetTop: 1, insetBottom: 1, clip: true }),
        Plot.tickX(ivs, { x: it => Math.max(x0 - 0.5, it.x0 - 0.5), y: "key", stroke: it => pcolorT(it.pid, t), strokeWidth: 3, insetTop: 1, insetBottom: 1, clip: true }),
        Plot.text(ivs.filter(it => fit(barLabel(it.e), it.x1 - it.x0 + 1, W)), { x: it => Math.max(x0 - 0.5, it.x0 - 0.5), y: "key", text: it => barLabel(it.e),
          textAnchor: "start", dx: 6, fontSize: 10, fill: t.ink, clip: true }),
        Plot.tickX(pts.filter(it => it.shape === "tick"), { x: "x0", y: "key", stroke: it => pcolorT(it.pid, t), strokeWidth: 1.5, strokeOpacity: 0.7, clip: true }),
        ...[["dot", "circle"], ["tri", "triangle"], ["diamond", "diamond2"]].map(([k, sym]) =>
          Plot.dot(pts.filter(it => it.shape === k), { x: "x0", y: "key", ...shape(sym), clip: true })),
        Plot.ruleY(bands.filter(b => b.head && b.key !== "ages"), { y: "key", stroke: t.grid, dy: -TH / 2 }),
        Plot.dot(bands.filter(b => b.sw != null), { x: () => x0 - 0.5, y: "key", dx: -(ML - 14), symbol: "square", r: 3.5, fill: b => pcolorT(b.sw, t) }),
        Plot.text(bands.filter(b => b.head), { x: () => x0 - 0.5, y: "key", dx: -(ML - 4), text: "label", textAnchor: "start", fill: t.ink, fontWeight: 600 }),
        ...[true, false].map(sw => Plot.text(bands.filter(b => !b.head && b.label && (b.sw != null) === sw), {  // dx must be constant per mark
          x: () => x0 - 0.5, y: "key", dx: -(ML - (sw ? 22 : 12)), text: b => trim(b.label), title: "label", textAnchor: "start", fill: t.axis })),
        lanes.length ? null : Plot.text(["No events in the selected lanes."], { text: d => d, frameAnchor: "middle", fill: t.axis }),
      ],
    };
  }, { onRender: plot => { state = { svg: plot, x: plot.scale("x"), y: plot.scale("y") }; wire(plot); } });

  // Pointer handling over the rendered svg: crosshair + tooltip, drag to zoom, click to open a turn.
  function hit(evt) {
    const { svg, x, y } = state, { px, py } = svgPoint(svg, evt);
    const band = bands.find(b => py >= y.apply(b.key) && py < y.apply(b.key) + y.bandwidth);
    const found = band?.row ? items.filter(it => it.key === band.key && (it.interval
      ? px >= x.apply(it.x0 - 0.5) - 2 && px <= x.apply(it.x1 + 0.5) + 2 : Math.abs(x.apply(it.x0) - px) <= 6)) : [];
    return { px, py, g: Math.round(x.invert(px)), row: band?.row, found };
  }
  function wire(svg) {
    const W = +svg.getAttribute("width"), inPlot = px => px >= ML && px <= W - 12;
    const cross = svgEl("line", { class: "cross", y1: 22, y2: H, style: "display:none" });
    const brush = svgEl("rect", { class: "brush", y: 22, height: H - 22, width: 0, style: "display:none" });
    svg.append(cross, brush);
    let drag = null;
    svg.addEventListener("pointerdown", e => { const p = hit(e); if (inPlot(p.px)) { drag = { x: p.px, moved: false }; svg.setPointerCapture(e.pointerId); } });
    svg.addEventListener("pointermove", e => {
      const p = hit(e);
      if (drag) {
        if (Math.abs(p.px - drag.x) > 5) drag.moved = true;
        const a = Math.max(ML, Math.min(drag.x, p.px)), b = Math.min(W - 12, Math.max(drag.x, p.px));
        setAttrs(brush, { x: a, width: b - a }, true);
        brush.style.display = drag.moved ? "" : "none";
        hideTip();
        return;
      }
      if (!inPlot(p.px)) { cross.style.display = "none"; hideTip(); return; }
      const cx = state.x.apply(p.g);
      setAttrs(cross, { x1: cx, x2: cx }, true);
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
        const a = Math.round(state.x.invert(Math.min(drag.x, p.px))), b = Math.round(state.x.invert(Math.max(drag.x, p.px)));
        drag = null;
        if (b - a >= 2) { S.tl.domain = [Math.max(1, a), Math.min(cg, b)]; refresh(); }
        return;
      }
      drag = null;
      if (inPlot(p.px)) go(`#turn/${p.found.length ? p.found[0].e.start_g : p.g}`);
    });
  }
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

// ------------------------------------------------------------------ charts
async function viewCharts() {
  const c = S.chart, cat = S.meta.catalog || [];
  const d = await api(`/api/metrics?metric=${encodeURIComponent(c.metric)}&vis=${c.vis}`);
  const groups = {};
  for (const m of cat) (groups[m.victory || "other"] = groups[m.victory || "other"] || []).push(m);
  const sel = h("select", { class: "fc-input", onchange: ev => { c.metric = ev.target.value; refresh(); } },
    Object.entries(groups).map(([gname, ms]) => h("optgroup", { label: gname[0].toUpperCase() + gname.slice(1) },
      ms.map(m => h("option", { value: m.metric, selected: m.metric === c.metric }, m.label)))));
  const players = Object.values(S.players);
  const series = players.map(p => ({ id: p.player_id, name: pname(p.player_id), color: pcolor(p.player_id), points: d.series[String(p.player_id)] || [] }))
    .filter(sr => sr.points.length);
  const hiddenOmni = Object.values(d.omniscient_hidden || {}).reduce((a, b) => a + b, 0);
  const legend = h("div", { class: "legend" }, series.map(sr => h("button", {
    class: c.hidden.has(sr.id) ? "fc-chip off" : "fc-chip", title: "Click to hide/show; double-click to isolate",
    onclick: () => { c.hidden.has(sr.id) ? c.hidden.delete(sr.id) : c.hidden.add(sr.id); refresh(); },
    ondblclick: () => { c.hidden = new Set(series.map(x => x.id).filter(id => id !== sr.id)); refresh(); },
  }, lineKey(sr.color), sr.name)),
    h("button", { class: "fc-chip", onclick: () => { c.hidden = new Set(); refresh(); } }, "Show all"),
    h("button", { class: "fc-chip", onclick: () => { c.hidden = new Set(series.map(x => x.id).filter(id => id !== S.local)); refresh(); } }, "Only you"));
  const box = h("div", { class: "chart" });
  const shown = series.filter(sr => !c.hidden.has(sr.id));
  lineChart(box, shown, xDomainFor(c.range), d.catalog);
  const table = c.table ? metricTable(shown) : null;
  return h("div", {},
    h("div", { class: "controls" }, sel,
      h("div", { class: "seg", title: "Omniscient values come from game logs and cover rivals you could not see" },
        [["visible", "As seen on screen"], ["all", "Include omniscient"]].map(([k, l]) => h("button", { class: c.vis === k ? "fc-chip fc-chip-selected" : "fc-chip", onclick: () => { c.vis = k; refresh(); } }, l))),
      h("div", { class: "seg" }, [["all", "Whole game"], ...S.meta.ages.map(a => [a.age, ageName(a.age)])].map(([k, l]) =>
        h("button", { class: c.range === k ? "fc-chip fc-chip-selected" : "fc-chip", onclick: () => { c.range = k; refresh(); } }, l))),
      h("span", { class: "sp" }),
      h("button", { class: "fc-btn fc-btn-secondary iconbtn", onclick: () => { c.table = !c.table; refresh(); } }, c.table ? "Hide table" : "Show table")),
    card({},
      h("div", { class: "cardhead" }, h("h2", {}, d.catalog?.label || c.metric), h("span", { class: "sec small" }, d.catalog?.unit ? `(${d.catalog.unit.replace("_", " ")})` : ""),
        h("span", { class: "sp" }), c.vis === "visible" && hiddenOmni ? h("span", { class: "hint" }, `${hiddenOmni.toLocaleString()} omniscient rival values hidden`) : null),
      d.catalog?.description ? h("div", { class: "small sec", style: "margin:-6px 0 6px" }, d.catalog.description) : null,
      series.length > 1 ? legend : null,
      figure(box, `${d.catalog?.label || c.metric} by player. One colour per player for the whole game; click the chart to open that turn.`)),
    table ? card({ style: "margin-top:14px" }, table) : null);
}

function bisect(points, g) {  // nearest index by g
  let lo = 0, hi = points.length - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (points[m][0] < g) lo = m + 1; else hi = m; }
  if (lo > 0 && Math.abs(points[lo - 1][0] - g) <= Math.abs(points[lo][0] - g)) lo--;
  return lo;
}

function lineChart(box, series, [x0, x1], catalog) {
  const pts = series.map(sr => ({ ...sr, points: sr.points.filter(p => p[0] >= x0 && p[0] <= x1) })).filter(sr => sr.points.length);
  if (S.chart.range === "all" && pts.length) x0 = Math.max(x0, Math.min(...pts.map(sr => sr.points[0][0])) - 1);  // skip turns with no data
  if (!pts.length) { box.replaceChildren(empty("No values for this metric in the selected range", S.chart.vis === "visible" ? " — try \"Include omniscient\"." : ".")); return; }
  // Lines break where turns are missing (gap > 3) so unobserved stretches are not interpolated: a NaN row splits the line.
  const rows = [], isolated = [];
  for (const sr of pts) {
    sr.points.forEach((p, i) => {
      const gapBefore = i === 0 || p[0] - sr.points[i - 1][0] > 3, gapAfter = i === sr.points.length - 1 || sr.points[i + 1][0] - p[0] > 3;
      if (i && gapBefore) rows.push({ id: sr.id, g: p[0] - 0.5, v: NaN });
      rows.push({ id: sr.id, g: p[0], v: p[1] });
      if (gapBefore && gapAfter) isolated.push({ id: sr.id, g: p[0], v: p[1] });  // a lone point would otherwise be invisible
    });
  }
  const real = rows.filter(r => !Number.isNaN(r.v)), mine = pts.find(sr => sr.id === S.local);
  const last = mine && mine.points[mine.points.length - 1][0] >= x1 - 2 ? [mine.points[mine.points.length - 1]] : [];
  // Captured on the container so the position is known before Plot's own pointer handler fires "input".
  let lastEvt = null;
  box.addEventListener("pointermove", e => { lastEvt = e; }, true);
  fcChart(box, t => {
    const paper = cssVar("--fc-color-surface-raised"), line = (data, w) => Plot.lineY(data, { x: "g", y: "v", z: "id", stroke: r => pcolorT(r.id, t), strokeWidth: w });
    return {
      height: 340, marginLeft: 56, marginRight: 16, marginTop: 26, marginBottom: 30,
      color: { type: "identity" },
      x: gScale(x0, x1, (box.clientWidth || 900) - 72),
      y: { label: null, zero: true, nice: true, tickFormat: fmt },
      marks: [
        Plot.ruleY([0], { stroke: t.axis }),
        Plot.ruleX(ageBounds(x0, x1), { stroke: t.axis }),
        Plot.text(ageStarts(x0, x1), { x: "g", text: "label", frameAnchor: "top", textAnchor: "start", dx: 4, dy: -18, fill: t.ink, fontWeight: 600 }),
        line(rows.filter(r => r.id !== S.local), 1.5), line(rows.filter(r => r.id === S.local), 2.5),  // the local player is drawn on top
        Plot.dot(isolated, { x: "g", y: "v", r: 2.5, fill: r => pcolorT(r.id, t) }),
        Plot.dot(last, { x: p => p[0], y: p => p[1], r: 4, fill: pcolorT(S.local, t), stroke: paper, strokeWidth: 2 }),
        Plot.ruleX(real, Plot.pointerX({ x: "g", stroke: t.axis })),
      ],
    };
  }, { onRender: (plot, t) => {
    // Plot's pointer picks the nearest turn; the tooltip and dots then list every player at that turn.
    const dots = svgEl("g", { style: "pointer-events:none" });
    plot.append(dots);
    plot.addEventListener("pointerleave", () => { dots.replaceChildren(); hideTip(); });
    plot.addEventListener("input", () => {
      dots.replaceChildren();
      if (!plot.value || !lastEvt) { hideTip(); return; }
      const g = plot.value.g, xs = plot.scale("x"), ys = plot.scale("y"), paper = cssVar("--fc-color-surface-raised"), at = [];
      for (const sr of pts) {
        const p = sr.points[bisect(sr.points, g)];
        if (!p || Math.abs(p[0] - g) > 2) continue;
        at.push({ sr, p });
        dots.append(svgEl("circle", { cx: xs.apply(p[0]), cy: ys.apply(p[1]), r: 4, fill: pcolorT(sr.id, t), stroke: paper, "stroke-width": 2 }));
      }
      at.sort((a, b) => b.p[1] - a.p[1]);
      showTip(lastEvt, h("div", { class: "tt" }, gLabel(g, true)), at.length ? at.map(({ sr, p }) => h("div", { class: "tr" }, lineKey(sr.color), h("b", {}, fmt(p[1])),
        h("span", {}, sr.name, p[0] !== g ? ` (${gLabel(p[0])})` : "", p[2] === "o" ? " · omniscient" : ""))) : h("div", { class: "muted" }, "no values this turn"));
    });
    plot.addEventListener("click", () => plot.value && go(`#turn/${plot.value.g}`));
  } });
}

function metricTable(series) {
  const gs = [...new Set(series.flatMap(sr => sr.points.map(p => p[0])))].sort((a, b) => b - a);
  const idx = series.map(sr => new Map(sr.points.map(p => [p[0], p])));
  return h("div", { class: "scroll" }, h("table", { class: "fc-table" },
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
    h("button", { class: "fc-btn fc-btn-secondary iconbtn", onclick: () => setG(t.g - 1), title: "Previous turn" }, "◀"), lab,
    h("button", { class: "fc-btn fc-btn-secondary iconbtn", onclick: () => setG(t.g + 1), title: "Next turn" }, "▶"), slider,
    h("button", { class: "fc-btn fc-btn-secondary iconbtn", disabled: t.prev_observed == null, onclick: () => setG(t.prev_observed) }, "◀ snapshot"),
    h("button", { class: "fc-btn fc-btn-secondary iconbtn", disabled: t.next_observed == null, onclick: () => setG(t.next_observed) }, "snapshot ▶"),
    h("button", { class: "fc-btn fc-btn-secondary iconbtn", onclick: () => setG(cg) }, "Latest")),
    h("div", { class: "small sec", style: "margin-top:6px" }, t.observed ? `Live snapshot captured ${new Date(t.captured_at).toLocaleString()}`
      : t.summary_g ? `No live snapshot this turn; empire summary below is from ${gLabel(t.summary_g)}.` : "No live snapshot at or before this turn; only logs and captures."));

  const evs = t.events.filter(e => !S.turn.localOnly || e.local || e.kind === "age");
  const exact = evs.filter(e => e.precision === "exact"), fuzzy = evs.filter(e => e.precision !== "exact");
  const groups = {};
  for (const e of exact) (groups[e.kind] = groups[e.kind] || []).push(e);
  const evRowEl = e => h("li", {}, h("div", { class: "row", style: "gap:6px" }, h("span", { class: "kind" }, (e.phase === "end" ? "ended · " : "") + (KIND_LABEL[e.kind] || e.kind)),
    h("b", {}, e.subject), e.players.slice(0, 3).map(p => h("span", { class: "row small sec", style: "gap:4px" }, swatch(p), pname(p, true)))), evDetail(e));
  const events = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Events"), h("span", { class: "sp" }),
    h("div", { class: "seg" }, [["Yours", true], ["Everyone", false]].map(([l, v]) => h("button", { class: S.turn.localOnly === v ? "fc-chip fc-chip-selected" : "fc-chip", onclick: () => { S.turn.localOnly = v; refresh(); } }, l)))),
    exact.length ? h("ul", { class: "plain" }, Object.values(groups).flat().map(evRowEl)) : empty("Nothing recorded at exactly this turn."),
    fuzzy.length ? h("details", {}, h("summary", {}, `${fuzzy.length} more first seen in an end-of-age capture (exact turn unknown)`), h("ul", { class: "plain" }, fuzzy.map(evRowEl))) : null);

  const deltas = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Your numbers"), h("span", { class: "sp" }), t.prev_g != null ? h("span", { class: "hint" }, `Δ vs ${gLabel(t.prev_g)}`) : null),
    t.deltas.length ? h("table", { class: "fc-table" }, h("tr", {}, h("th", {}, "Metric"), h("th", { class: "n" }, "Value"), h("th", { class: "n" }, "Change")),
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
  const chips = xs => xs?.length ? h("div", { class: "tagline", style: "margin-top:0" }, xs.map(x => h("span", { class: "fc-chip chip" }, x))) : h("span", { class: "muted" }, "—");
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
      h("table", { class: "fc-table" }, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Type"), h("th", { class: "n" }, "Pop")),
        st.slice().sort((a, b) => b.population - a.population).map(x => h("tr", {}, h("td", {}, x.name, x.capital ? " ★" : ""), h("td", { class: "sec" }, x.type), h("td", { class: "n" }, x.population))))) : null);
}

// ------------------------------------------------------------------ advisor
async function viewAdvisor() {
  const a = await api("/api/advisor");
  const voc = a.vocab || {};
  const ap = a.advisor_profile, pp = a.player_profile;
  const histList = (rows, fmtRow) => rows.length > 1 ? h("details", {}, h("summary", {}, `${rows.length - 1} earlier version${rows.length > 2 ? "s" : ""}`),
    h("ul", { class: "plain" }, rows.slice(1).map(r => h("li", { class: "small" }, h("b", {}, `v${r.version}`), ` from ${gLabel(r.valid_from_g)} · `, fmtRow(r))))) : null;
  const advisorCard = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Advisor personality"), h("span", { class: "sp" }), ap ? h("span", { class: "fc-chip chip" }, `v${ap.version} since ${gLabel(ap.valid_from_g)}`) : null),
    ap ? h("div", {}, h("div", { class: "leader" }, ap.name || pretty(ap.personality)), ap.name ? h("div", { class: "small muted" }, pretty(ap.personality)) : null, h("div", { class: "sec small" }, voc.personality?.[ap.personality] || ""),
      ap.voice ? h("p", {}, ap.voice) : null,
      h("div", { class: "tagline" }, (ap.priorities || []).map((p, i) => h("span", { class: "fc-chip chip", title: voc.advisor_priority?.[p] || "" }, h("b", {}, i + 1), " ", p.replace(/_/g, " ")))),
      ap.quirks ? h("p", { class: "small sec" }, ap.quirks) : null,
      histList(a.advisor_history, r => `${pretty(r.personality)} (${(r.priorities || []).join(", ")})`))
      : empty(h("b", {}, "No advisor yet. "), "Ask Claude to set up the advisor: it walks you through presets, saved profiles from earlier games, or a custom voice."));
  const playerCard = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Your playstyle"), h("span", { class: "sp" }), pp ? h("span", { class: "fc-chip chip" }, `v${pp.version} since ${gLabel(pp.valid_from_g)}`) : null),
    pp ? h("div", {}, pp.name ? h("div", { class: "leader" }, pp.name) : null,
      (pp.motives || []).length ? h("div", { class: "tagline", style: "margin-top:0" }, pp.motives.map(m => h("span", { class: "fc-chip chip", title: voc.motive?.[m] || "" }, m.replace(/_/g, " ")))) : null,
      h("div", { class: "tagline" }, (pp.playstyle || []).map(p => h("span", { class: "fc-chip chip", title: voc.playstyle?.[p] || "" }, h("b", {}, p)))),
      h("div", { class: "small sec", style: "margin-top:6px" }, `Risk tolerance: ${pp.risk_tolerance}`), pp.notes ? h("p", {}, pp.notes) : null,
      histList(a.player_history, r => `${(r.playstyle || []).join(", ")} · risk ${r.risk_tolerance}`))
      : empty(h("b", {}, "No player profile yet. "), "Ask Claude to set up your profile: are you here to fight, build, explore, mess around, or make a story?"));

  const plan = a.plan || [];
  const planEl = h("div", { class: "grid" }, a.plan_error ? h("div", { class: "error" }, "Goal evaluation failed: ", a.plan_error) : null,
    plan.length ? plan.map(strategyCard) : empty(h("b", {}, "No strategy or goals yet. "), "Tell Claude what you are going for (\"economic victory\", \"take Paris by Modern turn 100\"); it records a strategy, long-term goals, and short-term steps linked to metrics, and their progress shows here."));

  const kinds = [...new Set(a.annotations.map(n => n.kind))];
  const shown = a.annotations.filter(n => !S.notes.kind || n.kind === S.notes.kind);
  const log = card({}, h("div", { class: "cardhead" }, h("h2", {}, "Annotations"), h("span", { class: "sp" }),
    kinds.length > 1 ? h("div", { class: "seg" }, [["", "All"], ...kinds.map(k => [k, k.replace("_", " ")])].map(([k, l]) =>
      h("button", { class: S.notes.kind === k ? "fc-chip fc-chip-selected" : "fc-chip", onclick: () => { S.notes.kind = k; refresh(); } }, l))) : null),
    shown.length ? h("div", { class: "scroll", style: "max-height:600px" }, h("table", { class: "fc-table" },
      h("tr", {}, h("th", {}, "Turn"), h("th", {}, "Kind"), h("th", {}, "Note"), h("th", {}, "About")),
      shown.map(n => h("tr", {}, h("td", { style: "white-space:nowrap" }, h("a", { href: `#turn/${n.g}` }, gLabel(n.g)), h("div", { class: "small muted" }, n.author)),
        h("td", {}, h("span", { class: "kind" }, n.kind.replace("_", " ")), n.outcome ? h("div", { style: "margin-top:3px" }, badge(n.outcome)) : null),
        h("td", { style: "white-space:pre-wrap" }, n.text),
        h("td", { class: "small sec" }, n.scope ? `${n.scope} ${n.scope === "age" ? ageName(n.scope_key) : n.scope_key}` : n.goal_id ? `goal ${n.goal_id}` : n.strategy_id ? `strategy ${n.strategy_id}` : "")))))
      : empty("No annotations yet. Claude writes them as it advises: your intentions, priority changes, its warnings and advice, and its narrative of how each phase went."));
  return h("div", {}, h("div", { class: "grid g2" }, advisorCard, playerCard),
    h("div", { class: "section" }, h("h2", {}, "Plan"), planEl), h("div", { class: "section" }, log));
}

function strategyCard(st) {
  return card({}, h("div", { class: "cardhead" }, h("h2", {}, st.name), h("span", { class: "status" }, st.status), h("span", { class: "sp" }),
    (st.victory_types || []).map(v => h("span", { class: "fc-chip chip" }, v)),
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
  const btn = $("#theme");
  const label = () => btn.setAttribute("aria-checked", String(document.documentElement.dataset.theme === "dark"));
  label();
  btn.addEventListener("click", () => { fcToggleTheme(); label(); });
}
window.addEventListener("hashchange", () => { parseHash(); refresh(); });
initTheme();
parseHash();
refresh().then(poll);
setInterval(poll, 10000);
