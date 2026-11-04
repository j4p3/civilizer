"""Phase fact sheets: one per age, per war the local player fought, per goal, and one for the whole game.

No prose here. Each phase carries the numbers (`facts`) and a heuristic `verdict`; Claude reads them (MCP or
SQL) and writes the narrative as annotations scoped to the phase (annotations.scope/scope_key). Built from
store tables only, so it can be regenerated at any time.
"""

from __future__ import annotations

import json
import sqlite3

VP = {"vp_economic": "economic", "vp_science": "science", "vp_culture": "culture", "vp_military": "military"}


class Facts:
    def __init__(self, con: sqlite3.Connection, game: str):
        self.con, self.game = con, game
        g = con.execute("SELECT * FROM games WHERE game=?", (game,)).fetchone()
        self.local = g["local_player_id"] if g else 0
        self.current_g = g["current_g"] if g else 0
        self.majors = {r["player_id"] for r in con.execute("SELECT player_id FROM players WHERE game=?", (game,))}
        self.ages = [dict(r) for r in con.execute("SELECT * FROM ages WHERE game=? ORDER BY idx", (game,))]

    def value(self, metric: str, pid: int, g: int, lo: int = -1) -> float | None:
        """Latest value in [lo, g]."""
        r = self.con.execute("SELECT value FROM metrics WHERE game=? AND metric=? AND player_id=? AND g BETWEEN ? AND ? "
                             "ORDER BY g DESC LIMIT 1", (self.game, metric, pid, lo, g)).fetchone()
        return r[0] if r else None

    def first(self, metric: str, pid: int, lo: int, hi: int) -> float | None:
        r = self.con.execute("SELECT value FROM metrics WHERE game=? AND metric=? AND player_id=? AND g BETWEEN ? AND ? "
                             "ORDER BY g LIMIT 1", (self.game, metric, pid, lo, hi)).fetchone()
        return r[0] if r else None

    def rank(self, metric: str, pid: int, g: int, lo: int) -> dict | None:
        vals = [(p, v) for p in self.majors if (v := self.value(metric, p, g, lo)) is not None]
        order = sorted(vals, key=lambda kv: -kv[1])
        ids = [p for p, _ in order]
        if pid not in ids:
            return None
        return {"rank": ids.index(pid) + 1, "of": len(ids), "value": dict(vals)[pid], "leader": order[0][0], "leader_value": order[0][1]}

    def events(self, where: str, args: tuple = ()) -> list[dict]:
        out = []
        for r in self.con.execute(f"SELECT * FROM events WHERE game=? AND {where} ORDER BY start_g", (self.game, *args)):
            d = dict(r)
            d["players"], d["detail"] = json.loads(d["players"]), json.loads(d["detail"] or "{}")
            out.append(d)
        return out


def _combat(f: Facts, lo: int, hi: int, opponent: int | None = None) -> dict:
    kills = losses = 0
    for e in f.events("kind='unit_killed' AND start_g BETWEEN ? AND ?", (lo, hi)):
        v, k = e["detail"].get("victim"), e["detail"].get("killer")
        if k == f.local and (opponent is None or v == opponent):
            kills += 1
        elif v == f.local and (opponent is None or k == opponent):
            losses += 1
    return {"units_killed": kills, "units_lost": losses}


def _captures(f: Facts, lo: int, hi: int, opponent: int | None = None) -> dict:
    took, lost = [], []
    for e in f.events("kind='settlement_captured' AND start_g BETWEEN ? AND ?", (lo, hi)):
        by, frm = e["detail"].get("captured_by"), e["detail"].get("from_player")
        row = {"settlement": e["subject"], "g": e["start_g"], "turn": e["start_turn"], "event_key": e["event_key"]}
        if by == f.local and (opponent is None or frm == opponent):
            took.append(row | {"from_player": frm})
        elif frm == f.local and (opponent is None or by == opponent):
            lost.append(row | {"to_player": by})
    return {"captured": took, "lost": lost}


def _verdict(score: int, ongoing: bool = False) -> str:
    if score >= 2:
        return "went_well"
    if score <= -1:
        return "went_awry"
    return "ongoing" if ongoing else "mixed"


def _sign(a, b, ratio: float = 1.3) -> int:
    return (a > b * ratio) - (b > a * ratio)


