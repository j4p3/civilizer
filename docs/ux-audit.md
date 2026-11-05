# Civilizer dashboard — UI/UX audit (2026-11-04)

Scope: `src/civilizer/web/{index.html,app.js,style.css}`, API in `src/civilizer/dashboard.py`, Foolscap vendored under `src/civilizer/web/foolscap/`. No code changed.
Screenshots: `/tmp/claude-1000/-home-jp-src-civilizer/5f2016f4-70b4-48cf-bfe2-09344048e53a/scratchpad/shots/` (full page, `<game>-<tab>-<desk|mob>-<light|dark>.png`; `ada` = 1878972827 finished, `ibn` = -1906453415 in progress) and detail crops in `../crops/`.
Method: puppeteer-core + system chromium, 1280×900 and 390×844, `fc-theme` set via localStorage for both modes. Zero console errors on any page.

---

## 1. Inventory

| Page (hash) | What it shows | Player's question | Shots |
|---|---|---|---|
| `#home` Games index | Ruled list of games (leader, civ path, outcome, turn, seed, updated) + plated "Start a new game" how-to | "Which game do I open?" / "Is the monitor recording?" | `home-desk-light`, `home-mob-light`, `home-desk-dark` |
| `#overview` | Hero (character card + plated progress card with age bar, blurb, outcome chips); 12 stat tiles (`.tiles`, app.js:395); 4–5 victory-standings bar charts (app.js:402–414); Wars; Phases & initiatives list | "How am I doing right now, and against whom?" | `ibn-overview-desk-light`, `ada-overview-desk-light`, `ibn-overview-mob-light`, `ibn-overview-desk-dark` |
| `#story` | Phase cards by scope: game, ages (start/end metric table, standings chips, kv facts), wars (kv), goals (meter + metric table); every card carries outcome notes + Claude narratives; notes strip | "What happened and why?" (post-mortem reading) | `ibn-story-desk-light` (9163 px tall), `ada-story-desk-light`, `ibn-story-mob-light` (19 274 px tall) |
| `#timeline` | Gantt-style lane chart (app.js:636 `LANES`, 765 `drawTimeline`): ages, government, settlements, tech, civics, policies, legacies, production per city, wars, alliances, unit deaths; drag-zoom, click → turn | "When did things happen relative to each other?" | `ada-timeline-desk-light`, `ada-timeline-desk-dark`, `ibn-timeline-mob-light` |
| `#charts` | One metric (select of ~58) × all players as a line chart; visibility toggle; age range; legend chips; optional table | "How does my X compare to rivals over time?" | `ada-charts-desk-light`, `ada-charts-mob-light`, `ibn-charts-desk-light` |
| `#turn/<g>` | Turn nav (slider + snapshot buttons); Empire kv (research, civic, policies, traditions…, settlements table); Advisor notes; Events; "Your numbers" 40-row Δ-vs-previous-turn table | "What was the state at turn N / what changed this turn?" | `ada-turn-desk-light` (6641 px), `ibn-turn-mob-light` |
| `#advisor` | Advisor personality, player profile, Plan (strategy → goals → metrics with meters), Annotations table | "What did I say I'd do, and is it on track?" | `ibn-advisor-desk-light`, `ada-advisor-desk-light` |

Note: the brief mentioned "Players" and "Settlements" pages; they do not exist. Players appear only as bars/lines inside Overview and Charts; settlements only as a pop table inside Turn.

---

## 2. Findings, ranked by severity

### S1 — Bugs / broken rendering

**F1. `.status` class collision breaks every goal header (Advisor, Story).**
`style.css:59` defines `.status { display:inline-flex; width:1.5rem; height:1.5rem; cursor:help }` for the header monitor dot, but `app.js:1139` and `:585` also use `class="status"` for the goal meta text ("long-term · abandoned · P2"). Result: the text is squeezed into a 24 px box and wraps into a 4-line vertical stack overlapping the goal title and the deadline (crop `advisor-status-collision.png`; `ibn-advisor-desk-light.png` y≈630, `ibn-story-desk-light.png` goals section). Fix: rename the header selector to `#monitor` / `.monitor` (it already has the id) and leave `.status` as the mono label voice (`style.css:39,232`).

