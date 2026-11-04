"""Exporter into the central store: export.export() + snapshot digests -> data/civilizer.db.

`refresh(seed)` is idempotent: it replaces every exporter-owned row for the game and leaves the advisor
tables alone. The monitor calls it once per new turn.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from . import export, phases, store

DIGESTS = store.ROOT / "data" / "cache" / "digests"
YIELD_METRIC = {"YIELD_GOLD": "gold_per_turn", "YIELD_SCIENCE": "science_per_turn", "YIELD_CULTURE": "culture_per_turn",
                "YIELD_HAPPINESS": "happiness_per_turn", "YIELD_DIPLOMACY": "influence_per_turn",
                "YIELD_PRODUCTION": "production_per_turn", "YIELD_FOOD": "food_per_turn"}
VP_METRIC = {"VICTORY_ECONOMIC_MODERN": "vp_economic", "VICTORY_SCIENCE_MODERN": "vp_science",
             "VICTORY_CULTURE_MODERN": "vp_culture", "VICTORY_MILITARY_MODERN": "vp_military"}
STATS_METRIC = {"cities": "cities", "towns": "towns", "settlement_cap": "settlement_cap", "techs": "techs",
                "land_units": "land_units", "naval_units": "naval_units", "tiles_owned": "tiles_owned",
                "gold_balance": "gold_balance", "science": "science_per_turn", "culture": "culture_per_turn",
                "gold": "gold_per_turn", "production": "production_per_turn", "food": "food_per_turn",
                "happiness": "happiness_per_turn", "influence": "influence_per_turn"}
COMBAT_CLASSES = {"FORMATION_CLASS_LAND_COMBAT", "FORMATION_CLASS_NAVAL", "FORMATION_CLASS_AIR"}


# ---------------------------------------------------------------- snapshot digests

def digest(s: dict) -> dict:
    """The parts of a ~400 KB snapshot the store keeps: per-player metrics and the local player's summary."""
    g, me = s.get("game") or {}, s.get("me") or {}
    pid = me.get("id", g.get("local_player_id", 0))
    m: dict[int, dict[str, float]] = defaultdict(dict)
    mine = m[pid]
    for y, v in (me.get("per_turn") or {}).items():
        if y in YIELD_METRIC and v is not None:
            mine[YIELD_METRIC[y]] = v
    for k in ("gold_balance", "influence_balance", "settlements", "settlement_cap"):
        if me.get(k) is not None:
            mine[k] = me[k]
    cities = s.get("settlements") or []
    if cities:
        mine["population"] = sum(c.get("population") or 0 for c in cities)
        mine["cities"] = sum(c.get("type") == "city" for c in cities)
        mine["towns"] = sum(c.get("type") == "town" for c in cities)
        mine["unrest_settlements"] = sum(bool((c.get("happiness") or {}).get("unrest")) for c in cities)
    routes = (s.get("trade") or {}).get("routes") or {}
    if "established" in routes:
        mine["trade_routes"] = len(routes["established"])
    if s.get("units") is not None:
        mine["military_units"] = sum(u.get("class") in COMBAT_CLASSES for u in s["units"])
    rivals = s.get("players") or []
    mine["wars_active"] = sum(bool(p.get("at_war")) for p in rivals)
    if s.get("triumphs") is not None:
        mine["triumphs_done"] = sum(bool(t.get("done")) for t in s["triumphs"])
    if (g.get("age_progress") or {}).get("current") is not None:
        mine["age_progress"] = g["age_progress"]["current"]
    for p in rivals:
        if not p.get("met"):
            continue
        for y, v in (p.get("per_turn") or {}).items():
            if y in YIELD_METRIC and v is not None:
                m[p["id"]][YIELD_METRIC[y]] = v
        if p.get("settlements") is not None:
            m[p["id"]]["settlements"] = p["settlements"]
    for cv in s.get("countdown_victories") or []:
        name = VP_METRIC.get(cv.get("victory"))
        if not name:
            continue
        if cv.get("target") is not None:
            mine[name + "_target"] = cv["target"]
        for st in cv.get("standings") or []:
            if st.get("id") is not None and st.get("points") is not None:
                m[st["id"]][name] = st["points"]
    for v in s.get("victories") or []:
        name = {"VICTORY_DOMINATION": "domination_score", "VICTORY_SCORE": "score"}.get(v.get("victory"))
        for st in (v.get("standings") or []) if name else []:
            if st.get("id") is not None and st.get("score") is not None:
                m[st["id"]][name] = st["score"]
    lost = [n.get("summary") for n in (s.get("notifications") or {}).get("items") or [] if n.get("type") == "NOTIFICATION_UNIT_LOST"]
    summary = {
        "leader": me.get("leader"), "civilization": me.get("civilization"),
        "researching": me.get("researching"), "civic": me.get("civic"), "government": me.get("government"),
        "policies": me.get("policies") or [], "traditions": me.get("traditions") or [],
        "crisis_policies": me.get("crisis_policies") or [], "ideology": me.get("ideology"), "religion": me.get("religion"),
        "golden_age": me.get("golden_age"), "at_war_with": [p["id"] for p in rivals if p.get("at_war")],
        "age_progress": g.get("age_progress"), "notifications_unit_lost": lost,
        "settlements": [{k: c.get(k) for k in ("name", "type", "population", "capital", "x", "y")} for c in cities],
    }
    return {"game": {k: g.get(k) for k in ("turn", "seed", "age", "local_player_id", "difficulty")},
            "captured_at": s.get("captured_at"), "player_id": pid,
            "metrics": {str(k): v for k, v in m.items()}, "summary": summary}


