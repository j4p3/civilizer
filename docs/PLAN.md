# Plan: key metrics, consolidated dashboards, live surfacing

Status: planning only (2026-11-04). Nothing here is implemented. Part D (UI) and Part E (foolscap) come from the
UI/UX audit in `docs/ux-audit.md`.

## Why

The dashboard shows a lot of game data and very little game *analysis*. Everything on it is a surface number the
game already shows in its own screens (yields, ranks, legacy points). The analogue is baseball before sabermetrics:
batting average everywhere, and no number that says who is actually winning. The goal of this plan is to find the
Civ VII equivalent of wins above replacement (WAR), compute it from the data the monitor already collects, and
rebuild the dashboards around it.

## Sources read before the analysis

`README.md` (sources, their reach, the export format and `coverage`), `docs/SCHEMA.md` (every table; the
`metrics` long format with `source` and `visibility`; `metric_catalog` conventions: `description` with caveats,
`higher_is_better`, `victory`; `phases.facts` already carrying per-path standings at age end; `events.precision`
exact/range/state), `docs/NARRATIVES.md`, `docs/TODO.md`. Constraints they impose on anything below:

- **Visibility.** Advice relies on `visible` rows. A live metric that compares the player with "the field" must be
  computed from the rivals the player has met and can see (the rivals screen shows yields), and labelled as such.
  Post-game analysis may use `omniscient` rows. Store both, as the existing metrics do.
- **Coverage is explicit, not assumed.** Logs cover archived sessions only; captures bracket events between saves.
  Ada's Antiquity has legacy points from a capture and no yield series. Any derived series must carry the same
  `source`/`visibility` and inherit the gap rather than interpolate across it.
- **`vp_*` are per age; carried legacy points live in the capture (`series.legacy_points`)** and should become a
  stored metric before anything compares across ages.
- **Derived metrics already exist** (`combat_*_total`, `unexplained_gold`, `trade_income_per_turn`,
  `relationship`, `war_support`): follow their pattern (`source=derived`, catalog row with formula and caveats).
- **Game.Summary (`series.summary`)** has per-city counts of buildings by type for the current age. It is the
  natural input for diagnosing *why* conversion is low (gold buildings vs. resources slotted), not the metrics table.
- **Foolscap is vendored** (`scripts/vendor-foolscap.sh`); edit `~/src/foolscap`, then re-vendor. Never edit the
  copy.

## Part A. What the data says the key factor is

The full step-one analysis (victory rules, carry-over, the four hidden variables, the two-mode visibility rule) is
`docs/KEY-DRIVERS.md`. This part is the empirical evidence from the two recorded games that fed it.

Method: for each age with per-player yield series (Carthage Antiquity, n=10 majors; Ada Exploration tail and Ada
Modern, n=8), Spearman rank correlation between candidate factors and (a) each legacy/victory path's points at age
end, (b) a composite of the four path ranks. Script: `scratchpad/keyfactor.py` (to be moved into `scripts/`).

### A1. Engine beats everything, and the engine is population

| factor (at age end) | Carthage Ant. | Ada Expl. | Ada Modern |
|---|---|---|---|
| population share of world | 0.89 | 0.83 | 0.73 |
| production share | 0.89 | 0.83 | 0.59 |
| throughput share (sci+cul+gold+prod) | 0.94 | 0.67 | 0.31 |
| science share | 0.82 | 0.76 | 0.71 |
| gold share | 0.45 | 0.21 | 0.38 |
| culture share | 0.67 | −0.31 | −0.46 |
| yield per pop ("efficiency") | −0.30 | −0.40 | −0.67 |

(Composite correlation; sign negative means tall-and-efficient loses to wide.) Population share is the one factor
that is strongly positive in every sample. Yield per pop is negative in every sample: the game pays for footprint,
not elegance. This is the batting-average finding: efficiency looks like skill and does not predict standing.

Path-specific yields predict their own path (science share → science points 0.95 / 0.62 / 0.88) and almost nothing
else. Gold share does *not* predict economic points until Modern (0.41 / 0.40 / 0.79), because Antiquity and
Exploration economic paths count resources slotted and treasure fleets, not gold. Military points are predicted by
settlements/tiles (0.92 / 1.00 / 0.90), which is close to tautological: Pax Imperatoria counts settlements.

### A2. Engine carries across ages; late growth does not

Exploration throughput share at age end → Modern composite rank: 0.75. Exploration footprint → Modern: 0.71.
Within-age growth multiple → anything: ≤0 in Ada's samples (late growth is catch-up noise, not strength).

### A3. Conversion is where equal engines separate

