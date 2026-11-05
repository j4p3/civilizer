"""Phase fact sheets: one per age, per war the local player fought, per goal, and one for the whole game.

No prose here. Each phase carries the numbers (`facts`) and a heuristic `outcome`; Claude reads them (MCP or
SQL) and writes the narrative as annotations scoped to the phase (annotations.scope/scope_key). Built from
store tables only, so it can be regenerated at any time.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from functools import cached_property

VP = {"vp_economic": "economic", "vp_science": "science", "vp_culture": "culture", "vp_military": "military"}
RUN_UP = 10  # turns before a war whose diplomacy between the two sides counts as its run-up


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

    @cached_property
    def names(self) -> dict[int, str]:
        """Player id -> leader (majors) or independent / city-state name, from event details and snapshot city-states."""
        out: dict[int, str] = {}
        for r in self.con.execute("SELECT detail FROM events WHERE game=? AND kind IN ('met','befriend','suzerain','diplomacy_action')",
                                  (self.game,)):
            d = json.loads(r[0] or "{}")
            for k in ("other", "initiator", "recipient", "suzerain", "city_state"):
                if isinstance(d.get(k), int) and d.get(k + "_name"):
                    out.setdefault(d[k], d[k + "_name"])
        for r in self.con.execute("SELECT summary FROM turns WHERE game=?", (self.game,)):
            for c in json.loads(r[0] or "{}").get("city_states") or []:
                if isinstance(c.get("id"), int) and c.get("name"):
                    out[c["id"]] = c["name"]
        return out | {r[0]: r[1] for r in self.con.execute("SELECT player_id, leader FROM players WHERE game=?", (self.game,)) if r[1]}

    def who(self, pid: int | None) -> dict:
        return {"id": pid, "name": self.names.get(pid)}

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


def _outcome(score: int, ongoing: bool = False) -> str:
    if score >= 2:
        return "success"
    if score <= -1:
        return "failure"
    return "ongoing" if ongoing else "partial"


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
            "title": a["age"], "outcome": _outcome(score, in_progress),
            "facts": {"in_progress": in_progress, "metrics": metrics, "standings": standings, "war_opponents": foes,
                      **combat, **caps, "governments": pick("government"), "legacies": legacies,
                      "wonders": pick("production", lambda e: e["detail"].get("category") == "wonder" and e["detail"].get("completed")),
                      # settlement spans come from history captures; without one in this age the count is unknown
                      "settlements_founded": (len(pick("settlement")) or None) if f.events(
                          "kind='settlement' AND start_g BETWEEN ? AND ?", (lo, hi)) else None, "score": score}}


def _overlaps(e: dict, lo: int, hi: int) -> bool:
    return e["start_g"] <= hi and (e["end_g"] is None or e["end_g"] >= lo)


def _casus_belli(f: Facts, e: dict, foe: int) -> dict:
    """Who declared, the two sides' diplomacy in the run-up, and whether the opponent was already fighting an ally."""
    me, lo = f.local, e["start_g"]
    run_up, acted = [], set()
    ds = [d for d in f.events("kind IN ('diplomacy_action','relationship','diplo_response') AND start_g BETWEEN ? AND ?",
                              (lo - RUN_UP, lo)) if set(d["players"]) == {me, foe}]
    for d in sorted(ds, key=lambda d: d["kind"] == "diplo_response"):  # actions first: their responses fold into them
        det = d["detail"]
        if d["kind"] == "relationship":  # the rival's attitude toward the local player
            run_up.append({"g": d["start_g"], "what": f"attitude {det.get('previous')} -> {det.get('relationship')}", "by": f.names.get(foe)})
        elif d["kind"] == "diplomacy_action":
            acted.add((det.get("action"), det.get("initiator")))
            run_up.append({"g": d["start_g"], "what": det.get("action"), "group": det.get("group"),
                           "by": f.names.get(det.get("initiator")), "response": det.get("response"), "outcome": det.get("outcome")})
        elif (det.get("action"), det.get("proposer")) not in acted:  # a response to an action that started before the run-up
            run_up.append({"g": d["start_g"], "what": det.get("action"), "by": f.names.get(det.get("proposer")), "response": det.get("response")})
    run_up = sorted(({k: v for k, v in r.items() if v is not None} for r in run_up), key=lambda r: r["g"])
    joined = None
    for a in f.events("kind='alliance' AND start_g<=? AND (end_g IS NULL OR end_g>=?)", (lo, lo)):
        ally = next((p for p in a["players"] if p != me), None)
        if me not in a["players"] or ally == foe:
            continue
        # strictly before: AIs move after the local player, so an ally war starting the same turn followed ours
        for w in f.events("kind='war' AND start_g<? AND (end_g IS NULL OR end_g>=?)", (lo, lo)):
            if set(w["players"]) != {ally, foe} or (joined and joined["ally_war_start_g"] <= w["start_g"]):
                continue
            # an alliance event opened by a session's first log row carries that turn, not the signing: prefer the accepted proposal
            signed = [r["start_g"] for r in f.events("kind='diplo_response' AND start_g<=?", (lo,))
                      if set(r["players"]) == {me, ally} and r["detail"].get("action") == "Form Alliance"
                      and r["detail"].get("response") == "accepted"]
            joined = {"ally": f.who(ally), "ally_war_start_g": w["start_g"], "declared_by": f.who(w["detail"].get("declared_by")),
                      "war_type": w["detail"].get("war_type"), "allied_since_g": max(signed, default=a["start_g"])}
    return {"declared_by": f.who(e["detail"].get("declared_by")), "war_type": e["detail"].get("war_type"),
            "run_up": run_up, "joined_ally_war": joined}