def load_digests(seed: str) -> list[dict]:
    """Digests of every snapshot on the kept branch (same rule as export.load_snapshots), cached per file."""
    src = export.SNAPSHOTS / seed
    if not src.exists():
        return []
    cache = DIGESTS / seed
    cache.mkdir(parents=True, exist_ok=True)
    kept: list[dict] = []
    for p in sorted(src.glob("T*.json"), key=export._stamp):
        c = cache / p.name
        if c.exists() and c.stat().st_mtime >= p.stat().st_mtime:
            d = json.loads(c.read_text())
        else:
            try:
                d = digest(json.loads(p.read_text()))
            except ValueError:
                continue
            c.write_text(json.dumps(d))
        age, turn = d["game"].get("age"), d["game"].get("turn")
        if age not in export.AGES or turn is None:
            continue
        d["path"], d["_stamp"] = str(p), export._stamp(p)
        kept = [k for k in kept if not (k["game"]["age"] == age and k["game"]["turn"] >= turn)]
        kept.append(d)
    return sorted(kept, key=lambda d: (export.AGES.index(d["game"]["age"]), d["game"]["turn"]))


# ---------------------------------------------------------------- refresh

def refresh(seed: str | None = None, db: Path | None = None, run_export: bool = True) -> dict:
    out = export.export(seed) if run_export else export.GAMES / seed
    game = json.loads((out / "game.json").read_text())
    seed = game["game"]["seed"]
    digests = load_digests(seed)
    clock = export.Clock({a["age"]: a["turns"] for a in game["ages"]})
    local = game["game"].get("local_player_id") or 0

    with store.tx(db) as con:
        for t in ("ages", "players", "turns", "metrics", "events", "phases"):
            con.execute(f"DELETE FROM {t} WHERE game=?", (seed,))
        last = digests[-1] if digests else None
        cur_age = last["game"]["age"] if last else game["ages"][-1]["age"]
        cur_turn = last["game"]["turn"] if last else clock.lengths[cur_age]
        gm = game["game"]
        con.execute("INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            seed, gm.get("guid"), gm.get("leader"), gm.get("civilization"), gm.get("difficulty"), gm.get("map"),
            gm.get("map_size"), gm.get("speed"), gm.get("app_version"), local, cur_age, cur_turn,
            clock.offset[cur_age] + cur_turn, ((last or {}).get("summary", {}).get("age_progress") or {}).get("max"), store.now()))
        for i, a in enumerate(game["ages"]):
            con.execute("INSERT INTO ages VALUES (?,?,?,?,?)", (seed, a["age"], i, a["turns"], a["g_offset"]))
        for p in game["players"]:
            con.execute("INSERT INTO players VALUES (?,?,?,?,?,?)", (seed, p["id"], p.get("leader"), p.get("leader_type"),
                                                                    int(bool(p.get("is_human"))), json.dumps(p.get("civilizations") or {})))

        rows: dict[tuple, tuple] = {}

        def put(age, turn, pid, metric, value, source, vis=None):
            if value is None or age not in clock.offset or turn is None:
                return
            try:
                value = float(value)
            except (TypeError, ValueError):
                return
            g = clock.offset[age] + int(turn)
            vis = vis or ("visible" if pid == local else "omniscient")
            rows[(g, int(pid), metric)] = (seed, g, age, int(turn), int(pid), metric, value, source, vis)

        # lowest priority first: captures, logs, then snapshots (on-screen values win)
        for vt, ages in game["series"]["victory"].items():
            name = VP_METRIC.get(vt)
            for age, per in ages.items() if name else []:
                for pid, hist in per.items():
                    for i, v in enumerate(hist or []):
                        put(age, i + 1, int(pid), name, v, "capture")
        for age, srows in game["series"]["stats"].items():
            for r in srows:
                pid, t = r.get("player"), r.get("turn")
                if not isinstance(pid, (int, float)) or not isinstance(t, (int, float)):
                    continue
                for col, name in STATS_METRIC.items():
                    put(age, t, int(pid), name, r.get(col), "log:Player_Stats")
                if isinstance(r.get("cities"), (int, float)) and isinstance(r.get("towns"), (int, float)):
                    put(age, t, int(pid), "settlements", r["cities"] + r["towns"], "log:Player_Stats")
                if isinstance(r.get("urban_pop"), (int, float)) and isinstance(r.get("rural_pop"), (int, float)):
                    put(age, t, int(pid), "population", r["urban_pop"] + r["rural_pop"], "log:Player_Stats")
        for d in digests:
            a, t = d["game"]["age"], d["game"]["turn"]
            for pid, ms in d["metrics"].items():
                for name, v in ms.items():
                    put(a, t, int(pid), name, v, "snapshot", "visible")
            con.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?)", (seed, clock.offset[a] + t, a, t, d.get("captured_at"),
                                                                     d["path"], json.dumps(d["summary"])))

        events = [e for e in game["events"]]
        snap_ages = {d["game"]["age"] for d in digests}
        # snapshot spans are exact for the local player; drop the coarser capture spans they replace
        events = [e for e in events if not (e["kind"] == "government" and e["players"] == [local]
                                            and (e.get("start") or {}).get("age") in snap_ages)]
        events += _snapshot_spans(digests, clock, local)
        _cumulative(events, put, clock, cur_age, cur_turn)
        con.executemany("INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?,?)", rows.values())

        from .rules import text_index

        names = text_index()
        seen: dict[str, int] = {}
        for e in events:
            e["subject"] = _display(e["subject"], names)
            k = store.event_key(e)
            seen[k] = seen.get(k, 0) + 1
            if seen[k] > 1:
                k = f"{k}-{seen[k]}"
            e["event_key"] = k
            s, en = e.get("start") or {}, e.get("end") or {}
            con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                seed, k, e["kind"], e["subject"], s.get("g"), s.get("age"), s.get("turn"), s.get("lo"), s.get("hi"),
                en.get("g"), en.get("age"), en.get("turn"), json.dumps(e["players"]), int(local in e["players"]),
                int(bool(e.get("minor"))), e["precision"], e["source"], json.dumps(e.get("detail") or {}, default=export._json_default)))

        for p in phases.build(con, seed):
            con.execute("INSERT OR REPLACE INTO phases VALUES (?,?,?,?,?,?,?,?)", (
                seed, p["scope"], p["scope_key"], p["g_start"], p["g_end"], p["title"], p["verdict"], json.dumps(p["facts"], default=str)))
        store.set_meta(con, "last_refresh", {"game": seed, "at": store.now(), "g": clock.offset[cur_age] + cur_turn})
    return {"game": seed, "age": cur_age, "turn": cur_turn, "metrics": len(rows), "events": len(events), "turns": len(digests)}


