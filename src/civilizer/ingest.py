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
DIGEST_VERSION = 4  # bump when digest() gains fields: cached digests of older versions are recomputed


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
    income = (s.get("trade") or {}).get("trade_income_total")
    if isinstance(income, dict):
        mine["trade_income_per_turn"] = income.get("YIELD_GOLD", 0)
    if s.get("units") is not None:
        mine["military_units"] = sum(u.get("class") in COMBAT_CLASSES for u in s["units"])
    rivals = s.get("players") or []
    mine["wars_active"] = sum(bool(p.get("at_war")) for p in rivals)
    if s.get("triumphs") is not None:
        mine["triumphs_done"] = sum(bool(t.get("done")) for t in s["triumphs"])
    if (g.get("age_progress") or {}).get("current") is not None:
        mine["age_progress"] = g["age_progress"]["current"]
    city_states = s.get("city_states")
    if isinstance(city_states, list):  # met city-states and their suzerains, as the player sees them
        ids = {me.get("leader"): pid} | {p.get("leader"): p["id"] for p in rivals if p.get("leader")}
        held: dict[int, int] = defaultdict(int)
        for c in city_states:
            z = c.get("suzerain")
            zid = z if isinstance(z, int) else ids.get(z)  # older snapshots wrote the suzerain's id, newer ones its name
            if zid is not None and zid >= 0:
                held[zid] += 1
        mine["suzerainties"] = held.get(pid, 0)
        for p in rivals:
            if p.get("met"):
                m[p["id"]]["suzerainties"] = held.get(p["id"], 0)
    for p in rivals:
        if not p.get("met"):
            continue
        if p.get("relationship_level") is not None:
            m[p["id"]]["relationship"] = p["relationship_level"]
        for y, v in (p.get("per_turn") or {}).items():
            if y in YIELD_METRIC and v is not None:
                m[p["id"]][YIELD_METRIC[y]] = v
        if p.get("settlements") is not None:
            m[p["id"]]["settlements"] = p["settlements"]
        # supporters minus opposers of the war between us (model-diplo-ribbon.js), from the local player's side
        if isinstance((p.get("war_support") or {}).get("our_support"), (int, float)):
            m[p["id"]]["war_support"] = p["war_support"]["our_support"]
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
        "settlements": [{k: c.get(k) for k in ("name", "type", "population", "capital", "being_razed", "x", "y")} for c in cities],
        "city_states": [{k: c.get(k) for k in ("id", "name", "suzerain", "at_war")} for c in city_states]
        if isinstance(city_states, list) else None,
        "relationships": {str(p["id"]): {"leader": p.get("leader"), "relationship": p.get("relationship")}
                          for p in rivals if p.get("met") and p.get("relationship")},
        # None when the snapshot predates the claimed_victories section: outcome unknown, not "nobody won"
        "claimed_victories": s.get("claimed_victories"),
    }
    return {"v": DIGEST_VERSION, "game": {k: g.get(k) for k in ("turn", "seed", "age", "local_player_id", "difficulty")},
            "captured_at": s.get("captured_at"), "player_id": pid,
            "metrics": {str(k): v for k, v in m.items()}, "summary": summary}


