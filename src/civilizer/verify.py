"""Cross-check a live snapshot against independent sources (game logs, save file).

The snapshot comes from the UI API; the logs are written by the game core; the save
body is the serialized database. Agreement across all three is evidence that the
snapshot shows what the game (and its screen) shows.
"""

from __future__ import annotations

from . import logs, rules, save, timeline


def _close(a, b, tol=0.6) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def check(s: dict) -> list[tuple[str, object, object, bool]]:
    rows: list[tuple[str, object, object, bool]] = []
    add = lambda name, snap, other: rows.append((name, snap, other, _close(snap, other)))  # noqa: E731
    turn, me = s["game"]["turn"], s["me"]
    pid = me["id"]
    st = s.get("settlements") or []

    # 1. Player_Stats log row for the same turn (written by the game core at turn start)
    stat = next((r for r in logs.history("Player_Stats", player=pid) if r["turn"] == turn), None)
    if stat:
        add("cities (log)", sum(c["type"] == "city" for c in st), stat["cities"])
        add("towns (log)", sum(c["type"] == "town" for c in st), stat["towns"])
        add("settlement cap (log)", me["settlement_cap"], stat["settlement_cap"])
        add("urban pop total (log)", sum(c["urban_pop"] for c in st), stat["urban_pop"])
        add("rural pop total (log)", sum(c["rural_pop"] for c in st), stat["rural_pop"])
        add("gold balance (log)", me["gold_balance"], stat["gold_balance"])
        for y, col in [("YIELD_GOLD", "gold"), ("YIELD_SCIENCE", "science"), ("YIELD_CULTURE", "culture"),
                       ("YIELD_PRODUCTION", "production"), ("YIELD_FOOD", "food"),
                       ("YIELD_HAPPINESS", "happiness"), ("YIELD_DIPLOMACY", "influence")]:
            add(f"{col}/turn (log)", me["per_turn"][y], stat[col])
        us = s.get("units") or []
        if us and "class" in us[0]:
            combat = lambda dom: sum(u["class"] in ("FORMATION_CLASS_LAND_COMBAT", "FORMATION_CLASS_NAVAL") and u["domain"] == dom for u in us)  # noqa: E731
            add("land combat units (log)", combat("DOMAIN_LAND"), stat["land_units"])
            add("naval combat units (log)", combat("DOMAIN_SEA"), stat["naval_units"])
    else:
        rows.append((f"Player_Stats row for turn {turn}", None, "missing", False))

    # 2. per-city build queue + production from CityBuildQueue log
    text = rules.text_index()
    byname = {c["name"]: c for c in st}
    for r in logs.history("CityBuildQueue"):
        if r.get("Game Turn") != turn or r.get("Player") != pid:
            continue
        name = text.get(r["City"], r["City"])
        c = byname.get(name)
        if not c:
            rows.append((f"city {name} (log)", None, "not in snapshot", False))
            continue
        q = c.get("queue") or []
        add(f"{name}: producing (log)", q[0]["type"] if q else None, r["Current Item"])
        add(f"{name}: production/turn (log)", c["yields"]["YIELD_PRODUCTION"], r["Production Added"])

    # 3. save body: the autosave labeled turn+1 holds this turn's state
    try:
        _, tl = timeline.current_game()
        sv = next((e.info for e in reversed(tl) if e.info.turn == turn + 1 and not e.abandoned), None)
        if sv:
            body = save.decompress(sv.path.read_bytes())
            gold = save.body_balances(body, "GOLD")
            add(f"gold vs {sv.path.name} body (start of turn; differs if you bought things since)", round(me["gold_balance"]), gold[pid])
    except FileNotFoundError:
        pass

    # 4. internal consistency: production comes only from settlements
    add("sum of city production vs empire", round(sum(c["yields"]["YIELD_PRODUCTION"] for c in st), 1), me["per_turn"]["YIELD_PRODUCTION"])
    return rows


def other_sources(s: dict) -> dict:
    """Empire yield not attributable to settlements: the yields report's "Other" row
    (city-state bonuses, traditions, etc.). Informational, not an error."""
    st, pt = s.get("settlements") or [], s["me"]["per_turn"]
    return {y: round(pt[y] - sum(c["yields"].get(y, 0) for c in st), 1) for y in ("YIELD_SCIENCE", "YIELD_CULTURE", "YIELD_GOLD", "YIELD_DIPLOMACY")}


def render(rows) -> str:
    ok = sum(r[3] for r in rows)
    out = [f"{ok}/{len(rows)} checks agree", "", "| check | snapshot | other source | |", "|---|---|---|---|"]
    for name, a, b, good in rows:
        out.append(f"| {name} | {a} | {b} | {'ok' if good else 'DIFF'} |")
    return "\n".join(out)