**F2. Negative victory targets rendered literally.** `_standings` (dashboard.py:227) returns the sentinel `-1` for paths without a target at age start, and `standingsBars`/the standings card check `st.target ?` which is truthy for -1, so the ibn Overview prints "2,141 / -1 to win · -214100%" and "50 / -1 to win · -5000%" with an empty meter (crop `standings-neg-target.png`). Fix: treat `target <= 0` as null server-side (`_standings`) and in the client (`st.target > 0`).

**F3. Horizontal overflow on phones.** At a 390 px viewport, `document.scrollWidth` is 444 px on Overview, Turn, Advisor and 401 on Story (the puppeteer run logs it). Cause: `.g2 { minmax(420px, 1fr) }` (`style.css:102`) and `.hero` minimum 280 px + padding; the Turn "Empire" card is clipped on the right (crop `turn-mob-overflow.png`). Fix: `minmax(min(420px, 100%), 1fr)` for `.g2/.g3/.standings/.tiles`, and `main { overflow-x: clip }` as a backstop.

**F4. Tab bar truncates on phones with no affordance.** `nav.tabs` has `overflow-x:auto` but the last tabs are cut mid-word ("TU", `ibn-overview-mob-light.png` top) and nothing signals scrollability. Fix: fade mask or a `scroll-snap`/wrap; or collapse to 4 tabs (see §3).

**F5. Rank "of N" is inconsistent within one page.** Tiles say "#1 of 4" while standings say "#4 of 9" on the same screen (`ibn-overview-desk-light.png`). `api_overview` ranks only players that have a value in the last 3 turns (`_latest(..., cg-3, vis)`), so at the first turn of a new age only 4 of 9 rivals qualify. The user cannot know this. Fix: rank against all majors with the latest visible value in the age, and show the denominator once in the section head, not per tile.

**F6. Story age table shows nonsense at phase boundaries.** Antiquity card: every metric starts at 0 and change is "—" (no first-turn values); Exploration card at T1: start==end, change "±0" × 6 (`ibn-story-desk-light-1.png`). Both are pure noise. Fix: suppress the table when `g_end - g_start < 5` or when start is missing; say "no baseline" instead of 0.

### S2 — Tufte / information-design problems

**F7. Twelve one-number-per-card tiles are the dashboard anti-pattern.** `app.js:395–400`; each tile spends ~190×150 px on one number, one delta, a 30-turn sparkline with no y-scale and no comparison line, and (for 7 of 12) a rank. Data-ink ratio is low: borders, label, "vs Antiquity T151" repeated 12 times. The sparkline cannot answer "is 198 good?" because the rival band is absent. Replace with a single table of small multiples (§4.1): one row per metric, columns = value, Δ, rank, sparkline with rival envelope. Same information in a quarter of the height and scannable top-to-bottom.