def _display(subject: str, names: dict[str, str]) -> str:
    """GOVERNMENT_X / LEGACY_X(12) / BELIEF_X -> in-game name where the subject is a raw type id."""
    m = re.fullmatch(r"([A-Z][A-Z0-9_]+)(\(\d+\))?", subject or "")
    if not m:
        return subject
    return names.get(f"LOC_{m.group(1)}_NAME", subject)


def _snapshot_spans(digests: list[dict], clock: export.Clock, local: int) -> list[dict]:
    """Local player's government and each slotted policy/tradition as exact spans over observed turns."""
    out: list[dict] = []
    open_: dict[tuple, dict] = {}
    prev = None
    for d in digests:
        a, t = d["game"]["age"], d["game"]["turn"]
        here = clock.at(a, t)
        s = d["summary"]
        cur = set()
        if s.get("government"):
            cur.add(("government", s["government"], "government"))
        for cat in ("policies", "traditions", "crisis_policies"):
            for name in s.get(cat) or []:
                cur.add(("policy", name, cat.rstrip("s").replace("policie", "policy")))
        if prev and prev["game"]["age"] != a:  # age change closes everything
            for k in list(open_):
                open_.pop(k)["end"] = clock.at(prev["game"]["age"], prev["game"]["turn"])
        for k in [k for k in open_ if k not in cur]:
            open_.pop(k)["end"] = here
        for k in cur:
            if k not in open_:
                kind, name, cat = k
                e = export._ev(kind, here, None, [local], name, "snapshot", category=cat)
                open_[k] = e
                out.append(e)
        prev = d
    for e in open_.values():
        e["end"] = clock.at(prev["game"]["age"], prev["game"]["turn"]) if prev else None
        e["detail"]["ongoing"] = True
    return out


