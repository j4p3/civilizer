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

Each turn: call `briefing`. It returns the advisor personality to speak as (advisor_profile: personality, voice,
priorities, quirks), the player's playstyle and risk tolerance, the active strategy -> long-term -> short-term goal
tree with metric progress, current numbers with deltas, standings (visible values only), new local events,
alerts and recent annotations. If `setup_needed` is present, ask the player and set the profiles/plan first.

Advising: speak in the advisor's voice and lean on its priorities, but tailor the recommendation to the player's
playstyle and the active goals. Tie each recommendation to a goal metric and quote real numbers from the briefing.
When the player states a plan, propose a goal with metric links (add_goal with metrics, comparators >= <= gain>=
gain<= rank<=; `vocabulary` and `schema` list values and metrics) and confirm before adding. Record the player's
intentions and changes of priority with annotate(kind=intention|priority_change, author=player); profile/goal/strategy
changes record themselves. When an initiative resolves, set_goal_status / set_strategy_status with a verdict.

Narrative: code does not write prose. `phases` returns fact sheets (game, each age, each war, each goal) with a
heuristic verdict; write the story yourself as annotate(kind=narrative, scope=..., scope_key=...) and judge it with
annotate(kind=outcome, verdict=went_well|went_awry|mixed, same scope), which overrides the heuristic verdict.

Civilopedia and rules: for "what does X do / how does Y work" questions, call `civilopedia(query)` (the game's
own in-game encyclopedia from the install: concept pages on towns, combat, ages, legacy paths, celebrations, trade,
plus per-unit/building/wonder/civ/leader pages) to ground the explanation and for flavour. For exact numbers call
`game_rules(name)`: it reads the data XML modifiers, which are authoritative; pedia text summarizes, may lag patches
and omits numbers. Prefer game_rules over memory, since patches change values.

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
    tech, civic, government, policy, production, project, purchase, legacy, unit_killed, ...)."""
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
    """Phase fact sheets (scope game / age / war / goal) with heuristic_verdict, advisor_verdict (latest outcome
    annotation on that scope), effective_verdict, the phase's narrative/outcome annotations, and needs_narrative."""
    with _db() as con:
        return advisor.phases(con, _game(con, game), scope, with_facts)


@server.tool()
def vocabulary(field: str | None = None) -> list[dict]:
    """Allowed values: personality, advisor_priority, playstyle, strategy_focus, comparator, annotation_kind, verdict."""
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
    """Current advisor and player profiles."""
    with _db() as con:
        g = _game(con, game)
        return {"advisor": advisor.current_advisor_profile(con, g), "player": advisor.current_player_profile(con, g)}


# ---------------------------------------------------------------- writing (advisor tables only)

@server.tool()
def set_advisor_profile(personality: str, voice: str = "", priorities: list[str] | None = None, quirks: str = "",
                        game: str | None = None) -> dict:
    """New version of the advisor's personality (jocular, terse_military, sarcastic, scholarly, diplomatic, zealous,
    cautious_steward, machiavellian). priorities (vocab advisor_priority) default to the personality's."""
    with _db() as con:
        return advisor.set_advisor_profile(con, _game(con, game), personality, voice, priorities, quirks)


@server.tool()
def set_player_profile(playstyle: list[str], risk_tolerance: str = "medium", notes: str = "", game: str | None = None) -> dict:
    """New version of the player's profile (playstyle: aggressive, cautious, efficient, whimsical, builder, expansionist,
    opportunist, diplomat, roleplayer). Changes are logged as priority_change annotations."""
    with _db() as con:
        return advisor.set_player_profile(con, _game(con, game), playstyle, risk_tolerance, notes)


@server.tool()
def add_strategy(name: str, victory_types: list[str], description: str = "", game: str | None = None) -> dict:
    """Adopt a strategy (victory_types: science, culture, economic, military, expansion, score). Returns suggested metrics."""
    with _db() as con:
        return advisor.add_strategy(con, _game(con, game), name, victory_types, description)


@server.tool()
def set_strategy_status(strategy_id: int, status: str, note: str = "", verdict: str | None = None, game: str | None = None) -> dict:
    """Mark a strategy active / achieved / failed / abandoned (logged as an outcome annotation with a verdict)."""
    with _db() as con:
        return advisor.set_strategy_status(con, _game(con, game), strategy_id, status, note, verdict)


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
def set_goal_status(goal_id: int, status: str, resolution: str = "", verdict: str | None = None, game: str | None = None) -> dict:
    """Resolve or reopen a goal: active / achieved / failed / abandoned, with how it went (verdict defaults from status)."""
    with _db() as con:
        return advisor.set_goal_status(con, _game(con, game), goal_id, status, resolution, verdict)


@server.tool()
def annotate(text: str, kind: str = "observation", author: str = "advisor", verdict: str | None = None,
             goal_id: int | None = None, strategy_id: int | None = None, event_key: str | None = None, g: int | None = None,
             scope: str | None = None, scope_key: str | None = None, game: str | None = None) -> int:
    """Record a note not from the game: intention, priority_change, narrative, outcome, observation, warning, advice.
    scope/scope_key attach it to a phase (game; age AGE_*; war = event_key; goal = goal id): narrative = the phase's
    story, outcome + verdict (went_well / went_awry / mixed) overrides its heuristic verdict. g defaults to now."""
    with _db() as con:
        return advisor.annotate(con, _game(con, game), text, kind, author, verdict, goal_id, strategy_id, event_key, g, scope,
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


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