**F8. Victory standings: five bar charts with the same 8–9 names each sorted differently.** (`ada-overview-desk-light-1.png`). To find "where is Tecumseh" you re-read five lists. Each chart repeats the colour legend problem (9 hues). A player × path rank matrix (§4.2) shows the whole competitive picture in ~10 rows and lets you read both by player (row) and by path (column). Keep one bar chart only for the path the player is pursuing (the strategy's `victory_types`).

**F9. No comparison "in context" anywhere except Charts.** Overview tiles compare to *yourself 10 turns ago*; Story compares *start vs end* of age; neither shows the rival at the same moment or the target needed. Turn page deltas are vs previous turn only (so 30 of 40 rows read "–"). Every number should carry at least one of: rank, leader value, target. The API already has `rank/of`, `leader`, `target`, and `vp_*_target` metrics, so this is a presentation change.

**F10. Charts page is one metric at a time, 58-item select.** The single most useful view — "all my yields vs rivals" — requires 7 select changes. The legend uses 7 lines of chips on mobile (`ada-charts-mob-light.png`). Foolscap's own guideline says direct-label ≤4 series, legend above that; here the local player is already distinguished by weight. Replace with a faceted small-multiples grid (§4.1) with the local player drawn bold, rivals as thin grey, the *leader* named at the right edge, and a metric picker only for the detail chart.

**F11. Timeline: ink without data.** Policies lane: ~14 unlabeled bars (labels dropped by `fit()` when the bar is narrower than the text, app.js:805) — a row of grey rectangles that say nothing without hover (crop `timeline-unlabelled-bars.png`). Production lane: 16 per-city rows of hairline ticks (588 production + 373 purchase events) that read as noise. "Independents" lane: 8 identical bars ("Independent 10…23"). The 190 px fixed left margin (`ML=190`, app.js:766) leaves ~120 px of plot on a phone (`ibn-timeline-mob-light.png`). Fix: (a) collapse production/purchase to a per-turn density strip (one row, mark height ∝ count), (b) collapse independents' wars to one row with a count, (c) label-or-omit: a bar that cannot carry its label gets a leader number or is merged into a count, (d) `ML = min(190, 0.3*W)` and abbreviate on small widths, (e) move the figure caption's legend ("hollow = range") into an inline key row.

**F12. Hero card half-empty.** The character card on `ada-overview-desk-light.png` is ~400 px tall with 150 px of content; rivals are an inline sentence. On `ibn-overview` the left column is 60% whitespace. The "Build 1.5.0 · game -1906453415" line is apparatus, not content. Merge character + progress into one plated card (§3) or use the space for the standings matrix.

**F13. Story page is a 9 000–19 000 px scroll with repeated blocks.** Outcome notes for the same event appear in the game card *and* the age card (the T78 "abandoned" note appears twice with slightly different wording); standings chips appear in Overview, in the game card and in each age card; goals appear in Overview "Phases & initiatives", Story "Goals" and Advisor "Plan". Narratives (the part people want to read) sit under a fact table and a kv block. Fix: §3 consolidation; make the narrative the first thing in a phase card and put facts in a collapsed `<details>` or a side rail.

**F14. Turn page "Your numbers" table: 40 rows, most "–".** `t.deltas` lists every catalogue metric including `*_target` rows (Economic target 14.6K "–") that never change. Fix: group into yields / empire / war / victory; hide rows with no Δ unless expanded; show Δ as a tiny bar or ±N with a sparkline over the last 10 turns instead of a lone ▲/▼.

**F15. Settlements table (16 rows, name/type/pop) is the only per-settlement data on the site.** The store has much more (`settlements`, `trade_routes`, yields per city). This is the one place a "Settlements" page is warranted — but as a table with sparklines, not cards.

### S3 — Colour, theme, accessibility

**F16. Nine- to ten-player palettes.** `--fc-color-chart-1..12` in fixed player-id order; chart-7..12 reuse graph inks, so Amina (id 5, grey) and Catherine (id 6, slate) are near-identical in the standings bars; Charles (chart-6 olive) vs Harriet (chart-4 ochre) are close on cream. Foolscap's guideline: past six series prefer direct labels over colour. The app does label bars directly, so colour is redundant there; in Charts it is the only key. Fix: local player = `chart.ink` bold; rivals = one muted grey except the hovered/leader one, which takes a highlight colour; name at line end. Colour then carries ≤3 series at once.

**F17. Dark/light parity: good.** Blueprint mode renders every page without regressions (`*-dark.png`); plate offset, grid lines and hollow marks all re-theme. Only nit: `.tile .spark` stroke uses `t.axis` (gray-500-ish) which drops below 3:1 on prussian-900 panes; use `t.ink` for the line and `t.axis` for the baseline.

**F18. Accessibility gaps.**
- `nav.tabs a` lacks `aria-current="page"` (only `.on` class), index.html:31–38.
- All Plot SVGs have no accessible name; `fcChart` should pass `ariaLabel` from the figure caption (Plot supports `ariaLabel`/`ariaDescription` on the spec).
- `role="meter"` elements (app.js:154) lack `aria-label`/`aria-valuetext`.
- Tables (`fc-table`, 6 uses) are built without `<thead>`/`scope="col"`; `grep thead app.js` = 0.
- `#tip` has `role="tooltip"` but is never referenced by `aria-describedby`; hover-only tooltips on the timeline/charts have no keyboard path (no `tabindex`, no arrow-key stepping through turns).
- Monitor state in the header is a colour-only dot with the words in `title` (app.js:241–245) — fine for `aria-label`, but sighted users on touch get nothing. Show the word ("LIVE 12s") in the pill.
- The "3H" pill (store refreshed 3 h ago) is cryptic; "STORE 3h" or merge with the monitor pill.
- Legend chip "off" state uses `--fc-color-text-faint` (sub-AA, by Foolscap's own doc); the dashed border carries the state, so acceptable, but add `aria-pressed`.

### S4 — Navigation / hierarchy / polish

**F19. Scanning path on Overview is bottom-heavy.** The single most decision-relevant element (victory standings) is below the fold on desktop (y≈870) and 4 000 px down on mobile, after 12 tiles. The blurb (narrative prose) sits inside the plated progress card, above the data. Reorder: standings matrix → yields vs rivals → progress/blurb → phases.

**F20. Deep links are inconsistent.** Story "Timeline" link sets `S.tl.domain` through a click handler (app.js:515) rather than the URL, so the zoom is lost on reload/share; Charts metric/range live in `S.chart` not the hash. Put view state in the hash (`#charts/gold_per_turn/modern`).

**F21. Games index "Start a new game" plate dominates the page** (`home-mob-light.png`: 60% of the page). A 4-step runbook belongs behind a disclosure once at least one game exists.

**F22. Rounding/format nits.** "-81.8" deltas with one decimal next to integer values; "1 turns left"; "0 founded" in a 1-turn age; "±0" repeated; `vs Antiquity T151` wraps to three lines in every tile at 190 px.

---

## 3. Redundancy map and proposed structure

Where the same fact is rendered today:

| Fact | Overview | Story | Timeline | Charts | Turn | Advisor |
|---|---|---|---|---|---|---|
| Victory rank per path | bars ×5 | chips (game card + each age card) | – | line (vp_*) | table rows | goal metric rows |
| Goals + outcomes | Phases list | Goals section (cards) | – | – | notes | Plan (meters) |
| Age outcome badges | chips in hero | game card chips + age card badge | age band | – | – | – |
| Wars | Wars card | War cards | lanes | – | events | – |
| Blurb / narrative | blurb | narratives | – | – | notes | annotations table |
| Yields now | 12 tiles | age start/end table | – | one line | 40-row table | – |

Proposal: **four tabs** (plus the index), each comparison-first:

1. **Now** (replaces Overview + Charts + most of Turn's numbers)
   - Standings strip (§4.2) at the top; the plated element.
   - Yields vs rivals small-multiples table (§4.1); clicking a row expands the full-size faceted line chart in place (the current Charts figure), with turn hash state.
   - Compact progress line: age bar + "T81/140 · 58%" + outcome chips; blurb as one italic lede sentence.
   - Wars + current research/civic/government (the Turn page's Empire kv), one row each.
2. **Story** (Story + Advisor's Plan + Annotations)
   - Narrative-first phase cards; facts in a right rail / `<details>`; goals nested under the phase they belong to (they already share `g_start/g_end`); the annotations table becomes a filter on the same page. Advisor/player profile → a collapsed header strip ("Blunt exploiter · jp: aggressive, opportunist · risk high").
3. **Timeline** (keep, with F11 fixes and §4.3 layering) — absorbs the Story "notes strip" dots and the Turn page: clicking a turn opens a side sheet with that turn's events + Δ instead of a separate page.
4. **Empire** (new: Turn's settlements table expanded) — settlements × (pop, type, food, prod, gold, trade routes, unrest) with a sparkline per row; only page that is per-settlement.

Turn-by-turn survives as the side sheet / `#turn/<g>` deep link, not a tab. Advisor profile cards survive as a strip. That removes two tabs, three copies of goals, two copies of standings, and the 12-tile grid.

---

## 4. Tufte-style component proposals (ASCII)

### 4.1 Yield small multiples with rank and rival envelope (replaces tiles + Charts)

One row per metric. Sparkline shows the last 30 turns: you (bold), rival min–max band (light fill), leader (thin line), named at the right edge. Rank cell shows rank and Δrank over 10 turns.

```
YOUR EMPIRE · Exploration T1            you      Δ10t   rank   leader          last 30 turns
──────────────────────────────────────────────────────────────────────────────────────────
Gold/turn                               198.9   ▼ 82    1/9    —            ▁▂▃▃▄▅▆▇█▂   ╱‾‾╲ Amina 190
Science/turn                             46.5   ▼ 22    9/9    Rizal 95     ░░░░░░░░░░ ─────── you
Culture/turn                             92.0   ▲ 29    4/9    Isabella 140 ░░░░░▄▄▅▆█
Production/turn                         184.8   ▲ 62    1/9    —
Food/turn                               235.9   ▼ 40    1/9    —
Influence/turn                           36.3   ▲ 27    1/9    —
Treasury                                2,385   ▲1,496   ·      ·            carry-over cap 1,500 ┊
Settlements                            10 / 8   ▲ 1     1/9    —            ⚠ over cap by 2
Military units                             11   ±0      ·
Trade routes                                0   ▼ 3     ·      ·            ▇▇▇▆▅▃▁▁▁▁
```
Band = rival min..max (light fill), thin line = leader, bold = you. Rows with no rival data drop the band, not the row. Dots in the "rank" column could be tiny 9-cell strips (`▪▪▪▫▫▫▫▫▫`) if numerals feel heavy.

### 4.2 Standings strip: player × path matrix with rank deltas

```
VICTORY STANDINGS · Modern T81 · targets: econ 14.6K · sci 100 · cult 384 · mil 172
             ECON      SCI       CULT      MIL      SCORE   trend(10t)
Ada (you)  ① 15.3K █▌ ④ 41 ▍   ⑦ 120 ▎  ③ 78 ▍   ① 4,850   ▲1 econ, ▼1 mil
Tecumseh   ② 7.3K  ▊  ② 49 ▍   ④ 170 ▍  ① 89 ▌   ② 910
Harriet    ③ 6.8K  ▊  ③ 42 ▍   ⑤ 148 ▍  ⑤ 57 ▎   ③ 840
Xerxes     ④ 6.8K  ▊  ⑧ 14 ▏   ⑧ 112 ▎  ② 86 ▌   ⑦ 780
Amina      ⑤ 6.7K  ▊  ⑦ 19 ▏   ① 242 ▋  ④ 70 ▍   ④ 830
Franklin   ⑥ 5.6K  ▋  ⑥ 29 ▎   ② 192 ▌  ⑥ 54 ▎   ⑤ 800
Catherine  ⑦ 5.5K  ▋  ① 68 ▋   ⑥ 131 ▍  ⑦ 54 ▎   ⑥ 800
Lafayette  ⑧ 4.2K  ▌  ⑤ 31 ▎   ③ 187 ▌  ⑧ 40 ▏   ⑧ 760
```
Each cell: rank glyph, value, inline bar scaled to the path target (bar hits full width = victory). Your row is plated/highlighted; the leader cell in each column is underlined. Row sort = score or the player's chosen path. This replaces five bar charts and all standings chips. ~10 rows, one glance.

### 4.3 Timeline with layered events (fewer lanes, denser)

```
          Antiquity ─────────────────────── ┊ Exploration ───────────────── ┊ Modern ──────▸
Ages      ▏T1                     T140     ▏T1                      T140   ▏T1        T81 ┆
Rule      ▐Despotism▌▐Oligarchy────────────▌▐Monarchy────────────────────────▌▐Elect.Rep.───┆
Policies  ▁▂▃▃▄▄▄▄▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆  (slots filled, height = count; change = tick)
Tech/Civic ·· · ·· · · ··  · · ·· · · · · ·   (completion ticks; hover = name; dense = good)
Build     ▂▃▃▅▆▇▇▇▆▅▅▆▇▇█▇▆▅▆▇▇█ (items completed per turn, one strip for the empire)
Settle    ○         ○ ○    ▲      ▽              ○     ○ ○           ▲▲    (found/capture/lost)
Wars      ░░░Augustus░░░░░░░░▌  ▐Confucius▌            ▐Lafayette▌ ▐Franklin▌
Kills     ·· ··· ·•••••·· ·                     ·  ·   ••·   (dot size = count/turn)
Notes     ◆      ◆    ◆◆         ◆     (intentions / priority changes — from Story)
Gold/t    ╱‾‾‾╲__╱‾‾‾‾‾╲╱‾‾‾‾‾‾╲_╱‾‾‾‾   (one context sparkline so events sit on a yield)
```
Lanes go from ~45 bands to ~10. Layering: ages as a background band, a yield sparkline as the bottom context strip, event marks on top. Zoom by drag stays; clicking a turn opens the side sheet. Counts replace unlabeled bars.

### 4.4 Turn side sheet (replaces Turn tab)

```
┌ Modern T81 (g361) ◂ ▸ ─────────────────┐
│ Δ vs T80   gold ▼13  sci ▼1  prod ▲9   │
│            treasury ▲878  units ▲1     │
│ Events     St. Louis captured by       │
│            Lafayette                   │
│ Research   Mobilization  ▇▇▇▇▇▇▇░░ 3t  │
│ Civic      Wartime Mfg   ▇▇▇▇▇▇░░░ 5t  │
│ Notes      (advisor) …                 │
└────────────────────────────────────────┘
```

---

## 5. Foolscap coverage

Checked `site/assets/foolscap.css` class list (fc-btn*, fc-chip*, fc-pane*, fc-plate*, fc-table, fc-figure*, fc-note*, fc-stat, fc-dot, fc-label, fc-input, fc-field, fc-graph*…) and `foolscap-plot.js` (`fcTokens`, `fcPlotDefaults`, `fcChart`). The dashboard currently uses: fc-chip (29×), fc-table (6), fc-btn-secondary (7), fc-plate (2), fc-figure (1), fc-input (1). It does **not** use `fc-pane/-header/-body` (it has its own `.card`), `fc-stat`, `fc-note` (own `.empty`/`.error`), `fc-dot` (own `.dot`), `fc-label` (own `.lab`), `fc-byline`, `fc-footnote`. Half of style.css re-implements Foolscap components under new names; adopting the originals would delete ~60 lines.

| Proposal | Already in Foolscap | Needs | Push upstream? |
|---|---|---|---|
| 4.1 small-multiples table | `fc-table` (numeric cells, rules), `fcChart` (theming/resize), Plot `lineY`/`areaY`, `.fc-figure` | an inline sparkline mark spec with band + reference line, sized to a table cell; `fcChart` currently forces `width ≥ 320` (foolscap-plot.js:72) so it cannot render inside a cell | **Yes — `fcSpark`** |
| 4.2 standings matrix | `fc-table`, `mark` highlight, `fc-plate` for your row | a cell-level inline bar scaled to a target, and a rank glyph | **Yes — `.fc-bar` cell + `.fc-rank`** |
| 4.3 layered timeline | `fcChart`, Plot barX/dot/tickX/ruleX (what app.js does today) | a lane/band helper for gantt layouts; density strip (binned ticks) | Partly — `fcLanes` is generic enough; the density strip is just `Plot.rectY` with `binX`, no helper needed |
| 4.4 side sheet | `fc-pane` + `fc-pane-header` | a slide-in sheet / drawer with focus trap | **Yes — `.fc-sheet`**, generic |
| Rival-grey + highlight colour rule (F16) | `chart.ink`, `chart.1..12` | a documented convention: "many-series figures: ink for the subject, `chart.axis` for the rest, `chart.1` for the highlighted one" | Yes, as guidance in `docs/guidelines.md §6` plus a `t.subject/t.rest/t.highlight` triple in `fcTokens()` |
| Status word pill (F18) | `fc-chip` + `fc-dot` | nothing | no |
| aria-label for figures | — | `fcChart(el, build, { label })` passing `ariaLabel` into the spec and `aria-describedby` onto `.fc-figcaption` | **Yes**, small and generic |

### API sketches for the upstream extensions

```js
// foolscap-plot.js
// Sparkline in a table cell. Honors tokens; no axes; optional band and reference line.
// Returns {render, dispose} like fcChart but allows width < 320 (uses el.clientWidth or opts.width).
fcSpark(el, points, {
  width: 96, height: 20,
  band: [[g, lo, hi], ...],   // rival envelope, drawn with series fill at 0.15
  ref: [[g, v], ...] | number, // leader line or a constant target
  last: true,                  // dot on the final point
  label: "Gold per turn, last 30 turns, you vs rivals",  // aria-label
});

// Lane/gantt scaffold: computes band keys, interval packing into tracks, head rules;
// caller supplies marks. Keeps Plot as the renderer.
fcLanes(el, { x: [x0, x1], lanes: [{ key, label, rows: [{ id, label, items: [{x0, x1?, ...}] }] }],
  rowHeight: 16, labelWidth: w => Math.min(190, w * 0.3), marks: (ctx, t) => [...] });

// fcChart option: accessible name (sets spec.ariaLabel and links figcaption)
fcChart(el, build, { label: "…", onRender })
```

```css
/* foolscap.css — generic table apparatus */
.fc-bar          { --fc-bar: 0.45; /* 0..1 of target */ background: linear-gradient(to right, var(--fc-color-chart-ink) calc(var(--fc-bar)*100%), transparent 0); height: 6px; border: 1px solid var(--fc-color-border-strong); }
.fc-bar-met > i  { background: var(--fc-color-feedback-success); }
.fc-rank         { font-family: var(--fc-font-family-data); font-variant-numeric: tabular-nums; } /* ① ② … or "1/9" */
.fc-sheet        { position: fixed; inset: 0 0 0 auto; width: min(420px, 100%); border-left: var(--fc-border-width-heavy) solid var(--fc-color-border-strong); background: var(--fc-color-surface-raised); }
```

Why generic: a sparkline-in-a-cell, a target-scaled inline bar, a lane scaffold and a drawer are the four things every dense Foolscap dashboard (not only Civ) needs; none encode Civilizer semantics. The graph pattern (`docs/graph.md`) shows the repo already accepts such app-agnostic figure scaffolds.

Not worth upstreaming: age-aware x axis (`gScale`), player colouring, outcome badges — all Civ-specific.

---

## 6. Live-play phone view (one screen, 390×844)

What the player needs between turns: am I ahead, what changed, what's due, what did I say I'd do. One screen, no scrolling for the first three.

```
┌────────────────────────────────────────┐
│ Ibn Battuta · Exploration T1/160  ● LIVE│  sticky, 1 line, monitor word not dot
├────────────────────────────────────────┤
│ STANDINGS        you   lead   Δ10t     │
│ Econ    4/9    2,141  3,439  ▲1        │  one row per path, your value, leader value,
│ Sci     9/9        6     33  –         │  rank delta; tap row → path detail
│ Cult    4/9       24     65  ▲2        │
│ Mil     1/9       50     44  –         │
├────────────────────────────────────────┤
│ THIS TURN                              │
│ gold 199 ▼215   sci 47 ▼39  prod 185 ▲34│  3 yields that moved most, Δ vs last turn,
│ treasury 2,385 · cap 1,500  ⚠ 885 lost │  age-end cap warning when relevant
│ settlements 10/8 ⚠ over cap            │
├────────────────────────────────────────┤
│ DUE                                    │
│ Research  —  pick one          ⚠       │  anything idle or about to finish
│ Civic     —  pick one          ⚠       │
│ Goal: Profitable raiding  0%  by T80   │  the one open goal nearest its deadline
├────────────────────────────────────────┤
│ ADVISOR  (last note, T160)             │
│ "Check the carry-over cap before…"     │  one line, tap → Story
└────────────────────────────────────────┘
```
Rules: one plated block (Standings); mono caps labels; no sparklines (too small at 390 px — use ▲▼ with numbers); warnings only when a threshold is crossed (settlement cap, treasury over carry-over cap, idle research, overdue goal); refresh on `/api/version` token as today. This is `#now` on mobile; on desktop the same data is the top strip of the Now tab.

---

## Appendix — quick-fix list (ordered by effort)

1. Rename `.status` header rule → `#monitor` (style.css:59–60). 1 line.
2. `target > 0` guards (dashboard.py:227, app.js:403,408,444,447). 4 lines.
3. `minmax(min(420px,100%),1fr)` on `.g2/.g3/.standings` (style.css:102–105). 3 lines.
4. `aria-current` on tabs; `label` option in `fcChart`; `<thead>`/`scope` in `h("table")` builders. ~20 lines.
5. Suppress Story age tables with no baseline or <5 turns. ~5 lines.
6. Rank denominator: rank against all majors with any visible value this age. ~10 lines.
7. Then the structural work: §3 tabs, §4.1 table, §4.2 matrix.
