# civilizer

Extracts Sid Meier's Civilization VII game state for an AI coach. It reads three sources:

| Source | What it gives | How |
|---|---|---|
| **Live game** | Everything the player's screen shows: top-bar yields and treasury, every settlement (pop, yields, growth, happiness/unrest, build queue, buildings, trade income, assigned resources, treasure fleet timers), commerce screen trade routes (active / available / unavailable with reasons), rivals (relationship, war and war support, yields, route capacity), Triumph progress (major/minor, first-only races), research/civic, government and policies, units, visible enemy units, notifications | Civ VII ships with the Coherent Gameface UI debugger on `127.0.0.1:9444` (Chrome DevTools Protocol). `js/snapshot.js` runs inside the game UI and calls the same JS API the game's own screens use |
| **Save files** (`.Civ7Save`) | Metadata (turn, age, difficulty, map, mods, version), player roster, per-player gold and influence, save timeline with reload-branch detection | Uncompressed typed-chunk header and a zlib block stream holding the game database |
| **Logs** (`My Games/.../Logs/*.csv`) | Turn-by-turn stats (yields, treasury, happiness, settlements), trade route events, purchases, build queues, combat, diplomacy events | CSV parsing. The game wipes logs on every launch, so `archive-logs` copies them to `data/archive/` |

