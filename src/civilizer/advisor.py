"""Advisor service over the central store: profiles, strategies, goals <-> metrics, annotations, briefings.

Everything here writes only advisor-owned tables (see store.TABLES). The CLI (`civilizer advisor ...`) and
the MCP server (mcp_server.py) are thin wrappers over these functions.

Every change is recorded: profiles are versioned rows, and strategy/goal changes also write an annotation
(priority_change / intention / outcome), so `history()` can replay how the plan evolved.
"""

from __future__ import annotations

import json
import sqlite3

from . import store

STATUSES = ("active", "achieved", "failed", "abandoned")
STATUS_VERDICT = {"achieved": "went_well", "failed": "went_awry", "abandoned": "mixed"}
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


def annotate(con, game: str, text: str, kind: str = "observation", author: str = "advisor", verdict: str | None = None,
             goal_id: int | None = None, strategy_id: int | None = None, event_key: str | None = None, g: int | None = None,
             scope: str | None = None, scope_key: str | None = None) -> int:
    """Record a non-game note. scope/scope_key tie it to a phase (game / AGE_* / war event_key / goal id): narrative
    and outcome notes there are the phase's story, and an outcome verdict overrides the phase's heuristic verdict."""
    _check(con, "annotation_kind", kind)
    if verdict:
        _check(con, "verdict", verdict)
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
                (game, aid, g, age, turn, author, kind, text.strip(), verdict, goal_id, strategy_id, event_key, scope, scope_key,
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

def set_advisor_profile(con, game: str, personality: str, voice: str = "", priorities: list[str] | None = None,
                        quirks: str = "") -> dict:
    _check(con, "personality", personality)
    if not priorities:
        priorities = json.loads(_vocab(con, "personality")[personality]["extra"] or "[]")
    _check(con, "advisor_priority", priorities)
    g = game_row(con, game)["current_g"]
    v = (con.execute("SELECT max(version) FROM advisor_profiles WHERE game=?", (game,)).fetchone()[0] or 0) + 1
    con.execute("INSERT INTO advisor_profiles VALUES (?,?,?,?,?,?,?,?)",
                (game, v, personality, voice, json.dumps(priorities), quirks, g, store.now()))
    return current_advisor_profile(con, game)


def set_player_profile(con, game: str, playstyle: list[str], risk_tolerance: str = "medium", notes: str = "") -> dict:
    if not playstyle:
        raise AdvisorError("playstyle needs at least one value")
    _check(con, "playstyle", playstyle)
    if risk_tolerance not in ("low", "medium", "high"):
        raise AdvisorError("risk_tolerance must be low, medium or high")
    g = game_row(con, game)["current_g"]
    prev = current_player_profile(con, game)
    v = (prev["version"] if prev else 0) + 1
    con.execute("INSERT INTO player_profiles VALUES (?,?,?,?,?,?,?)",
                (game, v, json.dumps(playstyle), risk_tolerance, notes, g, store.now()))
    if prev:
        annotate(con, game, f"Playstyle changed: {', '.join(prev['playstyle'])} (risk {prev['risk_tolerance']}) -> "
                 f"{', '.join(playstyle)} (risk {risk_tolerance})." + (f" {notes}" if notes else ""), "priority_change", "player")
    return current_player_profile(con, game)


def _profiles(con, table: str, game: str, latest: bool) -> list[dict]:
    rows = con.execute(f"SELECT * FROM {table} WHERE game=? ORDER BY version" + (" DESC LIMIT 1" if latest else ""), (game,))
    out = []
    for r in rows:
        d = dict(r)
        for k in ("priorities", "playstyle"):
            if k in d:
                d[k] = json.loads(d[k] or "[]")
        out.append(d)
    return out


def current_advisor_profile(con, game: str) -> dict | None:
    d = next(iter(_profiles(con, "advisor_profiles", game, True)), None)
    if d:
        p = _vocab(con, "personality").get(d["personality"])
        d["personality_description"] = p["description"] if p else None
    return d


def current_player_profile(con, game: str) -> dict | None:
    d = next(iter(_profiles(con, "player_profiles", game, True)), None)
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


def set_strategy_status(con, game: str, strategy_id: int, status: str, note: str = "", verdict: str | None = None) -> dict:
    if status not in STATUSES:
        raise AdvisorError(f"status must be one of {', '.join(STATUSES)}")
    s = _strategy(con, game, strategy_id)
    g = game_row(con, game)["current_g"]
    con.execute("UPDATE strategies SET status=?, ended_g=? WHERE game=? AND strategy_id=?",
                (status, None if status == "active" else g, game, strategy_id))
    verdict = verdict or STATUS_VERDICT.get(status)
    annotate(con, game, f"Strategy {strategy_id} ({s['name']}) {s['status']} -> {status}. {note}".strip(),
             "outcome" if status != "active" else "priority_change", "player", verdict=verdict, strategy_id=strategy_id)
    return {"strategy_id": strategy_id, "status": status, "verdict": verdict}


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


def set_goal_status(con, game: str, goal_id: int, status: str, resolution: str = "", verdict: str | None = None) -> dict:
    if status not in STATUSES:
        raise AdvisorError(f"status must be one of {', '.join(STATUSES)}")
    goal = _goal(con, game, goal_id)
    g = game_row(con, game)["current_g"]
    con.execute("UPDATE goals SET status=?, resolved_g=?, resolution=? WHERE game=? AND goal_id=?",
                (status, None if status == "active" else g, resolution or None, game, goal_id))
    verdict = verdict or STATUS_VERDICT.get(status)
    annotate(con, game, f"Goal {goal_id} ({goal['title']}) {goal['status']} -> {status}. {resolution}".strip(),
             "outcome" if status != "active" else "priority_change", "player", verdict=verdict, goal_id=goal_id)
    return {"goal_id": goal_id, "status": status, "verdict": verdict}


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
            if p is not None:
                num += p * m["weight"]
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


# ---------------------------------------------------------------- history and phases

def history(con, game: str, what: str | None = None) -> list[dict]:
    """Chronological change log of the advisor layer: profile versions, strategy/goal creation, and every
    intention / priority_change / outcome annotation. what: profile, strategy, goal, or None for all."""
    out = []
    if what in (None, "profile"):
        for p in _profiles(con, "advisor_profiles", game, False):
            out.append({"g": p["valid_from_g"], "at": p["created_at"], "what": "advisor_profile", "version": p["version"],
                        "text": f"Advisor v{p['version']}: {p['personality']} (priorities {', '.join(p['priorities'])})"
                                + (f"; voice: {p['voice']}" if p["voice"] else "") + (f"; quirks: {p['quirks']}" if p["quirks"] else "")})
        for p in _profiles(con, "player_profiles", game, False):
            out.append({"g": p["valid_from_g"], "at": p["created_at"], "what": "player_profile", "version": p["version"],
                        "text": f"Player v{p['version']}: {', '.join(p['playstyle'])}, risk {p['risk_tolerance']}"
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
                    "goal_id": a["goal_id"], "strategy_id": a["strategy_id"], "verdict": a["verdict"], "text": a["text"]})
    out.sort(key=lambda e: (e["at"] or "", e["g"] or 0))
    return out


def phases(con, game: str, scope: str | None = None, with_facts: bool = True) -> list[dict]:
    """Phase fact sheets plus the advisor layer on top: narrative/outcome annotations on the same scope, and
    advisor_verdict (latest outcome annotation's verdict), which overrides the heuristic verdict in `effective_verdict`."""
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
        verdicts = [a for a in ns if a["kind"] == "outcome" and a["verdict"]]
        d["heuristic_verdict"] = d.pop("verdict")
        d["advisor_verdict"] = verdicts[-1]["verdict"] if verdicts else None
        d["effective_verdict"] = d["advisor_verdict"] or d["heuristic_verdict"]
        d["annotations"] = [{k: a[k] for k in ("annotation_id", "g", "kind", "author", "verdict", "text")} for a in ns]
        d["needs_narrative"] = not any(a["kind"] == "narrative" for a in ns)
        out.append(d)
    return out


# ---------------------------------------------------------------- briefing

KEY_METRICS = ["gold_per_turn", "science_per_turn", "culture_per_turn", "production_per_turn", "food_per_turn",
               "happiness_per_turn", "influence_per_turn", "gold_balance", "influence_balance", "settlements", "settlement_cap",
               "population", "military_units", "wars_active", "unrest_settlements", "trade_routes", "triumphs_done",
               "units_lost_total", "units_killed_total"]
STANDINGS = ["vp_economic", "vp_science", "vp_culture", "vp_military", "score"]
ENDED_KINDS = ("war", "alliance", "policy", "government", "production", "project", "tech_research", "civic_research")
EVENT_DETAIL = ("city", "item", "category", "completed", "victim", "killer", "war_type", "declared_by", "captured_by",
                "from_player", "type", "population")


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
        e |= {k: det[k] for k in EVENT_DETAIL if det.get(k) is not None}
        out.append(e)
    # majors and outcomes before noise: city-state wars and purchases sort last when capping
    noise = lambda e: (e.get("minor", False), e["kind"] in ("purchase", "policy"), -e["g"])
    kept = sorted(out, key=noise)[:cap]
    omitted: dict = {}
    for e in out:
        if e not in kept:
            omitted[e["kind"]] = omitted.get(e["kind"], 0) + 1
    return {"events": sorted(kept, key=lambda e: (e["g"], e["kind"])), "omitted": omitted}


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
    summary = con.execute("SELECT summary FROM turns WHERE game=? ORDER BY g DESC LIMIT 1", (game,)).fetchone()
    goals = evaluate_goals(con, game, include_closed=False)
    tree = goal_tree(con, game, active_only=True)
    plan = [{"strategy_id": s["strategy_id"], "name": s["name"], "victory_types": s["victory_types"],
             **({"description": s["description"]} if s.get("description") else {}),
             "goals": [_compact_goal(g) for g in s["goals"]]} for s in tree]
    ap, pp = current_advisor_profile(con, game), current_player_profile(con, game)
    setup = [msg for cond, msg in (
        (not ap, "No advisor personality yet: ask the player to pick one (vocabulary field=personality) and set_advisor_profile."),
        (not pp, "No player profile yet: ask how they like to play and set_player_profile."),
        (not plan, "No strategy or goals yet: ask what they are going for and add_strategy / add_goal with metrics."),
    ) if cond]
    gi = {k: gr[k] for k in ("game", "leader", "civilization", "difficulty", "current_age", "current_turn", "current_g",
                             "age_turns_max", "local_player_id", "updated_at")}
    lp = con.execute("SELECT leader, civilizations FROM players WHERE game=? AND player_id=?", (game, me)).fetchone()
    if lp:  # display names instead of LEADER_* / CIVILIZATION_*
        gi["leader"] = lp["leader"] or gi["leader"]
        gi["civilization"] = json.loads(lp["civilizations"] or "{}").get(gr["current_age"]) or gi["civilization"]
    return {
        "game": gi, "since_g": since,
        "advisor_profile": ap and {k: ap[k] for k in ("version", "personality", "personality_description", "voice", "priorities", "quirks")},
        "player_profile": pp and {k: pp[k] for k in ("version", "playstyle", "risk_tolerance", "notes")},
        "plan": plan,
        "state": state, "standings": standings,
        "local_summary": json.loads(summary[0]) if summary and summary[0] else None,
        "events_since": ev["events"], **({"events_omitted": ev["omitted"]} if ev["omitted"] else {}),
        "alerts": _alerts(state, standings, goals),
        "recent_annotations": [{k: a[k] for k in ("annotation_id", "g", "author", "kind", "text", "verdict", "goal_id", "scope", "scope_key")
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
