"""Central game store: one SQLite file (data/civilizer.db) shared by the monitor, the advisor and the dashboard.

Two kinds of tables:
  - game facts, owned by the exporter (`refresh`): rebuilt from snapshots, logs and history captures every
    turn. Safe to delete and regenerate.
  - advisor tables (profiles, strategies, goals, goal_metrics, annotations): written only by the advisor
    (CLI / MCP) and never touched by `refresh`. They reference game facts by (game, g) and by stable
    `event_key`s, so they survive re-exports.

Every table and column is described in TABLES below; `schema_docs` holds the same text inside the
database so a reader needs nothing but the file. docs/SCHEMA.md is generated from it (`civilizer schema`).

Turn axis: `g` is a game-wide turn index with ages laid end to end (see export.Clock); (age, turn) is the
on-screen turn within an age.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "civilizer.db"
SCHEMA_VERSION = 2

# table -> (owner, description, [(column, type, description)], primary key)
TABLES: dict[str, tuple[str, str, list[tuple[str, str, str]], str]] = {
    "meta": ("exporter", "Store-level key/values: schema_version, monitor heartbeat (monitor_seen_at, monitor_state).", [
        ("key", "TEXT", "Key name."),
        ("value", "TEXT", "Value (JSON or text)."),
    ], "key"),
    "games": ("exporter", "One row per game, keyed by the map seed.", [
        ("game", "TEXT", "Game id: the map seed as text. Every other table carries it."),
        ("guid", "TEXT", "Save-file game GUID."),
        ("leader", "TEXT", "Local player's leader type (LEADER_*)."),
        ("civilization", "TEXT", "Local player's civilization type at the newest save."),
        ("difficulty", "TEXT", "DIFFICULTY_*."),
        ("map", "TEXT", "Map script name."),
        ("map_size", "TEXT", "MAPSIZE_*."),
        ("speed", "TEXT", "GAMESPEED_*."),
        ("app_version", "TEXT", "Game build that wrote the newest save."),
        ("local_player_id", "INTEGER", "Player id of the human player."),
        ("current_age", "TEXT", "Age of the newest observed turn."),
        ("current_turn", "INTEGER", "On-screen turn of the newest observed turn."),
        ("current_g", "INTEGER", "Game-wide turn index of the newest observed turn."),
        ("age_turns_max", "INTEGER", "Age length the game reports for the current age (age_progress.max)."),
        ("updated_at", "TEXT", "ISO time of the last refresh."),
    ], "game"),
    "ages": ("exporter", "Ages of a game laid end to end.", [
        ("game", "TEXT", "Game id."),
        ("age", "TEXT", "AGE_ANTIQUITY / AGE_EXPLORATION / AGE_MODERN."),
        ("idx", "INTEGER", "0, 1, 2 in chronological order."),
        ("turns", "INTEGER", "Turns observed in this age (the last known turn)."),
        ("g_offset", "INTEGER", "g = g_offset + turn."),
    ], "game, age"),
    "players": ("exporter", "Major players. Ids persist across ages; civilization changes per age.", [
        ("game", "TEXT", "Game id."),
        ("player_id", "INTEGER", "In-game player id (0 = usually the human)."),
        ("leader", "TEXT", "Leader display name."),
        ("leader_type", "TEXT", "LEADER_*."),
        ("is_human", "INTEGER", "1 for the human player."),
        ("civilizations", "TEXT", "JSON object age -> civilization display name."),
    ], "game, player_id"),
    "turns": ("exporter", "Turns observed by a live snapshot, on the kept branch (reloaded-away turns dropped).", [
        ("game", "TEXT", "Game id."),
        ("g", "INTEGER", "Game-wide turn index."),
        ("age", "TEXT", "Age."),
        ("turn", "INTEGER", "On-screen turn."),
        ("captured_at", "TEXT", "When the snapshot was taken (game clock, UTC ISO)."),
        ("snapshot_path", "TEXT", "Path of the raw snapshot JSON (full detail: cities, units, trade routes, map)."),
        ("summary", "TEXT", "JSON digest of the local player's state at this turn: research, civic, government, policies, "
                            "wars, notifications. The raw snapshot has everything else."),
    ], "game, g"),
    "metrics": ("exporter", "Long-format numeric series: one value per (turn, player, metric). See metric_catalog.", [
        ("game", "TEXT", "Game id."),
        ("g", "INTEGER", "Game-wide turn index."),
        ("age", "TEXT", "Age."),
        ("turn", "INTEGER", "Turn within the age."),
        ("player_id", "INTEGER", "Player the value belongs to."),
        ("metric", "TEXT", "Metric name (metric_catalog.metric)."),
        ("value", "REAL", "Value."),
        ("source", "TEXT", "snapshot (on-screen), log:<name> (game log, turn-start values), capture (history.js), derived."),
        ("visibility", "TEXT", "visible = the player could see it on screen; omniscient = from logs/captures covering "
                               "unmet or fogged rivals. Advice should rely on visible values."),
    ], "game, g, player_id, metric"),
    "metric_catalog": ("exporter", "What each metric means. Goals link to metrics by name.", [
        ("metric", "TEXT", "Metric name."),
        ("label", "TEXT", "Short display label."),
        ("unit", "TEXT", "Unit (per_turn, count, points, gold, ...)."),
        ("description", "TEXT", "Meaning and caveats."),
        ("sources", "TEXT", "Where it comes from."),
        ("higher_is_better", "INTEGER", "1 if larger is better for the owner, 0 if smaller is, NULL if neutral."),
        ("victory", "TEXT", "Victory/strategy this metric most indicates (science, culture, economic, military, expansion), if any."),
    ], "metric"),
    "events": ("exporter", "Timeline: intervals and point events (ages, wars, settlements, techs, civics, governments, policies, "
               "production, projects, purchases, legacies, unit deaths). Rebuilt each refresh; event_key is stable.", [
        ("game", "TEXT", "Game id."),
        ("event_key", "TEXT", "Stable id (hash of kind, subject, players, start, source). Annotations reference it."),
        ("kind", "TEXT", "age, war, war_declared, alliance, settlement, settlement_captured, tech, civic, tech_research, "
                         "civic_research, government, religion, religion_founded, policy, production, project, purchase, "
                         "legacy, unit_killed."),
        ("subject", "TEXT", "Display subject (tech name, city name, unit type, 'war 0-1', ...)."),
        ("start_g", "INTEGER", "Game-wide start turn."),
        ("start_age", "TEXT", "Start age."),
        ("start_turn", "INTEGER", "Start turn within age."),
        ("start_lo", "INTEGER", "If precision='range': earliest possible start turn (within start_age)."),
        ("start_hi", "INTEGER", "If precision='range': latest possible start turn."),
        ("end_g", "INTEGER", "Game-wide end turn; NULL if open."),
        ("end_age", "TEXT", "End age."),
        ("end_turn", "INTEGER", "End turn within age."),
        ("players", "TEXT", "JSON list of involved player ids (for unit_killed: [victim, killer])."),
        ("local", "INTEGER", "1 if the local player is involved."),
        ("minor", "INTEGER", "1 if an independent power / city-state is involved."),
        ("precision", "TEXT", "exact; range (happened between start_lo and start_hi); state (seen at that turn)."),
        ("source", "TEXT", "snapshot, log:<name>, capture:<age>, derived."),
        ("detail", "TEXT", "JSON with kind-specific fields (city, item, category, completed, victim, killer, x, y, war_type, ...)."),
    ], "game, event_key"),
    "phases": ("exporter", "Fact sheets for phases and initiatives: each age, each war the local player fought, each goal, and "
               "the whole game, with a heuristic verdict. No prose: Claude writes the narrative as annotations with "
               "scope/scope_key pointing here. Regenerated each refresh.", [
        ("game", "TEXT", "Game id."),
        ("scope", "TEXT", "game, age, war, goal."),
        ("scope_key", "TEXT", "game: the game id; age: AGE_*; war: the war's events.event_key; goal: goals.goal_id as text."),
        ("g_start", "INTEGER", "First turn covered."),
        ("g_end", "INTEGER", "Last turn covered (current turn if ongoing)."),
        ("title", "TEXT", "Label (AGE_*, 'war with player N', goal title)."),
        ("verdict", "TEXT", "Heuristic: went_well, went_awry, mixed, ongoing. An advisor outcome annotation on the same scope overrides it."),
        ("facts", "TEXT", "JSON. age: metrics{start,end}, standings{path:{rank,of,value,leader,leader_value}}, war_opponents, "
                          "units_killed, units_lost, captured[], lost[], governments[], legacies[], wonders[], score. war: opponent, "
                          "declared_by, war_type, turns, ongoing, units_killed/lost, captured/lost. goal: horizon, status, "
                          "progress, overdue, metrics[]. game: age_verdicts, standings_now."),
    ], "game, scope, scope_key"),
    "advisor_profiles": ("advisor", "Versioned advisor personality: who Claude plays when advising. Latest version is current.", [
        ("game", "TEXT", "Game id."),
        ("version", "INTEGER", "1, 2, ... (latest wins)."),
        ("personality", "TEXT", "vocab field=personality (jocular, terse_military, sarcastic, scholarly, ...)."),
        ("voice", "TEXT", "Free-text voice/tone instructions layered on the personality."),
        ("priorities", "TEXT", "JSON list of the advisor's own priorities (vocab field=advisor_priority), most important first."),
        ("quirks", "TEXT", "Free text: idiosyncrasies, pet peeves, catchphrases (like a Civ AI agenda)."),
        ("valid_from_g", "INTEGER", "Game turn this version took effect."),
        ("created_at", "TEXT", "ISO time written."),
    ], "game, version"),
    "player_profiles": ("advisor", "Versioned player profile: how the player likes to play.", [
        ("game", "TEXT", "Game id."),
        ("version", "INTEGER", "1, 2, ... (latest wins)."),
        ("playstyle", "TEXT", "JSON list of vocab field=playstyle (aggressive, cautious, efficient, whimsical, ...)."),
        ("risk_tolerance", "TEXT", "low, medium, high."),
        ("notes", "TEXT", "Free text: what the player enjoys or avoids."),
        ("valid_from_g", "INTEGER", "Game turn this version took effect."),
        ("created_at", "TEXT", "ISO time written."),
    ], "game, version"),
    "strategies": ("advisor", "The player's strategies over time (usually one active). Tied to victory types or expansion.", [
        ("game", "TEXT", "Game id."),
        ("strategy_id", "INTEGER", "Id."),
        ("name", "TEXT", "Short name, e.g. 'Rail-baron economy'."),
        ("victory_types", "TEXT", "JSON list of vocab field=strategy_focus (science, culture, economic, military, expansion, score)."),
        ("description", "TEXT", "What the plan is."),
        ("status", "TEXT", "active, achieved, failed, abandoned."),
        ("started_g", "INTEGER", "Turn adopted."),
        ("ended_g", "INTEGER", "Turn it ended (status != active)."),
        ("created_at", "TEXT", "ISO time written."),
    ], "game, strategy_id"),
    "goals": ("advisor", "Long- and short-term goals. Short-term goals usually have a long-term parent; both may belong to a strategy.", [
        ("game", "TEXT", "Game id."),
        ("goal_id", "INTEGER", "Id."),
        ("strategy_id", "INTEGER", "strategies.strategy_id, or NULL."),
        ("parent_goal_id", "INTEGER", "Long-term goal this short-term goal serves, or NULL."),
        ("horizon", "TEXT", "short or long."),
        ("title", "TEXT", "Goal statement."),
        ("description", "TEXT", "Detail / why."),
        ("priority", "INTEGER", "1 = highest."),
        ("status", "TEXT", "active, achieved, failed, abandoned."),
        ("created_g", "INTEGER", "Turn set."),
        ("deadline_g", "INTEGER", "Target turn, or NULL."),
        ("resolved_g", "INTEGER", "Turn resolved."),
        ("resolution", "TEXT", "How it ended."),
    ], "game, goal_id"),
    "goal_metrics": ("advisor", "Associations between goals and observable metrics: how progress is measured.", [
        ("game", "TEXT", "Game id."),
        ("goal_id", "INTEGER", "goals.goal_id."),
        ("metric", "TEXT", "metric_catalog.metric."),
        ("player_id", "INTEGER", "Whose metric (default: local player). Use a rival's id for 'keep X below ...'."),
        ("comparator", "TEXT", "'>=' / '<=' on the value; 'gain>=' / 'gain<=' on value minus baseline; 'rank<=' on rank among majors (1 = top)."),
        ("target", "REAL", "Target number for the comparator."),
        ("baseline", "REAL", "Value when the link was made (auto-filled)."),
        ("baseline_g", "INTEGER", "Turn of the baseline."),
        ("weight", "REAL", "Relative weight within the goal (default 1)."),
    ], "game, goal_id, metric, player_id"),
    "annotations": ("advisor", "Non-game notes: player intentions, priority changes, advisor narrative, verdicts on how things went.", [
        ("game", "TEXT", "Game id."),
        ("annotation_id", "INTEGER", "Id."),
        ("g", "INTEGER", "Game turn the note is about."),
        ("age", "TEXT", "Age at g."),
        ("turn", "INTEGER", "Turn at g."),
        ("author", "TEXT", "advisor or player."),
        ("kind", "TEXT", "vocab field=annotation_kind (intention, priority_change, narrative, outcome, observation, warning, advice). "
                         "narrative/outcome notes with scope/scope_key are the story the dashboard shows for that phase."),
        ("text", "TEXT", "The note."),
        ("verdict", "TEXT", "went_well, went_awry, mixed, or NULL."),
        ("goal_id", "INTEGER", "Related goal, or NULL."),
        ("strategy_id", "INTEGER", "Related strategy, or NULL."),
        ("event_key", "TEXT", "Related timeline event, or NULL."),
        ("scope", "TEXT", "Phase this note narrates or judges (phases.scope: game, age, war, goal), or NULL."),
        ("scope_key", "TEXT", "phases.scope_key, or NULL."),
        ("created_at", "TEXT", "ISO time written."),
    ], "game, annotation_id"),
    "vocab": ("exporter", "Controlled vocabularies with descriptions (personalities, playstyles, strategy focuses, comparators, kinds).", [
        ("field", "TEXT", "personality, advisor_priority, playstyle, strategy_focus, comparator, annotation_kind, verdict."),
        ("value", "TEXT", "Allowed value."),
        ("description", "TEXT", "Meaning; for personality, how to speak; for strategy_focus, the metrics it suggests."),
        ("extra", "TEXT", "JSON: personality -> default priorities; strategy_focus -> suggested metrics."),
    ], "field, value"),
    "schema_docs": ("exporter", "This documentation: every table and column with its type and meaning.", [
        ("table_name", "TEXT", "Table."),
        ("column_name", "TEXT", "Column ('' for the table itself)."),
        ("type", "TEXT", "SQLite type."),
        ("owner", "TEXT", "exporter (regenerated) or advisor (persistent)."),
        ("description", "TEXT", "Meaning."),
    ], "table_name, column_name"),
}

# metric -> (label, unit, description, sources, higher_is_better, victory)
METRICS: dict[str, tuple[str, str, str, str, int | None, str | None]] = {
    "gold_per_turn": ("Gold/turn", "per_turn", "Net gold income.", "snapshot, log:Player_Stats", 1, "economic"),
    "science_per_turn": ("Science/turn", "per_turn", "Science yield.", "snapshot, log:Player_Stats", 1, "science"),
    "culture_per_turn": ("Culture/turn", "per_turn", "Culture yield.", "snapshot, log:Player_Stats", 1, "culture"),
    "happiness_per_turn": ("Happiness/turn", "per_turn", "Net happiness (celebrations).", "snapshot, log:Player_Stats", 1, None),
    "influence_per_turn": ("Influence/turn", "per_turn", "Influence (YIELD_DIPLOMACY).", "snapshot, log:Player_Stats", 1, None),
    "production_per_turn": ("Production/turn", "per_turn", "Empire production.", "snapshot, log:Player_Stats", 1, "military"),
    "food_per_turn": ("Food/turn", "per_turn", "Empire food.", "snapshot, log:Player_Stats", 1, "expansion"),
    "gold_balance": ("Treasury", "gold", "Gold in the treasury.", "snapshot, log:Player_Stats", 1, "economic"),
    "influence_balance": ("Influence bank", "influence", "Banked influence.", "snapshot", 1, None),
    "settlements": ("Settlements", "count", "Cities + towns.", "snapshot, log:Player_Stats", 1, "expansion"),
    "cities": ("Cities", "count", "Cities (not towns).", "snapshot, log:Player_Stats", 1, "expansion"),
    "towns": ("Towns", "count", "Towns.", "snapshot, log:Player_Stats", 1, "expansion"),
    "settlement_cap": ("Settlement cap", "count", "Settlement limit before unhappiness penalties.", "snapshot, log:Player_Stats", 1, "expansion"),
    "population": ("Population", "count", "Sum of settlement population.", "snapshot, log:Player_Stats (urban+rural)", 1, "expansion"),
    "tiles_owned": ("Tiles owned", "count", "Owned plots.", "log:Player_Stats", 1, "expansion"),
    "techs": ("Techs", "count", "Technologies researched this age (log counts per age).", "log:Player_Stats", 1, "science"),
    "land_units": ("Land units", "count", "Land units.", "log:Player_Stats", None, "military"),
    "naval_units": ("Naval units", "count", "Naval units.", "log:Player_Stats", None, "military"),
    "military_units": ("Military units", "count", "Own combat units (snapshot unit list, land+sea+air combat classes).", "snapshot", None, "military"),
    "trade_routes": ("Trade routes", "count", "Established trade routes.", "snapshot", 1, "economic"),
    "wars_active": ("Wars", "count", "Majors the player is at war with.", "snapshot", 0, "military"),
    "unrest_settlements": ("Unrest", "count", "Settlements in unrest.", "snapshot", 0, None),
    "units_lost_total": ("Units lost", "count", "Cumulative units/ships lost this game (Historian log, logged ages).", "derived from events", 0, "military"),
    "units_killed_total": ("Units killed", "count", "Cumulative enemy units/ships destroyed.", "derived from events", 1, "military"),
    "settlements_captured_total": ("Captured", "count", "Cumulative settlements captured.", "derived from events", 1, "military"),
    "vp_economic": ("Economic VP", "points", "Economic legacy/victory points this age (Modern: toward the economic victory target).",
                    "snapshot countdown_victories, capture", 1, "economic"),
    "vp_science": ("Science VP", "points", "Science legacy/victory points this age.", "snapshot, capture", 1, "science"),
    "vp_culture": ("Culture VP", "points", "Culture legacy/victory points this age.", "snapshot, capture", 1, "culture"),
    "vp_military": ("Military VP", "points", "Military legacy/victory points this age.", "snapshot, capture", 1, "military"),
    "vp_economic_target": ("Economic target", "points", "Points needed for the Modern economic victory.", "snapshot", None, "economic"),
    "vp_science_target": ("Science target", "points", "Points needed for the Modern science victory.", "snapshot", None, "science"),
    "vp_culture_target": ("Culture target", "points", "Points needed for the Modern culture victory.", "snapshot", None, "culture"),
    "vp_military_target": ("Military target", "points", "Points needed for the Modern military victory.", "snapshot", None, "military"),
    "domination_score": ("Domination", "points", "Domination victory score.", "snapshot victories", 1, "military"),
    "score": ("Score", "points", "Score victory points.", "snapshot victories", 1, "score"),
    "triumphs_done": ("Triumphs", "count", "Legacy-path triumphs completed this age.", "snapshot triumphs", 1, None),
    "age_progress": ("Age progress", "turns", "Age progress counter (age_progress.current of .max).", "snapshot", None, None),
}

VOCAB: dict[str, dict[str, tuple[str, list | None]]] = {
    "personality": {
        "jocular": ("Warm and joking. Celebrates wins with puns, ribs the player about blunders, never mean.",
                    ["growth", "happiness", "wonders"]),
        "terse_military": ("Clipped field-report style. Bullet points, threat first, numbers, no pleasantries.",
                           ["security", "military_strength", "production"]),
        "sarcastic": ("Dry, eye-rolling. Points out folly with irony but the advice underneath is sound.",
                      ["efficiency", "economy", "security"]),
        "scholarly": ("Measured and analytical, cites mechanics and history, explains the why.",
                      ["science", "long_term_planning", "culture"]),
        "diplomatic": ("Courteous, consensus-seeking, frames rivals as partners or problems to manage.",
                       ["alliances", "influence", "trade"]),
        "zealous": ("Fervent true believer in the current strategy. Rallying cries, urgency, little patience for detours.",
                    ["victory_progress", "faith", "culture"]),
        "cautious_steward": ("Worried accountant. Flags risk, budgets, contingencies; prefers safe compounding.",
                             ["economy", "security", "happiness"]),
        "machiavellian": ("Cold opportunist. Every rival is a mark; recommends betrayal when the numbers favor it.",
                          ["victory_progress", "military_strength", "influence"]),
    },
    "advisor_priority": {k: (v, None) for k, v in {
        "security": "Defend settlements, don't lose units cheaply.",
        "military_strength": "Field and upgrade armies.",
        "economy": "Gold income, treasury, trade.",
        "science": "Tech pace.",
        "culture": "Civics pace, wonders, tourism.",
        "growth": "Food, population, new settlements.",
        "production": "Production capacity.",
        "happiness": "Avoid unrest, celebrations.",
        "influence": "Influence income, city-states.",
        "alliances": "Relationships and alliances.",
        "trade": "Trade routes and resources.",
        "wonders": "Wonder construction.",
        "faith": "Religion spread.",
        "efficiency": "Least waste: idle units, overflow, unspent gold.",
        "victory_progress": "Whatever moves the chosen victory fastest.",
        "long_term_planning": "Set up the next age.",
    }.items()},
    "playstyle": {k: (v, None) for k, v in {
        "aggressive": "Seeks war and conquest; accepts losses for territory.",
        "cautious": "Defends first, avoids risky wars, keeps reserves.",
        "efficient": "Min-maxes yields and timing; wants optimal orders.",
        "whimsical": "Plays for fun and story; will chase wonders or odd ideas over optimal play.",
        "builder": "Wonders, districts, tall cities.",
        "expansionist": "Wide empire, many settlements.",
        "opportunist": "Switches plans to exploit openings.",
        "diplomat": "Alliances, city-states, peaceful influence.",
        "roleplayer": "Plays in character for the leader/civ.",
    }.items()},
    "strategy_focus": {
        "science": ("Science legacy path / Modern space race.", ["vp_science", "science_per_turn", "techs"]),
        "culture": ("Culture legacy path / Modern culture victory.", ["vp_culture", "culture_per_turn", "triumphs_done"]),
        "economic": ("Economic legacy path / Modern economic victory.", ["vp_economic", "gold_per_turn", "trade_routes", "gold_balance"]),
        "military": ("Military legacy path / Modern military victory / domination.",
                     ["vp_military", "domination_score", "settlements_captured_total", "units_killed_total", "units_lost_total"]),
        "expansion": ("Grow wide: settlements, population, land.", ["settlements", "population", "settlement_cap", "tiles_owned"]),
        "score": ("Maximize score.", ["score"]),
    },
    "comparator": {k: (v, None) for k, v in {
        ">=": "Current value at least target.",
        "<=": "Current value at most target.",
        "gain>=": "Value minus baseline at least target.",
        "gain<=": "Value minus baseline at most target (e.g. lose at most N units).",
        "rank<=": "Rank among majors (1 = highest value) at most target.",
    }.items()},
    "annotation_kind": {k: (v, None) for k, v in {
        "intention": "What the player means to do and why.",
        "priority_change": "The player changed priorities, strategy or goals.",
        "narrative": "Advisor's story of a phase or initiative.",
        "outcome": "How an initiative turned out (set verdict).",
        "observation": "Something notable in the game state.",
        "warning": "A risk the advisor flagged.",
        "advice": "A recommendation given to the player.",
    }.items()},
    "verdict": {k: (v, None) for k, v in {
        "went_well": "Succeeded or beat expectations.",
        "went_awry": "Failed or backfired.",
        "mixed": "Partly.",
        "ongoing": "Not settled yet.",
        "neutral": "No judgement.",
    }.items()},
}


def ddl() -> str:
    out = []
    for name, (_, _, cols, pk) in TABLES.items():
        body = ",\n  ".join(f"{c} {t}" for c, t, _ in cols)
        out.append(f"CREATE TABLE IF NOT EXISTS {name} (\n  {body},\n  PRIMARY KEY ({pk})\n);")
    out.append("CREATE INDEX IF NOT EXISTS metrics_by_metric ON metrics (game, metric, player_id, g);")
    out.append("CREATE INDEX IF NOT EXISTS events_by_start ON events (game, start_g);")
    return "\n".join(out)


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    _migrate(con)
    con.executescript(ddl())
    _seed_docs(con)
    return con


@contextmanager
def tx(path: Path | None = None):
    con = connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def _migrate(con: sqlite3.Connection) -> None:
    """v1 -> v2: narratives (prose) became phases (facts); annotations gained scope/scope_key."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "narratives" in tables:
        con.execute("DROP TABLE narratives")
    if "annotations" in tables:
        cols = {r[1] for r in con.execute("PRAGMA table_info(annotations)")}
        if "scope" not in cols:
            con.execute("ALTER TABLE annotations RENAME TO annotations_v1")
            con.executescript(ddl())
            con.execute("INSERT INTO annotations SELECT game, annotation_id, g, age, turn, author, kind, text, verdict, goal_id, "
                        "strategy_id, event_key, NULL, NULL, created_at FROM annotations_v1")
            con.execute("DROP TABLE annotations_v1")
    con.commit()