def outcome(claimed: list[dict] | None) -> tuple[str | None, list[dict]]:
    """(status, won victories) from a snapshot's claimed_victories. A place-1 claim is a won victory, and every victory
    type the game defines ends the game. None (snapshot predates the capture) gives status None: unknown."""
    won = [{"victory": v.get("victory"), "players": v.get("players") or []} for v in claimed or [] if v.get("place") == 1]
    return (None if claimed is None else "over" if won else "in_progress"), won


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
        d = None
        if c.exists() and c.stat().st_mtime >= p.stat().st_mtime:
            d = json.loads(c.read_text())
            if d.get("v") != DIGEST_VERSION:
                d = None
        if d is None:
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
        for t in ("ages", "players", "turns", "metrics", "events", "phases", "combat_rewards"):
            con.execute(f"DELETE FROM {t} WHERE game=?", (seed,))
        last = digests[-1] if digests else None
        cur_age = last["game"]["age"] if last else game["ages"][-1]["age"]
        cur_turn = last["game"]["turn"] if last else clock.lengths[cur_age]
        gm = game["game"]
        status, won = outcome(((last or {}).get("summary") or {}).get("claimed_victories"))
        con.execute("INSERT OR REPLACE INTO games (game, guid, leader, civilization, difficulty, map, map_size, speed, app_version, "
                    "local_player_id, current_age, current_turn, current_g, age_turns_max, status, victories, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            seed, gm.get("guid"), gm.get("leader"), gm.get("civilization"), gm.get("difficulty"), gm.get("map"),
            gm.get("map_size"), gm.get("speed"), gm.get("app_version"), local, cur_age, cur_turn,
            clock.offset[cur_age] + cur_turn, ((last or {}).get("summary", {}).get("age_progress") or {}).get("max"),
            status, json.dumps(won), store.now()))
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
        events += _relationship_events(digests, clock, local)
        spent = influence_spent(digests)
        _attach_influence(events, spent, local)
        _tag_capitals(events, game["players"])
        _cumulative(events, put, clock, cur_age, cur_turn)
        _diplomacy_metrics(events, spent, put, clock, cur_age, cur_turn, local)
        for d in digests:  # on-screen suzerainties (met city-states) win over the log-derived counts
            for pid, ms in d["metrics"].items():
                if "suzerainties" in ms:
                    put(d["game"]["age"], d["game"]["turn"], int(pid), "suzerainties", ms["suzerainties"], "snapshot", "visible")
        rewards = game.get("combat_rewards") or []
        _combat_metrics(rewards, game.get("unexplained_gold") or [], put, clock, cur_age, cur_turn, local)
        _insert_rewards(con, seed, rewards)
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
                en.get("g"), en.get("age"), en.get("turn"), json.dumps(e["players"]), int(_is_local(e, local)),
                int(bool(e.get("minor"))), e["precision"], e["source"], json.dumps(e.get("detail") or {}, default=export._json_default)))

        for p in phases.build(con, seed):
            con.execute("INSERT OR REPLACE INTO phases VALUES (?,?,?,?,?,?,?,?)", (
                seed, p["scope"], p["scope_key"], p["g_start"], p["g_end"], p["title"], p["outcome"], json.dumps(p["facts"], default=str)))
        store.set_meta(con, "last_refresh", {"game": seed, "at": store.now(), "g": clock.offset[cur_age] + cur_turn})
    return {"game": seed, "age": cur_age, "turn": cur_turn, "metrics": len(rows), "events": len(events), "turns": len(digests)}


def _is_local(e: dict, local: int) -> bool:
    """The local player is involved and would know: a rival's espionage against the player is hidden from the target
    (it may never be discovered), so it stays out of the player's timeline and briefing."""
    d = e.get("detail") or {}
    if e["kind"] == "diplomacy_action" and d.get("group") == "espionage" and d.get("initiator") != local:
        return False
    return local in e["players"]


def _display(subject: str, names: dict[str, str]) -> str:
    """GOVERNMENT_X / LEGACY_X(12) / BELIEF_X -> in-game name where the subject is a raw type id."""
    m = re.fullmatch(r"([A-Z][A-Z0-9_]+)(\(\d+\))?", subject or "")
    if not m:
        return subject
    return names.get(f"LOC_{m.group(1)}_NAME", subject)


def _tag_capitals(events: list[dict], players: list[dict]) -> None:
    """settlement_captured: capital=True when the settlement carries the losing civ's capital name (Civilizations
    CapitalName). Foreign settlements have no capital flag on screen; a renamed or moved capital is missed."""
    from .rules import capital_names

    caps = capital_names()
    civ = {(p["id"], a): c for p in players for a, c in (p.get("civilizations") or {}).items()}
    for e in events:
        if e["kind"] == "settlement_captured":
            frm, age = e["detail"].get("from_player"), (e.get("start") or {}).get("age")
            if caps.get(civ.get((frm, age))) == e["subject"]:
                e["detail"]["capital"] = True


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