Points at age end per 1,000 cumulative yield of the path's type varies ~5–10× across players in the same age:
economic conversion in Carthage Antiquity ranged 47–259 per 1,000 gold. Carthage had +109% gold over the AI median
and finished 10th of 10 economic: an engine that was never pointed at the scoring rule (resources slotted). Ada in
Modern had the best culture conversion (8.5 vs 2.6–7.3) on the lowest culture engine (−57% vs median).
Conversion is the sequencing/decision metric; the engine is the position metric. The advisor can act on conversion
every turn. It can only act on the engine over tens of turns.

### A4. The proposed metric family: Yields Above Replacement (YAR)

Replacement level = **the median AI major in this game at this turn**. It absorbs difficulty handicaps (Sovereign
vs Immortal AIs get different multipliers; the field is always the field) and needs no cross-game calibration.
Three computed metrics, all per player, per turn, all cheap to compute from existing `metrics` rows:

1. **Engine** — `pop_share` (population / sum over majors) and `engine_share` (population share + production
   share)/2. One number, 0–1, rank it. This is the WAR-like headline: the probability-of-winning proxy.
2. **Tempo** — `lead_turns[yield]`: the number of turns ago (or ahead) that the AI median trajectory reached the
   player's current value. "You're 14 turns ahead on science and 9 behind on culture" is comparable across yield
   types and across ages, and it is what the player feels. Composite: `lead_turns[engine]`.
3. **Conversion** — `conversion[path]` = path points gained this age / cumulative path-yield this age, divided by the
   field median of the same ratio. 1.0 = converting like the field. Below ~0.6 with an engine above median is the
   signature of "rich and losing" (Carthage T78) and should be an alert.

Plus one existing concept made comparable: **Legacy pace** = (path points / path target this age) / (age progress
fraction). ≥1.0 means on track to complete the path. The Modern economic victory has a moving target (`vp_economic_target`);
every other path's final milestone is a fixed `RequiredPathPoints` in `Base/modules/age-*/data/victories.xml`,
verified on 2026-11-04 (milestones 1/2/final):

| age | economic | science | culture | military |
|---|---|---|---|---|
| Antiquity | 7 / 14 / **20** | 3 / 6 / **10** | 2 / 4 / **7** | 6 / 9 / **12** |
| Exploration | 10 / 20 / **30** | 1 / 3 / **5** | 6 / 9 / **12** | 4 / 8 / **12** |
| Modern | 150 / 300 / **500** (legacy) | 1 / 2 / **3** | 5 / 10 / **15** | 10 / 15 / **20** |

These belong in `rules.py` (read from the XML, not hard-coded) and in `metric_catalog` as `vp_*_final` per age so
legacy pace is computable for every age, not only Modern.

### A5. Answering the cross-victory question

"How do you compare a science player's progress, which pays off only in Modern, with a domination player's?"
Three answers, in order of usefulness:

- **Within an age:** legacy pace is the common currency. Both paths have a target and both have a clock. A science
  player at pace 1.3 and a conqueror at pace 0.9 are comparable statements, and the dashboard should say them.
- **Across ages:** science and conquest both cash out into the *engine*. Techs raise yields; captured settlements
  raise population. So `lead_turns[engine]` is the cross-age exchange rate: the science player is buying N turns of
  engine lead for the next age; the conqueror is buying M. Show both players' engine tempo and the comparison is
  made without pretending codices and captured cities are the same unit.
- **At game end:** only Modern victories end the game, so the final test is Modern legacy pace. Everything earlier
  is position (engine) plus carried legacy points. Legacy points carried across transitions should be stored
  explicitly (the capture already has `legacy_points`).

What it can't do yet: compare across *games* (different difficulty, map, player count). That needs a
difficulty-normalized replacement baseline, which needs more than two games. Design for it (store the AI median
series per game so a cross-game baseline can be fitted later), do not build it now.

### A6. Validation on the two games before building UI

1. Compute Engine, Tempo, Conversion and Legacy pace per turn for both games (script in `scripts/`), reading
   coverage from the store so Ada's Antiquity is reported as "legacy points only", not as zeros.
2. Check they reproduce the known story without being told it: Carthage rich-and-losing until ~T100 (conversion
   alert fires), Carthage engine passing median around T100–110, Ada last-in-everything Antiquity (engine rank 8
   with 2 settlements over cap), Ada Exploration engine climb, Ada Modern conversion jump at T74/T76 (banker,
   project).
3. Check the Antiquity-end engine rank predicts Modern outcome better than Antiquity-end legacy rank (Ada: last in
   legacies at Antiquity end, won the game; expect engine rank to have been mid-table by Exploration T20).
