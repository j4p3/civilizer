# civilizer.db schema

Central game store: one SQLite file (data/civilizer.db) shared by the monitor, the advisor and the dashboard.

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

## meta

*exporter-owned.* Store-level key/values: schema_version, monitor heartbeat (monitor_seen_at, monitor_state).  Primary key: `(key)`.

| column | type | description |
|---|---|---|
| `key` | TEXT | Key name. |
| `value` | TEXT | Value (JSON or text). |

## games

*exporter-owned.* One row per game, keyed by the map seed.  Primary key: `(game)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id: the map seed as text. Every other table carries it. |
| `guid` | TEXT | Save-file game GUID. |
| `leader` | TEXT | Local player's leader type (LEADER_*). |
| `civilization` | TEXT | Local player's civilization type at the newest save. |
| `difficulty` | TEXT | DIFFICULTY_*. |
| `map` | TEXT | Map script name. |
| `map_size` | TEXT | MAPSIZE_*. |
| `speed` | TEXT | GAMESPEED_*. |
| `app_version` | TEXT | Game build that wrote the newest save. |
| `local_player_id` | INTEGER | Player id of the human player. |
| `current_age` | TEXT | Age of the newest observed turn. |
| `current_turn` | INTEGER | On-screen turn of the newest observed turn. |
| `current_g` | INTEGER | Game-wide turn index of the newest observed turn. |
| `age_turns_max` | INTEGER | Age length the game reports for the current age (age_progress.max). |
| `status` | TEXT | over (a player won a victory), in_progress, or NULL when the newest snapshot predates victory capture. From the newest snapshot's claimed_victories (Game.VictoryManager.getVictories). |
| `victories` | TEXT | JSON list of won victories, [{victory: VICTORY_*, players: [player ids]}]; [] while in progress. |
| `updated_at` | TEXT | ISO time of the last refresh. |

## ages

*exporter-owned.* Ages of a game laid end to end.  Primary key: `(game, age)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `age` | TEXT | AGE_ANTIQUITY / AGE_EXPLORATION / AGE_MODERN. |
| `idx` | INTEGER | 0, 1, 2 in chronological order. |
| `turns` | INTEGER | Turns observed in this age (the last known turn). |
| `g_offset` | INTEGER | g = g_offset + turn. |

## players

*exporter-owned.* Major players. Ids persist across ages; civilization changes per age.  Primary key: `(game, player_id)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `player_id` | INTEGER | In-game player id (0 = usually the human). |
| `leader` | TEXT | Leader display name. |
| `leader_type` | TEXT | LEADER_*. |
| `is_human` | INTEGER | 1 for the human player. |
| `civilizations` | TEXT | JSON object age -> civilization display name. |

## turns

*exporter-owned.* Turns observed by a live snapshot, on the kept branch (reloaded-away turns dropped).  Primary key: `(game, g)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `g` | INTEGER | Game-wide turn index. |
| `age` | TEXT | Age. |
| `turn` | INTEGER | On-screen turn. |
| `captured_at` | TEXT | When the snapshot was taken (game clock, UTC ISO). |
| `snapshot_path` | TEXT | Path of the raw snapshot JSON (full detail: cities, units, trade routes, map). |
| `summary` | TEXT | JSON digest of the local player's state at this turn: research, civic, government, policies, wars, notifications, settlements (name, type, population, capital, being_razed, x, y), city_states. The raw snapshot has everything else. |

## metrics