def _combatants(f: Facts, foe: int, lo: int, hi: int) -> dict:
    """Each side: majors at war with the other side's leader during the war (ours: anyone fighting the opponent; theirs:
    the opponent's allies fighting us), and city-states that joined in suzerain wars once it started."""
    me = f.local
    alliances = f.events("kind='alliance'")
    allied = lambda a, b: any(set(x["players"]) == {a, b} and _overlaps(x, lo, hi) for x in alliances)
    side = {"us": {"majors": [f.who(me)], "city_states": []}, "them": {"majors": [f.who(foe)], "city_states": []}}
    seen = {me, foe}
    for w in f.events("kind='war' AND start_g<=? AND (end_g IS NULL OR end_g>=?) AND precision!='state'", (hi, lo)):
        ps = w["players"]
        for leader, enemy, key in ((foe, me, "us"), (me, foe, "them")):
            if leader not in ps:
                continue
            x = next((p for p in ps if p != leader), None)
            if x in seen:
                continue
            if x in f.majors and (key == "us" or allied(x, foe)):
                side[key]["majors"].append(f.who(x) | {"since_g": w["start_g"], "allied": allied(x, enemy)})
            elif x not in f.majors and w["detail"].get("war_type") == "Suzerain War" and w["start_g"] >= lo:
                side[key]["city_states"].append(f.who(x) | {"since_g": w["start_g"]})
            else:
                continue
            seen.add(x)
    return side


def _war_support(f: Facts, foe: int, lo: int, hi: int) -> dict | None:
    """The local player's war support against the opponent per snapshot turn (supporters minus opposers)."""
    pts = [[g, int(v) if v == int(v) else v] for g, v in f.con.execute(
        "SELECT g, value FROM metrics WHERE game=? AND metric='war_support' AND player_id=? AND g BETWEEN ? AND ? ORDER BY g",
        (f.game, foe, lo, hi))]
    if not pts:
        return None
    vals = [v for _, v in pts]
    return {"series": pts, "first": vals[0], "last": vals[-1], "min": min(vals), "max": max(vals)}