def _combat_metrics(rewards: list[dict], unexplained: list[dict], put, clock: export.Clock, cur_age: str, cur_turn: int,
                    local: int) -> None:
    """Cumulative combat_* / kill_gold_total / count metrics at each turn they change and at the current turn, plus the
    per-turn unexplained_gold and its running unexplained_gold_total (positive turns only). Unexplained gold is never
    attributed to a combat kind. The local player gets every combat metric and unexplained_gold_total (0 from the first
    turn) so goals can use them before the first reward."""
    from . import combat

    first = next(((a, 1) for a in export.AGES if clock.lengths.get(a)), None)
    gains: dict[int, list[tuple[int, str, int, float]]] = defaultdict(list)
    for r in unexplained:
        put(r["age"], r["turn"], r["player"], "unexplained_gold", r["value"], "log:Player_Treasury")
        if r["age"] in clock.offset and r["turn"] is not None:
            gains[r["player"]].append((clock.offset[r["age"]] + int(r["turn"]), r["age"], int(r["turn"]), r["value"]))
    for pid in set(gains) | ({local} if first else set()):
        if pid == local and first:
            put(*first, pid, "unexplained_gold_total", 0, "derived")
        total = 0.0
        for _, age, turn, v in sorted(gains.get(pid, [])):
            if v > 0:
                total += v
                put(age, turn, pid, "unexplained_gold_total", round(total, 2), "derived")
        put(cur_age, cur_turn, pid, "unexplained_gold_total", round(total, 2), "derived")
    at = {e["g"]: (e["age"], e["turn"]) for e in rewards}
    inc = combat.cumulative(rewards)
    if first:
        for metric in store.METRICS:
            if store.METRICS[metric][3].startswith("derived from combat_rewards") and (local, metric) not in inc:
                put(*first, local, metric, 0, "derived")
                put(cur_age, cur_turn, local, metric, 0, "derived")
    for (pid, metric), per_g in inc.items():
        total = 0.0
        if pid == local and first:
            put(*first, pid, metric, 0, "derived")
        for g in sorted(per_g):
            total += per_g[g]
            put(*at[g], pid, metric, round(total, 2), "derived")
        put(cur_age, cur_turn, pid, metric, round(total, 2), "derived")


def influence_spent(digests: list[dict]) -> list[tuple[str, int, float]]:
    """[(age, turn, influence)]: the local player's influence bank at turn t plus that turn's influence income minus the
    bank at turn t+1, for consecutive snapshots in one age. Positive: spent that turn (befriending, endeavors,
    sanctions, responses, support); negative: gained beyond the per-turn yield. Not attributed to an action."""
    out = []
    for a, b in zip(digests, digests[1:]):
        ga, gb = a["game"], b["game"]
        if ga.get("age") != gb.get("age") or gb.get("turn") != (ga.get("turn") or 0) + 1:
            continue
        pid = str(a.get("player_id", 0))
        ma, mb = a["metrics"].get(pid) or {}, b["metrics"].get(str(b.get("player_id", 0))) or {}
        bal, inc, nxt = ma.get("influence_balance"), ma.get("influence_per_turn"), mb.get("influence_balance")
        if None in (bal, inc, nxt):
            continue
        out.append((ga["age"], ga["turn"], round(bal + inc - nxt, 2)))
    return out


def _attach_influence(events: list[dict], spent: list[tuple[str, int, float]], local: int) -> None:
    """The turn's influence spending on the local player's diplomacy actions started that turn (a turn total, shared by
    every action started that turn: not split between them)."""
    by_turn = {(a, t): v for a, t, v in spent}
    for e in events:
        s, d = e.get("start") or {}, e.get("detail") or {}
        if e["kind"] in ("diplomacy_action", "befriend") and d.get("initiator") == local and e["precision"] == "exact":
            v = by_turn.get((s.get("age"), s.get("turn")))
            if v is not None:
                d["turn_influence_spent"] = v


