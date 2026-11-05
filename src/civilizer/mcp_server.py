"""MCP server (stdio) over the central store, for Claude Code acting as the in-game advisor.

Reads the store; writes only advisor tables. Never talks to the running game: the monitor does that.
Registered for this repo in .mcp.json (`uv run --directory /home/jp/src/civilizer civilizer mcp`).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import advisor, store

INSTRUCTIONS = """Civ VII advisor over civilizer's store (data/civilizer.db), refreshed every turn by `civilizer monitor`.
This server never touches the running game; never run `civilizer eval` or ad-hoc JS against it (it can crash the game).

Setup: when the briefing has `setup_needed` (or the player wants to change advisor, profile or strategy), run
`setup_wizard` and follow it: it returns one question at a time with numbered options to show the player and says
which tool to call with the answer. Advisor and player profiles are reusable records shared across games
(`saved_profiles`, `use_profile`); strategies belong to the game.

Each turn: call `briefing`. It returns the advisor to speak as (advisor_profile: style_prompt is the style to
follow in every reply, plus voice, priorities, quirks), the player's motives, playstyle and risk tolerance, the active strategy -> long-term -> short-term goal
tree with metric progress, current numbers with deltas, standings (visible values only), new local events,
alerts and recent annotations. If `setup_needed` is present, ask the player and set the profiles/plan first.

Advising: speak in the advisor's voice and lean on its priorities, but tailor the recommendation to the player's
playstyle and the active goals. Tie each recommendation to a goal metric and quote real numbers from the briefing.
When the player states a plan, propose a goal with metric links (add_goal with metrics, comparators >= <= gain>=
gain<= rank<=; `vocabulary` and `schema` list values and metrics) and confirm before adding. Record the player's
intentions and changes of priority with annotate(kind=intention|priority_change, author=player); profile/goal/strategy
changes record themselves. When an initiative resolves, set_goal_status / set_strategy_status with an outcome
(success / partial / failure / changed_plans / ongoing); a plan dropped because the game or new information ruled it
out is changed_plans, not failure. Re-sending the current status re-judges the outcome without moving the end turn.

Narrative: code does not write prose. `phases` returns fact sheets (game, each age, each war, each goal) with a
heuristic outcome; write the story yourself as annotate(kind=narrative, scope=..., scope_key=...) and judge it with
annotate(kind=outcome, outcome=success|partial|failure|changed_plans, same scope), which overrides the heuristic outcome.

Civilopedia and rules: for "what is X / what does X unlock / what unlocks X / which should I pick" use the indexed
Civilopedia first (the game's own text plus facts from its data XML, linked): `pedia_search(query, kind, age)` to find
pages, `pedia_page(key)` for a page's text, facts (cost, yields, category such as gold/production building, placement,
unlocked_by, unique_to, unit stats, what a tech/civic unlocks) and links, `pedia_links(key, relation)` to follow
unlocks / unlocked_by / prereq / unique_to / has_unique / replaces / upgrades_to / mentions, `pedia_compare(keys)` to
weigh options side by side, `pedia_filter` for "buildings in antiquity for Carthage", and `pedia_reachable` for what
the player's next techs/civics would unlock. Quote what they return; don't fill gaps from memory. `civilopedia(query)`
returns long chapter text (history, flavour). For modifier-level numbers call `game_rules(name)`: it reads the raw data
XML modifiers, which are authoritative. Patches change values, so prefer these tools over memory.