def age_phase(f: Facts, a: dict) -> dict | None:
    if not a["turns"]:
        return None
    lo, hi = a["g_offset"] + 1, a["g_offset"] + a["turns"]
    in_progress = lo <= f.current_g <= hi
    me = f.local
    metrics = {m: {"start": f.first(m, me, lo, hi), "end": f.value(m, me, hi, lo)}
               for m in ("settlements", "population", "science_per_turn", "gold_per_turn", "culture_per_turn", "production_per_turn")}
    standings = {label: r for m, label in VP.items() if (r := f.rank(m, me, hi, lo))}
    wars = f.events("kind='war' AND local=1 AND minor=0 AND start_g BETWEEN ? AND ?", (lo, hi))
    foes = sorted({p for e in wars for p in e["players"] if p != me})
    combat, caps = _combat(f, lo, hi), _captures(f, lo, hi)
    pick = lambda kind, extra=lambda e: True: [{"subject": e["subject"], "g": e["start_g"], "event_key": e["event_key"]}
                                               for e in f.events(f"kind='{kind}' AND local=1 AND start_g BETWEEN ? AND ?", (lo, hi)) if extra(e)]
    s = metrics["settlements"]
    score = (_sign(s["end"], s["start"], 1.0) if s["start"] is not None and s["end"] is not None else 0)
    score += sum(r["rank"] == 1 for r in standings.values()) - (sum(r["rank"] > r["of"] // 2 for r in standings.values()) > len(standings) // 2)
    score += _sign(combat["units_killed"], combat["units_lost"]) + _sign(len(caps["captured"]), len(caps["lost"]), 1.0)
    legacies = pick("legacy")
    score += bool(legacies)
    return {"scope": "age", "scope_key": a["age"], "g_start": lo, "g_end": min(hi, f.current_g) if in_progress else hi,
            "title": a["age"], "verdict": _verdict(score, in_progress),
            "facts": {"in_progress": in_progress, "metrics": metrics, "standings": standings, "war_opponents": foes,
                      **combat, **caps, "governments": pick("government"), "legacies": legacies,
                      "wonders": pick("production", lambda e: e["detail"].get("category") == "wonder" and e["detail"].get("completed")),
                      # settlement spans come from history captures; without one in this age the count is unknown
                      "settlements_founded": (len(pick("settlement")) or None) if f.events(
                          "kind='settlement' AND start_g BETWEEN ? AND ?", (lo, hi)) else None, "score": score}}


def war_phase(f: Facts, e: dict) -> dict:
    me = f.local
    foe = next((p for p in e["players"] if p != me), None)
    lo, hi = e["start_g"], e["end_g"] or f.current_g
    ongoing = e["detail"].get("end_reason") == "age_end_or_ongoing" and hi >= f.current_g - 1
    combat, caps = _combat(f, lo, hi, foe), _captures(f, lo, hi, foe)
    score = _sign(combat["units_killed"], combat["units_lost"]) + 2 * _sign(len(caps["captured"]), len(caps["lost"]), 1.0)
    return {"scope": "war", "scope_key": e["event_key"], "g_start": lo, "g_end": hi, "title": f"war with player {foe}",
            "verdict": _verdict(score, ongoing) if score else ("ongoing" if ongoing else "mixed"),
            "facts": {"opponent": foe, "declared_by": e["detail"].get("declared_by"), "war_type": e["detail"].get("war_type"),
                      "start_age": e["start_age"], "start_turn": e["start_turn"], "turns": hi - lo + 1, "ongoing": ongoing,
                      **combat, **caps, "score": score}}


def goal_phases(con: sqlite3.Connection, game: str) -> list[dict]:
    from . import advisor

    out = []
    for g in advisor.evaluate_goals(con, game):
        prog = g["progress"]
        verdict = {"achieved": "went_well", "failed": "went_awry", "abandoned": "mixed"}.get(g["status"]) or (
            "went_well" if prog is not None and prog >= 1 else "ongoing")
        out.append({"scope": "goal", "scope_key": str(g["goal_id"]), "g_start": g["created_g"],
                    "g_end": g.get("resolved_g") or g["current_g"], "title": g["title"], "verdict": verdict,
                    "facts": {k: g[k] for k in ("horizon", "status", "progress", "overdue", "metrics", "strategy_id", "parent_goal_id")}})
    return out


def build(con: sqlite3.Connection, game: str) -> list[dict]:
    f = Facts(con, game)
    out = [p for a in f.ages if (p := age_phase(f, a))]
    out += [war_phase(f, e) for e in f.events("kind='war' AND local=1 AND minor=0 AND precision!='state'")]
    out += goal_phases(con, game)
    ages = [p for p in out if p["scope"] == "age"]
    score = sum({"went_well": 1, "went_awry": -1}.get(p["verdict"], 0) for p in ages)
    standings = {label: r for m, label in VP.items() if (r := f.rank(m, f.local, f.current_g, f.current_g - 3))}
    out.insert(0, {"scope": "game", "scope_key": game, "g_start": 1, "g_end": f.current_g, "title": "game",
                   "verdict": _verdict(2 * score if score > 0 else score, True),
                   "facts": {"age_verdicts": {p["scope_key"]: p["verdict"] for p in ages}, "standings_now": standings}})
    return out