*exporter-owned.* Long-format numeric series: one value per (turn, player, metric). See metric_catalog.  Primary key: `(game, g, player_id, metric)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `g` | INTEGER | Game-wide turn index. |
| `age` | TEXT | Age. |
| `turn` | INTEGER | Turn within the age. |
| `player_id` | INTEGER | Player the value belongs to. |
| `metric` | TEXT | Metric name (metric_catalog.metric). |
| `value` | REAL | Value. |
| `source` | TEXT | snapshot (on-screen), log:<name> (game log, turn-start values), capture (history.js), derived. |
| `visibility` | TEXT | visible = the player could see it on screen; omniscient = from logs/captures covering unmet or fogged rivals. Advice should rely on visible values. |

## metric_catalog

*exporter-owned.* What each metric means. Goals link to metrics by name.  Primary key: `(metric)`.

| column | type | description |
|---|---|---|
| `metric` | TEXT | Metric name. |
| `label` | TEXT | Short display label. |
| `unit` | TEXT | Unit (per_turn, count, points, gold, ...). |
| `description` | TEXT | Meaning and caveats. |
| `sources` | TEXT | Where it comes from. |
| `higher_is_better` | INTEGER | 1 if larger is better for the owner, 0 if smaller is, NULL if neutral. |
| `victory` | TEXT | Victory/strategy this metric most indicates (science, culture, economic, military, expansion), if any. |

## events

*exporter-owned.* Timeline: intervals and point events (ages, wars, settlements, techs, civics, governments, policies, production, projects, purchases, legacies, unit deaths, diplomacy). Rebuilt each refresh; event_key is stable.  Primary key: `(game, event_key)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `event_key` | TEXT | Stable id (hash of kind, subject, players, start, source). Annotations reference it. |
| `kind` | TEXT | age, war, war_declared, alliance, settlement, settlement_captured, tech, civic, tech_research, civic_research, government, religion, religion_founded, policy, production, project, purchase, legacy, unit_killed; diplomacy (diplomacy.py, DiplomacySummary + Game_Gossip): diplomacy_action (an action between two players from start to end: endeavors, sanctions, treaties, espionage, alliance proposals, city-state projects; players [initiator, recipient]), befriend (befriending an independent, start to end, with its stages and support; rivals = other majors befriending it at the same time), suzerain (a player is suzerain of a city-state: from gaining it to losing it; players [suzerain, city-state]), diplo_response (a proposal accepted / supported / rejected / opposed; players [responder, proposer]), met (first meeting; players [major, other]), relationship (snapshot: a met rival's attitude to the player changed level). |
| `subject` | TEXT | Display subject (tech name, city name, unit type, 'war 0-1', ...). |
| `start_g` | INTEGER | Game-wide start turn. |
| `start_age` | TEXT | Start age. |
| `start_turn` | INTEGER | Start turn within age. |
| `start_lo` | INTEGER | If precision='range': earliest possible start turn (within start_age). |
| `start_hi` | INTEGER | If precision='range': latest possible start turn. |
| `end_g` | INTEGER | Game-wide end turn; NULL if open. |
| `end_age` | TEXT | End age. |
| `end_turn` | INTEGER | End turn within age. |
| `players` | TEXT | JSON list of involved player ids (for unit_killed: [victim, killer]). |
| `local` | INTEGER | 1 if the local player is involved (and would know: a rival's espionage against the player is 0). |
| `minor` | INTEGER | 1 if an independent power / city-state is involved. |
| `precision` | TEXT | exact; range (happened between start_lo and start_hi); state (seen at that turn). |
| `source` | TEXT | snapshot, log:<name>, capture:<age>, derived. |
| `detail` | TEXT | JSON with kind-specific fields (city, item, category, completed, victim, killer, x, y, war_type, ...). diplomacy_action / befriend: action, action_type, group (endeavor, sanction, treaty, espionage, project, unique), proposal, initiator(_name), recipient(_name), recipient_kind, stages [{turn, stage}], stage, support / opposition {player: n}, support_log, response, outcome (success, canceled), end_reason (age_end_or_ongoing, restarted), lost_to, rivals, turn_influence_spent (local: the turn's whole influence spending, not split between actions), started_before_logs. suzerain: suzerain(_name), city_state(_name), capital, bonus, end_reason (replaced, incorporated, captured, age_end_or_ongoing), lost_to. diplo_response: action, response, proposer, responder. met: player, other, other_kind. settlement_captured: captured_by, from_player, capital (the losing civ's capital name). |

## combat_rewards

*exporter-owned.* Yields gained from combat, one row per reward (a kill paying two yields is two rows): kills (CombatLog + the post-combat-yield rules in the game data), pillage / coastal raids and trade-route plunder (Game_Gossip), dispersed independent powers (Game_Gossip + Independents). Every major player; rivals' rows come from logs the player cannot see. See combat.py for what the game does not log.  Primary key: `(game, reward_id)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `reward_id` | TEXT | Stable id (hash of kind, turn, player, plot, units, modifier). |
| `g` | INTEGER | Game-wide turn index. |
| `age` | TEXT | Age. |
| `turn` | INTEGER | Turn within the age (log turn: the turn whose end processed it). |
| `player_id` | INTEGER | Player who gained the reward. |
| `kind` | TEXT | kill (unit destroyed), pillage (land pillage or naval coastal raid: the log does not tell them apart), plunder (trade route), disperse (independent power camp). |
| `yield_type` | TEXT | YIELD_GOLD / YIELD_SCIENCE / YIELD_CULTURE / YIELD_DIPLOMACY (influence) / YIELD_PRODUCTION / YIELD_FOOD; for dispersals the camp type's yield; NULL when unknown (pillage, plunder). |
| `amount` | REAL | Yield gained; NULL when the game does not log it (pillage, plunder, most dispersals). Never estimated: unexplained treasury change is the separate unexplained_gold metric. |
| `basis` | TEXT | How amount was obtained: rules (computed from the logged defeated strength or unit cost and the game-data modifier), notification (exact, from a snapshot notification), unknown (amount NULL). |
| `x` | INTEGER | Plot x (combat plot, pillaged plot, or the dispersed camp). |
| `y` | INTEGER | Plot y. |
| `unit` | TEXT | Acting unit type (UNIT_*), if logged. |
| `target` | TEXT | Defeated unit type, dispersed tribe (INDEPENDENT_*), or plundered route 'from -> to'. |
| `target_owner` | INTEGER | Owner of the defeated unit (player id; independents have ids too), if logged. |
| `source` | TEXT | log:CombatLog, log:Game_Gossip. |
| `detail` | TEXT | JSON: modifier and granted_by (kill rules), victim_strength, city_state_type and base_amount (the unscaled XML DisperseRewardAmount). |

## phases

*exporter-owned.* Fact sheets for phases and initiatives: each age, each war the local player fought, each goal, and the whole game, with a heuristic outcome. No prose: Claude writes the narrative as annotations with scope/scope_key pointing here. Regenerated each refresh.  Primary key: `(game, scope, scope_key)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `scope` | TEXT | game, age, war, goal. |
| `scope_key` | TEXT | game: the game id; age: AGE_*; war: the war's events.event_key; goal: goals.goal_id as text. |
| `g_start` | INTEGER | First turn covered. |
| `g_end` | INTEGER | Last turn covered (current turn if ongoing). |
| `title` | TEXT | Label (AGE_*, 'war with player N', goal title). |
| `outcome` | TEXT | Heuristic: success, partial, failure, changed_plans (abandoned goals), ongoing (vocab field=outcome). An advisor outcome annotation on the same scope overrides it. |
| `facts` | TEXT | JSON. age: metrics{start,end}, standings{path:{rank,of,value,leader,leader_value}}, war_opponents, units_killed, units_lost, captured[], lost[], governments[], legacies[], wonders[], score. war: opponent, declared_by, war_type, turns, ongoing, units_killed/lost, captured/lost, casus_belli{declared_by, war_type, run_up[] (diplomacy and relationship changes between the two sides in the 10 turns before), joined_ally_war{ally, ally_war_start_g, declared_by, allied_since_g}}, combatants{us, them: {majors[], city_states[]}}, war_support{series[[g, value]], first, last, min, max}, notable{settlements[] (captured, lost, razing, razed, in order), peak_kill_turns[], upsets[] (capital_taken, capital_lost, eliminated, founded_lost)}. goal: horizon, status, progress, overdue, metrics[]. game: age_outcomes, standings_now. |

## advisor_profiles

*advisor-owned.* Reusable advisor personalities (not tied to a game): who Claude plays when advising. Editing makes a new version; games link to a version through game_profiles.  Primary key: `(profile_id, version)`.

| column | type | description |
|---|---|---|
| `profile_id` | INTEGER | Id, shared by all versions. |
| `version` | INTEGER | 1, 2, ... (latest is what new games get). |
| `name` | TEXT | Display name for picking it again (default: the preset label). |
| `personality` | TEXT | Advisor preset key: built-in (vocab field=personality) or custom (data/advisors/<key>.md), whose style prompt Claude follows. |
| `voice` | TEXT | Free-text voice/tone instructions layered on the personality. |
| `priorities` | TEXT | JSON list of the advisor's own priorities (vocab field=advisor_priority), most important first. |
| `quirks` | TEXT | Free text: idiosyncrasies, pet peeves, catchphrases (like a Civ AI agenda). |
| `created_at` | TEXT | ISO time written. |

## player_profiles

*advisor-owned.* Reusable player profiles (not tied to a game): why and how the player likes to play. Editing makes a new version; games link to a version through game_profiles.  Primary key: `(profile_id, version)`.

| column | type | description |
|---|---|---|
| `profile_id` | INTEGER | Id, shared by all versions. |
| `version` | INTEGER | 1, 2, ... (latest is what new games get). |
| `name` | TEXT | Display name for picking it again. |
| `motives` | TEXT | JSON list of vocab field=motive (fight, build, explore, mess_around, story): why they play. |
| `playstyle` | TEXT | JSON list of vocab field=playstyle (aggressive, cautious, efficient, whimsical, ...). |
| `risk_tolerance` | TEXT | low, medium, high. |
| `notes` | TEXT | Free text: what the player enjoys or avoids, including setup follow-up answers. |
| `created_at` | TEXT | ISO time written. |

## game_profiles

*advisor-owned.* Which advisor / player profile version each game uses, over time (latest seq per role is current).  Primary key: `(game, role, seq)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `role` | TEXT | advisor or player. |
| `seq` | INTEGER | 1, 2, ... per game and role (latest wins). |
| `profile_id` | INTEGER | advisor_profiles / player_profiles profile_id. |
| `version` | INTEGER | Profile version in effect. |
| `valid_from_g` | INTEGER | Game turn this link took effect. |
| `created_at` | TEXT | ISO time written. |

## strategies

*advisor-owned.* The player's strategies over time (usually one active). Tied to victory types or expansion.  Primary key: `(game, strategy_id)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `strategy_id` | INTEGER | Id. |
| `name` | TEXT | Short name, e.g. 'Rail-baron economy'. |
| `victory_types` | TEXT | JSON list of vocab field=strategy_focus (science, culture, economic, military, expansion, score). |
| `description` | TEXT | What the plan is. |
| `status` | TEXT | active, achieved, failed, abandoned. |
| `started_g` | INTEGER | Turn adopted. |
| `ended_g` | INTEGER | Turn it ended (status != active). |
| `created_at` | TEXT | ISO time written. |

## goals

*advisor-owned.* Long- and short-term goals. Short-term goals usually have a long-term parent; both may belong to a strategy.  Primary key: `(game, goal_id)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `goal_id` | INTEGER | Id. |
| `strategy_id` | INTEGER | strategies.strategy_id, or NULL. |
| `parent_goal_id` | INTEGER | Long-term goal this short-term goal serves, or NULL. |
| `horizon` | TEXT | short or long. |
| `title` | TEXT | Goal statement. |
| `description` | TEXT | Detail / why. |
| `priority` | INTEGER | 1 = highest. |
| `status` | TEXT | active, achieved, failed, abandoned. |
| `created_g` | INTEGER | Turn set. |
| `deadline_g` | INTEGER | Target turn, or NULL. |
| `resolved_g` | INTEGER | Turn resolved. |
| `resolution` | TEXT | How it ended. |

## goal_metrics

*advisor-owned.* Associations between goals and observable metrics: how progress is measured.  Primary key: `(game, goal_id, metric, player_id)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `goal_id` | INTEGER | goals.goal_id. |
| `metric` | TEXT | metric_catalog.metric. |
| `player_id` | INTEGER | Whose metric (default: local player). Use a rival's id for 'keep X below ...'. |
| `comparator` | TEXT | '>=' / '<=' on the value; 'gain>=' / 'gain<=' on value minus baseline; 'rank<=' on rank among majors (1 = top). |
| `target` | REAL | Target number for the comparator. |
| `baseline` | REAL | Value when the link was made (auto-filled). |
| `baseline_g` | INTEGER | Turn of the baseline. |
| `weight` | REAL | Relative weight within the goal (default 1). |

## annotations

*advisor-owned.* Non-game notes: player intentions, priority changes, advisor narrative, outcomes of how things went.  Primary key: `(game, annotation_id)`.

| column | type | description |
|---|---|---|
| `game` | TEXT | Game id. |
| `annotation_id` | INTEGER | Id. |
| `g` | INTEGER | Game turn the note is about. |
| `age` | TEXT | Age at g. |
| `turn` | INTEGER | Turn at g. |
| `author` | TEXT | advisor or player. |
| `kind` | TEXT | vocab field=annotation_kind (intention, priority_change, narrative, blurb, outcome, observation, warning, advice). narrative/outcome notes with scope/scope_key are the story the dashboard shows for that phase. |
| `text` | TEXT | The note. |
| `outcome` | TEXT | vocab field=outcome (success, partial, failure, changed_plans, ongoing), or NULL. |
| `goal_id` | INTEGER | Related goal, or NULL. |
| `strategy_id` | INTEGER | Related strategy, or NULL. |
| `event_key` | TEXT | Related timeline event, or NULL. |
| `scope` | TEXT | Phase this note narrates or judges (phases.scope: game, age, war, goal), or NULL. |
| `scope_key` | TEXT | phases.scope_key, or NULL. |
| `created_at` | TEXT | ISO time written. |

## vocab

*exporter-owned.* Controlled vocabularies with descriptions (personalities, playstyles, strategy focuses, comparators, kinds).  Primary key: `(field, value)`.

| column | type | description |
|---|---|---|
| `field` | TEXT | personality, advisor_priority, playstyle, motive, strategy_focus, comparator, annotation_kind, outcome. |
| `value` | TEXT | Allowed value. |
| `description` | TEXT | Meaning; for personality, how to speak; for strategy_focus, the metrics it suggests. |
| `extra` | TEXT | JSON: personality -> default priorities; motive -> follow-up angles; strategy_focus -> suggested metrics. |

## schema_docs

*exporter-owned.* This documentation: every table and column with its type and meaning.  Primary key: `(table_name, column_name)`.

| column | type | description |
|---|---|---|
| `table_name` | TEXT | Table. |
| `column_name` | TEXT | Column ('' for the table itself). |
| `type` | TEXT | SQLite type. |
| `owner` | TEXT | exporter (regenerated) or advisor (persistent). |
| `description` | TEXT | Meaning. |

## Metrics

| metric | unit | description | sources | victory |
|---|---|---|---|---|
| `gold_per_turn` | per_turn | Net gold income. | snapshot, log:Player_Stats | economic |
| `science_per_turn` | per_turn | Science yield. | snapshot, log:Player_Stats | science |
| `culture_per_turn` | per_turn | Culture yield. | snapshot, log:Player_Stats | culture |
| `happiness_per_turn` | per_turn | Net happiness (celebrations). | snapshot, log:Player_Stats |  |
| `influence_per_turn` | per_turn | Influence (YIELD_DIPLOMACY). | snapshot, log:Player_Stats |  |
| `production_per_turn` | per_turn | Empire production. | snapshot, log:Player_Stats | military |
| `food_per_turn` | per_turn | Empire food. | snapshot, log:Player_Stats | expansion |
| `gold_balance` | gold | Gold in the treasury. | snapshot, log:Player_Stats | economic |
| `influence_balance` | influence | Banked influence. | snapshot |  |
| `settlements` | count | Cities + towns. | snapshot, log:Player_Stats | expansion |
| `cities` | count | Cities (not towns). | snapshot, log:Player_Stats | expansion |
| `towns` | count | Towns. | snapshot, log:Player_Stats | expansion |
| `settlement_cap` | count | Settlement limit before unhappiness penalties. | snapshot, log:Player_Stats | expansion |
| `population` | count | Sum of settlement population. | snapshot, log:Player_Stats (urban+rural) | expansion |
| `tiles_owned` | count | Owned plots. | log:Player_Stats | expansion |
| `techs` | count | Technologies researched this age (log counts per age). | log:Player_Stats | science |
| `land_units` | count | Land units. | log:Player_Stats | military |
| `naval_units` | count | Naval units. | log:Player_Stats | military |
| `military_units` | count | Own combat units (snapshot unit list, land+sea+air combat classes). | snapshot | military |
| `trade_routes` | count | Established trade routes. | snapshot | economic |
| `wars_active` | count | Majors the player is at war with. | snapshot | military |
| `unrest_settlements` | count | Settlements in unrest. | snapshot |  |
| `units_lost_total` | count | Cumulative units/ships lost this game (Historian log, logged ages). | derived from events | military |
| `units_killed_total` | count | Cumulative enemy units/ships destroyed. | derived from events | military |
| `settlements_captured_total` | count | Cumulative settlements captured. | derived from events | military |
| `vp_economic` | points | Economic legacy/victory points this age (Modern: toward the economic victory target). | snapshot countdown_victories, capture | economic |
| `vp_science` | points | Science legacy/victory points this age. | snapshot, capture | science |
| `vp_culture` | points | Culture legacy/victory points this age. | snapshot, capture | culture |
| `vp_military` | points | Military legacy/victory points this age. | snapshot, capture | military |
| `vp_economic_target` | points | Points needed for the Modern economic victory. | snapshot | economic |
| `vp_science_target` | points | Points needed for the Modern science victory. | snapshot | science |
| `vp_culture_target` | points | Points needed for the Modern culture victory. | snapshot | culture |
| `vp_military_target` | points | Points needed for the Modern military victory. | snapshot | military |
| `domination_score` | points | Domination victory score. | snapshot victories | military |
| `score` | points | Score victory points. | snapshot victories | score |
| `triumphs_done` | count | Legacy-path triumphs completed this age. | snapshot triumphs |  |
| `age_progress` | turns | Age progress counter (age_progress.current of .max). | snapshot |  |
| `combat_gold_total` | gold | Cumulative gold from combat with an exact amount: kill rewards (rules) and dispersal notifications. Pillage, plunder and most dispersal gold are not logged and not included (see unexplained_gold). Compare with trade_income_per_turn. | derived from combat_rewards | economic |
| `combat_science_total` | science | Cumulative science from combat (kill rewards such as Yi Sun-sin's; dispersal amounts only when a notification was seen). | derived from combat_rewards | science |
| `combat_culture_total` | culture | Cumulative culture from combat (kill rewards: Napoleon, Cocorico...). | derived from combat_rewards | culture |
| `combat_influence_total` | influence | Cumulative influence from combat (kill rewards: Lakshmibai, Gilgamesh). | derived from combat_rewards |  |
| `combat_production_total` | production | Cumulative production from combat (dispersal notifications). | derived from combat_rewards |  |
| `combat_food_total` | food | Cumulative food from combat (dispersal notifications). | derived from combat_rewards |  |
| `kill_gold_total` | gold | Cumulative gold from destroying units (Privateer, Edward Teach, Merry Life...), computed from CombatLog and the game's rules. | derived from combat_rewards (rules) | economic |
| `pillages_total` | count | Cumulative tiles pillaged or coastal-raided (Game_Gossip). | derived from combat_rewards | military |
| `independents_dispersed_total` | count | Cumulative independent powers dispersed. | derived from combat_rewards | military |
| `trade_routes_plundered_total` | count | Cumulative trade routes plundered. | derived from combat_rewards | military |
| `unexplained_gold` | gold | Treasury change not explained by per-turn income or logged purchases; may include pillage/plunder/dispersal, deals, treasure fleets, etc.; not attributed. Per turn: next turn's balance minus this turn's balance and gold yield (Player_Treasury), minus exact combat gold (kill rules, notifications). Absent on turns with a logged purchase (CityPurchase has no prices). Negative: upgrades, gifts, deals paid. | log:Player_Treasury | economic |
| `unexplained_gold_total` | gold | Cumulative sum of positive unexplained_gold turns (unexplained gains; negative turns are not netted). Not attributed to any source. | derived from Player_Treasury | economic |
| `trade_income_per_turn` | per_turn | Gold per turn from trade routes (snapshot trade.trade_income_total). | snapshot | economic |
| `suzerainties` | count | City-states the player is suzerain of now. Snapshot (met city-states, visible) where one exists; otherwise from the logs (suzerain events; an age end clears them). | snapshot city_states, derived from events |  |
| `suzerainties_total` | count | Cumulative times the player became suzerain of a city-state (logged ages only). | derived from events |  |
| `befriend_actions_active` | count | Independents the player is befriending right now. | derived from events |  |
| `diplomatic_actions_started_total` | count | Cumulative diplomatic actions the player started: befriending, endeavors, sanctions, treaties, espionage, alliance proposals, war declarations (not land claims). | derived from events |  |
| `independents_met` | count | Distinct independent powers met this age (resets each age). | derived from events |  |
| `influence_spent` | influence | Influence spent this turn: bank + influence income - next turn's bank (consecutive snapshots). Negative: gained beyond income. Local player only; not attributed to an action. | snapshot |  |
| `influence_spent_total` | influence | Cumulative sum of positive influence_spent turns. | derived from snapshots |  |
| `relationship` | points | A met rival's attitude toward the local player, -100 (hostile) .. 100 (helpful); stored under the rival's player_id. | snapshot |  |
| `war_support` | points | War support in the local player's war with a rival: supporters minus opposers of the war, from the local player's side (positive favours us); stored under the opponent's player_id for turns at war. | snapshot | military |

## vocab: personality

- `cautious_steward`: Worried accountant. Flags risk, budgets, contingencies; prefers safe compounding. (economy, security, happiness)
- `diplomatic`: Courteous, consensus-seeking, frames rivals as partners or problems to manage. (alliances, influence, trade)
- `jocular`: Warm and joking. Celebrates wins with puns, ribs the player about blunders, never mean. (growth, happiness, wonders)
- `machiavellian`: Cold opportunist. Every rival is a mark; recommends betrayal when the numbers favor it. (victory_progress, military_strength, influence)
- `sarcastic`: Dry, eye-rolling. Points out folly with irony but the advice underneath is sound. (efficiency, economy, security)
- `scholarly`: Measured and analytical, cites mechanics and history, explains the why. (science, long_term_planning, culture)
- `terse_military`: Clipped field-report style. Bullet points, threat first, numbers, no pleasantries. (security, military_strength, production)
- `zealous`: Fervent true believer in the current strategy. Rallying cries, urgency, little patience for detours. (victory_progress, faith, culture)

## vocab: advisor_priority

- `security`: Defend settlements, don't lose units cheaply.
- `military_strength`: Field and upgrade armies.
- `economy`: Gold income, treasury, trade.
- `science`: Tech pace.
- `culture`: Civics pace, wonders, tourism.
- `growth`: Food, population, new settlements.
- `production`: Production capacity.
- `happiness`: Avoid unrest, celebrations.
- `influence`: Influence income, city-states.
- `alliances`: Relationships and alliances.
- `trade`: Trade routes and resources.
- `wonders`: Wonder construction.
- `faith`: Religion spread.
- `efficiency`: Least waste: idle units, overflow, unspent gold.
- `victory_progress`: Whatever moves the chosen victory fastest.
- `long_term_planning`: Set up the next age.

## vocab: playstyle

- `aggressive`: Seeks war and conquest; accepts losses for territory.
- `cautious`: Defends first, avoids risky wars, keeps reserves.
- `efficient`: Min-maxes yields and timing; wants optimal orders.
- `whimsical`: Plays for fun and story; will chase wonders or odd ideas over optimal play.
- `builder`: Wonders, districts, tall cities.
- `expansionist`: Wide empire, many settlements.
- `opportunist`: Switches plans to exploit openings.
- `diplomat`: Alliances, city-states, peaceful influence.
- `roleplayer`: Plays in character for the leader/civ.

## vocab: motive

- `fight`: Fight: War, conquest, armies and commanders. (early rush or late, decisive wars?, conquer and keep cities, or raze and move on?, how many units are you willing to lose for a city?)
- `build`: Build: Cities, wonders, districts, a thriving economy. (tall (few big cities) or wide (many settlements)?, which wonders or eras excite you?, do you enjoy optimizing yields, or just watching things grow?)
- `explore`: Explore: Scouting, the map, the Distant Lands, discoveries. (land or sea first?, do you want to race for the Distant Lands / treasure fleets?, settle what you find, or just see it?)
- `mess_around`: Mess around: Low stakes, try odd ideas, see what happens. (any experiment you want to try this game (a weird civ swap, all-towns, only wonders)?, should I stay quiet unless something is on fire, or pitch wild ideas?)
- `story`: Make a story: Play in character and remember the game as a narrative. (who is your leader in this story: a hero, a schemer, a reluctant ruler?, any rival you want as the nemesis or the old friend?, should I narrate turning points as they happen?)

## vocab: strategy_focus

- `science`: Science legacy path / Modern space race. (vp_science, science_per_turn, techs)
- `culture`: Culture legacy path / Modern culture victory. (vp_culture, culture_per_turn, triumphs_done)
- `economic`: Economic legacy path / Modern economic victory. (vp_economic, gold_per_turn, trade_routes, gold_balance)
- `military`: Military legacy path / Modern military victory / domination. (vp_military, domination_score, settlements_captured_total, units_killed_total, units_lost_total)
- `expansion`: Grow wide: settlements, population, land. (settlements, population, settlement_cap, tiles_owned)
- `score`: Maximize score. (score)

## vocab: comparator

- `>=`: Current value at least target.
- `<=`: Current value at most target.
- `gain>=`: Value minus baseline at least target.
- `gain<=`: Value minus baseline at most target (e.g. lose at most N units).
- `rank<=`: Rank among majors (1 = highest value) at most target.

## vocab: annotation_kind

- `intention`: What the player means to do and why.
- `priority_change`: The player changed priorities, strategy or goals.
- `narrative`: Advisor's story of a phase or initiative.
- `blurb`: One- or two-sentence summary of the game for the overview page (scope=game).
- `outcome`: How an initiative turned out (set outcome).
- `observation`: Something notable in the game state.
- `warning`: A risk the advisor flagged.
- `advice`: A recommendation given to the player.

## vocab: outcome

- `success`: Achieved what it set out to do, or beat expectations.
- `partial`: Achieved part of it, or at a higher cost than planned.
- `failure`: Tried and fell short, or backfired.
- `changed_plans`: Dropped or superseded by a new plan or new information (e.g. the game's rules ruled it out); not judged as a failure.
- `ongoing`: Not settled yet.