def _notable(f: Facts, foe: int, lo: int, hi: int) -> dict:
    """Settlements taken, lost (to anyone) and razed during the war, peak kill turns, and upsets."""
    me = f.local
    caps = f.events("kind='settlement_captured' AND start_g<=?", (f.current_g,))
    razing: dict[str, int] = {}  # settlement -> first turn seen being razed (razing begun during the war)
    rows, upsets = [], []
    prev: dict[str, dict] = {}
    for g, summ in f.con.execute("SELECT g, summary FROM turns WHERE game=? AND g>=? ORDER BY g", (f.game, lo)):
        here = {c["name"]: c for c in json.loads(summ or "{}").get("settlements") or []}
        for n, c in here.items():
            if c.get("being_razed") and n not in razing and g <= hi:
                razing[n] = g
                rows.append({"g": g, "settlement": n, "event": "razing"})
        for n in prev.keys() - here.keys():  # gone while being razed, and nobody captured it from us meanwhile: razed
            if n in razing and not any(c["subject"] == n and c["detail"].get("from_player") == me and razing[n] <= c["start_g"] <= g
                                       for c in caps):
                rows.append({"g": g, "settlement": n, "event": "razed"})
        prev = here
    for c in caps:
        by, frm, n, g = c["detail"].get("captured_by"), c["detail"].get("from_player"), c["subject"], c["start_g"]
        if not lo <= g <= hi:
            continue
        if by == me and frm == foe:
            rows.append({"g": g, "settlement": n, "event": "captured", "from": f.names.get(frm), "capital": c["detail"].get("capital")})
            if c["detail"].get("capital"):
                upsets.append({"g": g, "what": "capital_taken", "settlement": n, "player": f.names.get(frm)})
        elif frm == me:
            rows.append({"g": g, "settlement": n, "event": "lost", "to": f.names.get(by), "third_party": by != foe or None,
                         "while_razing": n in razing and razing[n] <= g or None, "capital": c["detail"].get("capital")})
            if c["detail"].get("capital"):
                upsets.append({"g": g, "what": "capital_lost", "settlement": n, "player": f.names.get(by)})
            # founded (or at least not taken from anyone): no earlier capture of it by the local player
            if not any(x["subject"] == n and x["detail"].get("captured_by") == me and x["start_g"] < g for x in caps):
                upsets.append({"g": g, "what": "founded_lost", "settlement": n, "player": f.names.get(by)})
    # eliminated: settlements reached 0, or the opponent vanished from the snapshots that continued after the war
    last = f.con.execute("SELECT MAX(g) FROM metrics WHERE game=? AND metric='settlements' AND player_id=?", (f.game, foe)).fetchone()[0]
    after = f.con.execute("SELECT MIN(g) FROM turns WHERE game=? AND g>?", (f.game, last or 0)).fetchone()[0]
    if f.value("settlements", foe, hi, lo) == 0 or (last is not None and after is not None and lo <= last <= hi + 1):
        took = [r["g"] for r in rows if r["event"] == "captured" and r["g"] >= (last or 0)]
        upsets.append({"g": took[-1] if took else after, "what": "eliminated", "player": f.names.get(foe)})
    kills, losses = Counter(), Counter()
    for e in f.events("kind='unit_killed' AND start_g BETWEEN ? AND ?", (lo, hi)):
        v, k = e["detail"].get("victim"), e["detail"].get("killer")
        if k == me and v == foe:
            kills[e["start_g"]] += 1
        elif v == me and k == foe:
            losses[e["start_g"]] += 1
    peaks = [{"g": g, "killed": n, "lost": losses[g]} for g, n in sorted(kills.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    order = {"captured": 0, "lost": 0, "razing": 1, "razed": 2}
    rows = [{k: v for k, v in r.items() if v is not None} for r in sorted(rows, key=lambda r: (r["g"], order[r["event"]]))]
    return {"settlements": rows, "peak_kill_turns": peaks, "upsets": sorted(upsets, key=lambda u: u["g"])}


def war_phase(f: Facts, e: dict) -> dict:
    me = f.local
    foe = next((p for p in e["players"] if p != me), None)
    lo, hi = e["start_g"], e["end_g"] or f.current_g
    ongoing = e["detail"].get("end_reason") == "age_end_or_ongoing" and hi >= f.current_g - 1
    combat, caps = _combat(f, lo, hi, foe), _captures(f, lo, hi, foe)
    score = _sign(combat["units_killed"], combat["units_lost"]) + 2 * _sign(len(caps["captured"]), len(caps["lost"]), 1.0)
    return {"scope": "war", "scope_key": e["event_key"], "g_start": lo, "g_end": hi, "title": f"war with player {foe}",
            "outcome": _outcome(score, ongoing) if score else ("ongoing" if ongoing else "partial"),
            "facts": {"opponent": foe, "opponent_name": f.names.get(foe), "declared_by": e["detail"].get("declared_by"),
                      "war_type": e["detail"].get("war_type"), "start_age": e["start_age"], "start_turn": e["start_turn"],
                      "turns": hi - lo + 1, "ongoing": ongoing, **combat, **caps, "score": score,
                      "casus_belli": _casus_belli(f, e, foe), "combatants": _combatants(f, foe, lo, hi),
                      "war_support": _war_support(f, foe, lo, hi), "notable": _notable(f, foe, lo, hi)}}


def goal_phases(con: sqlite3.Connection, game: str) -> list[dict]:
    from . import advisor

    out = []
    for g in advisor.evaluate_goals(con, game):
        prog = g["progress"]
        outcome = advisor.STATUS_OUTCOME.get(g["status"]) or ("success" if prog is not None and prog >= 1 else "ongoing")
        out.append({"scope": "goal", "scope_key": str(g["goal_id"]), "g_start": g["created_g"],
                    "g_end": g.get("resolved_g") or g["current_g"], "title": g["title"], "outcome": outcome,
                    "facts": {k: g[k] for k in ("horizon", "status", "progress", "overdue", "metrics", "strategy_id", "parent_goal_id")}})
    return out


def build(con: sqlite3.Connection, game: str) -> list[dict]:
    f = Facts(con, game)
    out = [p for a in f.ages if (p := age_phase(f, a))]
    out += [war_phase(f, e) for e in f.events("kind='war' AND local=1 AND minor=0 AND precision!='state'")]
    out += goal_phases(con, game)
    ages = [p for p in out if p["scope"] == "age"]
    score = sum({"success": 1, "failure": -1}.get(p["outcome"], 0) for p in ages)
    standings = {label: r for m, label in VP.items() if (r := f.rank(m, f.local, f.current_g, f.current_g - 3))}
    out.insert(0, {"scope": "game", "scope_key": game, "g_start": 1, "g_end": f.current_g, "title": "game",
                   "outcome": _outcome(2 * score if score > 0 else score, True),
                   "facts": {"age_outcomes": {p["scope_key"]: p["outcome"] for p in ages}, "standings_now": standings}})
    return out
