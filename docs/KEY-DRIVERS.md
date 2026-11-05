# What actually drives a game of Civ VII

Step one of `docs/PLAN.md`: the abstractions that strip away the minutiae and point at the thing you care about for
winning. The analogue is wins above replacement: one computed number that explains outcomes better than the surface
stats the game shows you. Status: analysis, 2026-11-04. Sources: the game's own rules XML and Civilopedia (via
`civilizer rules` / `pedia`), community theorycrafting (cited inline, mostly from the game's first months and to be
treated as hypotheses), and the two recorded games in the store.

## 1. How the game decides who won

Read the victory rules before deciding what to measure. They constrain everything.

**Only the Modern Age ends the game.** Three of the four Modern victories (Economic/GDP, Cultural/Tourism,
Military/Dominion) are *relative*: you trigger a 5-turn countdown by holding a lead over **second place** of at least
a multiplier that decays with age progress: 6× (Exploration 50–79%), 4× (Exploration 80% – Modern 20%), 3× (Modern
20–39%), 2× (40–59%), 1.5× (60–79%), 1.25× (80–100%). Science is *absolute*: 100 Innovation plus an active Launch Pad
for 5 turns. If nobody wins, score decides, and score weights turns spent in a countdown at 1,000 each.
(`AGES:VICTORY_*_MODERN` pedia pages.)

Consequences:
- Three victories are a **race against the runner-up's rate**, not against a fixed number. Ada won Economic at
  15,335 against a target of 14,588 = 2 × Tecumseh's 7,294. The thing that mattered was the ratio, and the ratio's
  required value was falling 0.5× every ~20% of the age. A player with ratio 1.6 at Modern 55% is 5 turns from a
  countdown. A player with 1.6 at 30% is 30 turns away.
- Science is a **time trial**: Innovation accrues mostly from projects (5–15 points each) and is capped by how fast
  cities can run them, which is production.
- Antiquity and Exploration cannot be won. They are **position-building**, scored by legacy paths whose points buy
  Dedications (carried bonuses) and golden ages at the transition.

**Age length is shared.** The Age Progress meter fills from every player's milestones (5/5/10 points per
milestone tier), future techs/civics (10 each), and a slow per-turn tick; at 95% a 10-turn countdown begins. So the
field's pace sets your clock. A fast field shortens Antiquity and devalues anything that needs 40 more turns.

**Legacy milestones are fixed and small.** Final milestones (verified in `victories.xml`): Antiquity econ 20 /
sci 10 / cul 7 / mil 12; Exploration 30 / 5 / 12 / 12; Modern 500 / 3 / 15 / 20. These are *targets*, so progress
toward them is naturally expressed as a fraction, which is the first thing that makes paths comparable.

## 2. What carries across a transition (and therefore what is real)

From the pedia and the transition rules: settlements, population, tiles, Wonders, Unique Quarters and Ageless
buildings carry; standard buildings keep base yields but **lose adjacency**; Great Works go; a chosen set of units
go; gold carries (capped at 3,000 after Continuity); legacy points convert to Dedications; cities may demote to towns.
Techs and civics reset: the age's whole tree is new.

So the durable state of an empire at a transition is: **how many people live where, on how many tiles, with how many
slots** (resource capacity, specialist capacity, quarter tiles). Yields are *derived* from that state each age;
they are re-earned. This is why population and footprint, not yields, are the right "position" variable: they are
what survives.