4. Decide thresholds for alerts from the distributions (conversion < 0.6, tempo swing > 5 turns in 10 turns).
   Also settle the Engine definition: population alone vs population+production vs population+tiles, correlated with
   composite standing per age-sample and with the *next* age's standing; population alone is the default unless a
   composite wins clearly on both games.
5. Only then add them to `metric_catalog` and `ingest.refresh` as derived metrics (`source=derived`), visibility
   `visible` when computed from visible rows only, `omniscient` otherwise.

## Part B. Store and ingest changes (after A6)

- New derived metrics in `metrics`: `pop_share`, `engine_share`, `engine_rank`, `lead_turns_<yield>`,
  `lead_turns_engine`, `conversion_<path>`, `legacy_pace_<path>`, and `ai_median_<yield>` (the replacement
  baseline itself, stored so charts can draw it and so cross-game calibration is possible later).
- `metric_catalog` rows with `description` that states the formula, and `victory` tags so the dashboard can group
  them.
- **Visibility propagates through computation**: each derived row is written twice when the inputs differ — once
  from `visible` inputs only (field = met rivals; labelled with the count) and once `omniscient` — so live mode and
  post-game mode read different numbers for the same metric, by design (`KEY-DRIVERS.md` §5.1). Pace and victory
  closeness are the exception: their inputs are visible for the whole field.