def _relationship_events(digests: list[dict], clock: export.Clock, local: int) -> list[dict]:
    """A met rival's attitude toward the local player changed level (Hostile .. Helpful) between consecutive snapshots."""
    out, prev = [], {}
    for d in digests:
        a, t = d["game"]["age"], d["game"]["turn"]
        rel = (d.get("summary") or {}).get("relationships") or {}
        for pid, r in rel.items():
            old = prev.get(pid)
            if old and old["relationship"] != r["relationship"]:
                out.append(export._ev("relationship", clock.at(a, t), clock.at(a, t), [local, int(pid)],
                                      f"{r.get('leader') or 'player ' + pid}: {old['relationship']} -> {r['relationship']}",
                                      "snapshot", previous=old["relationship"], relationship=r["relationship"]))
            prev[pid] = r
    return out


def _diplomacy_metrics(events: list[dict], spent: list[tuple[str, int, float]], put, clock: export.Clock, cur_age: str,
                       cur_turn: int, local: int) -> None:
    """Diplomacy counts per player (diplomacy.metric_series) at each turn they change and at the current turn, the local
    player's per-turn influence_spent and its running total of positive turns. The local player gets every count from
    0 at the first turn so goals can use them before the first event."""
    from . import diplomacy

    first = next(((a, 1) for a in export.AGES if clock.lengths.get(a)), None)
    ages = sorted(clock.offset.items(), key=lambda kv: kv[1])

    def at_g(g: int) -> tuple[str, int]:
        age = max((a for a, off in ages if off < g), key=lambda a: clock.offset[a])
        return age, g - clock.offset[age]

    series = diplomacy.metric_series(events, clock, cur_age, cur_turn)
    if first:
        for metric in DIPLOMACY_METRICS:
            if (local, metric) not in series:
                put(*first, local, metric, 0, "derived")
                put(cur_age, cur_turn, local, metric, 0, "derived")
    for (pid, metric), per_g in series.items():
        if pid == local and first and metric != "independents_met":
            put(*first, pid, metric, 0, "derived")
        for g, v in sorted(per_g.items()):
            put(*at_g(g), pid, metric, v, "derived")
    total = 0.0
    for age, turn, v in spent:
        put(age, turn, local, "influence_spent", v, "snapshot", "visible")
        if v > 0:
            total += v
            put(age, turn, local, "influence_spent_total", round(total, 2), "derived", "visible")
    if spent:
        put(cur_age, cur_turn, local, "influence_spent_total", round(total, 2), "derived", "visible")


DIPLOMACY_METRICS = ("suzerainties", "suzerainties_total", "befriend_actions_active", "diplomatic_actions_started_total",
                     "independents_met")


def _insert_rewards(con, seed: str, rewards: list[dict]) -> None:
    cols = ("kind", "turn", "age", "player_id", "x", "y", "unit", "target", "modifier", "yield")
    seen: dict[str, int] = {}
    for e in rewards:
        k = store.hashed("|".join(str(e.get(c)) for c in cols))
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:
            k = f"{k}-{seen[k]}"
        detail = {c: e[c] for c in ("modifier", "granted_by", "victim_strength", "city_state_type", "base_amount")
                  if e.get(c) is not None}
        con.execute("INSERT INTO combat_rewards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            seed, k, e["g"], e["age"], e["turn"], e["player_id"], e["kind"], e.get("yield"), e.get("amount"), e["basis"],
            e.get("x"), e.get("y"), e.get("unit"), e.get("target"), e.get("target_owner"), e["source"], json.dumps(detail)))


def latest_seed() -> str | None:
    snaps = [d for d in export.SNAPSHOTS.glob("*") if d.is_dir()] if export.SNAPSHOTS.exists() else []
    if not snaps:
        return None
    return max(snaps, key=lambda d: max((p.stat().st_mtime for p in d.glob("T*.json")), default=0)).name
