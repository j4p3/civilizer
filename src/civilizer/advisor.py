"""Advisor service over the central store: profiles, strategies, goals <-> metrics, annotations, briefings.

Everything here writes only advisor-owned tables (see store.TABLES). The CLI (`civilizer advisor ...`) and
the MCP server (mcp_server.py) are thin wrappers over these functions.

Every change is recorded: profiles are versioned rows, and strategy/goal changes also write an annotation
(priority_change / intention / outcome), so `history()` can replay how the plan evolved.
"""

from __future__ import annotations

import json
import sqlite3

from . import presets, store

STATUSES = ("active", "achieved", "failed", "abandoned")
# default outcome when a goal/strategy resolves without one: dropping a plan is a change of plans, not a failure
STATUS_OUTCOME = {"achieved": "success", "failed": "failure", "abandoned": "changed_plans"}
SCOPES = ("game", "age", "war", "goal")


class AdvisorError(ValueError):
    pass


# ---------------------------------------------------------------- helpers

def game_row(con: sqlite3.Connection, game: str | None) -> sqlite3.Row:
    if game:
        r = con.execute("SELECT * FROM games WHERE game=?", (str(game),)).fetchone()
    else:
        r = con.execute("SELECT * FROM games ORDER BY updated_at DESC LIMIT 1").fetchone()
    if not r:
        raise AdvisorError(f"no game {game!r} in the store" if game else
                           "no game in the store yet; run `civilizer refresh` or `civilizer monitor`")
    return r


def _at(con: sqlite3.Connection, game: str, g: int) -> tuple[str | None, int | None]:
    r = con.execute("SELECT age, g_offset FROM ages WHERE game=? AND g_offset < ? ORDER BY g_offset DESC LIMIT 1", (game, g)).fetchone()
    return (r["age"], g - r["g_offset"]) if r else (None, None)


def _next_id(con, table: str, col: str, game: str) -> int:
    return (con.execute(f"SELECT max({col}) FROM {table} WHERE game=?", (game,)).fetchone()[0] or 0) + 1


def _vocab(con, field: str) -> dict[str, sqlite3.Row]:
    return {r["value"]: r for r in con.execute("SELECT * FROM vocab WHERE field=?", (field,))}


def _check(con, field: str, values) -> None:
    allowed = _vocab(con, field)
    for v in [values] if isinstance(values, str) else values or []:
        if v not in allowed:
            raise AdvisorError(f"unknown {field} {v!r}; one of: {', '.join(allowed)}")


def _outcome(outcome: str | None) -> str | None:
    """Validate an outcome; the pre-v8 verdict names (went_well, went_awry, mixed) are accepted as aliases."""
    if not outcome:
        return None
    outcome = store.OUTCOME_ALIASES.get(outcome, outcome)
    if outcome not in store.VOCAB["outcome"]:  # the code's list: an older process may have reseeded the vocab table
        raise AdvisorError(f"unknown outcome {outcome!r}; one of: {', '.join(store.VOCAB['outcome'])} "
                           f"(or old verdicts {', '.join(store.OUTCOME_ALIASES)})")
    return outcome


def _goal(con, game: str, goal_id: int) -> sqlite3.Row:
    r = con.execute("SELECT * FROM goals WHERE game=? AND goal_id=?", (game, goal_id)).fetchone()
    if not r:
        raise AdvisorError(f"no goal {goal_id}")
    return r


def _strategy(con, game: str, strategy_id: int) -> sqlite3.Row:
    r = con.execute("SELECT * FROM strategies WHERE game=? AND strategy_id=?", (game, strategy_id)).fetchone()
    if not r:
        raise AdvisorError(f"no strategy {strategy_id}")
    return r


def value_at(con, game: str, metric: str, pid: int, g: int, lo: int | None = None) -> tuple[float | None, int | None]:
    """Latest value at or before g (and at or after lo)."""
    r = con.execute("SELECT value, g FROM metrics WHERE game=? AND metric=? AND player_id=? AND g<=? AND g>=? ORDER BY g DESC LIMIT 1",
                    (game, metric, pid, g, -1 if lo is None else lo)).fetchone()
    return (r["value"], r["g"]) if r else (None, None)


def _first_after(con, game: str, metric: str, pid: int, g: int) -> float | None:
    r = con.execute("SELECT value FROM metrics WHERE game=? AND metric=? AND player_id=? AND g>=? ORDER BY g LIMIT 1",
                    (game, metric, pid, g)).fetchone()
    return r[0] if r else None


def standing(con, game: str, metric: str, pid: int, g: int, window: int = 10, visible_only: bool = True) -> dict:
    """Rank of pid among majors on metric (1 = best; direction from metric_catalog.higher_is_better), using each
    player's latest value in [g-window, g]. visible_only skips omniscient values so ranks match the player's screen."""
    hib = con.execute("SELECT higher_is_better FROM metric_catalog WHERE metric=?", (metric,)).fetchone()
    lower_better = bool(hib) and hib[0] == 0
    vis = " AND m.visibility='visible'" if visible_only else ""
    rows = con.execute(
        "SELECT p.player_id, p.leader, (SELECT m.value FROM metrics m WHERE m.game=p.game AND m.metric=? AND m.player_id=p.player_id "
        f"AND m.g BETWEEN ? AND ?{vis} ORDER BY m.g DESC LIMIT 1) AS value FROM players p WHERE p.game=?",
        (metric, g - window, g, game)).fetchall()
    vals = sorted([r for r in rows if r["value"] is not None], key=lambda r: r["value"] if lower_better else -r["value"])
    ids = [r["player_id"] for r in vals]
    top = vals[0] if vals else None
    return {"rank": ids.index(pid) + 1 if pid in ids else None, "of": len(ids),
            "leader": {"player_id": top["player_id"], "leader": top["leader"], "value": top["value"]} if top else None}


def rank_at(con, game: str, metric: str, pid: int, g: int, window: int = 10) -> tuple[int | None, int]:
    s = standing(con, game, metric, pid, g, window)
    return s["rank"], s["of"]


# ---------------------------------------------------------------- annotations

def _check_scope(con, game: str, scope: str, scope_key: str | None) -> str:
    if scope not in SCOPES:
        raise AdvisorError(f"scope must be one of {', '.join(SCOPES)}")
    if scope == "game":
        return scope_key or game
    if not scope_key:
        raise AdvisorError(f"scope {scope} needs a scope_key (see the phases tool)")
    ok = {"age": "SELECT 1 FROM ages WHERE game=? AND age=?",
          "war": "SELECT 1 FROM events WHERE game=? AND event_key=? AND kind='war'",
          "goal": "SELECT 1 FROM goals WHERE game=? AND goal_id=CAST(? AS INTEGER)"}[scope]
    if not con.execute(ok, (game, scope_key)).fetchone() and not con.execute(
            "SELECT 1 FROM phases WHERE game=? AND scope=? AND scope_key=?", (game, scope, scope_key)).fetchone():
        raise AdvisorError(f"no {scope} {scope_key!r} in this game (see the phases tool for keys)")
    return str(scope_key)


