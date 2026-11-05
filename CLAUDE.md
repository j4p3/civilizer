# Civilizer — Civ VII coach

This repo is a game-state extractor for Sid Meier's Civilization VII (native Linux,
Steam) plus coaching instructions. When the user asks about their Civ game, get the
facts from the tool before advising. Never guess at game state.

## Getting the game state

```bash
uv run civilizer report            # best available: live game, else newest save + log history
uv run civilizer live --detail     # live, with per-city yield tooltip breakdowns
uv run civilizer live --json       # full structured snapshot
uv run civilizer saves             # save timeline; flags saves on abandoned (reloaded) branches
uv run civilizer save head         # newest save of the current game: roster, gold/influence per player
uv run civilizer history --last 10 # turn-by-turn stats (from archived + current logs)
uv run civilizer history --log Game_TradeManager   # any log in logs.HISTORY_LOGS
uv run civilizer rules "tariffs"   # game rules: description + modifiers from the game's data XML
uv run civilizer archive-logs      # do this before the user relaunches the game (logs are wiped)
uv run civilizer verify           # cross-check live snapshot vs logs and save
uv run civilizer map <settlement|x,y> [--radius N] [--live]   # terrain/resources/owners/units nearby
uv run civilizer up               # what the user runs per play session: dashboard + monitor (reuses running ones)
uv run civilizer monitor          # run in background while the user plays: per-turn snapshot + log archive + store refresh
uv run civilizer dashboard        # web dashboard over the store, http://127.0.0.1:8777
uv run civilizer advisor brief    # per-turn advisor briefing from the store (also via the `civilizer` MCP server)
uv run civilizer pedia "<topic>"  # Civilopedia text; `rules` has the authoritative modifier numbers
uv run civilizer pedia search|page|links|compare|filter|reachable ...  # indexed Civilopedia (data/civilopedia.db): facts + links
```

## Central store

`data/civilizer.db` is the single structured repository: the monitor writes game facts into it every
turn, the dashboard reads it, and the advisor reads it and writes profiles, strategies, goals and
annotations. Its schema is documented in `docs/SCHEMA.md` (generated: `civilizer schema --write docs/SCHEMA.md`
after editing `store.TABLES`) and in the `schema_docs` table. Exporter-owned tables are rebuilt by
`ingest.refresh`; advisor-owned tables must never be written by the exporter. Narrative prose is written by
Claude as annotations scoped to a `phases` row, not generated in code.

Source priority: the **live snapshot** shows what the player sees right now. Saves can lag
behind it and can sit on abandoned branches, so check `civilizer saves` before treating a
save as current. Logs cover only sessions that were archived or are still in `Logs/`.

Live snapshots are archived automatically under `data/snapshots/<seed>/`. Read old
ones to compare turns instead of re-querying.

## Hard safety rule: the live game can crash

The live channel evaluates JS inside the running game (Coherent UI debugger, port 9444).
Native bindings do not validate arguments: on 2026-11-02, an exploratory call
(`player.Stats.getYields` with no args) segfaulted the game and lost the user's progress.

- Only run `civilizer live`/`report`, which execute the vetted `src/civilizer/js/snapshot.js`.
- Do not use `civilizer eval` or write ad-hoc CDP calls against the user's game.
- To extend `snapshot.js`, copy a call site from Firaxis's UI code under
  `~/.local/share/Steam/steamapps/common/Sid Meier's Civilization VII/Base/modules/**/ui*/`
  with the same argument shapes, cite it in a comment, and tell the user before the
  first run so they can save first.

## Coaching

- Answer from the snapshot: quote the actual numbers (per-turn yields, treasury,
  settlement cap, Triumph progress, trade route statuses and why
  routes are unavailable, war support, unrest, treasure fleet timers).
- The default view respects fog of war, so it matches the player's screen. `--omniscient`
  exists for debugging. Don't use it for advice unless the user asks.
- For "why did X change" questions, diff two archived snapshots or use `history` rows
  around the turn. Reloads show up as abandoned saves in `civilizer saves`.
- For mechanics questions, use `civilizer rules <name>` and read the modifier blocks
  rather than relying on memory. Civ VII patches change numbers.
- Autosaves are labeled one turn ahead of the on-screen turn: AutoSave_01_0116 loads as turn 115.
  Prefer the live value.
