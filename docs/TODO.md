# TODO

## Reverse-engineer the save's game-state blob

`save.py` inflates the zlib block stream in a `.Civ7Save` but only reads a few things: the header, the player
roster, and gold/influence balances found next to known markers. The rest is the engine's serialized in-memory
state in an undocumented binary layout. The game reloads it fine, so per-tile data (improvements, worked tiles,
districts) is in there somewhere.

Why it matters: the live snapshot only has per-settlement improvement counts, and per-tile queries would need new
native calls into the running game, which can segfault it (see CLAUDE.md). In the Carthage game (seed
-1906453415), Cowrie, Pearls and Salt sat unimproved next to the capital for 70 turns, and no tool could show it.

Approach: work offline on copies of autosaves only, never against the running game. Diff consecutive autosaves
where exactly one known improvement was placed on a known tile, find the plot array and the improvement field,
then confirm it against `civilizer map` terrain/resource data.

## After the key-metric analysis: surface Engine / Tempo / Conversion

Blocked on `docs/PLAN.md` Part A6 (validate the metrics on the Carthage and Ada games first).

Post-game analysis:
- Age and game cards open with Engine rank (population share vs the AI median line), Tempo lead in turns for the
  player's path yield, and Conversion ratio for the path, each as number + sparkline, before any narrative.
- The phase fact sheet handed to Claude for narratives gains three computed turning points: the turn the engine
  crossed the field median, the worst conversion stretch, the largest tempo swing.
- Overview: replace the four standings bars with one engine-share-over-the-game chart, age boundaries and carried
  legacy points annotated.

Live during play:
- `advisor brief` leads with Engine rank delta since `since_g`, then Tempo, then Conversion alerts (ratio < ~0.6
  with engine above median = "rich and losing"), then goals.
- Phone glance view: one screen with those three numbers, the active goal's progress and the current alert.
- The follower speaks only on a tempo swing, a conversion alert or a goal event.