def _cumulative(events: list[dict], put, clock: export.Clock, cur_age: str, cur_turn: int) -> None:
    """units_lost_total / units_killed_total / settlements_captured_total per player, at each turn they change."""
    series: dict[tuple[int, str], dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for e in events:
        s = e.get("start") or {}
        if not s:
            continue
        if e["kind"] == "unit_killed":
            d = e.get("detail") or {}
            series[(d.get("victim"), "units_lost_total")][s["g"]] += 1
            series[(d.get("killer"), "units_killed_total")][s["g"]] += 1
        elif e["kind"] == "settlement_captured":
            series[((e.get("detail") or {}).get("captured_by"), "settlements_captured_total")][s["g"]] += 1
    ages = sorted(clock.offset.items(), key=lambda kv: kv[1])
    for (pid, metric), per_g in series.items():
        if pid is None:
            continue
        total = 0
        for g in sorted(per_g):
            total += per_g[g]
            age = max((a for a, off in ages if off < g), key=lambda a: clock.offset[a])
            put(age, g - clock.offset[age], pid, metric, total, "derived")
        put(cur_age, cur_turn, pid, metric, total, "derived")


def latest_seed() -> str | None:
    snaps = [d for d in export.SNAPSHOTS.glob("*") if d.is_dir()] if export.SNAPSHOTS.exists() else []
    if not snaps:
        return None
    return max(snaps, key=lambda d: max((p.stat().st_mtime for p in d.glob("T*.json")), default=0)).name