`query` runs read-only SQL for anything else (tables in `schema`; g = game-wide turn index)."""

server = MCPServer("civilizer", instructions=INSTRUCTIONS)


@contextmanager
def _db():
    """Store transaction; advisor validation errors become tool errors the model can read and fix."""
    try:
        with store.tx() as con:
            yield con
    except (advisor.AdvisorError, sqlite3.Error) as e:
        raise ToolError(str(e)) from e


def _game(con, game: str | None) -> str:
    return advisor.game_row(con, game)["game"]


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur]


# ---------------------------------------------------------------- reading

@server.tool(structured_output=False)
def briefing(game: str | None = None, since_g: int | None = None, max_events: int = 25) -> str:
    """Per-turn briefing (compact JSON): advisor personality, player profile, active plan with goal progress, current
    numbers with deltas since since_g (default: previous observed turn), standings, local events since then, alerts,
    recent notes. Deltas of 0 are omitted; as_of_g appears only when a value is older than the current turn."""
    with _db() as con:
        # compact JSON: this is called every turn, and indentation would add ~40% to the payload
        return json.dumps(advisor.briefing(con, game, since_g, max_events), separators=(",", ":"), ensure_ascii=False, default=str)


@server.tool()
def schema() -> str:
    """Markdown documentation of every table, column, metric and vocabulary in the store."""
    return store.schema_markdown()


@server.tool()
def query(sql: str, params: list | None = None, limit: int = 200) -> list[dict]:
    """Read-only SQL against civilizer.db (one SELECT / WITH statement). Tables: see `schema`. g = game-wide turn."""
    if not sql.lstrip().lower().startswith(("select", "with")):
        raise ToolError("only SELECT/WITH queries")
    if not store.DB.exists():
        raise ToolError(f"no store at {store.DB}; run `civilizer refresh`")
    con = sqlite3.connect(f"file:{store.DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return _rows(con.execute(sql, params or []).fetchmany(min(limit, 2000)))
    except sqlite3.Error as e:
        raise ToolError(f"SQL error: {e}") from e
    finally:
        con.close()


@server.tool()
def metric_history(metric: str, player_id: int | None = None, last: int = 20, game: str | None = None) -> list[dict]:
    """Recent values of one metric (metric_catalog.metric) for a player (default local), oldest first."""
    with _db() as con:
        gr = advisor.game_row(con, game)
        pid = gr["local_player_id"] if player_id is None else player_id
        rows = _rows(con.execute("SELECT g, age, turn, value, source, visibility FROM metrics WHERE game=? AND metric=? AND "
                                 "player_id=? ORDER BY g DESC LIMIT ?", (gr["game"], metric, pid, last)))
        return rows[::-1]


@server.tool()
def timeline(kinds: list[str] | None = None, since_g: int = 0, until_g: int | None = None, local_only: bool = True,
             include_minor: bool = True, limit: int = 200, game: str | None = None) -> list[dict]:
    """Timeline events overlapping [since_g, until_g] (kinds: war, war_declared, alliance, settlement, settlement_captured,
    tech, civic, government, policy, production, project, purchase, legacy, unit_killed, diplomacy_action, befriend,
    suzerain, diplo_response, met, relationship, ...)."""
    with _db() as con:
        g = _game(con, game)
        q = "SELECT * FROM events WHERE game=? AND (start_g>=? OR end_g>=?)"
        args: list = [g, since_g, since_g]
        if until_g is not None:
            q += " AND start_g<=?"
            args.append(until_g)
        if local_only:
            q += " AND local=1"
        if not include_minor:
            q += " AND minor=0"
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        return [r | {"players": json.loads(r["players"]), "detail": json.loads(r["detail"] or "{}")}
                for r in _rows(con.execute(q + " ORDER BY start_g LIMIT ?", (*args, limit)))]


@server.tool()
def phases(scope: str | None = None, with_facts: bool = True, game: str | None = None) -> list[dict]:
    """Phase fact sheets (scope game / age / war / goal) with heuristic_outcome, advisor_outcome (latest outcome
    annotation on that scope), effective_outcome, the phase's narrative/outcome annotations, and needs_narrative."""
    with _db() as con:
        return advisor.phases(con, _game(con, game), scope, with_facts)


@server.tool()
def vocabulary(field: str | None = None) -> list[dict]:
    """Allowed values: personality, advisor_priority, playstyle, strategy_focus, comparator, annotation_kind, outcome."""
    with _db() as con:
        if field:
            return _rows(con.execute("SELECT * FROM vocab WHERE field=?", (field,)))
        return _rows(con.execute("SELECT * FROM vocab"))


@server.tool()
def goals(active_only: bool = False, game: str | None = None) -> list[dict]:
    """Full plan: strategies -> long-term goals -> short-term goals with per-metric progress (rolled up to parents)."""
    with _db() as con:
        return advisor.goal_tree(con, _game(con, game), active_only)


@server.tool()
def annotations(kind: str | None = None, scope: str | None = None, scope_key: str | None = None, goal_id: int | None = None,
                since_g: int | None = None, limit: int = 50, game: str | None = None) -> list[dict]:
    """Annotations (non-game notes), oldest first, filtered by kind / scope / goal / turn."""
    with _db() as con:
        return advisor.annotations(con, _game(con, game), kind, scope, scope_key, goal_id, since_g, limit)


@server.tool()
def advisor_history(what: str | None = None, game: str | None = None) -> list[dict]:
    """Change log of profiles, strategies and goals (what: profile, strategy, goal, or all), oldest first."""
    with _db() as con:
        return advisor.history(con, _game(con, game), what)


@server.tool()
def profiles(game: str | None = None) -> dict:
    """This game's current advisor and player profiles."""
    with _db() as con:
        g = _game(con, game)
        return {"advisor": advisor.current_advisor_profile(con, g), "player": advisor.current_player_profile(con, g)}