def annotate(con, game: str, text: str, kind: str = "observation", author: str = "advisor", outcome: str | None = None,
             goal_id: int | None = None, strategy_id: int | None = None, event_key: str | None = None, g: int | None = None,
             scope: str | None = None, scope_key: str | None = None) -> int:
    """Record a non-game note. scope/scope_key tie it to a phase (game / AGE_* / war event_key / goal id): narrative
    and outcome notes there are the phase's story, and an outcome note's outcome overrides the phase's heuristic one."""
    _check(con, "annotation_kind", kind)
    outcome = _outcome(outcome)
    if author not in ("advisor", "player"):
        raise AdvisorError("author must be advisor or player")
    if not text or not text.strip():
        raise AdvisorError("annotation text is empty")
    if goal_id is not None:
        goal = _goal(con, game, goal_id)
        strategy_id = strategy_id if strategy_id is not None else goal["strategy_id"]
        if scope is None:
            scope, scope_key = "goal", str(goal_id)
    if strategy_id is not None:
        _strategy(con, game, strategy_id)
    if scope is not None:
        scope_key = _check_scope(con, game, scope, scope_key)
    elif scope_key is not None:
        raise AdvisorError("scope_key given without scope")
    g = g if g is not None else game_row(con, game)["current_g"]
    age, turn = _at(con, game, g)
    aid = _next_id(con, "annotations", "annotation_id", game)
    con.execute("INSERT INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (game, aid, g, age, turn, author, kind, text.strip(), outcome, goal_id, strategy_id, event_key, scope, scope_key,
                 store.now()))
    return aid