def _seed_docs(con: sqlite3.Connection) -> None:
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row and row[0] == str(SCHEMA_VERSION) and con.execute("SELECT count(*) FROM schema_docs").fetchone()[0]:
        return
    with con:
        con.execute("DELETE FROM schema_docs")
        for name, (owner, desc, cols, _) in TABLES.items():
            con.execute("INSERT INTO schema_docs VALUES (?,?,?,?,?)", (name, "", "TABLE", owner, desc))
            for c, t, d in cols:
                con.execute("INSERT INTO schema_docs VALUES (?,?,?,?,?)", (name, c, t, owner, d))
        con.execute("DELETE FROM metric_catalog")
        for m, (label, unit, d, src, hib, vic) in METRICS.items():
            con.execute("INSERT INTO metric_catalog VALUES (?,?,?,?,?,?,?)", (m, label, unit, d, src, hib, vic))
        con.execute("DELETE FROM vocab")
        for field, vals in VOCAB.items():
            for v, (d, extra) in vals.items():
                con.execute("INSERT INTO vocab VALUES (?,?,?,?)", (field, v, d, json.dumps(extra) if extra else None))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


def schema_markdown() -> str:
    lines = ["# civilizer.db schema", "", __doc__.strip(), ""]
    for name, (owner, desc, cols, pk) in TABLES.items():
        lines += [f"## {name}", "", f"*{owner}-owned.* {desc}  Primary key: `({pk})`.", "", "| column | type | description |",
                  "|---|---|---|"]
        lines += [f"| `{c}` | {t} | {d} |" for c, t, d in cols]
        lines.append("")
    lines += ["## Metrics", "", "| metric | unit | description | sources | victory |", "|---|---|---|---|---|"]
    lines += [f"| `{m}` | {u} | {d} | {s} | {v or ''} |" for m, (_, u, d, s, _, v) in METRICS.items()]
    lines.append("")
    for field, vals in VOCAB.items():
        lines += [f"## vocab: {field}", ""]
        lines += [f"- `{v}`: {d}" + (f" ({', '.join(e)})" if e else "") for v, (d, e) in vals.items()]
        lines.append("")
    return "\n".join(lines)


def set_meta(con: sqlite3.Connection, key: str, value) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value if isinstance(value, str) else json.dumps(value)))


def get_meta(con: sqlite3.Connection, key: str):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def event_key(e: dict) -> str:
    s, d = e.get("start") or {}, e.get("detail") or {}
    raw = "|".join(str(x) for x in (e["kind"], e["subject"], sorted(e["players"]), s.get("age"), s.get("turn"), e["source"],
                                     d.get("city"), d.get("item"), d.get("x"), d.get("y")))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