- A live-only `self_tempo_<yield>` (turns gained/lost against the player's own trajectory) that needs no rivals.
- `phases.py` heuristics switch from "rank 1 counts" to engine rank + legacy pace at age end; war verdicts add the
  engine delta over the war (did the war buy engine or spend it?).
- `advisor brief` leads with Engine rank and tempo deltas since `since_g`, then Conversion alerts, then the
  existing goals/metrics block.
- Schema version bump; `docs/SCHEMA.md` regenerated.

## Part C. Code cleanup (independent of the metric work)

- `web/app.js` (1,163 lines) is one file with router, header, seven renderers, charting and timeline. Split now
  (decided) into `app.js` (router/state), `pages/*.js`, `charts.js`, `timeline.js`; keep the no-build `h()`/`s()`
  helpers; ES modules, no bundler.
- `dashboard.py` ranks standings in `_standings` and again in `api_overview`; `phases.py` ranks a third way. One
  ranking helper in the store layer, used by all three and by the advisor.
- Metric-series fetching is repeated in `api_overview`, `api_metrics`, `api_turn` and `advisor.py`; one
  `series(con, game, metric, players, lo, hi, vis)` function.
- Stale war events surviving reloads, pedia index showing the wrong age's resource text, `trade_routes`
  undercount: known bugs, listed in `docs/TODO.md`, fix during this pass because the new metrics depend on clean
  event ranges.

## Part D. Dashboard consolidation (from `docs/ux-audit.md`)

The audit (44 screenshots, desktop and 390 px, both themes) found the real page set is Games + Overview, Story,
Timeline, Charts, Turn by turn, Advisor. Its structural findings, adopted here:

- **Bugs to fix first** (appendix, ~40 lines total): `.status` CSS collision breaking every goal header; the `-1`
  standings target rendered literally; horizontal overflow at 390 px from `minmax(420px)`; inconsistent rank
  denominators ("#1 of 4" vs "#4 of 9") on one screen; tab bar truncation.
- **Tufte problems**: Overview is twelve one-number tiles with no rival context above five bar charts that re-sort
  the same nine names; the timeline spends ~45 lanes on unlabeled policy bars and hairline production ticks; goals
  are rendered in three places, standings in three, outcomes twice; Story pages run 9–19k px with the narrative
  buried under fact tables.
- **Proposed structure, four tabs**, comparison-first, with the two modes from `KEY-DRIVERS.md` §5.1 as a toggle
  that changes *which* computed metrics lead, not only which raw rows show:
  1. **Now** — standings matrix (player × path, rank glyph, target-scaled inline bar) plated at the top; below it the
     mode-dependent key metrics (live: Pace, Victory closeness, own Conversion, Engine vs met rivals; post-game:
     Engine share and Tempo vs the true field); then the yield small-multiples table (you bold, rival envelope,
     leader named, rank + Δrank), expandable in place to the full chart.
  2. **Story** — narrative-first phase cards, facts in a collapsed rail, goals nested under their phase, the
     advisor plan and annotations absorbed here. Each age card opens with Engine rank, Tempo and Conversion.
  3. **Timeline** — ~10 layered lanes (rule, policy count strip, tech/civic ticks, build density, settlements, wars,
     kills, notes) over one context yield sparkline; clicking a turn opens a side sheet with that turn's Δ and
     events, replacing the Turn tab.
  4. **Empire** — the only per-settlement page: settlements × yields with a sparkline per row.
- **Phone live view** (audit §6): one screen — standings rows (you / leader / Δ10t), the three yields that moved
  most, cap and idle warnings, the nearest goal, the last advisor line. The key-metric row from Now sits above the
  standings when in live mode.
- **Accessibility**: `aria-current` on tabs, named figures, labelled meters, table heads; status as a word not only
  a dot; a 9–12 player colour convention (subject ink, rest grey, one highlight) instead of twelve hues.

## Part E. Foolscap extensions (from `docs/ux-audit.md` §5)

The dashboard re-implements `fc-pane/-note/-dot/-label/-stat` under its own class names (~60 lines deletable by
adopting the originals). Generic extensions to build in `~/src/foolscap` and re-vendor:

| extension | why generic | API sketch |
|---|---|---|
| `fcSpark(el, points, {band, ref, last, label})` | sparkline in a table cell; `fcChart` forces width ≥ 320 today | returns `{render, dispose}` like `fcChart` |
| `.fc-bar` / `.fc-rank` | target-scaled inline bar and rank glyph as table apparatus | `--fc-bar: 0..1` custom property |
| `fcLanes(el, {x, lanes, rowHeight, labelWidth, marks})` | gantt scaffold, Plot stays the renderer | caller supplies marks |
| `.fc-sheet` | slide-in drawer with focus trap | fixed right, `min(420px, 100%)` |
| `fcChart(el, build, {label})` | accessible name for figures | sets `ariaLabel`, links figcaption |
| many-series guideline + `t.subject / t.rest / t.highlight` tokens | the 8–12 player problem is every dense dashboard's problem | `docs/guidelines.md` §6 |

Civ-specific pieces that stay in `web/`: the age-aware x axis, player colouring, outcome badges.

## Part F. Surfacing the metrics in post-game analysis and live

Post-game (Story/phases):
- Every age card opens with three numbers and three sparklines: Engine rank (with the AI median line), Tempo lead
  in turns for the player's chosen path yield, Conversion ratio for the chosen path. The narrative follows.
- The age fact sheet given to Claude for narratives includes the turn the engine crossed the median, the worst
  conversion stretch, and the largest tempo swing, so the roast has the turning points handed to it.
- The game overview replaces the four-path standings bars with one "position" chart: engine share of all majors
  over the whole game, age boundaries marked, legacy points carried at each transition annotated.

Live (per turn, phone-first):
- One screen: Engine rank and its change since last check; Tempo lead for the active path; Conversion ratio with a
  single-line reason when it's low ("gold +109% vs field, economic conversion 0.5: resources unslotted"); the
  current goal's progress; alerts.
- The follower speaks only on a tempo swing, a conversion alert, or a goal event, consistent with the existing
  "speak when there's a decision" rule.

## Part G. Order of work

1. A6 validation script and findings note (no product code).
2. Part C cleanup, with tests, so the new metrics land on a clean base.
3. Part B derived metrics + catalog + schema bump.
4. Part E extensions in `~/src/foolscap` (branch + PR there), then `scripts/vendor-foolscap.sh`; Part D page
   consolidation on top of the re-vendored copy. Civilizer-specific pieces stay in `web/` and never go upstream.
5. Part F live view and advisor brief changes.

## Review gate

This plan is step three of the process the user set: (1) the key-drivers analysis in `KEY-DRIVERS.md`, (2) the UI/UX
audit in `ux-audit.md`, (3) this merged modification plan. Nothing in Parts B–G is built until the plan is reviewed and
approved. Validation (A6) is analysis, not product code, and can run before approval.

## Decisions (user, 2026-11-04)

- **Replacement level = the mean of all majors, including the human.** Tempo and "above replacement" are measured
  against that mean. Note for the implementation: the player's own value moves the baseline, so a large outlier
  (Ada Modern, +234% gold) reads smaller than against a median; label the field size and compute the AI-only median
  as a secondary, unshown series so the two can be compared during A6.
- **Engine = population share, unless A6 shows a composite is materially more predictive.** Simpler is better, but
  not at the cost of accuracy: A6 must report population alone vs population+production vs population+tiles on the
  same samples, and the composite wins only if it beats population alone by a margin that survives both games.
- **Modes = one dashboard with the global toggle; the mode changes which computed metrics lead** (and which
  visibility the derived rows are read with).
- **Part C = full `app.js` split into router / pages / charts / timeline modules now**, before the four-tab rebuild.

## Open questions

- Victory closeness in Exploration: recompute from the rule (6×/4× from 50% age progress) even though the game shows
  no target there?
- Conversion's path-yield mapping as a first cut (econ→gold before Modern is known to be a poor proxy)?
- Live view: phone-first as assumed, or desktop second monitor?
- Foolscap: open the upstream PRs as part of this work, or batch them after the dashboard proves the components?