# ---------------------------------------------------------------- writing (advisor tables only)

@server.tool()
def saved_profiles(role: str | None = None) -> list[dict]:
    """Reusable advisor / player profiles (latest version each), most recently used first, with the games that used them."""
    with _db() as con:
        return advisor.list_profiles(con, role)


@server.tool()
def setup_wizard(step: str | None = None, game: str | None = None) -> dict:
    """Next setup question to put to the player (advisor, then player, then strategy; step forces one to change it):
    question, numbered options, multi_select, how to present them, and which tool to call with the answer."""
    with _db() as con:
        return advisor.setup_wizard(con, _game(con, game), step)


@server.tool()
def set_advisor_profile(personality: str, voice: str = "", priorities: list[str] | None = None, quirks: str = "",
                        name: str | None = None, profile_id: int | None = None, game: str | None = None) -> dict:
    """Use an advisor for this game. personality: an advisor preset key (built-in or custom; see setup_wizard).
    profile_id: make a new version of that saved profile; without it a saved profile with identical settings is
    reused or a new one is saved. priorities (vocab advisor_priority) default to the preset's."""
    with _db() as con:
        return advisor.set_advisor_profile(con, _game(con, game), personality, voice, priorities, quirks, name, profile_id)


@server.tool()
def create_advisor_preset(key: str, label: str, description: str, prompt: str, priorities: list[str],
                          overwrite: bool = False) -> dict:
    """Save a custom advisor style prompt as data/advisors/<key>.md (role line + bullets on tone and structure, like
    the built-ins). Then set_advisor_profile(personality=key). key: lowercase letters, digits, _."""
    with _db() as con:
        return advisor.create_advisor_preset(con, key, label, description, prompt, priorities, overwrite)


@server.tool()
def set_player_profile(motives: list[str] | None = None, playstyle: list[str] | None = None, risk_tolerance: str = "medium",
                       notes: str = "", name: str | None = None, profile_id: int | None = None, game: str | None = None) -> dict:
    """Use a player profile for this game (motives: fight, build, explore, mess_around, story; playstyle: aggressive,
    cautious, efficient, whimsical, builder, expansionist, opportunist, diplomat, roleplayer). profile_id makes a new
    version of that saved profile. Changes within a game are logged as priority_change annotations."""
    with _db() as con:
        return advisor.set_player_profile(con, _game(con, game), playstyle, risk_tolerance, notes, motives, name, profile_id)


@server.tool()
def use_profile(role: str, profile_id: int, version: int | None = None, game: str | None = None) -> dict:
    """Link a saved advisor or player profile (role advisor|player; latest version unless given) to this game."""
    with _db() as con:
        return advisor.use_profile(con, _game(con, game), role, profile_id, version)


@server.tool()
def choose_strategy(presets: list[str], victory_types: list[str] | None = None, name: str | None = None, notes: str = "",
                    game: str | None = None) -> dict:
    """Adopt a strategy from setup_wizard presets (science, culture, economic, military, expansion, score, just_win,
    not_sure). just_win alone needs victory_types you pick for this civ; without them nothing is saved and the reply
    says what to decide. Returns guidance and suggested metrics for starter goals."""
    with _db() as con:
        return advisor.choose_strategy(con, _game(con, game), presets, victory_types, name, notes)


@server.tool()
def add_strategy(name: str, victory_types: list[str], description: str = "", game: str | None = None) -> dict:
    """Adopt a strategy (victory_types: science, culture, economic, military, expansion, score). Returns suggested metrics."""
    with _db() as con:
        return advisor.add_strategy(con, _game(con, game), name, victory_types, description)


@server.tool()
def set_strategy_status(strategy_id: int, status: str, note: str = "", outcome: str | None = None, game: str | None = None,
                        verdict: str | None = None) -> dict:
    """Mark a strategy active / achieved / failed / abandoned (logged as an outcome annotation). outcome: success /
    partial / failure / changed_plans / ongoing, default from status (abandoned -> changed_plans). verdict: old name."""
    with _db() as con:
        return advisor.set_strategy_status(con, _game(con, game), strategy_id, status, note, outcome or verdict)


@server.tool()
def add_goal(title: str, horizon: str = "short", strategy_id: int | None = None, parent_goal_id: int | None = None,
             priority: int = 2, deadline_g: int | None = None, description: str = "", metrics: list[dict] | None = None,
             game: str | None = None) -> dict:
    """Add a goal. horizon short|long; short goals usually set parent_goal_id to a long goal (inheriting its strategy).
    priority 1 = highest. deadline_g is a game-wide turn (briefing.game.current_g + N). metrics: list of
    {metric, comparator ('>=','<=','gain>=','gain<=','rank<='), target, player_id?, weight?}."""
    with _db() as con:
        return advisor.add_goal(con, _game(con, game), title, horizon, strategy_id, parent_goal_id, priority, deadline_g,
                                description, metrics)


