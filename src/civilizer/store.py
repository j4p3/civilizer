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

from . import presets

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "civilizer.db"
SCHEMA_VERSION = 10  # 9: war_support metric, war phase facts; 10: blurb annotation kind (reseeds schema_docs / metric_catalog / vocab)
# old verdict values (schema <= 7) -> outcome; still accepted as input aliases
OUTCOME_ALIASES = {"went_well": "success", "went_awry": "failure", "mixed": "partial"}

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
        ("status", "TEXT", "over (a player won a victory), in_progress, or NULL when the newest snapshot predates victory capture. "
                           "From the newest snapshot's claimed_victories (Game.VictoryManager.getVictories)."),
        ("victories", "TEXT", "JSON list of won victories, [{victory: VICTORY_*, players: [player ids]}]; [] while in progress."),
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
                            "wars, notifications, settlements (name, type, population, capital, being_razed, x, y), city_states. The raw "
                            "snapshot has everything else."),
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
               "production, projects, purchases, legacies, unit deaths, diplomacy). Rebuilt each refresh; event_key is stable.", [
        ("game", "TEXT", "Game id."),
        ("event_key", "TEXT", "Stable id (hash of kind, subject, players, start, source). Annotations reference it."),
        ("kind", "TEXT", "age, war, war_declared, alliance, settlement, settlement_captured, tech, civic, tech_research, "
                         "civic_research, government, religion, religion_founded, policy, production, project, purchase, "
                         "legacy, unit_killed; diplomacy (diplomacy.py, DiplomacySummary + Game_Gossip): diplomacy_action "
                         "(an action between two players from start to end: endeavors, sanctions, treaties, espionage, alliance "
                         "proposals, city-state projects; players [initiator, recipient]), befriend (befriending an independent, "
                         "start to end, with its stages and support; rivals = other majors befriending it at the same time), "
                         "suzerain (a player is suzerain of a city-state: from gaining it to losing it; players [suzerain, "
                         "city-state]), diplo_response (a proposal accepted / supported / rejected / opposed; players "
                         "[responder, proposer]), met (first meeting; players [major, other]), relationship (snapshot: a met "
                         "rival's attitude to the player changed level)."),
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
        ("local", "INTEGER", "1 if the local player is involved (and would know: a rival's espionage against the player is 0)."),
        ("minor", "INTEGER", "1 if an independent power / city-state is involved."),
        ("precision", "TEXT", "exact; range (happened between start_lo and start_hi); state (seen at that turn)."),
        ("source", "TEXT", "snapshot, log:<name>, capture:<age>, derived."),
        ("detail", "TEXT", "JSON with kind-specific fields (city, item, category, completed, victim, killer, x, y, war_type, ...). "
                           "diplomacy_action / befriend: action, action_type, group (endeavor, sanction, treaty, espionage, project, "
                           "unique), proposal, initiator(_name), recipient(_name), recipient_kind, stages [{turn, stage}], stage, "
                           "support / opposition {player: n}, support_log, response, outcome (success, canceled), end_reason "
                           "(age_end_or_ongoing, restarted), lost_to, rivals, turn_influence_spent (local: the turn's whole "
                           "influence spending, not split between actions), started_before_logs. suzerain: suzerain(_name), "
                           "city_state(_name), capital, bonus, end_reason (replaced, incorporated, captured, age_end_or_ongoing), "
                           "lost_to. diplo_response: action, response, proposer, responder. met: player, other, other_kind. "
                           "settlement_captured: captured_by, from_player, capital (the losing civ's capital name)."),
    ], "game, event_key"),
    "combat_rewards": ("exporter", "Yields gained from combat, one row per reward (a kill paying two yields is two rows): kills "
                       "(CombatLog + the post-combat-yield rules in the game data), pillage / coastal raids and trade-route "
                       "plunder (Game_Gossip), dispersed independent powers (Game_Gossip + Independents). Every major player; "
                       "rivals' rows come from logs the player cannot see. See combat.py for what the game does not log.", [
        ("game", "TEXT", "Game id."),
        ("reward_id", "TEXT", "Stable id (hash of kind, turn, player, plot, units, modifier)."),
        ("g", "INTEGER", "Game-wide turn index."),
        ("age", "TEXT", "Age."),
        ("turn", "INTEGER", "Turn within the age (log turn: the turn whose end processed it)."),
        ("player_id", "INTEGER", "Player who gained the reward."),
        ("kind", "TEXT", "kill (unit destroyed), pillage (land pillage or naval coastal raid: the log does not tell them apart), "
                         "plunder (trade route), disperse (independent power camp)."),
        ("yield_type", "TEXT", "YIELD_GOLD / YIELD_SCIENCE / YIELD_CULTURE / YIELD_DIPLOMACY (influence) / YIELD_PRODUCTION / "
                               "YIELD_FOOD; for dispersals the camp type's yield; NULL when unknown (pillage, plunder)."),
        ("amount", "REAL", "Yield gained; NULL when the game does not log it (pillage, plunder, most dispersals). Never "
                           "estimated: unexplained treasury change is the separate unexplained_gold metric."),
        ("basis", "TEXT", "How amount was obtained: rules (computed from the logged defeated strength or unit cost and the "
                          "game-data modifier), notification (exact, from a snapshot notification), unknown (amount NULL)."),
        ("x", "INTEGER", "Plot x (combat plot, pillaged plot, or the dispersed camp)."),
        ("y", "INTEGER", "Plot y."),
        ("unit", "TEXT", "Acting unit type (UNIT_*), if logged."),
        ("target", "TEXT", "Defeated unit type, dispersed tribe (INDEPENDENT_*), or plundered route 'from -> to'."),
        ("target_owner", "INTEGER", "Owner of the defeated unit (player id; independents have ids too), if logged."),
        ("source", "TEXT", "log:CombatLog, log:Game_Gossip."),
        ("detail", "TEXT", "JSON: modifier and granted_by (kill rules), victim_strength, city_state_type and base_amount "
                           "(the unscaled XML DisperseRewardAmount)."),
    ], "game, reward_id"),
    "phases": ("exporter", "Fact sheets for phases and initiatives: each age, each war the local player fought, each goal, and "
               "the whole game, with a heuristic outcome. No prose: Claude writes the narrative as annotations with "
               "scope/scope_key pointing here. Regenerated each refresh.", [
        ("game", "TEXT", "Game id."),
        ("scope", "TEXT", "game, age, war, goal."),
        ("scope_key", "TEXT", "game: the game id; age: AGE_*; war: the war's events.event_key; goal: goals.goal_id as text."),
        ("g_start", "INTEGER", "First turn covered."),
        ("g_end", "INTEGER", "Last turn covered (current turn if ongoing)."),
        ("title", "TEXT", "Label (AGE_*, 'war with player N', goal title)."),
        ("outcome", "TEXT", "Heuristic: success, partial, failure, changed_plans (abandoned goals), ongoing (vocab field=outcome). "
                            "An advisor outcome annotation on the same scope overrides it."),
        ("facts", "TEXT", "JSON. age: metrics{start,end}, standings{path:{rank,of,value,leader,leader_value}}, war_opponents, "
                          "units_killed, units_lost, captured[], lost[], governments[], legacies[], wonders[], score. war: opponent, "
                          "declared_by, war_type, turns, ongoing, units_killed/lost, captured/lost, casus_belli{declared_by, "
                          "war_type, run_up[] (diplomacy and relationship changes between the two sides in the 10 turns before), "
                          "joined_ally_war{ally, ally_war_start_g, declared_by, allied_since_g}}, combatants{us, them: "
                          "{majors[], city_states[]}}, war_support{series[[g, value]], first, last, min, max}, notable{"
                          "settlements[] (captured, lost, razing, razed, in order), peak_kill_turns[], upsets[] (capital_taken, "
                          "capital_lost, eliminated, founded_lost)}. goal: horizon, status, "
                          "progress, overdue, metrics[]. game: age_outcomes, standings_now."),
    ], "game, scope, scope_key"),
    "advisor_profiles": ("advisor", "Reusable advisor personalities (not tied to a game): who Claude plays when advising. "
                         "Editing makes a new version; games link to a version through game_profiles.", [
        ("profile_id", "INTEGER", "Id, shared by all versions."),
        ("version", "INTEGER", "1, 2, ... (latest is what new games get)."),
        ("name", "TEXT", "Display name for picking it again (default: the preset label)."),
        ("personality", "TEXT", "Advisor preset key: built-in (vocab field=personality) or custom (data/advisors/<key>.md), "
                                "whose style prompt Claude follows."),
        ("voice", "TEXT", "Free-text voice/tone instructions layered on the personality."),
        ("priorities", "TEXT", "JSON list of the advisor's own priorities (vocab field=advisor_priority), most important first."),
        ("quirks", "TEXT", "Free text: idiosyncrasies, pet peeves, catchphrases (like a Civ AI agenda)."),
        ("created_at", "TEXT", "ISO time written."),
    ], "profile_id, version"),
    "player_profiles": ("advisor", "Reusable player profiles (not tied to a game): why and how the player likes to play. "
                        "Editing makes a new version; games link to a version through game_profiles.", [
        ("profile_id", "INTEGER", "Id, shared by all versions."),
        ("version", "INTEGER", "1, 2, ... (latest is what new games get)."),
        ("name", "TEXT", "Display name for picking it again."),
        ("motives", "TEXT", "JSON list of vocab field=motive (fight, build, explore, mess_around, story): why they play."),
        ("playstyle", "TEXT", "JSON list of vocab field=playstyle (aggressive, cautious, efficient, whimsical, ...)."),
        ("risk_tolerance", "TEXT", "low, medium, high."),
        ("notes", "TEXT", "Free text: what the player enjoys or avoids, including setup follow-up answers."),
        ("created_at", "TEXT", "ISO time written."),
    ], "profile_id, version"),
    "game_profiles": ("advisor", "Which advisor / player profile version each game uses, over time (latest seq per role is current).", [
        ("game", "TEXT", "Game id."),
        ("role", "TEXT", "advisor or player."),
        ("seq", "INTEGER", "1, 2, ... per game and role (latest wins)."),
        ("profile_id", "INTEGER", "advisor_profiles / player_profiles profile_id."),
        ("version", "INTEGER", "Profile version in effect."),
        ("valid_from_g", "INTEGER", "Game turn this link took effect."),
        ("created_at", "TEXT", "ISO time written."),
    ], "game, role, seq"),
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
    "annotations": ("advisor", "Non-game notes: player intentions, priority changes, advisor narrative, outcomes of how things went.", [
        ("game", "TEXT", "Game id."),
        ("annotation_id", "INTEGER", "Id."),
        ("g", "INTEGER", "Game turn the note is about."),
        ("age", "TEXT", "Age at g."),
        ("turn", "INTEGER", "Turn at g."),
        ("author", "TEXT", "advisor or player."),
        ("kind", "TEXT", "vocab field=annotation_kind (intention, priority_change, narrative, blurb, outcome, observation, warning, advice). "
                         "narrative/outcome notes with scope/scope_key are the story the dashboard shows for that phase."),
        ("text", "TEXT", "The note."),
        ("outcome", "TEXT", "vocab field=outcome (success, partial, failure, changed_plans, ongoing), or NULL."),
        ("goal_id", "INTEGER", "Related goal, or NULL."),
        ("strategy_id", "INTEGER", "Related strategy, or NULL."),
        ("event_key", "TEXT", "Related timeline event, or NULL."),
        ("scope", "TEXT", "Phase this note narrates or judges (phases.scope: game, age, war, goal), or NULL."),
        ("scope_key", "TEXT", "phases.scope_key, or NULL."),
        ("created_at", "TEXT", "ISO time written."),
    ], "game, annotation_id"),
    "vocab": ("exporter", "Controlled vocabularies with descriptions (personalities, playstyles, strategy focuses, comparators, kinds).", [
        ("field", "TEXT", "personality, advisor_priority, playstyle, motive, strategy_focus, comparator, annotation_kind, outcome."),
        ("value", "TEXT", "Allowed value."),
        ("description", "TEXT", "Meaning; for personality, how to speak; for strategy_focus, the metrics it suggests."),
        ("extra", "TEXT", "JSON: personality -> default priorities; motive -> follow-up angles; strategy_focus -> suggested metrics."),
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
    # combat rewards (combat_rewards table): cumulative over the game's logged turns, summing rows with a known amount
    "combat_gold_total": ("Combat gold", "gold", "Cumulative gold from combat with an exact amount: kill rewards (rules) and "
                          "dispersal notifications. Pillage, plunder and most dispersal gold are not logged and not included "
                          "(see unexplained_gold). Compare with trade_income_per_turn.",
                          "derived from combat_rewards", 1, "economic"),
    "combat_science_total": ("Combat science", "science", "Cumulative science from combat (kill rewards such as Yi Sun-sin's; "
                             "dispersal amounts only when a notification was seen).", "derived from combat_rewards", 1, "science"),
    "combat_culture_total": ("Combat culture", "culture", "Cumulative culture from combat (kill rewards: Napoleon, Cocorico...).",
                             "derived from combat_rewards", 1, "culture"),
    "combat_influence_total": ("Combat influence", "influence", "Cumulative influence from combat (kill rewards: Lakshmibai, "
                               "Gilgamesh).", "derived from combat_rewards", 1, None),
    "combat_production_total": ("Combat production", "production", "Cumulative production from combat (dispersal notifications).",
                                "derived from combat_rewards", 1, None),
    "combat_food_total": ("Combat food", "food", "Cumulative food from combat (dispersal notifications).",
                          "derived from combat_rewards", 1, None),
    "kill_gold_total": ("Kill gold", "gold", "Cumulative gold from destroying units (Privateer, Edward Teach, Merry Life...), "
                        "computed from CombatLog and the game's rules.", "derived from combat_rewards (rules)", 1, "economic"),
    "pillages_total": ("Pillages", "count", "Cumulative tiles pillaged or coastal-raided (Game_Gossip).", "derived from combat_rewards",
                       None, "military"),
    "independents_dispersed_total": ("Dispersed", "count", "Cumulative independent powers dispersed.", "derived from combat_rewards",
                                     None, "military"),
    "trade_routes_plundered_total": ("Routes plundered", "count", "Cumulative trade routes plundered.", "derived from combat_rewards",
                                     None, "military"),
    "unexplained_gold": ("Other gold", "gold", "Treasury change not explained by per-turn income or logged purchases; may "
                         "include pillage/plunder/dispersal, deals, treasure fleets, etc.; not attributed. Per turn: next "
                         "turn's balance minus this turn's balance and gold yield (Player_Treasury), minus exact combat gold "
                         "(kill rules, notifications). Absent on turns with a logged purchase (CityPurchase has no prices). "
                         "Negative: upgrades, gifts, deals paid.", "log:Player_Treasury", None, "economic"),
    "unexplained_gold_total": ("Other gold total", "gold", "Cumulative sum of positive unexplained_gold turns (unexplained "
                               "gains; negative turns are not netted). Not attributed to any source.",
                               "derived from Player_Treasury", 1, "economic"),
    "trade_income_per_turn": ("Trade income", "per_turn", "Gold per turn from trade routes (snapshot trade.trade_income_total).",
                              "snapshot", 1, "economic"),
    # diplomacy (events of kinds befriend / suzerain / diplomacy_action / met; diplomacy.py)
    "suzerainties": ("Suzerainties", "count", "City-states the player is suzerain of now. Snapshot (met city-states, visible) "
                     "where one exists; otherwise from the logs (suzerain events; an age end clears them).",
                     "snapshot city_states, derived from events", 1, None),
    "suzerainties_total": ("Suzerainties won", "count", "Cumulative times the player became suzerain of a city-state "
                           "(logged ages only).", "derived from events", 1, None),
    "befriend_actions_active": ("Befriending", "count", "Independents the player is befriending right now.",
                                "derived from events", None, None),
    "diplomatic_actions_started_total": ("Diplomatic actions", "count", "Cumulative diplomatic actions the player started: "
                                         "befriending, endeavors, sanctions, treaties, espionage, alliance proposals, war "
                                         "declarations (not land claims).", "derived from events", None, None),
    "independents_met": ("Independents met", "count", "Distinct independent powers met this age (resets each age).",
                         "derived from events", 1, None),
    "influence_spent": ("Influence spent", "influence", "Influence spent this turn: bank + influence income - next turn's "
                        "bank (consecutive snapshots). Negative: gained beyond income. Local player only; not attributed "
                        "to an action.", "snapshot", None, None),
    "influence_spent_total": ("Influence spent total", "influence", "Cumulative sum of positive influence_spent turns.",
                              "derived from snapshots", None, None),
    "relationship": ("Relationship", "points", "A met rival's attitude toward the local player, -100 (hostile) .. 100 "
                     "(helpful); stored under the rival's player_id.", "snapshot", 1, None),
    "war_support": ("War support", "points", "War support in the local player's war with a rival: supporters minus opposers "
                    "of the war, from the local player's side (positive favours us); stored under the opponent's player_id "
                    "for turns at war.", "snapshot", 1, "military"),
}

VOCAB: dict[str, dict[str, tuple[str, list | None]]] = {
    # built-in advisor presets (src/civilizer/advisors/*.md); custom ones live in data/advisors/ (presets.advisors())
    "personality": {k: (a["description"], a["priorities"]) for k, a in presets.builtin_advisors().items()},
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
    "motive": {k: (f"{label}: {d}", followups) for k, (label, d, followups) in presets.MOTIVES.items()},
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
        "blurb": "One- or two-sentence summary of the game for the overview page (scope=game).",
        "outcome": "How an initiative turned out (set outcome).",
        "observation": "Something notable in the game state.",
        "warning": "A risk the advisor flagged.",
        "advice": "A recommendation given to the player.",
    }.items()},
    # the judgment, separate from goal/strategy status (the lifecycle): an abandoned goal can be a failure or a change of plans
    "outcome": {k: (v, None) for k, v in {
        "success": "Achieved what it set out to do, or beat expectations.",
        "partial": "Achieved part of it, or at a higher cost than planned.",
        "failure": "Tried and fell short, or backfired.",
        "changed_plans": "Dropped or superseded by a new plan or new information (e.g. the game's rules ruled it out); "
                         "not judged as a failure.",
        "ongoing": "Not settled yet.",
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
    """v1 -> v2: narratives (prose) became phases (facts); annotations gained scope/scope_key.
    v2 -> v3: games gained status and victories. v3 -> v4: profiles became reusable records linked to games by game_profiles.
    v4 -> v5: combat_rewards table and combat metrics (new table only: created by ddl()).
    v5 -> v6: metric catalog only (unexplained_gold replaces treasury_residual and the estimated *_gold_total
    metrics); _seed_docs reseeds it and refresh rebuilds the metric rows.
    v6 -> v7: diplomacy event kinds and metrics (catalog and docs only; the next refresh backfills them).
    v7 -> v8: verdict became outcome (_migrate_outcomes)."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "games" in tables:
        cols = {r[1] for r in con.execute("PRAGMA table_info(games)")}
        for c in ("status", "victories"):
            if c not in cols:
                con.execute(f"ALTER TABLE games ADD COLUMN {c} TEXT")
    _migrate_profiles(con, tables)
    if "narratives" in tables:
        con.execute("DROP TABLE narratives")
    if "annotations" in tables:
        cols = {r[1] for r in con.execute("PRAGMA table_info(annotations)")}
        if "scope" not in cols:
            con.execute("ALTER TABLE annotations RENAME TO annotations_v1")
            con.executescript(ddl())
            con.execute(f"INSERT INTO annotations SELECT game, annotation_id, g, age, turn, author, kind, text, "
                        f"{_outcome_sql('verdict')}, goal_id, strategy_id, event_key, NULL, NULL, created_at FROM annotations_v1")
            con.execute("DROP TABLE annotations_v1")
    con.commit()
    _migrate_outcomes(con)


def _outcome_sql(col: str) -> str:
    return f"CASE {col} " + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in OUTCOME_ALIASES.items()) + f" WHEN 'neutral' THEN NULL ELSE {col} END"


def outcome_of(value: str | None) -> str | None:
    """Read side: phases rows written by a pre-v8 exporter still carry verdict values until it restarts."""
    return OUTCOME_ALIASES.get(value, value)


def _migrate_outcomes(con: sqlite3.Connection) -> None:
    """v7 -> v8: annotations.verdict and phases.verdict renamed to outcome, values mapped (went_well -> success,
    went_awry -> failure, mixed -> partial, neutral -> NULL). Detected from the columns rather than schema_version,
    which an older process sharing the file keeps rewriting. One IMMEDIATE transaction, so a concurrent writer waits
    and sees either the old or the new table; the column count does not change, so its positional INSERTs still fit."""
    def has_verdict(table):
        return "verdict" in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if not (has_verdict("annotations") or has_verdict("phases")):
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        for table in ("annotations", "phases"):
            if has_verdict(table):  # re-checked under the write lock
                con.execute(f"ALTER TABLE {table} RENAME COLUMN verdict TO outcome")
                con.execute(f"UPDATE {table} SET outcome = {_outcome_sql('outcome')} WHERE outcome IS NOT NULL")
        con.commit()
    except BaseException:
        con.rollback()
        raise


def _migrate_profiles(con: sqlite3.Connection, tables: set[str]) -> None:
    """v3 per-game profile rows (game, version, ...) -> one reusable profile per game, its versions linked in game_profiles."""
    for role, table in (("advisor", "advisor_profiles"), ("player", "player_profiles")):
        if table not in tables or "game" not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            continue
        con.execute(f"ALTER TABLE {table} RENAME TO {table}_v3")
        con.executescript(ddl())
        pids = {}
        for r in con.execute(f"SELECT * FROM {table}_v3 ORDER BY game, version").fetchall():
            pid = pids.setdefault(r["game"], len(pids) + 1)
            name = r["personality"].replace("_", " ").capitalize() if role == "advisor" else "Player"
            if role == "advisor":
                con.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?)", (pid, r["version"], name, r["personality"], r["voice"],
                                                                              r["priorities"], r["quirks"], r["created_at"]))
            else:
                con.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?)", (pid, r["version"], name, "[]", r["playstyle"],
                                                                              r["risk_tolerance"], r["notes"], r["created_at"]))
            con.execute("INSERT INTO game_profiles VALUES (?,?,?,?,?,?,?)",
                        (r["game"], role, r["version"], pid, r["version"], r["valid_from_g"], r["created_at"]))
        con.execute(f"DROP TABLE {table}_v3")


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


def hashed(raw: str) -> str:
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def event_key(e: dict) -> str:
    s, d = e.get("start") or {}, e.get("detail") or {}
    raw = "|".join(str(x) for x in (e["kind"], e["subject"], sorted(e["players"]), s.get("age"), s.get("turn"), e["source"],
                                     d.get("city"), d.get("item"), d.get("x"), d.get("y")))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
