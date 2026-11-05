"""Combat-derived rewards: yields a player gains from kills, pillage/coastal raids, trade-route plunder and
dispersing independent powers, rebuilt from the game's own logs plus the rules in its data XML.

What the game writes (all in Logs/, archived by `civilizer archive-logs`):
  - CombatLog: every combat with player ids, unit types, base strengths and who was destroyed. Kill rewards
    (EFFECT_ADJUST_UNIT_POST_COMBAT_YIELD: Privateer gold, Yi Sun-sin science, Edward Teach gold, Lakshmibai /
    Gilgamesh influence, Napoleon culture, Cocorico, Merry Life...) are computed from it: PercentDefeatedStrength
    of the defeated unit's logged strength, or PercentDefeatedUnitCost of its production cost. basis='rules'.
  - Game_Gossip: GOSSIP_PILLAGE (pillager, plot), GOSSIP_PLUNDER_TRADE_ROUTE (plunderer, plot),
    GOSSIP_INDEPENDENT_DISPERSE (disperser, plot, unit). Independents: DISPERSE TRIBE (tribe type, camp plot),
    which gives the reward's yield type (CityStateTypes.YieldType). No amounts are logged for these.
  - Player_Treasury: gold balance and gold yield at each turn start, so the treasury change a turn's per-turn income
    does not explain is known. On turns without a logged purchase (CityPurchase logs no prices), that change minus
    the turn's exact combat gold (rules, notification) is reported per player and turn as unexplained gold. It is
    never attributed to an event: it can be pillage, plunder or dispersal gold, but also deals, treasure fleets,
    narrative events or upgrades. The advisor interprets it.
  - Live snapshots: NOTIFICATION_DISPERSE_IP_REWARD states the exact dispersal reward, but it expires at the end of
    the turn, so the per-turn snapshot rarely sees it. basis='notification' when it does.

Not logged anywhere civilizer reads: pillage / coastal-raid / plunder yield amounts and yield types, dispersal
amounts (the XML base is scaled by the game), healing, captured units (Corsair, Edward Teach, Pirate Republic
settler capture), mementos and great-person kill rewards. Pillage, plunder and dispersal events without a
notification are kept with amount NULL (basis='unknown'); no amount is estimated for them.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

from . import rules

CACHE = Path(__file__).resolve().parents[2] / "data" / "cache" / "combat_rules.json"
CACHE_VERSION = 1
POST_COMBAT = "EFFECT_ADJUST_UNIT_POST_COMBAT_YIELD"
ATTACH = "EFFECT_ATTACH_MODIFIERS"
GRANT_ABILITY = "EFFECT_UNIT_ADJUST_ABILITY"
# requirement types a kill rule may carry that this module can evaluate from CombatLog + the units table
KNOWN_REQS = {"REQUIREMENT_UNIT_DOMAIN_MATCHES", "REQUIREMENT_UNIT_TAG_MATCHES",
              "REQUIREMENT_OPPONENT_UNIT_DOMAIN_MATCHES", "REQUIREMENT_OPPONENT_UNIT_TAG_MATCHES"}
YIELD_SHORT = {"YIELD_GOLD": "gold", "YIELD_SCIENCE": "science", "YIELD_CULTURE": "culture", "YIELD_DIPLOMACY": "influence",
               "YIELD_PRODUCTION": "production", "YIELD_FOOD": "food", "YIELD_HAPPINESS": "happiness"}


# ---------------------------------------------------------------- rules from the game's data XML

def _modifier(el: ET.Element) -> dict:
    args = {a.get("name"): (a.text or "").strip() for a in el.findall("Argument")}
    reqs = []
    for r in el.findall("SubjectRequirements/Requirement"):
        reqs.append({"type": r.get("type"), "args": {a.get("name"): (a.text or "").strip() for a in r.findall("Argument")},
                     "inverse": r.get("inverse") == "true"})
    return {"id": el.get("id"), "effect": el.get("effect"), "collection": el.get("collection"), "args": args, "reqs": reqs,
            "owner_reqs": bool(el.findall("OwnerRequirements/Requirement"))}


def _scan(files: list[Path]) -> dict:
    t: dict[str, list[dict]] = defaultdict(list)
    mods: dict[str, dict] = {}
    want = {"Units", "Unit_Stats", "Unit_Costs", "TypeTags", "UnitClass_Abilities", "UnitAbilities", "UnitAbilityModifiers",
            "TraitModifiers", "LeaderTraits", "CivilizationTraits", "TraditionModifiers", "Traditions", "Independents",
            "CityStateTypes"}
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for el in root.iter():  # GameEffects files use xmlns="GameEffects"
            if isinstance(el.tag, str) and "}" in el.tag:
                el.tag = el.tag.split("}", 1)[1]
        for tbl in root:
            if tbl.tag in want:
                t[tbl.tag] += [dict(r.attrib) for r in tbl.findall("Row")]
        for m in root.iter("Modifier"):
            if m.get("id") and m.get("effect"):
                mods[m.get("id")] = _modifier(m)
    return {"tables": t, "mods": mods}


def build_rules(files: list[Path] | None = None) -> dict:
    """Units (domain, tags, strength, cost), kill-reward rules with how they are granted, and independents."""
    s = _scan(files if files is not None else rules._data_files())
    t, mods = s["tables"], s["mods"]
    units: dict[str, dict] = {}
    for r in t["Units"]:
        if r.get("UnitType"):
            units.setdefault(r["UnitType"], {"tags": []}).update(
                {k: r[a] for k, a in (("domain", "Domain"), ("core_class", "CoreClass"), ("formation", "FormationClass")) if r.get(a)})
    for r in t["Unit_Stats"]:
        if r.get("UnitType") in units and r.get("Combat"):
            units[r["UnitType"]]["combat"] = int(r["Combat"])
    for r in t["Unit_Costs"]:
        if r.get("UnitType") in units and r.get("YieldType", "YIELD_PRODUCTION") == "YIELD_PRODUCTION" and r.get("Cost"):
            units[r["UnitType"]]["cost"] = int(r["Cost"])
    for r in t["TypeTags"]:
        if r.get("Type") in units and r.get("Tag") and r["Tag"] not in units[r["Type"]]["tags"]:
            units[r["Type"]]["tags"].append(r["Tag"])

    inactive = {r["UnitAbilityType"] for r in t["UnitAbilities"] if r.get("Inactive") == "true"}
    ability_class = defaultdict(set)
    for r in t["UnitClass_Abilities"]:
        ability_class[r.get("UnitAbilityType")].add(r.get("UnitClassType"))

    # who attaches each modifier: trait (leader / civilization), tradition, unit ability, or a parent modifier
    granted_by = defaultdict(list)
    for r in t["TraitModifiers"]:
        granted_by[r.get("ModifierId")].append(("trait", r.get("TraitType"), []))
    for r in t["TraditionModifiers"]:
        granted_by[r.get("ModifierId")].append(("tradition", r.get("TraditionType"), []))
    for r in t["UnitAbilityModifiers"]:
        granted_by[r.get("ModifierId")].append(("ability", r.get("UnitAbilityType"), []))
    for m in mods.values():
        if m["effect"] == ATTACH and m["args"].get("ModifierId"):
            granted_by[m["args"]["ModifierId"]].append(("modifier", m["id"], m["reqs"]))
    trait_owner = defaultdict(list)
    for r in t["LeaderTraits"]:
        trait_owner[r.get("TraitType")].append(("leader", r.get("LeaderType")))
    for r in t["CivilizationTraits"]:
        trait_owner[r.get("TraitType")].append(("civilization", r.get("CivilizationType")))

    def grants(mid: str, depth: int = 0) -> list[dict]:
        """Every way modifier mid reaches a unit: [{kind, value, reqs}] with the killer requirements of parents."""
        out = []
        for kind, val, reqs in granted_by.get(mid, []):
            if kind == "modifier" and depth < 3:
                parent = mods.get(val) or {}
                if parent.get("owner_reqs"):
                    continue
                for g in grants(val, depth + 1):
                    out.append(g | {"reqs": g["reqs"] + reqs})
            elif kind == "trait":
                out += [{"kind": k, "value": v, "reqs": reqs, "via": val} for k, v in trait_owner.get(val, [])]
            elif kind == "tradition":
                out.append({"kind": "tradition", "value": val, "reqs": reqs})
            elif kind == "ability":
                if val in inactive:  # switched on by some other effect (commander, great person, civic): unknown from logs
                    out.append({"kind": "unsupported", "value": f"inactive ability {val}", "reqs": reqs})
                    continue
                for cls in sorted(ability_class.get(val, ())):
                    out.append({"kind": "unit_tag", "value": cls, "reqs": reqs, "via": val})
        return out

    kill_rules, skipped = [], []
    for m in mods.values():
        if m["effect"] != POST_COMBAT:
            continue
        a = m["args"]
        rule = {"modifier": m["id"], "yield": a.get("YieldType"), "pct_strength": float(a.get("PercentDefeatedStrength") or 0),
                "pct_cost": float(a.get("PercentDefeatedUnitCost") or 0),
                "support_only": a.get("OnlyWhenDefeatedSupportUnit") == "true", "reqs": m["reqs"],
                "arg_filters": {k: a[k] for k in ("UnitDomain", "Tag", "AbilityType", "ConstructibleClass", "ConstructibleType") if a.get(k)}}
        all_gs = grants(m["id"])
        gs = [g for g in all_gs if g["kind"] != "unsupported"]
        unknown = [r["type"] for g in gs for r in g["reqs"] if r["type"] not in KNOWN_REQS] + \
                  [r["type"] for r in m["reqs"] if r["type"] not in KNOWN_REQS]
        if not gs or unknown or rule["arg_filters"] or m["owner_reqs"]:
            why = ("; ".join(g["value"] for g in all_gs) or "not attached to a leader, civ, tradition or unit") if not gs else \
                f"unsupported {unknown or rule['arg_filters'] or 'owner requirements'}"
            skipped.append({"modifier": m["id"], "yield": rule["yield"], "why": why})
            continue
        rule["grants"] = gs
        kill_rules.append(rule)

    trad_names = {r["TraditionType"]: r.get("Name") for r in t["Traditions"] if r.get("TraditionType")}
    cs_types = {r["CityStateType"]: {"yield": r.get("YieldType"), "base": int(r.get("DisperseRewardAmount") or 0)}
                for r in t["CityStateTypes"] if r.get("CityStateType")}
    independents = {r["IndependentType"]: r.get("CityStateType") for r in t["Independents"]
                    if r.get("IndependentType") and r.get("CityStateType")}
    return {"version": CACHE_VERSION, "units": units, "kill_rules": kill_rules, "skipped_rules": skipped,
            "tradition_names": trad_names, "city_state_types": cs_types, "independents": independents}


_RULES: dict | None = None


def load_rules(rebuild: bool = False) -> dict:
    """build_rules() cached in data/cache, rebuilt when a patch touches the game data."""
    global _RULES
    if _RULES is not None and not rebuild:
        return _RULES
    files = rules._data_files()
    newest = max((f.stat().st_mtime for f in files), default=0)
    if CACHE.exists() and not rebuild and CACHE.stat().st_mtime >= newest:
        try:
            r = json.loads(CACHE.read_text())
            if r.get("version") == CACHE_VERSION:
                _RULES = r
                return r
        except ValueError:
            pass
    r = build_rules(files)
    if files:  # no game data (tests, CI): don't cache the empty rule set
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(r))
    _RULES = r
    return r


# ---------------------------------------------------------------- kill rewards

def _req_ok(req: dict, killer: dict, victim: dict) -> bool:
    typ, args = req["type"], req["args"]
    subj = victim if "OPPONENT" in typ else killer
    if typ.endswith("DOMAIN_MATCHES"):
        ok = subj.get("domain") == args.get("UnitDomain")
    else:
        ok = args.get("Tag") in subj.get("tags", [])
    return ok != req.get("inverse", False)


def kill_rewards(killer_type: str, victim_type: str, victim_strength: float | None, ctx: dict, R: dict) -> list[dict]:
    """Yields the killer's owner gets for destroying victim_type. ctx: leader, civilization (type ids; None if
    unknown), traditions (set of TRADITION_* or None if unknown). Returns [{modifier, yield, amount, unknown?}]."""
    units = R["units"]
    killer, victim = units.get(killer_type), units.get(victim_type)
    if not killer or not victim:
        return []
    out = []
    for rule in R["kill_rules"]:
        hit = None
        for g in rule["grants"]:
            if not all(_req_ok(r, killer, victim) for r in g["reqs"]):
                continue
            if g["kind"] == "unit_tag":
                ok = g["value"] in killer.get("tags", [])
            elif g["kind"] in ("leader", "civilization"):
                have = ctx.get(g["kind"])
                ok = None if have is None else have == g["value"]
            elif g["kind"] == "tradition":
                have = ctx.get("traditions")
                ok = None if have is None else g["value"] in have
            else:
                ok = False
            if ok:  # None (leader/civ/traditions unknown) counts as not granted
                hit = g
                break
        if not hit or not all(_req_ok(r, killer, victim) for r in rule["reqs"]):
            continue
        if rule["support_only"] and victim.get("core_class") != "CORE_CLASS_SUPPORT":
            continue
        amount = 0.0
        if rule["pct_strength"]:
            strength = victim_strength if victim_strength not in (None, "") else victim.get("combat")
            amount += rule["pct_strength"] / 100 * float(strength or 0)
        if rule["pct_cost"]:
            amount += rule["pct_cost"] / 100 * float(victim.get("cost") or 0)
        if amount > 0:
            out.append({"modifier": rule["modifier"], "yield": rule["yield"], "amount": round(amount, 2),
                        "granted_by": f"{hit['kind']}:{hit.get('via') or hit['value']}"})
    return out


# ---------------------------------------------------------------- log parsing

_UNIT = re.compile(r"^\(-?\d+\)(.*)$")
_XY = re.compile(r"\((-?\d+)\)\((-?\d+)\)")


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def combat_kills(rows: list[dict]) -> list[dict]:
    """CombatLog unit kills: {turn, killer, victim, killer_unit, victim_unit, victim_strength, x, y}."""
    out = []
    for r in rows:
        if (r.get("SourceType") or "").strip() != "Unit vs Unit":
            continue
        dest = (r.get("Destroyed") or "").strip()
        xy = _XY.search(r.get("Location") or "")
        att, dfn = _UNIT.match(r.get("Attacker") or ""), _UNIT.match(r.get("Defender") or "")
        base = {"turn": _int(r.get("Turn")), "x": int(xy.group(1)) if xy else None, "y": int(xy.group(2)) if xy else None}
        sides = []
        if dest in ("Defender", "Both"):
            sides.append((r.get("AttPlayer"), r.get("DefPlayer"), att, dfn, r.get("DefStr")))
        if dest in ("Attacker", "Both"):
            sides.append((r.get("DefPlayer"), r.get("AttPlayer"), dfn, att, r.get("AttStr")))
        for kp, vp, ku, vu, vs in sides:
            if _int(kp) is None or not ku or not vu:
                continue
            out.append(base | {"killer": _int(kp), "victim": _int(vp), "killer_unit": ku.group(1).strip(),
                               "victim_unit": vu.group(1).strip(), "victim_strength": _num(vs),
                               "combat_type": (r.get("CombatType") or "").strip()})
    return out


def gossip(rows: list[dict], kinds: tuple[str, ...]) -> list[dict]:
    """Game_Gossip rows of the given GOSSIP_* types: {turn, type, player_name, civ, x, y, extra[]}. The log writes
    unquoted leader names ("Xerxes, the Achaemenid"), so fields are located from the GOSSIP_* token."""
    out = []
    for r in rows:
        vals = [str(v).strip() for v in r.values()]
        idx = next((i for i, v in enumerate(vals) if v.startswith("GOSSIP_")), None)
        if idx is None or idx < 4 or vals[idx] not in kinds:
            continue
        out.append({"turn": _int(vals[0]), "type": vals[idx], "player_name": ", ".join(vals[1:idx - 3]), "civ": vals[idx - 3],
                    "x": _int(vals[idx - 2]), "y": _int(vals[idx - 1]), "extra": [v for v in vals[idx + 1:] if v]})
    return out


def dispersals(rows: list[dict]) -> list[dict]:
    """Independents 'DISPERSE TRIBE' rows: {turn, tribe, x, y}."""
    out = []
    for r in rows:
        if (r.get("Action") or "").strip() != "DISPERSE TRIBE":
            continue
        xy = (r.get("Other") or "").split()
        out.append({"turn": _int(r.get("Turn")), "tribe": (r.get("Tribe") or "").strip(),
                    "x": _int(xy[0]) if len(xy) > 0 else None, "y": _int(xy[1]) if len(xy) > 1 else None})
    return out


def treasury_residuals(rows: list[dict]) -> dict[tuple[int, int], float]:
    """{(player, turn): balance[turn+1] - balance[turn] - gold_yield[turn]} for consecutive logged turns."""
    by: dict[int, dict[int, tuple[float, float]]] = defaultdict(dict)
    for r in rows:
        p, t = _int(r.get("Player")), _int(r.get("Turn"))
        bal, y = _num(r.get("Gold Balance")), _num(r.get("Gold Yield"))
        if None not in (p, t, bal, y):
            by[p][t] = (bal, y)
    out = {}
    for p, turns in by.items():
        for t, (bal, y) in turns.items():
            nxt = turns.get(t + 1)
            if nxt:
                out[(p, t)] = round(nxt[0] - bal - y, 2)
    return out


_RECEIVED = re.compile(r"You have received (\d[\d,]*(?:\.\d+)?)\s*(?:\[icon:(YIELD_[A-Z]+)\])?\s*([A-Za-z]+)?", re.IGNORECASE)
_YIELD_WORD = {"gold": "YIELD_GOLD", "science": "YIELD_SCIENCE", "culture": "YIELD_CULTURE", "influence": "YIELD_DIPLOMACY",
               "production": "YIELD_PRODUCTION", "food": "YIELD_FOOD", "happiness": "YIELD_HAPPINESS"}


def notification_reward(summary: str) -> tuple[str | None, float] | None:
    """(yield, amount) from a NOTIFICATION_DISPERSE_IP_REWARD summary, else None."""
    m = _RECEIVED.search(summary or "")
    if not m:
        return None
    amount = float(m.group(1).replace(",", ""))
    y = m.group(2) or _YIELD_WORD.get((m.group(3) or "").lower())
    return y, amount


# ---------------------------------------------------------------- assembling reward events

def _near(a: dict, b: dict, r: int = 2) -> bool:
    return None not in (a.get("x"), a.get("y"), b.get("x"), b.get("y")) and abs(a["x"] - b["x"]) <= r and abs(a["y"] - b["y"]) <= r


def rewards(logs_by_age: dict[str, dict[str, list[dict]]], clock, contexts, name_to_id: dict[str, int],
            notices: dict[tuple[str, int], list[dict]] | None = None, R: dict | None = None) -> dict:
    """Combat reward events and per-turn unexplained gold.

    logs_by_age: log name -> age -> rows (export.merged_logs). contexts(pid, age, turn) -> ctx for kill_rewards.
    name_to_id: leader display name -> player id (for Game_Gossip). notices: (age, turn) -> local snapshot
    notifications seen at that on-screen turn. Returns {"events": [...], "unexplained": {(age, pid, turn): gold}}:
    events carry an amount only from an exact source (rules, notification); see unexplained_gold()."""
    R = R or load_rules()
    notices = notices or {}
    events: list[dict] = []

    def ev(kind, age, turn, pid, yield_type, amount, basis, source, **detail):
        p = clock.at(age, turn)
        if p is None or pid is None:
            return None
        e = {"kind": kind, "age": age, "turn": turn, "g": p["g"], "player_id": pid, "yield": yield_type,
             "amount": amount, "basis": basis, "source": source}
        e |= {k: v for k, v in detail.items() if v not in (None, "", [])}
        events.append(e)
        return e

    for age, rows in (logs_by_age.get("CombatLog") or {}).items():
        for k in combat_kills(rows):
            ctx = contexts(k["killer"], age, k["turn"])
            for rw in kill_rewards(k["killer_unit"], k["victim_unit"], k["victim_strength"], ctx, R):
                ev("kill", age, k["turn"], k["killer"], rw["yield"], rw["amount"], "rules", "log:CombatLog",
                   x=k["x"], y=k["y"], unit=k["killer_unit"], target=k["victim_unit"], target_owner=k["victim"],
                   modifier=rw["modifier"], granted_by=rw["granted_by"], victim_strength=k["victim_strength"])

    for age, rows in (logs_by_age.get("Game_Gossip") or {}).items():
        camps = dispersals((logs_by_age.get("Independents") or {}).get(age, []))
        routes = [r for r in (logs_by_age.get("Game_TradeManager") or {}).get(age, [])
                  if "Plundered" in str(r.get("Event", ""))]
        for gsp in gossip(rows, ("GOSSIP_PILLAGE", "GOSSIP_PLUNDER_TRADE_ROUTE", "GOSSIP_INDEPENDENT_DISPERSE")):
            pid = name_to_id.get(gsp["player_name"])
            if pid is None:
                continue  # independents and city-states pillage too; only majors are tracked
            t = gsp["turn"]
            if gsp["type"] == "GOSSIP_PILLAGE":
                ev("pillage", age, t, pid, None, None, "unknown", "log:Game_Gossip", x=gsp["x"], y=gsp["y"])
            elif gsp["type"] == "GOSSIP_PLUNDER_TRADE_ROUTE":
                rt = next((r for r in routes if export_turn(r) == t), None)
                ev("plunder", age, t, pid, None, None, "unknown", "log:Game_Gossip", x=gsp["x"], y=gsp["y"],
                   target=rt and f"{rt.get('Initiating Player City', '')} -> {rt.get('Resource City', '')}")
            else:
                unit = next((x.split("=", 1)[1] for x in gsp["extra"] if x.startswith("Dispersing Unit=")), None)
                camp = next((c for c in camps if c["turn"] == t and _near(c, gsp)), None) or \
                    next((c for c in camps if c["turn"] == t), None)
                cst = R["independents"].get(camp["tribe"]) if camp else None
                info = R["city_state_types"].get(cst) or {}
                exact = None
                for n in notices.get((age, t), []) + notices.get((age, t + 1), []):
                    exact = notification_reward(n.get("summary", ""))
                    if exact:
                        break
                if camp:
                    camps.remove(camp)
                yld = (exact and exact[0]) or info.get("yield")
                ev("disperse", age, t, pid, yld, exact[1] if exact else None, "notification" if exact else "unknown",
                   "log:Game_Gossip", x=camp["x"] if camp else gsp["x"], y=camp["y"] if camp else gsp["y"], unit=unit,
                   target=camp and camp["tribe"], target_owner=None, city_state_type=cst, base_amount=info.get("base"))

    return {"events": events, "unexplained": unexplained_gold(logs_by_age, events)}


def unexplained_gold(logs_by_age: dict[str, dict[str, list[dict]]], events: list[dict]) -> dict[tuple[str, int, int], float]:
    """{(age, player, turn): treasury change not explained by per-turn income, minus the turn's exact combat gold}.
    Turns with a logged purchase are left out: CityPurchase has no prices, so their change cannot be split.
    Not attributed to any event or kind."""
    exact: dict[tuple, float] = defaultdict(float)
    for e in events:
        if e.get("amount") is not None and e.get("yield") == "YIELD_GOLD":
            exact[(e["age"], e["player_id"], e["turn"])] += e["amount"]
    purchases = {(age, _int(r.get("Player")), export_turn(r))
                 for age, rows in (logs_by_age.get("CityPurchase") or {}).items() for r in rows}
    out = {}
    for age, rows in (logs_by_age.get("Player_Treasury") or {}).items():
        for (pid, t), v in treasury_residuals(rows).items():
            if (age, pid, t) not in purchases:
                out[(age, pid, t)] = round(v - exact.get((age, pid, t), 0.0), 2)
    return out


def export_turn(r: dict) -> int | None:
    for k in ("Game Turn", "Turn", "turn"):
        if r.get(k) not in (None, ""):
            return _int(r[k])
    return None


# ---------------------------------------------------------------- metrics

KINDS = ("kill", "pillage", "plunder", "disperse")
COUNT_METRIC = {"pillage": "pillages_total", "plunder": "trade_routes_plundered_total", "disperse": "independents_dispersed_total"}


def cumulative(events: list[dict]) -> dict[tuple[int, str], dict[int, float]]:
    """(player, metric) -> {g: increment}. combat_<yield>_total per yield and kill_gold_total from exact amounts, plus
    event counts (amount known or not)."""
    inc: dict[tuple[int, str], dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for e in events:
        pid, g = e["player_id"], e["g"]
        if e["kind"] in COUNT_METRIC:
            inc[(pid, COUNT_METRIC[e["kind"]])][g] += 1
        if e.get("amount") is None or not e.get("yield"):
            continue
        short = YIELD_SHORT.get(e["yield"])
        if short:
            inc[(pid, f"combat_{short}_total")][g] += e["amount"]
        if e["kind"] == "kill" and e["yield"] == "YIELD_GOLD":
            inc[(pid, "kill_gold_total")][g] += e["amount"]
    return inc