def annotations(con, game: str, kind: str | None = None, scope: str | None = None, scope_key: str | None = None,
                goal_id: int | None = None, since_g: int | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT * FROM annotations WHERE game=?", [game]
    for col, v in (("kind", kind), ("scope", scope), ("scope_key", scope_key), ("goal_id", goal_id)):
        if v is not None:
            q += f" AND {col}=?"
            args.append(v)
    if since_g is not None:
        q += " AND g>=?"
        args.append(since_g)
    rows = con.execute(q + " ORDER BY annotation_id DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows[::-1]]


# ---------------------------------------------------------------- profiles

def _advisor_preset(con, personality: str) -> dict:
    p = presets.advisor(personality)
    if not p:
        raise AdvisorError(f"unknown advisor preset {personality!r}; one of: {', '.join(presets.advisors())} "
                           "(or create_advisor_preset for a custom one)")
    return p


ROLES = {"advisor": ("advisor_profiles", ("personality", "voice", "priorities", "quirks")),
         "player": ("player_profiles", ("motives", "playstyle", "risk_tolerance", "notes"))}
_JSON_COLS = ("priorities", "playstyle", "motives")


def _role(role: str) -> tuple[str, tuple[str, ...]]:
    if role not in ROLES:
        raise AdvisorError("role must be advisor or player")
    return ROLES[role]


def _decode(r) -> dict:
    d = dict(r)
    for k in _JSON_COLS:
        if k in d:
            d[k] = json.loads(d[k] or "[]")
    return d


def get_profile(con, role: str, profile_id: int, version: int | None = None) -> dict:
    table, _ = _role(role)
    q = f"SELECT * FROM {table} WHERE profile_id=?" + (" AND version=?" if version else "") + " ORDER BY version DESC LIMIT 1"
    r = con.execute(q, (profile_id, version) if version else (profile_id,)).fetchone()
    if not r:
        raise AdvisorError(f"no {role} profile {profile_id}" + (f" v{version}" if version else ""))
    return _decode(r)


def _save_profile(con, role: str, fields: dict, name: str | None, profile_id: int | None) -> dict:
    """New version of profile_id, or (without one) an existing profile whose latest version has the same fields,
    or a new profile. Returns the stored row."""
    table, cols = _role(role)
    enc = {k: json.dumps(fields[k]) if k in _JSON_COLS else fields[k] for k in cols}
    if profile_id is not None:
        prev = get_profile(con, role, profile_id)
        if all(prev[k] == fields[k] for k in cols) and (name is None or name == prev["name"]):
            return prev
        version, name = prev["version"] + 1, name or prev["name"]
    else:
        latest = con.execute(f"SELECT * FROM {table} p WHERE version=(SELECT max(version) FROM {table} WHERE profile_id=p.profile_id) "
                             + "".join(f" AND {k} IS ?" for k in cols) + " ORDER BY profile_id DESC LIMIT 1",
                             [enc[k] for k in cols]).fetchone()
        if latest and (name is None or name == latest["name"]):
            return _decode(latest)
        profile_id = (con.execute(f"SELECT max(profile_id) FROM {table}").fetchone()[0] or 0) + 1
        version = 1
    con.execute(f"INSERT INTO {table} (profile_id, version, name, {', '.join(cols)}, created_at) VALUES (?,?,?,{','.join('?' * len(cols))},?)",
                (profile_id, version, name, *[enc[k] for k in cols], store.now()))
    return get_profile(con, role, profile_id, version)


def _link(con, game: str, role: str, profile: dict) -> None:
    cur = con.execute("SELECT profile_id, version, seq FROM game_profiles WHERE game=? AND role=? ORDER BY seq DESC LIMIT 1",
                      (game, role)).fetchone()
    if cur and (cur["profile_id"], cur["version"]) == (profile["profile_id"], profile["version"]):
        return
    con.execute("INSERT INTO game_profiles VALUES (?,?,?,?,?,?,?)", (game, role, (cur["seq"] if cur else 0) + 1, profile["profile_id"],
                                                                    profile["version"], game_row(con, game)["current_g"], store.now()))


def _advisor_preset(con, personality: str) -> dict:
    p = presets.advisor(personality)
    if not p:
        raise AdvisorError(f"unknown advisor preset {personality!r}; one of: {', '.join(presets.advisors())} "
                           "(or create_advisor_preset for a custom one)")
    return p


def set_advisor_profile(con, game: str, personality: str, voice: str = "", priorities: list[str] | None = None,
                        quirks: str = "", name: str | None = None, profile_id: int | None = None) -> dict:
    """Use an advisor for this game. personality is an advisor preset key (src/civilizer/advisors/ or data/advisors/).
    With profile_id: a new version of that saved profile (other games keep the version they linked). Without: reuses a
    saved profile with identical settings, else creates one. Either way the game is linked to it."""
    preset = _advisor_preset(con, personality)
    priorities = priorities or preset["priorities"]
    _check(con, "advisor_priority", priorities)
    prof = _save_profile(con, "advisor", {"personality": personality, "voice": voice, "priorities": priorities, "quirks": quirks},
                         name if name or profile_id is not None else preset["label"], profile_id)
    _link(con, game, "advisor", prof)
    return current_advisor_profile(con, game)


def create_advisor_preset(con, key: str, label: str, description: str, prompt: str, priorities: list[str],
                          overwrite: bool = False) -> dict:
    """Write a custom advisor style prompt to data/advisors/<key>.md (shared across games)."""
    if not priorities:
        raise AdvisorError("priorities needs at least one value (vocab advisor_priority)")
    _check(con, "advisor_priority", priorities)
    try:
        return presets.create_advisor(key, label, description, prompt, priorities, overwrite)
    except presets.PresetError as e:
        raise AdvisorError(str(e)) from e


def _motive_phrase(p: dict) -> str:
    return ", ".join(p.get("motives") or []) or "-"


def _player_phrase(p: dict) -> str:
    return f"motives {_motive_phrase(p)}; playstyle {', '.join(p['playstyle']) or '-'}; risk {p['risk_tolerance']}"


def set_player_profile(con, game: str, playstyle: list[str] | None = None, risk_tolerance: str = "medium", notes: str = "",
                       motives: list[str] | None = None, name: str | None = None, profile_id: int | None = None) -> dict:
    """Use a player profile for this game; profile_id / reuse semantics as set_advisor_profile."""
    playstyle, motives = playstyle or [], motives or []
    if not playstyle and not motives:
        raise AdvisorError("give at least one motive (fight, build, explore, mess_around, story) or playstyle")
    _check(con, "playstyle", playstyle)
    _check(con, "motive", motives)
    if risk_tolerance not in ("low", "medium", "high"):
        raise AdvisorError("risk_tolerance must be low, medium or high")
    prev = current_player_profile(con, game)
    prof = _save_profile(con, "player", {"motives": motives, "playstyle": playstyle, "risk_tolerance": risk_tolerance,
                                         "notes": notes}, name if name or profile_id is not None else "Player", profile_id)
    _link(con, game, "player", prof)
    cur = current_player_profile(con, game)
    if prev and (prev["profile_id"], prev["version"]) != (cur["profile_id"], cur["version"]):
        annotate(con, game, f"Playstyle changed: {_player_phrase(prev)} -> {_player_phrase(cur)}." + (f" {notes}" if notes else ""),
                 "priority_change", "player")
    return cur


def use_profile(con, game: str, role: str, profile_id: int, version: int | None = None) -> dict:
    """Link a saved profile (latest version unless given) to this game."""
    prev = current_player_profile(con, game) if role == "player" else None
    _link(con, game, role, get_profile(con, role, profile_id, version))
    if role == "advisor":
        return current_advisor_profile(con, game)
    cur = current_player_profile(con, game)
    if prev and (prev["profile_id"], prev["version"]) != (cur["profile_id"], cur["version"]):
        annotate(con, game, f"Playstyle changed: {_player_phrase(prev)} -> {_player_phrase(cur)}.", "priority_change", "player")
    return cur


def list_profiles(con, role: str | None = None) -> list[dict]:
    """Saved profiles (latest version each), most recently used first, with the games that used them."""
    out = []
    for r in [role] if role else list(ROLES):
        table, _ = _role(r)
        for p in con.execute(f"SELECT * FROM {table} p WHERE version=(SELECT max(version) FROM {table} WHERE profile_id=p.profile_id)"):
            d = _decode(p) | {"role": r}
            uses = con.execute("SELECT gp.game, max(gp.created_at) AS at, g.leader FROM game_profiles gp LEFT JOIN games g USING (game) "
                               "WHERE gp.role=? AND gp.profile_id=? GROUP BY gp.game ORDER BY at DESC", (r, d["profile_id"])).fetchall()
            d["games"] = [{"game": u["game"], "leader": u["leader"]} for u in uses]
            d["last_used"] = uses[0]["at"] if uses else None
            out.append(d)
    return sorted(out, key=lambda d: d["last_used"] or d["created_at"], reverse=True)


def profile_history(con, game: str, role: str) -> list[dict]:
    """Profile versions this game used, newest first (profile fields + seq, valid_from_g, linked_at)."""
    table, _ = _role(role)
    rows = con.execute(f"SELECT p.*, gp.seq, gp.valid_from_g, gp.created_at AS linked_at FROM game_profiles gp JOIN {table} p "
                       "ON p.profile_id=gp.profile_id AND p.version=gp.version WHERE gp.game=? AND gp.role=? ORDER BY gp.seq DESC",
                       (game, role))
    return [_decode(r) for r in rows]


def current_advisor_profile(con, game: str) -> dict | None:
    """The game's advisor profile plus its preset's style prompt (style_prompt) and file (style_file)."""
    d = next(iter(profile_history(con, game, "advisor")), None)
    if d:
        p = presets.advisor(d["personality"])
        d["personality_description"] = p["description"] if p else None
        d["style_prompt"] = p["prompt"] if p else None
        d["style_file"] = p["file"] if p else None
    return d


def current_player_profile(con, game: str) -> dict | None:
    d = next(iter(profile_history(con, game, "player")), None)
    if d:
        ps = _vocab(con, "playstyle")
        d["playstyle_descriptions"] = {s: ps[s]["description"] for s in d["playstyle"] if s in ps}
    return d


# ---------------------------------------------------------------- strategies and goals

def add_strategy(con, game: str, name: str, victory_types: list[str], description: str = "") -> dict:
    if not victory_types:
        raise AdvisorError("victory_types needs at least one value (science, culture, economic, military, expansion, score)")
    _check(con, "strategy_focus", victory_types)
    g = game_row(con, game)["current_g"]
    sid = _next_id(con, "strategies", "strategy_id", game)
    con.execute("INSERT INTO strategies VALUES (?,?,?,?,?,?,?,?,?)",
                (game, sid, name, json.dumps(victory_types), description, "active", g, None, store.now()))
    annotate(con, game, f"New strategy {sid}: {name} ({', '.join(victory_types)}). {description}".strip(), "priority_change",
             "player", strategy_id=sid)
    return {"strategy_id": sid, "suggested_metrics": suggested_metrics(con, victory_types)}


def _change(old: str, new: str) -> str:
    return f"{old} -> {new}" if old != new else f"{new} (outcome re-judged)"


def set_strategy_status(con, game: str, strategy_id: int, status: str, note: str = "", outcome: str | None = None) -> dict:
    """Setting the current status again only records a new outcome; the original end turn is kept."""
    if status not in STATUSES:
        raise AdvisorError(f"status must be one of {', '.join(STATUSES)}")
    outcome = _outcome(outcome) or STATUS_OUTCOME.get(status)
    s = _strategy(con, game, strategy_id)
    g = game_row(con, game)["current_g"]
    if status != s["status"]:
        con.execute("UPDATE strategies SET status=?, ended_g=? WHERE game=? AND strategy_id=?",
                    (status, None if status == "active" else g, game, strategy_id))
    annotate(con, game, f"Strategy {strategy_id} ({s['name']}) {_change(s['status'], status)}. {note}".strip(),
             "outcome" if status != "active" else "priority_change", "player", outcome=outcome, strategy_id=strategy_id)
    return {"strategy_id": strategy_id, "status": status, "outcome": outcome}


def strategies(con, game: str, active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM strategies WHERE game=?" + (" AND status='active'" if active_only else "") + " ORDER BY strategy_id"
    return [dict(r) | {"victory_types": json.loads(r["victory_types"])} for r in con.execute(q, (game,))]


def suggested_metrics(con, focuses: list[str]) -> list[str]:
    vocab = _vocab(con, "strategy_focus")
    out = []
    for f in focuses:
        out += [m for m in json.loads(vocab[f]["extra"] or "[]") if m not in out]
    return out


def add_goal(con, game: str, title: str, horizon: str = "short", strategy_id: int | None = None, parent_goal_id: int | None = None,
             priority: int = 2, deadline_g: int | None = None, description: str = "", metrics: list[dict] | None = None) -> dict:
    if horizon not in ("short", "long"):
        raise AdvisorError("horizon must be short or long")
    if parent_goal_id is not None:
        p = _goal(con, game, parent_goal_id)
        strategy_id = strategy_id if strategy_id is not None else p["strategy_id"]
    if strategy_id is not None:
        _strategy(con, game, strategy_id)
    g = game_row(con, game)["current_g"]
    gid = _next_id(con, "goals", "goal_id", game)
    con.execute("INSERT INTO goals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (game, gid, strategy_id, parent_goal_id, horizon, title, description, priority, "active", g, deadline_g, None, None))
    linked = [link_metric(con, game, gid, record=False, **m) for m in metrics or []]
    annotate(con, game, f"New {horizon}-term goal {gid}: {title}" + (f" (by g{deadline_g})" if deadline_g else "")
             + (f"; measured by {', '.join(_metric_phrase(m) for m in linked)}" if linked else ""), "intention", "player",
             goal_id=gid, strategy_id=strategy_id)
    sugg = []
    if strategy_id is not None and not metrics:
        sugg = suggested_metrics(con, json.loads(_strategy(con, game, strategy_id)["victory_types"]))
    return {"goal_id": gid, "metrics": linked, "suggested_metrics": sugg}


def _metric_phrase(m: dict) -> str:
    who = "" if m.get("local", True) else f" (player {m['player_id']})"
    return f"{m['metric']}{who} {m['comparator']} {m['target']:g}"


def link_metric(con, game: str, goal_id: int, metric: str, comparator: str = ">=", target: float = 0,
                player_id: int | None = None, weight: float = 1.0, record: bool = True) -> dict:
    """Measure a goal by a metric; baseline = the value (or rank) now. Re-linking the same metric replaces the target."""
    _goal(con, game, goal_id)
    if not con.execute("SELECT 1 FROM metric_catalog WHERE metric=?", (metric,)).fetchone():
        raise AdvisorError(f"unknown metric {metric!r}; see metric_catalog / the schema tool")
    _check(con, "comparator", comparator)
    gr = game_row(con, game)
    pid = gr["local_player_id"] if player_id is None else player_id
    if not con.execute("SELECT 1 FROM players WHERE game=? AND player_id=?", (game, pid)).fetchone():
        raise AdvisorError(f"no major player {pid}")
    cg = gr["current_g"]
    base = rank_at(con, game, metric, pid, cg)[0] if comparator == "rank<=" else value_at(con, game, metric, pid, cg)[0]
    old = con.execute("SELECT * FROM goal_metrics WHERE game=? AND goal_id=? AND metric=? AND player_id=?",
                      (game, goal_id, metric, pid)).fetchone()
    con.execute("INSERT OR REPLACE INTO goal_metrics VALUES (?,?,?,?,?,?,?,?,?)",
                (game, goal_id, metric, pid, comparator, float(target), base, cg, weight))
    out = {"goal_id": goal_id, "metric": metric, "player_id": pid, "local": pid == gr["local_player_id"],
           "comparator": comparator, "target": float(target), "baseline": base, "baseline_g": cg}
    if record:
        was = f" (was {old['comparator']} {old['target']:g})" if old else ""
        annotate(con, game, f"Goal {goal_id} measured by {_metric_phrase(out)}{was}; baseline {base}.", "priority_change",
                 "player", goal_id=goal_id)
    return out


def unlink_metric(con, game: str, goal_id: int, metric: str, player_id: int | None = None) -> None:
    pid = game_row(con, game)["local_player_id"] if player_id is None else player_id
    n = con.execute("DELETE FROM goal_metrics WHERE game=? AND goal_id=? AND metric=? AND player_id=?",
                    (game, goal_id, metric, pid)).rowcount
    if not n:
        raise AdvisorError(f"goal {goal_id} has no metric {metric} for player {pid}")
    annotate(con, game, f"Goal {goal_id} no longer measured by {metric}.", "priority_change", "player", goal_id=goal_id)


def update_goal(con, game: str, goal_id: int, title: str | None = None, description: str | None = None,
                priority: int | None = None, deadline_g: int | None = None, parent_goal_id: int | None = None,
                strategy_id: int | None = None, note: str = "") -> dict:
    """Edit a goal; the diff is recorded as a priority_change annotation (use deadline_g=-1 to clear the deadline)."""
    old = _goal(con, game, goal_id)
    new = {"title": title, "description": description, "priority": priority,
           "deadline_g": None if deadline_g == -1 else deadline_g, "parent_goal_id": parent_goal_id, "strategy_id": strategy_id}
    changes = {k: v for k, v in new.items() if (v is not None or (k == "deadline_g" and deadline_g == -1)) and v != old[k]}
    if parent_goal_id is not None:
        if parent_goal_id == goal_id:
            raise AdvisorError("a goal cannot be its own parent")
        _goal(con, game, parent_goal_id)
    if strategy_id is not None:
        _strategy(con, game, strategy_id)
    if not changes:
        raise AdvisorError("nothing to change")
    con.execute(f"UPDATE goals SET {', '.join(f'{k}=?' for k in changes)} WHERE game=? AND goal_id=?",
                (*changes.values(), game, goal_id))
    diff = "; ".join(f"{k} {old[k]!r} -> {v!r}" for k, v in changes.items())
    annotate(con, game, f"Goal {goal_id} changed: {diff}." + (f" {note}" if note else ""), "priority_change", "player", goal_id=goal_id)
    return dict(_goal(con, game, goal_id))


def set_goal_status(con, game: str, goal_id: int, status: str, resolution: str = "", outcome: str | None = None) -> dict:
    """Resolve or reopen a goal. Setting the current status again only records a new outcome (and the resolution,
    if given); resolved_g is kept."""
    if status not in STATUSES:
        raise AdvisorError(f"status must be one of {', '.join(STATUSES)}")
    outcome = _outcome(outcome) or STATUS_OUTCOME.get(status)
    goal = _goal(con, game, goal_id)
    g = game_row(con, game)["current_g"]
    if status != goal["status"]:
        con.execute("UPDATE goals SET status=?, resolved_g=?, resolution=? WHERE game=? AND goal_id=?",
                    (status, None if status == "active" else g, resolution or None, game, goal_id))
    elif resolution:
        con.execute("UPDATE goals SET resolution=? WHERE game=? AND goal_id=?", (resolution, game, goal_id))
    annotate(con, game, f"Goal {goal_id} ({goal['title']}) {_change(goal['status'], status)}. {resolution}".strip(),
             "outcome" if status != "active" else "priority_change", "player", outcome=outcome, goal_id=goal_id)
    return {"goal_id": goal_id, "status": status, "outcome": outcome}


def _progress(comparator: str, cur, base, target, rank) -> tuple[float | None, bool | None]:
    """(progress 0..1, met) for one goal metric.

    >= / <=      distance covered from the baseline toward the target (1 when met).
    gain>=       gain since baseline / target.
    gain<=       a limit ("lose at most N"): 1 while within it, 0 once exceeded.
    rank<=       1 when at or above the target rank, else target/rank.
    """
    if comparator == "rank<=":
        if rank is None:
            return None, None
        return (1.0 if rank <= target else target / rank), rank <= target
    if cur is None:
        return None, None
    clip = lambda p: max(0.0, min(1.0, p))
    if comparator in (">=", "<="):
        sign = 1 if comparator == ">=" else -1
        if sign * (cur - target) >= 0:
            return 1.0, True
        if base is not None and sign * (target - base) > 0:
            return clip((cur - base) / (target - base)), False
        # no usable baseline (missing, or already past the target when linked): fall back to a ratio
        if comparator == ">=":
            return (clip(cur / target) if target > 0 and cur > 0 else 0.0), False
        return (clip(target / cur) if target > 0 and cur > 0 else 0.0), False
    gain = cur - (base or 0)
    if comparator == "gain>=":
        met = gain >= target
        return (1.0 if met else clip(gain / target) if target > 0 else 0.0), met
    if comparator == "gain<=":
        met = gain <= target
        return (1.0 if met else 0.0), met
    return None, None


def evaluate_goals(con, game: str, include_closed: bool = True) -> list[dict]:
    """Goals with per-metric progress. progress = weighted mean of the goal's own metrics and its child goals
    (each child weighs 1: achieved = 1, failed = 0, abandoned and unmeasured children are left out)."""
    gr = game_row(con, game)
    cg, me = gr["current_g"], gr["local_player_id"]
    cat = {r["metric"]: r for r in con.execute("SELECT * FROM metric_catalog")}
    goals = {}
    for goal in con.execute("SELECT * FROM goals WHERE game=? ORDER BY priority, goal_id", (game,)).fetchall():
        d = dict(goal)
        d["current_g"] = cg
        at = d["resolved_g"] or cg
        ms, num, den = [], 0.0, 0.0
        for m in con.execute("SELECT * FROM goal_metrics WHERE game=? AND goal_id=?", (game, goal["goal_id"])):
            cur, cur_g = value_at(con, game, m["metric"], m["player_id"], at)
            base = m["baseline"]
            if base is None and m["comparator"] in ("gain>=", "gain<="):
                base = _first_after(con, game, m["metric"], m["player_id"], m["baseline_g"] or 0)
            st = standing(con, game, m["metric"], m["player_id"], at) if m["comparator"] == "rank<=" else None
            rank = st["rank"] if st else None
            p, met = _progress(m["comparator"], cur, base, m["target"], rank)
            ms.append({"metric": m["metric"], "label": cat[m["metric"]]["label"] if m["metric"] in cat else m["metric"],
                       "player_id": m["player_id"], "local": m["player_id"] == me, "comparator": m["comparator"],
                       "target": m["target"], "baseline": base, "baseline_g": m["baseline_g"],
                       "current": rank if st else cur, "value": cur, "as_of_g": cur_g,
                       **({"rank": rank, "of": st["of"]} if st else {}),
                       **({"gain": cur - (base or 0)} if cur is not None and m["comparator"].startswith("gain") else {}),
                       "progress": p, "met": met, "weight": m["weight"]})
            num += (p or 0) * m["weight"]  # an unmeasured metric counts as 0, so a goal can't read 100% (or alert "met") without it
            den += m["weight"]
        d["metrics"] = ms
        d["metric_progress"] = num / den if den else None
        d["_num"], d["_den"] = num, den
        goals[d["goal_id"]] = d
    # roll children up into parents, deepest first (a goal's children all have larger depth)
    depth = {}
    for gid in goals:
        n, p = 0, goals[gid]["parent_goal_id"]
        while p in goals and n < 20:
            n, p = n + 1, goals[p]["parent_goal_id"]
        depth[gid] = n
    for gid in sorted(goals, key=lambda k: -depth[k]):
        d = goals[gid]
        kids = [goals[k] for k in goals if goals[k]["parent_goal_id"] == gid]
        kp = [{"achieved": 1.0, "failed": 0.0}.get(k["status"], k["progress"]) for k in kids if k["status"] != "abandoned"]
        kp = [p for p in kp if p is not None]
        d["children_progress"] = sum(kp) / len(kp) if kp else None
        num, den = d.pop("_num") + sum(kp), d.pop("_den") + len(kp)
        d["progress"] = num / den if den else None
        d["overdue"] = bool(d["deadline_g"] and cg > d["deadline_g"] and d["status"] == "active" and (d["progress"] or 0) < 1)
        d["turns_left"] = d["deadline_g"] - cg if d["deadline_g"] and d["status"] == "active" else None
    return [d for d in goals.values() if include_closed or d["status"] == "active"]


def goal_tree(con, game: str, active_only: bool = False) -> list[dict]:
    """Strategies -> long-term goals -> short-term goals (any depth), with progress."""
    goals = evaluate_goals(con, game, include_closed=not active_only)
    ids = {g["goal_id"] for g in goals}
    by_parent: dict = {}
    for g in goals:
        by_parent.setdefault(g["parent_goal_id"] if g["parent_goal_id"] in ids else None, []).append(g)
    for g in goals:
        g["children"] = by_parent.get(g["goal_id"], [])
    roots = by_parent.get(None, [])
    out = [s | {"goals": [g for g in roots if g["strategy_id"] == s["strategy_id"]]} for s in strategies(con, game, active_only)]
    known = {s["strategy_id"] for s in out}
    loose = [g for g in roots if g["strategy_id"] not in known]
    if loose:
        out.append({"strategy_id": None, "name": "Unattached goals", "victory_types": [], "status": "active", "goals": loose})
    return out


def _compact_goal(g: dict) -> dict:
    """Goal digest for the per-turn briefing: progress and one line per metric."""
    r = lambda x: None if x is None else round(x, 2)
    d = {"goal_id": g["goal_id"], "title": g["title"], "horizon": g["horizon"], "priority": g["priority"],
         "progress": r(g["progress"])}
    for k in ("deadline_g", "turns_left"):
        if g.get(k) is not None:
            d[k] = g[k]
    if g["overdue"]:
        d["overdue"] = True
    if g["metrics"]:
        d["metrics"] = [f"{m['metric']}{'' if m['local'] else '@p' + str(m['player_id'])} {m['comparator']} {m['target']:g}: "
                        + ("no data" if m["current"] is None else
                           f"{'rank ' if m['comparator'] == 'rank<=' else ''}{m['current']:g}"
                           + (f" (gain {m['gain']:+g})" if "gain" in m else "")
                           + f" [{'met' if m['met'] else f'{(m['progress'] or 0) * 100:.0f}%'}]")
                        for m in g["metrics"]]
    if g.get("children"):
        d["children"] = [_compact_goal(c) for c in g["children"]]
    return d


# ---------------------------------------------------------------- setup wizard

WIZARD_STEPS = ("advisor", "player", "strategy")
PRESENT = ("Show the question and the options as a numbered list (n. label: description), exactly as given, and say the "
           "player can answer with {how} or in their own words. Do not call the next tool until they answer.")


def _saved_options(con, role: str, describe) -> list[dict]:
    """Saved profiles of a role as wizard options (key 'profile:<id>'), most recently used first."""
    out = []
    for p in list_profiles(con, role)[:5]:
        used = ", ".join(sorted({u["leader"] or u["game"] for u in p["games"]})) or "not used yet"
        out.append({"key": f"profile:{p['profile_id']}", "label": f"Saved: {p['name']}", "description": f"{describe(p)} (used: {used})",
                    "profile_id": p["profile_id"]})
    return out


def _numbered(options: list[dict]) -> list[dict]:
    return [{"n": i} | o for i, o in enumerate(options, 1)]


def setup_wizard(con, game: str, step: str | None = None) -> dict:
    """Next setup question for Claude to put to the player: advisor preset, then player motives (plus Claude's own
    follow-ups), then strategy presets. step forces one step (to change a choice later); otherwise the first missing."""
    gr = game_row(con, game)
    have = {"advisor": current_advisor_profile(con, game), "player": current_player_profile(con, game),
            "strategy": strategies(con, game, active_only=True)}
    done = [k for k in WIZARD_STEPS if have[k]]
    if step is not None and step not in WIZARD_STEPS:
        raise AdvisorError(f"step must be one of {', '.join(WIZARD_STEPS)}")
    step = step or next((k for k in WIZARD_STEPS if not have[k]), None)
    head = {"game": {k: gr[k] for k in ("leader", "civilization", "current_age", "current_turn", "current_g")}, "done": done}
    if step is None:
        return head | {"step": "done", "next": "Setup is complete: call briefing and advise in the advisor's style_prompt."}

    if step == "advisor":
        opts = _saved_options(con, "advisor", lambda p: f"{p['personality']}" + (f", {p['voice']}" if p["voice"] else ""))
        opts += [{"key": k, "label": a["label"], "description": a["description"]} | ({"custom": True} if a["source"] == "custom" else {})
                 for k, a in presets.advisors().items()]
        opts.append({"key": "custom", "label": "Create your own",
                     "description": "Describe the advisor you want (a character, a tone, a famous figure's style) and I'll write it."})
        return head | {
            "step": "advisor", "multi_select": False, "options": _numbered(opts),
            "question": "Who should advise you this game?",
            "present": PRESENT.format(how="a number"),
            "on_answer": (
                "Saved profile: use_profile(role=advisor, profile_id). "
                "Preset: set_advisor_profile(personality=<key>) (saves a reusable profile). From then on, follow briefing.advisor_profile.style_prompt "
                "(also in style_file) whenever you speak to the player. "
                "Create your own (or a free-text description): ask one question if the description is too thin to write a "
                "voice from, then draft a style prompt in the same shape as the built-ins (role line, then 4-6 bullets on "
                "tone, structure, what to lead with, and the rule that the persona never bends the facts), show it to the "
                "player, and on approval call create_advisor_preset(key, label, description, prompt, priorities) and then "
                "set_advisor_profile(personality=<key>). The file is reused in later games."),
            "advisor_priorities": {k: r["description"] for k, r in _vocab(con, "advisor_priority").items()},
        }

    if step == "player":
        opts = _saved_options(con, "player", lambda p: _player_phrase(p) + (f"; {p['notes']}" if p["notes"] else ""))
        opts += [{"key": k, "label": label, "description": d, "followup_angles": f} for k, (label, d, f) in presets.MOTIVES.items()]
        return head | {
            "step": "player", "multi_select": True, "options": _numbered(opts),
            "question": "What are you here for this game? Pick a saved profile, or any motives that apply.",
            "present": PRESENT.format(how="numbers (e.g. 2, 5)") + " Leave out followup_angles when presenting.",
            "on_answer": (
                "Saved profile: use_profile(role=player, profile_id), then ask if anything changed this game; if so, "
                "set_player_profile(profile_id=<id>, ...) makes a new version of it. "
                "Otherwise ask 1-2 follow-up questions you write yourself, one at a time, built from the chosen motives' "
                "followup_angles, the leader/civ in play and anything the player already said (numbered options when "
                "natural, always allowing free text). Stop when you know what they enjoy, what they want to avoid and "
                "how much risk they accept. Then summarize the profile in one or two lines, and call "
                "set_player_profile(motives=[keys], playstyle=[inferred vocab playstyle], risk_tolerance=low|medium|high, "
                "notes=<their answers, in their words>, name=<short name, e.g. the player's name>). It is saved for "
                "future games."),
            "playstyles": {k: r["description"] for k, r in _vocab(con, "playstyle").items()},
        }

    pp = have["player"]
    opts = [{"key": k, "label": label, "description": d} for k, (label, d, _, _) in presets.STRATEGIES.items()]
    return head | {
        "step": "strategy", "multi_select": True, "options": _numbered(opts),
        "question": "What are you going for? Pick one or more.",
        "player_profile": pp and {k: pp[k] for k in ("motives", "playstyle", "risk_tolerance")},
        "present": PRESENT.format(how="numbers (e.g. 1, 3)") + " You may mark one option (Recommended) based on the "
                   "player profile and the leader/civ, with a one-line reason.",
        "on_answer": "choose_strategy(presets=[keys], notes=<anything the player added>). It returns guidance and "
                     "suggested metrics: propose one long-term goal and one or two short-term goals with metrics and "
                     "deadlines, confirm with the player, then add_goal.",
    }


def choose_strategy(con, game: str, preset_keys: list[str], victory_types: list[str] | None = None, name: str | None = None,
                    notes: str = "") -> dict:
    """Adopt a strategy from wizard presets. 'just_win' without other presets needs victory_types chosen by the advisor:
    without them nothing is written and the reply says what to decide."""
    unknown = [k for k in preset_keys if k not in presets.STRATEGIES]
    if not preset_keys or unknown:
        raise AdvisorError(f"unknown strategy preset(s) {unknown}; one of: {', '.join(presets.STRATEGIES)}")
    chosen = [presets.STRATEGIES[k] for k in preset_keys]
    vt = list(victory_types or [])
    for _, _, types, _ in chosen:
        vt += [t for t in types if t not in vt]
    guidance = {k: presets.STRATEGIES[k][3] for k in preset_keys}
    if not vt:  # only just_win, and the advisor has not picked a path yet
        gr = game_row(con, game)
        return {"needs_victory_types": guidance["just_win"], "leader": gr["leader"], "civilization": gr["civilization"],
                "victory_types": list(_vocab(con, "strategy_focus"))}
    name = name or " + ".join(label for label, _, _, _ in chosen)
    desc = " ".join(d for _, d, _, _ in chosen) + (f" Player: {notes}" if notes else "")
    if "just_win" in preset_keys:
        desc += f" Advisor picked {', '.join(vt)}."
    return add_strategy(con, game, name, vt, desc) | {"victory_types": vt, "guidance": guidance,
                                                       "next": "Propose starter goals with metrics, confirm, then add_goal."}


# ---------------------------------------------------------------- history and phases

def history(con, game: str, what: str | None = None) -> list[dict]:
    """Chronological change log of the advisor layer: profile versions, strategy/goal creation, and every
    intention / priority_change / outcome annotation. what: profile, strategy, goal, or None for all."""
    out = []
    if what in (None, "profile"):
        for p in profile_history(con, game, "advisor")[::-1]:
            out.append({"g": p["valid_from_g"], "at": p["linked_at"], "what": "advisor_profile", "version": p["version"],
                        "text": f"Advisor {p['name']} (profile {p['profile_id']} v{p['version']}): {p['personality']} "
                                f"(priorities {', '.join(p['priorities'])})"
                                + (f"; voice: {p['voice']}" if p["voice"] else "") + (f"; quirks: {p['quirks']}" if p["quirks"] else "")})
        for p in profile_history(con, game, "player")[::-1]:
            out.append({"g": p["valid_from_g"], "at": p["linked_at"], "what": "player_profile", "version": p["version"],
                        "text": f"Player {p['name']} (profile {p['profile_id']} v{p['version']}): {_player_phrase(p)}"
                                + (f"; {p['notes']}" if p["notes"] else "")})
    q = "SELECT * FROM annotations WHERE game=? AND kind IN ('intention','priority_change','outcome')"
    if what == "strategy":
        q += " AND strategy_id IS NOT NULL AND goal_id IS NULL"
    elif what == "goal":
        q += " AND goal_id IS NOT NULL"
    elif what == "profile":
        q += " AND 0"
    for a in con.execute(q, (game,)):
        out.append({"g": a["g"], "at": a["created_at"], "what": a["kind"], "annotation_id": a["annotation_id"], "author": a["author"],
                    "goal_id": a["goal_id"], "strategy_id": a["strategy_id"], "outcome": a["outcome"], "text": a["text"]})
    out.sort(key=lambda e: (e["at"] or "", e["g"] or 0))
    return out


def phases(con, game: str, scope: str | None = None, with_facts: bool = True) -> list[dict]:
    """Phase fact sheets plus the advisor layer on top: narrative/outcome annotations on the same scope, and
    advisor_outcome (latest outcome annotation's outcome), which overrides heuristic_outcome in `effective_outcome`."""
    q, args = "SELECT * FROM phases WHERE game=?", [game]
    if scope:
        q += " AND scope=?"
        args.append(scope)
    notes: dict = {}
    for a in con.execute("SELECT * FROM annotations WHERE game=? AND scope IS NOT NULL AND kind IN ('narrative','outcome') "
                         "ORDER BY annotation_id", (game,)):
        notes.setdefault((a["scope"], a["scope_key"]), []).append(a)
    out = []
    for p in con.execute(q + " ORDER BY g_start, scope", args):
        d = dict(p)
        d["facts"] = json.loads(d["facts"] or "{}") if with_facts else None
        ns = notes.get((p["scope"], p["scope_key"]), [])
        judged = [a for a in ns if a["kind"] == "outcome" and a["outcome"]]
        d["heuristic_outcome"] = store.outcome_of(d.pop("outcome"))
        d["advisor_outcome"] = judged[-1]["outcome"] if judged else None
        d["effective_outcome"] = d["advisor_outcome"] or d["heuristic_outcome"]
        d["annotations"] = [{k: a[k] for k in ("annotation_id", "g", "kind", "author", "outcome", "text")} for a in ns]
        d["needs_narrative"] = not any(a["kind"] == "narrative" for a in ns)
        out.append(d)
    return out


# ---------------------------------------------------------------- briefing

KEY_METRICS = ["gold_per_turn", "science_per_turn", "culture_per_turn", "production_per_turn", "food_per_turn",
               "happiness_per_turn", "influence_per_turn", "gold_balance", "influence_balance", "settlements", "settlement_cap",
               "population", "military_units", "wars_active", "unrest_settlements", "trade_routes", "triumphs_done",
               "units_lost_total", "units_killed_total", "trade_income_per_turn", "combat_gold_total", "suzerainties",
               "befriend_actions_active", "influence_spent_total"]
STANDINGS = ["vp_economic", "vp_science", "vp_culture", "vp_military", "score"]
ENDED_KINDS = ("war", "alliance", "policy", "government", "production", "project", "tech_research", "civic_research",
               "diplomacy_action", "befriend", "suzerain")
EVENT_DETAIL = ("city", "item", "category", "completed", "victim", "killer", "war_type", "declared_by", "captured_by",
                "from_player", "type", "population",
                # diplomacy
                "action", "group", "initiator_name", "recipient_name", "stage", "response", "proposer", "outcome", "end_reason",
                "lost_to_name", "bonus", "rivals", "turn_influence_spent", "previous", "relationship")


def _events_since(con, game: str, since: int, cg: int, cap: int) -> dict:
    """Local events that started or finished in (since, cg]. Drops still-open intervals (their end_g is just 'now')
    and backfilled 'range' events whose window is wider than the briefing window (those are not news)."""
    out = []
    window = max(1, cg - since)
    for r in con.execute(
            "SELECT * FROM events WHERE game=? AND local=1 AND ((start_g>? AND start_g<=?) OR (end_g>? AND end_g<=? AND kind IN "
            f"({','.join('?' * len(ENDED_KINDS))}))) ORDER BY start_g, kind", (game, since, cg, since, cg, *ENDED_KINDS)):
        det = json.loads(r["detail"] or "{}")
        started = since < r["start_g"] <= cg
        if r["precision"] == "range" and (r["start_hi"] or 0) - (r["start_lo"] or 0) > window:
            continue
        if not started and (det.get("ongoing") or det.get("end_reason") == "age_end_or_ongoing"):
            continue
        if not started and r["kind"] in ("production", "project") and not det.get("completed"):
            continue
        e = {"kind": r["kind"], "subject": r["subject"], "g": r["start_g"] if started else r["end_g"],
             "phase": "started" if started else "ended", "players": json.loads(r["players"]), "event_key": r["event_key"]}
        if r["minor"]:
            e["minor"] = True
        # end_reason and the start turn's influence spending belong to one phase each
        e |= {k: det[k] for k in EVENT_DETAIL if det.get(k) is not None
              and not (k == "end_reason" and started) and not (k == "turn_influence_spent" and not started)}
        out.append(e)
    # majors and outcomes before noise: city-state wars, purchases and attitude drift sort last when capping (the
    # player's own befriending and suzerainties involve a city-state but are not noise)
    noise = lambda e: (e.get("minor", False) and e["kind"] not in ("suzerain", "befriend"),
                       e["kind"] in ("purchase", "policy", "relationship", "met"), -e["g"])
    kept = sorted(out, key=noise)[:cap]
    omitted: dict = {}
    for e in out:
        if e not in kept:
            omitted[e["kind"]] = omitted.get(e["kind"], 0) + 1
    return {"events": sorted(kept, key=lambda e: (e["g"], e["kind"])), "omitted": omitted}


def diplomacy_since(con, game: str, since: int, cg: int) -> dict:
    """The local player's diplomacy in (since, cg] beyond start/end events: befriending and other actions that moved
    (stage entered, support added), and every befriending still running with its stage, support and racing rivals.

    {"progress": [{g, kind, subject, stages: [...], support_added}], "befriending": [{subject, since_g, stage, support,
    rivals}], "suzerain_of": [names]}. Empty dict when there is nothing."""
    gr = game_row(con, game)
    me, g_off = gr["local_player_id"], dict(con.execute("SELECT age, g_offset FROM ages WHERE game=?", (game,)).fetchall())
    progress, active, suz = [], [], []
    for r in con.execute("SELECT * FROM events WHERE game=? AND local=1 AND kind IN ('diplomacy_action','befriend','suzerain') "
                         "AND start_g<=? AND (end_g IS NULL OR end_g>?) ORDER BY start_g", (game, cg, since)):
        d = json.loads(r["detail"] or "{}")
        ongoing = r["end_g"] is None or d.get("end_reason") == "age_end_or_ongoing"
        if r["kind"] == "suzerain":
            if ongoing:
                suz.append(d.get("city_state_name") or r["subject"])
            continue
        off = g_off.get(r["start_age"], 0)
        stages = [s["stage"] for s in d.get("stages") or [] if since < off + s["turn"] <= cg and off + s["turn"] != r["start_g"]]
        added = sum(x.get("support", 0) for x in d.get("support_log") or []
                    if since < off + x["turn"] <= cg and off + x["turn"] != r["start_g"] and x.get("player") == me)
        if stages or added:
            progress.append({"g": r["start_g"], "kind": r["kind"], "subject": r["subject"]}
                            | ({"stages": stages} if stages else {}) | ({"support_added": added} if added else {}))
        if r["kind"] == "befriend" and ongoing and d.get("initiator") == me:
            active.append({"subject": r["subject"], "since_g": r["start_g"], "stage": d.get("stage"),
                           "support": (d.get("support") or {}).get(str(me))} | ({"rivals": d["rivals"]} if d.get("rivals") else {}))
    out: dict = {}
    if progress:
        out["progress"] = progress
    if active:
        out["befriending"] = active
    if suz:
        out["suzerain_of"] = suz
    return out


def combat_rewards_since(con, game: str, pid: int, since: int, cg: int) -> dict:
    """Combat rewards the player gained in (since, cg], and the unattributed gold of the same turns.

    {"by_kind": {kind: {count, yields: {YIELD_X: exact amount}, unknown_amount: n}},
     "other_gold": {"gained": x, "lost": y, "turns": [{g, gold, combat_events: {kind: n}}]}}
    yields sum only exact amounts (rules, notification); unknown_amount counts rows the game does not log an amount for
    (pillage, plunder, most dispersals). other_gold is the unexplained_gold metric for g in [since, cg] (turn g's value
    needs turn g+1's treasury, so it lands a turn late; |gold| < 1 is rounding and skipped): treasury change not explained
    by income or purchases, never attributed to combat. gained sums the positive turns (as unexplained_gold_total). Each turn lists that turn's combat event counts so the advisor can judge what it was."""
    by_kind: dict = {}
    for r in con.execute("SELECT kind, yield_type, amount FROM combat_rewards WHERE game=? AND player_id=? AND g>? AND g<=?",
                         (game, pid, since, cg)):
        k = by_kind.setdefault(r["kind"], {"count": 0, "yields": {}})
        k["count"] += 1
        if r["amount"] is None:
            k["unknown_amount"] = k.get("unknown_amount", 0) + 1
        else:
            y = r["yield_type"] or "unknown"
            k["yields"][y] = round(k["yields"].get(y, 0) + r["amount"], 2)
    turns = []
    for r in con.execute("SELECT g, value FROM metrics WHERE game=? AND player_id=? AND metric='unexplained_gold' AND g>=? AND g<=? "
                         "AND abs(value)>=1 ORDER BY g", (game, pid, since, cg)):
        counts = {c["kind"]: c["n"] for c in con.execute(
            "SELECT kind, count(*) AS n FROM combat_rewards WHERE game=? AND player_id=? AND g=? GROUP BY kind ORDER BY kind",
            (game, pid, r["g"]))}
        turns.append({"g": r["g"], "gold": r["value"]} | ({"combat_events": counts} if counts else {}))
    out: dict = {}
    if by_kind:
        out["by_kind"] = by_kind
    if turns:
        out["other_gold"] = {"gained": round(sum(t["gold"] for t in turns if t["gold"] > 0), 2),
                             "lost": round(sum(t["gold"] for t in turns if t["gold"] < 0), 2), "turns": turns}
    return out


def briefing(con, game: str | None = None, since_g: int | None = None, max_events: int = 25, max_annotations: int = 8) -> dict:
    """What the advisor needs this turn, compactly: who to speak as, whom to advise, the active plan with progress,
    current numbers with deltas since since_g (default: the previous observed turn), standings, news, alerts."""
    gr = game_row(con, game)
    game, cg, me = gr["game"], gr["current_g"], gr["local_player_id"]
    prev = con.execute("SELECT max(g) FROM turns WHERE game=? AND g<?", (game, cg)).fetchone()[0]
    since = since_g if since_g is not None else (prev if prev is not None else cg - 1)
    state = {}
    for m in KEY_METRICS:
        v, vg = value_at(con, game, m, me, cg)
        if v is None:
            continue
        p, _ = value_at(con, game, m, me, since)
        d = None if p is None else round(v - p, 2)
        state[m] = {"value": v} | ({"delta": d} if d else {}) | ({"as_of_g": vg} if vg != cg else {})
    standings = {}
    for m in STANDINGS:
        s = standing(con, game, m, me, cg, window=3)
        if not s["rank"]:
            continue
        v, _ = value_at(con, game, m, me, cg)
        t, _ = value_at(con, game, m + "_target", me, cg, lo=cg - 3)
        lead = s["leader"]
        standings[m] = {"rank": s["rank"], "of": s["of"], "value": v} | ({"target": t} if t else {}) | (
            {"leader": lead["leader"], "leader_value": lead["value"]} if lead and lead["player_id"] != me else {})
    ev = _events_since(con, game, since, cg, max_events)
    combat = combat_rewards_since(con, game, me, since, cg)
    diplo = diplomacy_since(con, game, since, cg)
    summary = con.execute("SELECT summary FROM turns WHERE game=? ORDER BY g DESC LIMIT 1", (game,)).fetchone()
    goals = evaluate_goals(con, game, include_closed=False)
    tree = goal_tree(con, game, active_only=True)
    plan = [{"strategy_id": s["strategy_id"], "name": s["name"], "victory_types": s["victory_types"],
             **({"description": s["description"]} if s.get("description") else {}),
             "goals": [_compact_goal(g) for g in s["goals"]]} for s in tree]
    ap, pp = current_advisor_profile(con, game), current_player_profile(con, game)
    setup = [msg for cond, msg in (
        (not ap, "No advisor yet: run setup_wizard (step advisor)."),
        (not pp, "No player profile yet: run setup_wizard (step player)."),
        (not plan, "No strategy or goals yet: run setup_wizard (step strategy)."),
    ) if cond]
    gi = {k: gr[k] for k in ("game", "leader", "civilization", "difficulty", "current_age", "current_turn", "current_g",
                             "age_turns_max", "local_player_id", "updated_at")}
    lp = con.execute("SELECT leader, civilizations FROM players WHERE game=? AND player_id=?", (game, me)).fetchone()
    if lp:  # display names instead of LEADER_* / CIVILIZATION_*
        gi["leader"] = lp["leader"] or gi["leader"]
        gi["civilization"] = json.loads(lp["civilizations"] or "{}").get(gr["current_age"]) or gi["civilization"]
    return {
        "game": gi, "since_g": since,
        "advisor_profile": ap and {k: ap[k] for k in ("profile_id", "version", "name", "personality", "personality_description",
                                                     "style_prompt", "style_file", "voice", "priorities", "quirks")},
        "player_profile": pp and {k: pp[k] for k in ("profile_id", "version", "name", "motives", "playstyle", "risk_tolerance", "notes")},
        "plan": plan,
        "state": state, "standings": standings,
        "local_summary": json.loads(summary[0]) if summary and summary[0] else None,
        "events_since": ev["events"], **({"events_omitted": ev["omitted"]} if ev["omitted"] else {}),
        **({"combat_rewards_since": combat} if combat else {}),
        **({"diplomacy": diplo} if diplo else {}),
        "alerts": _alerts(state, standings, goals),
        "recent_annotations": [{k: a[k] for k in ("annotation_id", "g", "author", "kind", "text", "outcome", "goal_id", "scope", "scope_key")
                                if a[k] is not None} for a in annotations(con, game, limit=max_annotations)],
        **({"setup_needed": setup} if setup else {}),
        "monitor": {"last_refresh": json.loads(store.get_meta(con, "last_refresh") or "null"),
                    "seen_at": store.get_meta(con, "monitor_seen_at"), "state": store.get_meta(con, "monitor_state")},
    }


def _alerts(state: dict, standings: dict, goals: list[dict]) -> list[str]:
    a = []
    v = lambda k: (state.get(k) or {}).get("value")
    d = lambda k: (state.get(k) or {}).get("delta")
    if (v("gold_per_turn") or 0) < 0:
        a.append(f"Negative gold income ({v('gold_per_turn'):.0f}/turn).")
    if v("unrest_settlements"):
        a.append(f"{v('unrest_settlements'):.0f} settlement(s) in unrest.")
    if v("settlements") and v("settlement_cap") and v("settlements") > v("settlement_cap"):
        a.append(f"Over settlement cap ({v('settlements'):.0f}/{v('settlement_cap'):.0f}).")
    if d("units_lost_total"):
        a.append(f"Lost {d('units_lost_total'):.0f} unit(s) since last check.")
    if d("settlements") and d("settlements") < 0:
        a.append(f"Settlements down by {-d('settlements'):.0f}.")
    if d("wars_active") and d("wars_active") > 0:
        a.append(f"Wars with majors up by {d('wars_active'):.0f}.")
    for m, s in standings.items():
        if (s.get("target") or 0) <= 0:  # no target published yet (the game reports -1 early on)
            continue
        if s.get("target") and s.get("value") is not None and s["value"] >= s["target"]:
            a.append(f"{m} at {s['value']:.0f} has reached its victory target {s['target']:.0f}.")
        elif s.get("target") and s.get("leader_value") is not None and s["leader_value"] >= 0.85 * s["target"]:
            a.append(f"{s['leader']} is near the {m} target ({s['leader_value']:.0f}/{s['target']:.0f}).")
    for g in goals:
        if g["overdue"]:
            a.append(f"Goal {g['goal_id']} overdue: {g['title']} ({(g['progress'] or 0) * 100:.0f}%).")
        elif g["turns_left"] is not None and 0 <= g["turns_left"] <= 3 and (g["progress"] or 0) < 1:
            a.append(f"Goal {g['goal_id']} due in {g['turns_left']} turn(s): {g['title']} ({(g['progress'] or 0) * 100:.0f}%).")
        elif g["progress"] is not None and g["progress"] >= 1:
            a.append(f"Goal {g['goal_id']} targets met: {g['title']}. Confirm with the player and set_goal_status achieved.")
        for m in g["metrics"]:
            if m["comparator"] == "gain<=" and m["met"] is False:
                a.append(f"Goal {g['goal_id']} limit broken: {m['metric']} gain {m.get('gain', 0):+g} > {m['target']:g}.")
    return a