@server.tool()
def update_goal(goal_id: int, title: str | None = None, description: str | None = None, priority: int | None = None,
                deadline_g: int | None = None, parent_goal_id: int | None = None, strategy_id: int | None = None,
                note: str = "", game: str | None = None) -> dict:
    """Change a goal's title, priority, deadline (-1 clears it), parent or strategy; the diff is logged."""
    with _db() as con:
        return advisor.update_goal(con, _game(con, game), goal_id, title, description, priority, deadline_g, parent_goal_id,
                                   strategy_id, note)


@server.tool()
def link_metric(goal_id: int, metric: str, comparator: str = ">=", target: float = 0, player_id: int | None = None,
                weight: float = 1.0, game: str | None = None) -> dict:
    """Measure a goal by a metric; baseline is the current value (rank for rank<=). Re-linking replaces the target."""
    with _db() as con:
        return advisor.link_metric(con, _game(con, game), goal_id, metric, comparator, target, player_id, weight)


@server.tool()
def unlink_metric(goal_id: int, metric: str, player_id: int | None = None, game: str | None = None) -> str:
    """Stop measuring a goal by a metric."""
    with _db() as con:
        advisor.unlink_metric(con, _game(con, game), goal_id, metric, player_id)
        return "ok"


@server.tool()
def set_goal_status(goal_id: int, status: str, resolution: str = "", outcome: str | None = None, game: str | None = None,
                    verdict: str | None = None) -> dict:
    """Resolve or reopen a goal: active / achieved / failed / abandoned, with how it went: outcome success / partial /
    failure / changed_plans / ongoing (default from status: abandoned -> changed_plans). Same status again: re-judge
    the outcome only. verdict: old name for outcome (went_well / went_awry / mixed still accepted)."""
    with _db() as con:
        return advisor.set_goal_status(con, _game(con, game), goal_id, status, resolution, outcome or verdict)


@server.tool()
def annotate(text: str, kind: str = "observation", author: str = "advisor", outcome: str | None = None,
             goal_id: int | None = None, strategy_id: int | None = None, event_key: str | None = None, g: int | None = None,
             scope: str | None = None, scope_key: str | None = None, game: str | None = None, verdict: str | None = None) -> int:
    """Record a note not from the game: intention, priority_change, narrative, outcome, observation, warning, advice.
    scope/scope_key attach it to a phase (game; age AGE_*; war = event_key; goal = goal id): narrative = the phase's
    story, kind=outcome + outcome (success / partial / failure / changed_plans / ongoing) overrides its heuristic
    outcome. verdict: old name for outcome. g defaults to now."""
    with _db() as con:
        return advisor.annotate(con, _game(con, game), text, kind, author, outcome or verdict, goal_id, strategy_id, event_key, g, scope,
                                scope_key)


# ---------------------------------------------------------------- game knowledge (install files, not the store)

@server.tool()
def civilopedia(query: str = "", section: str | None = None, limit: int = 3, max_chars: int = 2500,
                with_rules: bool = False) -> dict:
    """Search the game's Civilopedia (concepts, ages, civs, leaders, units, buildings, wonders, techs, civics,
    governments, map). Returns matching pages with section and chapter text, plus other matching titles. Empty query
    with a section lists that section's pages. with_rules adds the data-XML modifier rows for the page's game type."""
    from . import pedia

    try:
        return pedia.search(query, section=section, limit=limit, max_chars=max_chars, with_rules=with_rules)
    except pedia.PediaError as e:
        raise ToolError(str(e)) from e


@server.tool()
def game_rules(name: str, max_types: int = 3, max_refs: int = 25) -> list[dict]:
    """Exact game rules for an in-game name (policy, building, unit, tradition, ...): description plus the data XML
    rows and <Modifier> blocks that reference it. Authoritative for numbers; slower (scans the install)."""
    from . import rules

    out = rules.lookup(name, max_types=max_types)
    for r in out:
        r["references"] = [{"file": f, "xml": x} for f, x in r["references"][:max_refs]]
    return out


def _pedia(fn, *args, **kw) -> str:
    """Compact JSON (like briefing): pedia pages are large and indentation would add a third."""
    from . import civilopedia

    try:
        return json.dumps(fn(*args, **kw), separators=(",", ":"), ensure_ascii=False)
    except civilopedia.PediaIndexError as e:
        raise ToolError(str(e)) from e