Plus `rules`, a lookup over the game's installed text and data XML, and `pedia`, an indexed Civilopedia
(see [Civilopedia index](#civilopedia-index)).

## Getting started

One-time setup:

```bash
cd ~/src/civilizer
uv sync                               # dependencies
uv tool install --editable .          # optional: puts `civilizer` on PATH
ln -s ~/src/civilizer/.claude/skills/civ-coach ~/.claude/skills/civ-coach   # optional: coach skill outside this repo
```

The game's UI debugger (port 9444) is on by default. If `My Games/Sid Meier's Civilization VII/AppOptions.txt`
has an uncommented `UIDebugger 0`, change it to 1. `civilizer up` warns about this.

Each play session, new game or loaded save:

1. Launch Civ VII.
2. Run `civilizer up`. It serves the dashboard at http://127.0.0.1:8777, opens it, and starts the monitor.
   The dashboard opens on a Games screen that lists every recorded game and repeats these steps.
   The monitor waits at the main menu and records a turn each time you end one. If a monitor or dashboard
   is already running, `up` reuses it.
3. Start or load the game.
4. Open Claude Code in this repo (`cd ~/src/civilizer && claude`) and approve the `civilizer` MCP server
   from `.mcp.json` the first time. Then ask the advisor to set up the game. A short wizard asks you to pick
   an advisor (a preset, a saved one, or a custom voice you describe), what you're here for (fight, build,
   explore, mess around, make a story) plus a follow-up or two, and a strategy ("just win" and "not sure" are
   options). Advisor and player profiles are saved and offered again in later games.
   Outside this repo the MCP tools aren't loaded; the skill falls back to the `civilizer` CLI.

A new game appears in the store and dashboard after its first recorded turn. Until the game's first
autosave, its metadata (leader, difficulty) comes from the live snapshot, and the map name is unknown.
Log sessions and saves are matched to the game by snapshot times, so an older game's history never mixes in.

Run the pieces separately with `civilizer monitor` and `civilizer dashboard`. To rebuild the store from
what's archived without the game running, use `civilizer refresh`.

The dashboard uses the [Foolscap](https://github.com/j4p3/foolscap) design system: its tokens, components,
fonts and Observable Plot chart helper are vendored under `src/civilizer/web/foolscap/` so the page works
offline. After changing Foolscap (expected at `~/src/foolscap`), refresh the copies with
`scripts/vendor-foolscap.sh`. Edit Foolscap itself, not the vendored files.

## Usage

```bash
uv run civilizer status
uv run civilizer report [--detail]        # live if running, else newest save + history
uv run civilizer live [--detail] [--json] [--omniscient]
uv run civilizer saves [--all]
uv run civilizer save head|PATH [--json] [--dump-body FILE]
uv run civilizer history [--log NAME] [--player N] [--cols a,b] [--last N]
uv run civilizer rules "Tariffs"
uv run civilizer archive-logs
uv run civilizer verify [--snapshot FILE]  # cross-check a snapshot against logs + save
uv run civilizer live --map                # + every revealed plot (terrain/feature/resource/owner/river)
uv run civilizer map Baltimore --radius 4  # text map around a settlement or x,y
uv run civilizer watch                     # while playing: snapshot (with map) once per turn, archive logs
uv run civilizer capture                   # whole-game history from the loaded game (js/history.js)
uv run civilizer export [--seed S]         # timeline export to games/<seed>/
```

Installed globally with `uv tool install --editable .` (command `civilizer`), and the
coach skill is symlinked at `~/.claude/skills/civ-coach`. Undo with
`uv tool uninstall civilizer && rm ~/.claude/skills/civ-coach`.

When the game isn't running, `report` shows the newest save. Cities, units and the map
in the save body aren't decoded, so it attaches the archived live snapshot of the same
state if one exists (snapshot turn = save turn - 1).

Live snapshots are archived to `data/snapshots/<game seed>/T<turn>-<time>.json`, so
running the report once per turn builds a full history.


## Analyzer and advisor

Three interfaces share one store, `data/civilizer.db` (SQLite, documented in
[docs/SCHEMA.md](docs/SCHEMA.md) and inside the file as `schema_docs`, `metric_catalog`, `vocab`):

```
game (port 9444) --snapshot.js--> monitor --> snapshots + logs archive --> export --> ingest --> civilizer.db
                                                                                                 |  ^
                                         dashboard (http://127.0.0.1:8777) <--- reads -----------+  |
                                         Claude advisor (MCP / sqlite / CLI) <-- reads, annotates --+
```

```bash
uv run civilizer up                 # play session: dashboard + monitor (what Getting started uses)
uv run civilizer monitor            # while playing: per-turn snapshot -> archive -> refresh the store
uv run civilizer refresh            # rebuild the store's game facts from what is archived (no game needed)
uv run civilizer dashboard          # http://127.0.0.1:8777
uv run civilizer mcp                # MCP stdio server (registered in .mcp.json)
uv run civilizer advisor brief      # the advisor's per-turn briefing; also profile/strategy/goal/annotate
uv run civilizer advisor setup      # setup wizard step as JSON (advisor, player, strategy); also profiles/use/preset
uv run civilizer pedia "Tariffs"    # Civilopedia full-text reader (long chapter text)
uv run civilizer pedia search cothon # indexed Civilopedia: search / page / links / compare / filter / reachable
uv run civilizer schema             # store documentation
```

- **Game facts** (exporter-owned, rebuilt every turn): `games`, `ages`, `players`, `turns`, `metrics`
  (long format, with `visibility` = visible | omniscient), `events` (the timeline, with stable
  `event_key`s), `phases` (fact sheets and heuristic outcomes per age, war and goal; no prose).
- **Advisor tables** (never touched by refresh): reusable, versioned `advisor_profiles` (the
  personality Claude plays: a preset from `src/civilizer/advisors/*.md` or a custom one in
  `data/advisors/*.md`, each a style prompt Claude follows) and `player_profiles` (motives and playstyle),
  linked to each game through `game_profiles`; `strategies` (victory types or expansion), `goals` (long-term goals with
  short-term children) linked to metrics through `goal_metrics`, and `annotations`: the player's
  intentions, priority changes, and Claude's narrative and outcomes (success, partial, failure,
  changed_plans, ongoing) of how each phase went.

## Civilopedia index

`data/civilopedia.db` (SQLite, built from the install on first use; read-only on the game files) holds the
in-game Civilopedia as connected pages: the prose pages (concepts, ages, history chapters) plus one page per
gameplay item with facts from the data XML and explicit edges. Source: `src/civilizer/civilopedia.py`.

```bash
uv run civilizer pedia build [--force]               # build; skipped when the version key is unchanged
uv run civilizer pedia search "gold building" -k building -a antiquity
uv run civilizer pedia page Cothon                     # text + facts + links (key or title)
uv run civilizer pedia links "Code of Laws" -r unlocks # also unlocked_by, prereq(_of), unique_to/has_unique, mentions ...
uv run civilizer pedia compare Cothon Dockyard Market  # facts side by side
uv run civilizer pedia filter -k building -a antiquity -c carthage
uv run civilizer pedia reachable Pottery Chiefdom -s 2 -c carthage   # next techs/civics and what they unlock
uv run civilizer pedia info                            # version, counts
```

The same functions are MCP tools (`pedia_search`, `pedia_page`, `pedia_links`, `pedia_compare`, `pedia_filter`,
`pedia_reachable`), returning compact JSON.

- **Tables**: `pages` (key = gameplay type such as `BUILDING_COTHON` / `NODE_CIVIC_AQ_MAIN_CODE_OF_LAWS`, or
  `CONCEPTS:BIOME_4`, `TERM:SETTLEMENT_LIMIT`; kind, title, section, ages, module, dlc, listed, summary, text,
  chapters JSON, facts JSON), `pages_fts` (FTS5: title, aliases, summary, body, facts), `aliases` (title and key
  lookups), `edges` (src, dst, rel, anchor, detail), `meta` (version key, Steam build id, counts).
- **Facts**: cost, base yields, maintenance, adjacencies, building category (the game's TypeTags: Cothon
  `production`, Dockyard `gold`), placement (terrain, biome, district, river...), unlocked_by, unique_to, quarter,
  unit stats / upgrades / replaces, tech/civic prereqs and unlocks with modifier text ("+1 Settlement Limit."),
  civ and leader abilities, unique items and next-age civ unlocks, resource yields and where they spawn.
- **Edges**: `mentions` from the game's own `[TIP:LOC_PEDIA_CONCEPTS_<TERM>_TOOLTIP]anchor[/TIP]` markup (the only
  in-text link syntax; each tooltip becomes a `term` page), `[icon:<TYPE>]` tokens and, in concept prose, page
  titles (detail `via: name`); data edges `unlocks`, `prereq`, `unique_to`, `replaces`, `upgrades_to`,
  `belongs_to_quarter`, `requires_building`, `placed_on`, `improves_resource`, `found_in`, `unlocks_civ`,
  `obsoletes`, `see_also`. Each is queryable backwards under its inverse name (`unlocked_by`, `has_unique`, ...).
- **Version key**: Steam build id, extractor source and path/mtime/size of every data, config and text file;
  queries rebuild a stale index (checked every 10 minutes).
- **Limits**: all modules are merged (the game builds one database per age and owned DLC), so `<Update>`/`<Delete>`
  rows are not applied and an item's age comes from its Age column, its age module, its civ or its unlocking
  node. Unit abilities, promotions and great-person details beyond the action text are not extracted.
  `civilizer rules` stays the tool for raw modifier XML.

## Game export

`export` merges every source into `games/<seed>/` (gitignored):

- `game.json`: everything below in one file, for a web timeline.
- `events.jsonl` and `events.csv`: one event per line.
- `series_victory.csv`: `victory, age, turn, g, player, points`.
- `series_stats.csv`: Player_Stats per turn, per player, per age (logged turns only).

`game.json` contains:

- `game`: seed, guid, leader, civ, difficulty, map, speed, version.
- `ages`: each age's `turns` and `g_offset`. `g` is the game-wide turn index, with the ages laid end to end.
- `players`: majors by id (ids persist across ages), with the civilization per age. `minors`: city-states and independents per age.
- `series.victory[TYPE][AGE][player_id]`: points per turn, for the current and past ages.
- `series.summary`: the game's summary store (`Game.Summary`): objects (players, cities, religions) plus counts per city and type for buildings, units trained and wonders. It resets at each age start, so it describes the current age only.
- `series.legacy_points`: legacy points per player, by age.
- `series.stats`: the Player_Stats rows.
- `events[]`: `{id, kind, start, end, players, subject, source, precision, detail, minor?}`.
  - `start` and `end` are `{age, turn, g}`. When the turn is only bracketed, they also carry `lo` and `hi`.
  - Kinds: `age`, `war`, `war_declared`, `alliance`, `settlement`, `settlement_captured`, `tech`, `civic`,
    `tech_research`, `civic_research`, `government`, `religion`, `religion_founded`, `unit_killed`, `project`,
    `production`, `purchase`, `legacy`, and diplomacy (`diplomacy.py`): `diplomacy_action`, `befriend`,
    `suzerain`, `diplo_response`, `met`.
  - `precision` is one of:
    - `exact`: from a log or a per-turn snapshot.
    - `range`: happened between two captures.
    - `state`: the state was observed at a capture, with no dates.
  - `minor`: true when an independent power or city-state is involved.
- `coverage`: which turns each source covers, so gaps are explicit.

Sources and their reach:

- **Logs**: wars, captures, religion, kills, production and diplomacy (DiplomacySummary, Game_Gossip), for every player. They only cover
  archived sessions. One session can run into the next age, which is detected from turn resets.
- **Snapshots**: the local player's exact research, civic and government turns.
- **Captures**: every player's researched nodes, government, religion, settlements and wars at
  the captured turn, plus the victory history for all ages. Capture each old save once
  (load it, then run `civilizer capture`) to bracket events in ages without logs.

Log turns are the turn whose end processed the event. Snapshot and capture turns are the
on-screen turn.

## Safety

The live channel executes JavaScript in the running game. The game's native bindings
don't validate arguments, and a malformed call **segfaults the game**. This happened
during development. `snapshot.js` only uses call shapes copied from Firaxis's shipped UI
code, and each one cites its source file. Keep it that way. `civilizer eval` exists for
development and requires `--yes`.

By default the snapshot respects fog of war: unmet civs are anonymous, foreign
settlements appear only if revealed, and enemy units only if visible. `--omniscient`
lifts that.

## Save format notes

`CIV7` magic, then groups of chunks `[u32 marker][u32 type][u32 pad][payload]`. Markers
are stable hashes of field names, shared with Civ VI for game-level fields. Types:
2 utf8, 3 utf16, 8 u32, 29 record, 30 array of markerless records, 32 bytes, and others.
After the header comes a zlib stream framed as `[u32 len][deflate]...`, starting at
`00 00 01 00 78 9c`. It inflates to about 40 MB of game database. Per-player gold and
influence sit after the markers `23 1e 99 37` / `50 3c a8 4a`: next `FXSBLKED`, +8, a
24-bit value, in player-id order. City records exist in the body (each city's x,y as consecutive int32s, in city order,
around offset 18 M in a 43 MB body), but their fields aren't mapped; the live channel
and `watch` archive cover that.

Autosaves are labeled one turn ahead: AutoSave_01_0116 (header turn 116, 1119 gold) loaded as on-screen turn 115 with 1119.3 gold.

Verified against the screen: a screenshot of the top bar (1598 gold +478, +316 science, +152 culture, +364 happiness, 259 influence +66, 13/13 settlements) matched a snapshot taken at the same moment.

## Prior art

- [pydt/civ7-save-parser](https://github.com/pydt/civ7-save-parser): header and chunk format, `REVERSING.md`, marker dictionary
- [iqqmuT/civ7-save-editor](https://github.com/iqqmuT/civ7-save-editor): body decompression, gold/influence navigation
- [ghost-ng/Civ7-Developer-Docs](https://github.com/ghost-ng/Civ7-Developer-Docs): community notes on the UI scripting API
