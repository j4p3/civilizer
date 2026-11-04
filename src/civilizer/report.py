"""Render game state as compact markdown: what a coach would read off the screen."""

from __future__ import annotations

from .save import SaveInfo

Y = {
    "YIELD_GOLD": "gold", "YIELD_SCIENCE": "sci", "YIELD_CULTURE": "cul", "YIELD_HAPPINESS": "hap",
    "YIELD_DIPLOMACY": "inf", "YIELD_PRODUCTION": "prod", "YIELD_FOOD": "food",
}


def _y(d: dict | None, keys=("YIELD_GOLD", "YIELD_SCIENCE", "YIELD_CULTURE", "YIELD_HAPPINESS", "YIELD_DIPLOMACY", "YIELD_PRODUCTION", "YIELD_FOOD")) -> str:
    if not d:
        return "-"
    return " ".join(f"{Y.get(k, k)} {_n(d[k])}" for k in keys if k in d and d[k])


def _n(v) -> str:
    if isinstance(v, float):
        return f"{v:+.1f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else str(v)
    return str(v)


def _plain(text: str | None) -> str:
    """Strip Civ markup like [B], [icon:X], [TIP:..]..[/TIP] from localized text."""
    import re

    t = re.sub(r"\[icon:[^\]]*\]", "", text or "")
    t = re.sub(r"\[TIP:[^\]]*\]|\[/?[A-Za-z]+\]|\[n\]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def live_report(s: dict) -> str:
    g, me = s.get("game") or {}, s.get("me") or {}
    L = []
    L.append(f"# Turn {g.get('turn')} · {(g.get('age') or '').replace('AGE_', '').title()} · {me.get('leader')} of {me.get('civilization')}")
    ap = g.get("age_progress") or {}
    if ap:
        pct = 100 * ap["current"] / ap["max"] if ap.get("max") else 0
        L.append(f"Age progress {ap['current']}/{ap['max']} ({pct:.0f}%) · crisis stages at {g.get('crisis_stage_trigger_percent')}% · {g.get('difficulty', '')}")
    L.append("")
    L.append("## Empire")
    L.append(f"- Treasury: **{me.get('gold_balance')} gold**, **{me.get('influence_balance')} influence**")
    L.append(f"- Per turn: {_y(me.get('per_turn'))}")
    L.append(f"- Settlements {me.get('settlements')}/{me.get('settlement_cap')} cap")
    for k, label in (("researching", "Researching"), ("civic", "Civic")):
        r = me.get(k)
        if r:
            L.append(f"- {label}: {r['name']} ({r['progress']}/{r['cost']}, {r['turns_left']} turns)")
    L.append(f"- Government: {me.get('government')} · Policies: {', '.join(me.get('policies') or []) or '-'}")
    if me.get("traditions"):
        L.append(f"- Traditions: {', '.join(me['traditions'])}")
    if me.get("crisis_policies"):
        L.append(f"- Crisis policies: {', '.join(me['crisis_policies'])}")
    if me.get("slots"):
        L.append(f"- Slots: {me['slots']}")
    if me.get("golden_age"):
        L.append(f"- Celebration: {me['golden_age']['turns_left']} turns left")
    rel = me.get("religion") or {}
    if rel.get("name"):
        L.append(f"- Religion: {rel['name']} (holy city {rel.get('holy_city')})")
    um = me.get("unit_maintenance")
    if um:
        L.append(f"- Unit upkeep: {um['total_gold']} gold/turn")
    tr = (s.get("trade") or {}).get("trade_income_total")
    if tr:
        L.append(f"- Income from trade (all settlements): {_y(tr)}")

    L.append("")
    L.append("## Settlements")
    L.append("| Settlement | Type | Pop (u/r/spec) | Yields/turn | Growth | Happy | Producing | Trade |")
    L.append("|---|---|---|---|---|---|---|---|")
    for c in s.get("settlements") or []:
        gr = c.get("growth") or {}
        grow = f"{gr.get('turns_to_growth')}t" + (f" · focus {gr['town_focus']}" if gr.get("town_focus") else "")
        q = c.get("queue") or []
        prod = f"{q[0]['name']} ({q[0]['turns']}t)" if q else "—"
        if len(q) > 1:
            prod += f" +{len(q) - 1}"
        h = c.get("happiness") or {}
        happy = f"{_n(h.get('per_turn'))}" + (" UNREST" if h.get("unrest") else "")
        trade = f"{c.get('trade_route_count') or 0} routes" if c.get("trade_route_count") else "—"
        if c.get("trade_income"):
            trade += f" [{_y(c['trade_income'])}]"
        flags = "".join(
            [" ★" if c.get("capital") else "", " RAZING" if c.get("being_razed") else "", " distant" if c.get("distant_lands") else ""]
        )
        L.append(
            f"| {c['name']}{flags} | {c.get('type')} | {c.get('population')} ({c.get('urban_pop')}/{c.get('rural_pop')}/{c.get('specialists')}) "
            f"| {_y(c.get('yields'))} | {grow} | {happy} | {prod} | {trade} |"
        )
    tf = [(c["name"], c["treasure_fleet"]) for c in s.get("settlements") or [] if c.get("treasure_fleet")]
    if tf:
        L.append("")
        L.append("**Treasure fleets:** " + "; ".join(f"{n}: {('next in ' + str(t['turns_until_next']) + 't') if 'turns_until_next' in t else 'blocked — ' + ' / '.join(t['blocked'])}" for n, t in tf))

    L.append("")
    L.append("## Rivals")
    L.append("| Player | Relationship | War | Per turn | Settl. | Routes |")
    L.append("|---|---|---|---|---|---|")
    for p in s.get("players") or []:
        if not p.get("met") and not p.get("leader"):
            L.append(f"| (unmet #{p['id']}) | | | | | |")
            continue
        war = ""
        if p.get("at_war"):
            ws = p.get("war_support") or {}
            war = f"WAR (support {ws.get('our_support', '?'):+})" if isinstance(ws.get("our_support"), int) else "WAR"
        tr = p.get("trade_routes") or {}
        L.append(
            f"| {p.get('leader')} ({p.get('civilization')}) | {p.get('relationship')}{' · allied' if p.get('allied') else ''} | {war} "
            f"| {_y(p.get('per_turn'), ('YIELD_GOLD', 'YIELD_SCIENCE', 'YIELD_CULTURE', 'YIELD_DIPLOMACY'))} | {p.get('settlements')} | {tr.get('current')}/{tr.get('capacity')} |"
        )

    routes = (s.get("trade") or {}).get("routes")
    if routes:
        L.append("")
        L.append("## Trade routes (commerce screen)")
        fmt = lambda r: f"{r['from']}→{r['to']} [p{r['to_owner']}, {r['domain']}] imports {', '.join(r['imports']) or '-'}"  # noqa: E731
        for r in routes.get("established", []):
            L.append(f"- active: {fmt(r)}")
        for r in routes.get("available", []):
            L.append(f"- AVAILABLE: {fmt(r)}")
        why: dict[str, list[str]] = {}
        for r in routes.get("unavailable", []):
            reasons = ["NO_RESOURCES (destination has no resources to trade)" if w == "NO_RESOURCES" else w for w in r.get("why", [])]
            why.setdefault(", ".join(reasons), []).append(f"{r['from']}→{r['to']}")
        for k, v in why.items():
            L.append(f"- unavailable ({k}): {'; '.join(v[:8])}{' …' if len(v) > 8 else ''}")

    cv = s.get("countdown_victories")
    if cv:
        names = {"VICTORY_ECONOMIC_MODERN": "GDP", "VICTORY_SCIENCE_MODERN": "Innovation",
                 "VICTORY_CULTURE_MODERN": "Tourism", "VICTORY_MILITARY_MODERN": "Dominion"}
        L.append("")
        L.append("## Victory points (victories screen; delta = last turn's gain)")
        for v in cv:
            st = ", ".join(f"{r['player'].split(',')[0]} {r['points']} (+{r['last_turn_delta']})" for r in v["standings"])
            L.append(f"- {names.get(v['victory'], v['victory'])} (target {v['target']}): {st}")

    tri = s.get("triumphs")
    if tri is not None:
        L.append("")
        done = [t for t in tri if t["done"]]
        L.append(f"## Triumphs: {len(done)} complete ({sum(t['major'] for t in done)} major)")
        for t in sorted(tri, key=lambda t: (not t["major"], t["done"], -(t.get("current") or 0) / max(t.get("goal") or 1, 1))):
            if t["done"] and not t["major"]:
                continue
            prog = f"{t.get('current', 0)}/{t.get('goal', '?')}" if not t["done"] else "DONE"
            race = f" · first-only, won by {t['race_winner']}" if t.get("race_winner") else (" · first-only" if t.get("first_only") else "")
            L.append(f"- {'**' if t['major'] else ''}{t['name']}{'**' if t['major'] else ''} [{prog}]{race}: {_plain(t['requirement'])}")
        minor_done = [t["name"] for t in done if not t["major"]]
        if minor_done:
            L.append(f"- minor done: {', '.join(minor_done)}")
    for v in s.get("victories") or []:
        top = ", ".join(f"{r['player']} {r['score']}" for r in v["standings"][:4])
        L.append(f"- {v['name']} (goal {v['standings'][0]['total']}): {top}")

    cs = s.get("city_states") or []
    if cs:
        L.append("")
        L.append("## City-states met")
        L.append(", ".join(f"{c['name']}{' (suzerain: ' + str(c['suzerain']) + ')' if c.get('suzerain') else ''}{' WAR' if c.get('at_war') else ''}" for c in cs))

    units = s.get("units") or []
    if units:
        L.append("")
        L.append(f"## Units ({len(units)})")
        hurt = [u for u in units if u.get("hp") is not None and u.get("hp_max") and u["hp"] < u["hp_max"]]
        idle = [u for u in units if u.get("moves") and u["moves"] > 0]
        by: dict[str, int] = {}
        for u in units:
            by[u["name"]] = by.get(u["name"], 0) + 1
        L.append(", ".join(f"{k}×{v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])))
        if hurt:
            L.append("Damaged: " + ", ".join(f"{u['name']}@{'army' if u.get('in_army') else (u['x'], u['y'])} {u['hp']}/{u['hp_max']}" for u in hurt))
        L.append(f"With moves left: {len(idle)}")
    en = s.get("visible_enemy_units") or []
    if en:
        L.append("")
        L.append(f"## Visible enemy units ({len(en)})")
        L.append(", ".join(f"{u['name']}[p{u['owner']}]@({u['x']},{u['y']}) {u.get('hp')}hp" for u in en[:60]))

    n = (s.get("notifications") or {}).get("items") or []
    if n:
        L.append("")
        L.append("## Notifications")
        for x in n[:25]:
            L.append(f"- {_plain(x['summary']) or x['type']}")
    if s.get("errors"):
        L.append("")
        L.append(f"_extraction errors: {'; '.join(s['errors'])}_")
    return "\n".join(L)


def save_report(info: SaveInfo, gold: list[int] | None = None, influence: list[int] | None = None) -> str:
    L = [f"# {info.save_name} · turn {info.turn} · {info.age} · saved {info.saved_at:%Y-%m-%d %H:%M %Z}" if info.saved_at else f"# {info.save_name}"]
    L.append(f"{info.host_leader} / {info.host_civ} · {info.difficulty} · {info.map_size} · {info.map_name} · game {info.game_guid} · v{info.app_version}")
    L.append("")
    L.append("| slot | leader | civ | human | alive | gold* | influence* |")
    L.append("|---|---|---|---|---|---|---|")
    for p in info.players:
        if p.level != "CIVILIZATION_LEVEL_FULL_CIV":
            continue
        g = gold[p.slot] if gold and p.slot is not None and p.slot < len(gold) else ""
        f = influence[p.slot] if influence and p.slot is not None and p.slot < len(influence) else ""
        L.append(f"| {p.slot} | {p.leader} | {p.civilization} | {'yes' if p.is_human else ''} | {p.alive} | {g} | {f} |")
    if gold:
        L.append("")
        L.append("*gold/influence from the compressed body, indexed by player id.")
    L.append("")
    L.append(f"Note: autosaves are labeled one turn ahead. Loading a save with header turn {info.turn} resumes on screen turn {info.turn - 1 if info.turn else '?'} "
             "(observed 2026-11-02: AutoSave_01_0116 loaded as Game.turn 115, same 1119 gold).")
    return "\n".join(L)