Community play confirms it from the other side: the dominant Antiquity theory is "maximize the next era's slingshot,
not Antiquity yields" (six-city, zero-town Antiquity into a Deity Exploration with six overseas settlements by T18 and
the economic path done by T72 — [CivFanatics: City-Heavy Antiquity](https://forums.civfanatics.com/threads/city-heavy-antiquity-strategy-for-exploration-snowball.696281/)),
and the wide-vs-tall threads agree expansion is "free until the limit" and the real variable is how many settlements
become cities ([CivFanatics: Current Meta and Tall vs Wide](https://forums.civfanatics.com/threads/current-meta-and-tall-vs-wide.695443/)).

## 3. The hidden variables

Four abstractions, each a function of data the monitor already records, each defined relative to **the field** (the
other majors in this game), because the AI's handicaps make absolute numbers meaningless across difficulties:
Immortal AIs run gold and happiness at handicap level 60 and science/culture/buildings at 4 of 6; Deity at 80 and 5
(`base-standard/data/difficulties.xml`; the percent each level maps to is in the handicap system tables and should
be read by `rules.py` rather than quoted).

### 3.1 Engine (position)

`engine_share = population / Σ population over majors` (with production share as a secondary). It is what carries
across transitions, it is what every yield is derived from, and in the two recorded games it is the single factor
that correlates with composite legacy standing in every age-sample (0.89, 0.83, 0.73; see `PLAN.md` A1), while
yield-per-pop correlates *negatively* everywhere. Engine is the WAR headline: where you stand.

Replacement level (decided 2026-11-04) is the **mean of all majors in this game at this turn, the human
included**. "Engine above replacement" is the difference from that mean, in share points, with the rank alongside.
The AI-only median is kept as an unshown comparison series during validation. Population share is the default
definition; `PLAN.md` A6 tests it against population+production and population+tiles and keeps the simplest one that
isn't clearly less predictive.

### 3.2 Tempo (time)

Yields compound: a unit of yield at turn 50 is worth several at turn 150 because it buys the things that buy more
yield. The only quantitative treatment found is a Civ V model fitting required science-per-turn to an exponential
(e^0.0176·T, so turn 100 ≈ 2.4× turn 50) and concluding early differences dominate
([CivFanatics: yield discount](https://forums.civfanatics.com/threads/math-calculating-a-yield-discount.666344)).
Its numbers don't transfer; its shape does, and Civ VII adds a sharper version: the shared age clock means tempo is
literally turns.

So express every comparison in **turns**, not points: `lead_turns[x] = current_turn − (the turn at which the field
median reached the player's current value of x)`, negative when behind. It is the same unit for science, culture,
gold, production and engine; it is the same unit in every age; and it is how players already think ("I'm ten turns
behind on Cartography"). The composite `lead_turns[engine]` is the cross-age exchange rate: a science player and a
conqueror each convert their path into N turns of engine lead for the next age, and those N's are comparable.

### 3.3 Conversion (decisions)

`conversion[path] = (path points gained this age / cumulative path-yield this age) / field median of the same`.
This isolates sequencing from engine. In the recorded games it varied 5–10× between players with similar engines
(economic 47–259 points per 1,000 gold in Carthage's Antiquity). Carthage had +109% gold over the median and
finished 10th of 10 economic: the engine was never pointed at the scoring rule (resources slotted, not gold earned).
Ada in Modern had the best culture conversion on the lowest culture engine. Conversion is the metric the advisor can
move *this turn*; engine moves over tens of turns. A low conversion with an above-median engine is the alert that
would have fired for Carthage at T60 and been right.

Why conversion varies: each path scores a narrow proxy (slotted resources; slotted codices; wonders; settlements),
so the engine's yield has to be routed through the right buildings, slots and projects. The export's
`series.summary` (per-city counts of buildings by type) is the natural diagnostic for *why* a conversion is low.

### 3.4 Pace (the clock)

`legacy_pace[path] = (points / final milestone) / (age progress fraction)`; ≥1.0 is on track to finish the path
before the age ends. In Modern, replace the milestone by the victory condition itself:
`victory_closeness = (my points / second place's points) / required multiplier for the current age progress`,
which is ≥1.0 exactly when a countdown can start, and whose derivative says how many turns away it is. Science uses
`innovation / 100` plus a Launch Pad flag. These are the within-age common currency across victory types: "pace 1.3
on Great Library" and "pace 0.9 on Pax Imperatoria" are commensurable statements.

### 3.5 What is deliberately *not* a key variable

- **Gold per turn, treasury.** Instrumental. Gold only matters through what it buys (engine or conversion), and it
  is the yield the game lets you over-accumulate (cap 3,000 at transition). Carthage proved this.
- **Score.** Community AI-only studies (Civ V/VP, ~90 games) found score explains r² ≈ 0.16 of winning
  ([CivFanatics](https://forums.civfanatics.com/threads/some-statistics-pertaining-to-relative-civilization-victory-type-power-in-100-ai-only-games.678818)).
  Same shape expected here.
- **Rank on a single yield.** Predicts its own path and nothing else (PLAN.md A1).
- **Yield per settlement / per pop.** Looks like skill; correlates negatively with standing. The game pays for
  footprint.

## 4. The model in one line

> Position (engine share vs field) compounds through time (tempo in turns) and is converted into legacy or victory
> points at a rate the player controls (conversion), against a shared clock (pace). Everything else on the screen
> is an input to one of those four.

What the four say about the two recorded games, in hindsight: Ada's Antiquity was an engine problem (last in
population share, over cap, raided), her Exploration an engine recovery (tempo swung ~+30 turns in 60), her Modern a
conversion sprint (banker, projects) timed to a falling required multiplier. Carthage's Antiquity was a conversion
failure on a healthy engine until T100, then a conquest that bought engine directly.

## 5. What this app can and cannot compute

| variable | inputs | available now | gap |
|---|---|---|---|
| Engine share, rank, vs median | `population`, `production_per_turn` per major | yes, every logged/visible turn | Ada Antiquity has no yield rows (captures only); live view must use `visible` rows = met rivals |
| Tempo (lead_turns) | any per-major series | yes | needs the field-median series stored (`ai_median_<x>`) |
| Conversion | `vp_<path>`, cumulative path-yield | yes per age | path-yield mapping is a modelling choice (econ→gold is wrong before Modern: use slotted resources when available) |
| Legacy pace | `vp_<path>`, milestone table, `age_progress` | milestones must be read from `victories.xml` | `age_progress` exists only for the local player's snapshots |
| Victory closeness | `vp_*`, `vp_*_target` (Modern), age progress | yes in Modern | required-multiplier table to encode from the pedia rules |
| Carried legacy points | capture `series.legacy_points` | in export only | promote to a metric |
| Why conversion is low | `series.summary` buildings by type; snapshot resources slotted | partly | summary resets per age; only local player has slot detail |

### 5.1 Two modes, two metric sets

The dashboard already has a visible/omniscient toggle for *data* (rivals' build orders and income before you met
them). The same split applies to *computations*: a derived number inherits the weakest visibility of its inputs, so a
metric that is excellent post-game may be unavailable or biased live. The two modes should therefore pick different
proxies rather than show the same metric with holes in it.

What the store says about visibility of the inputs:

| input | live (visible) | post-game (omniscient) |
|---|---|---|
| legacy / victory points, all majors | **yes** — the Victories screen shows the whole field (`vp_*` visible rows exist for unmet rivals) | yes |
| yields, population, settlements of rivals | met rivals only (rivals screen) | all majors (logs, captures) |
| own yields, slots, resources, projects | yes, in full detail | yes |
| age progress | yes (own snapshot) | yes |
| field median of a yield | median of *met* majors, biased toward neighbours | true median |

So:

- **Live mode** leads with **Pace** and **Victory closeness** (both exact and whole-field live, because they're built
  from `vp_*` and age progress), then **Conversion for the player** (own points / own yields, exact), then **Engine
  and Tempo against the met field**, labelled "vs N met rivals" and never ranked "of 8" when only 4 are visible. A
  live-only proxy worth adding: **self-tempo**, the player's own yield trajectory against their own earlier pace
  (turns gained or lost in the last 10 turns), which needs no rival data at all.
- **Post-game mode** leads with **Engine share and Tempo against the true field**, the metrics that explained outcomes
  best in the two recorded games but that the player could not have seen at the time, then Conversion for every
  player (so the narrative can say who converted and who didn't), then Pace as the record of the race. It can also
  compute what the live view *missed*: the turn at which the true engine rank diverged from the met-rivals engine
  rank, which is itself a finding for the postmortem ("you were 3rd of the four you'd met and 7th of 8").

The store must record derived rows with `visibility` set from their inputs, as it already does for raw metrics, so
the live view can filter on `visible` and the post-game view on everything.

## 6. Open questions for the user

Resolved: replacement level = mean of all majors including the human; Engine = population share unless validation
shows a composite is clearly more predictive (see `PLAN.md`, Decisions). Still open:

- Should the Modern "victory closeness" use the game's own `vp_*_target` (already 2× runner-up) or recompute from
  the rule so it works in Exploration (where the game shows no target but the rule applies from 50%)?
- Is the path-yield mapping for conversion acceptable as a first cut (econ→gold, sci→science, cul→culture,
  mil→production), with slotted-resource and project-based refinements later?