@server.tool(structured_output=False)
def pedia_search(query: str, kind: str | None = None, age: str | None = None, limit: int = 10) -> str:
    """Search the indexed Civilopedia (game text + data XML facts; built from the install on first use). Returns ranked
    hits: key, title, kind, age and a one-line summary (techs/civics: what they unlock). kind: concept, term (glossary),
    unit, building, wonder, improvement, quarter, tech, civic, tradition (civ-unique), policy, civilization, leader,
    government, belief, resource, terrain, biome, feature, natural_wonder, project, legacy_path (comma-separate several).
    age: antiquity | exploration | modern (items with no age match every age). Then open a hit with pedia_page(key)."""
    from . import civilopedia

    return _pedia(civilopedia.search, query, kind, age, limit)


@server.tool(structured_output=False)
def pedia_page(key_or_title: str, max_chars: int = 4000) -> str:
    """One Civilopedia page by key (BUILDING_COTHON, NODE_CIVIC_AQ_MAIN_CODE_OF_LAWS, CONCEPTS:BIOME_4) or title
    ("Cothon", "Code of Laws"): readable chapter text, structured facts from the game data (cost, base_yields,
    maintenance, adjacencies, category e.g. production/gold building, placement, unlocked_by, unique_to, quarter, unit
    combat stats/upgrades/replaces, tech/civic prereqs, unlocks and mastery_unlocks incl. modifier effects like
    "+1 Settlement Limit", civ abilities and unique items), outgoing links by relation and incoming links (unlocked_by,
    has_unique, prereq_of, ...). also_matches lists other pages with the same title (e.g. unit tiers)."""
    from . import civilopedia

    return _pedia(civilopedia.page, key_or_title, max_chars)


@server.tool(structured_output=False)
def pedia_links(key: str, relation: str | None = None, direction: str = "out", kind: str | None = None) -> str:
    """Graph neighbours of a Civilopedia page. relation (forward / inverse): unlocks / unlocked_by, prereq / prereq_of,
    unique_to / has_unique, replaces / replaced_by, upgrades_to / upgrades_from, belongs_to_quarter / quarter_buildings,
    requires_building / required_by, placed_on / allows, improves_resource / improved_by, found_in / has,
    unlocks_civ / unlocked_by_civ, obsoletes / obsoleted_by, mentions / mentioned_by (in-text links, anchor = link
    text; via=name marks title matches in concept prose), see_also. direction: out | in | both (an inverse name already
    reads inwards). kind filters the neighbours. Examples: ("Code of Laws", "unlocks"), ("Merchant", "unlocked_by"),
    ("Carthage", "has_unique")."""
    from . import civilopedia

    return _pedia(civilopedia.links, key, relation, direction, kind)


@server.tool(structured_output=False)
def pedia_compare(keys: list[str], fields: list[str] | None = None) -> str:
    """Side-by-side facts for 2-8 pages (keys or titles), e.g. candidate buildings, civics or units: facts that differ
    (one value per page, in order) and facts that are the same. fields limits the rows (cost, base_yields, category,
    unlocked_by, combat, unlocks, ...)."""
    from . import civilopedia

    return _pedia(civilopedia.compare, keys, fields)


@server.tool(structured_output=False)
def pedia_filter(kind: str | None = None, age: str | None = None, civ: str | None = None, category: str | None = None,
                 unlocked_by: str | None = None, include_unique: bool = True, limit: int = 200) -> str:
    """List pages by kind and age for one civ: generic items plus that civ's uniques (other civs' uniques dropped),
    with category and unlocked_by per item. category: building yield category (gold, production, science, culture,
    food, happiness, diplomacy, military, trade). unlocked_by: only items a given tech/civic unlocks.
    Example: kind=building, age=antiquity, civ=carthage."""
    from . import civilopedia

    return _pedia(civilopedia.filter_pages, kind, age, civ, category, unlocked_by, include_unique, limit)


@server.tool(structured_output=False)
def pedia_reachable(known: list[str], steps: int = 1, system: str | None = None, civ: str | None = None,
                    age: str | None = None) -> str:
    """Techs/civics researchable within `steps` steps of the researched ones (keys or titles), each with cost and what
    it unlocks (and mastery unlocks). Step 1 = researchable now. Covers the age's main trees plus civ's unique civics.
    system: tech | civic. With no known nodes, give age to list the tree roots. Use for "what should I research next"."""
    from . import civilopedia

    return _pedia(civilopedia.reachable, known, steps, system, civ, age)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
