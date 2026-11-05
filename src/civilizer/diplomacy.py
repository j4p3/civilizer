"""Diplomatic activity rebuilt from the game's logs: actions between players (endeavors, sanctions, treaties, espionage,
alliance proposals), befriending independents and the race to become their suzerain, suzerainties gained and lost,
responses to proposals, and first meetings. Wars, peace, alliances and city captures stay in export.py (kinds war,
war_declared, alliance, settlement_captured).

What the game writes (all in Logs/, archived by `civilizer archive-logs`):
  - DiplomacySummary: `Game Turn, Initiator, Recipient, Action, Details, Mayhem` with player ids (majors first,
    independents / city-states after them). Actions:
      Met                                  a pair met (logged once each way)
      Diplomacy Action Started             Details = the action's name ("Research Collaboration"; befriending an
                                           independent is the raw key LOC_DIPLOMACY_ACTION_GIVE_INFLUENCE_TOKEN_NAME)
      Diplomacy Action Enter Stage         "<action> Entering Stage <stage>". Befriending moves through
                                           DIPLOMACY_INFLUENCE_INDEPENDENT_VERY_HOSTILE / _FRIENDLY / _FRIENDLY_FIRST_GIFT /
                                           _SUZERAIN; reaching _SUZERAIN makes the initiator suzerain.
      Diplomacy Action Support Changed     "<action> - P <id> increased support by <n>": the initiator (influence put in to
                                           start or speed it up) or the recipient (supported a proposal).
      Diplomacy Action Opposition Changed  "<action> - <id> increased opposition by <n>"
      Diplomacy Action Ended               "<action> result: Success | Canceled"
      At War / Peace / Allied / Alliance Ends / City Capture   (export.py)
    A same-turn reload logs the turn again; only the last pass of a turn is kept (_last_pass).
  - Game_Gossip: GOSSIP_BECOME_SUZERAIN (leader name, the city-state's capital, the suzerain bonus chosen) and
    GOSSIP_REJECT_DEAL (who rejected a deal). GOSSIP_DIPLOMACY_ACTION_STARTED / _RESPONSE / _RELATIONSHIP_CHANGED carry
    only a leader name and add nothing to DiplomacySummary.
  - Independents: "CREATE TRIBE <player id>" says which independent (INDEPENDENT_<AGE>_<n>) a player id is. Ids are
    reused across ages.
  - Game data XML: DiplomacyActions (type, Name), DiplomaticProjects_UI_Data (group: endeavor / sanction / treaty /
    espionage / project / unique) and DiplomaticActionResponses (actions whose target may reject them: proposals).

Not logged: influence costs (the snapshot influence bank gives the local player's per-turn spending, see ingest), which
rival leads a befriending race at any moment (only who put support in and when), deal contents, and suzerainties
won before the first archived log of an age.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

from . import rules

CACHE = Path(__file__).resolve().parents[2] / "data" / "cache" / "diplomacy_rules.json"
CACHE_VERSION = 2
BEFRIEND = "DIPLOMACY_ACTION_GIVE_INFLUENCE_TOKEN"
INCORPORATE = "DIPLOMACY_ACTION_CS_INCORPORATE"
FORM_ALLIANCE = "DIPLOMACY_ACTION_FORM_ALLIANCE"
SUZERAIN_STAGE = "DIPLOMACY_INFLUENCE_INDEPENDENT_SUZERAIN"
# land claims are automatic on settling (no recipient); war declarations are export.py's war / war_declared
SKIP = {"DIPLOMACY_ACTION_LAND_CLAIM", "DIPLOMACY_ACTION_DECLARE_WAR", "DIPLOMACY_ACTION_DECLARE_FORMAL_WAR"}
SKIP_NAMES = {"Land Claim", "Declare War", "Declare Formal War"}
# names the logic depends on, for when the game data is not installed (tests, CI)
FALLBACK_NAMES = {"Form Alliance": FORM_ALLIANCE, "Incorporate": INCORPORATE, "Land Claim": "DIPLOMACY_ACTION_LAND_CLAIM",
                  "Declare War": "DIPLOMACY_ACTION_DECLARE_WAR"}
KINDS = ("diplomacy_action", "befriend", "suzerain", "diplo_response", "met")


# ---------------------------------------------------------------- rules from the game's data XML

def build_rules(files: list[Path] | None = None, names: dict[str, str] | None = None) -> dict:
    """{actions: {DIPLOMACY_ACTION_X: {group, name, responses}}, by_name: {display name or LOC key: type}}."""
    files = files if files is not None else [f for f in rules._data_files() if "diplomacy" in f.name.lower()]
    names = names if names is not None else rules.text_index()
    actions: dict[str, dict] = {}
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for tbl in root:
            for r in tbl.findall("Row"):
                a = r.attrib
                t = a.get("DiplomacyActionType")
                if not t:
                    continue
                if tbl.tag == "DiplomacyActions" or (tbl.tag == "DiplomaticProjects_UI_Data" and a.get("DiplomacyActionGroup")):
                    row = actions.setdefault(t, {"group": None, "name": None, "responses": []})
                    if a.get("DiplomacyActionGroup"):  # DiplomaticProjects_UI_Data carries the group
                        row["group"] = a["DiplomacyActionGroup"].removeprefix("DIPLOMACY_ACTION_GROUP_").lower()
                    if a.get("Name") and tbl.tag == "DiplomacyActions":
                        row["name"] = a["Name"]
                elif tbl.tag == "DiplomaticActionResponses" and a.get("DiplomaticResponseType"):
                    row = actions.setdefault(t, {"group": None, "name": None, "responses": []})
                    resp = a["DiplomaticResponseType"].removeprefix("DIPLOMACY_RESPONSE_").lower()
                    if resp not in row["responses"]:
                        row["responses"].append(resp)
    by_name: dict[str, str] = {}
    for t, a in sorted(actions.items()):
        for key in (a["name"], names.get(a["name"] or "")):
            if key and key not in by_name:
                by_name[key] = t
    return {"version": CACHE_VERSION, "actions": actions, "by_name": by_name}


_RULES: dict | None = None


def load_rules(rebuild: bool = False) -> dict:
    """build_rules() cached in data/cache, rebuilt when a patch touches the game data."""
    global _RULES
    if _RULES is not None and not rebuild:
        return _RULES
    files = [f for f in rules._data_files() if "diplomacy" in f.name.lower()]
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


EMPTY_RULES = {"version": CACHE_VERSION, "actions": {}, "by_name": {}}


# ---------------------------------------------------------------- log parsing

_STAGE = re.compile(r"^(?P<action>.*?) Entering Stage (?P<stage>\S+)$")
_ENDED = re.compile(r"^(?P<action>.*?) result: (?P<result>\w+)$")
_SUPPORT = re.compile(r"^(?P<action>.*?) - (?:P )?(?P<who>-?\d+) increased (?P<what>support|opposition) by (?P<n>-?\d+)$")
_TRIBE = re.compile(r"^CREATE TRIBE (\d+)$")


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _turn(r: dict) -> int | None:
    for k in ("Game Turn", "Turn", "turn"):
        if r.get(k) not in (None, ""):
            return _int(r[k])
    return None


def _last_pass(rows: list[tuple]) -> list[tuple]:
    """A same-turn reload logs the turn again from its first row: keep the last pass."""
    if len(rows) < 2:
        return rows
    first = rows[0]
    last = max(i for i, r in enumerate(rows) if r == first)
    return rows[last:]


def summary_rows(rows: list[dict]) -> list[tuple[int, int, int, str, str]]:
    """DiplomacySummary rows as (turn, initiator, recipient, action, details), one pass per turn."""
    by_turn: dict[int, list[tuple]] = defaultdict(list)
    order: list[int] = []
    for r in rows:
        t, a, b = _turn(r), _int(r.get("Initiator")), _int(r.get("Recipient"))
        if None in (t, a, b):
            continue
        if t not in by_turn:
            order.append(t)
        by_turn[t].append((t, a, b, str(r.get("Action") or "").strip(), str(r.get("Details") or "").strip()))
    return [row for t in order for row in _last_pass(by_turn[t])]


def tribes(rows: list[dict]) -> list[dict]:
    """Independents 'CREATE TRIBE <id>' rows: [{turn, id, type, x, y}] in log order."""
    out = []
    for r in rows:
        m = _TRIBE.match(str(r.get("Action") or "").strip())
        if not m:
            continue
        xy = str(r.get("Other") or "").split()
        out.append({"turn": _turn(r), "id": int(m.group(1)), "type": str(r.get("Tribe") or "").strip(),
                    "x": _int(xy[0]) if xy else None, "y": _int(xy[1]) if len(xy) > 1 else None})
    return out


class Names:
    """Display names and kinds (major / independent / other) of player ids, per age and turn."""

    def __init__(self, majors: dict[int, str], tribe_rows: dict[str, list[dict]], minors: dict[str, dict[int, str]],
                 text: dict[str, str]):
        self.majors, self.minors, self.text = majors, minors, text
        self.tribes: dict[str, dict[int, list[tuple[int, str]]]] = defaultdict(lambda: defaultdict(list))
        for age, rows in tribe_rows.items():
            for t in rows:
                self.tribes[age][t["id"]].append((t["turn"] or 0, t["type"]))

    def tribe_type(self, pid: int, age: str, turn: int) -> str | None:
        created = self.tribes.get(age, {}).get(pid)
        if not created:
            return None
        before = [ty for t, ty in created if t <= turn]
        return before[-1] if before else created[0][1]

    def kind(self, pid: int, age: str, turn: int) -> str:
        # independents first: their ids come from this game's own logs and snapshots, while the majors list can
        # carry stray ids (export matches saves to the game heuristically)
        if pid in self.tribes.get(age, {}) or pid in self.minors.get(age, {}):
            return "independent"
        if pid in self.majors:
            return "major"
        first = min(self.tribes.get(age, {}) or [10 ** 9])
        return "major" if 0 <= pid < first else "other"

    def __call__(self, pid: int, age: str, turn: int) -> str:
        ty = self.tribe_type(pid, age, turn)
        if ty:
            return self.text.get(f"LOC_{ty}_MINOR_NAME") or self.text.get(f"LOC_{ty}_NAME") or ty
        if pid in self.minors.get(age, {}):
            return self.minors[age][pid]
        return self.majors.get(pid) or f"player {pid}"

    def capital(self, pid: int, age: str, turn: int) -> str | None:
        ty = self.tribe_type(pid, age, turn)
        return self.text.get(f"LOC_{ty}_MAJOR_NAME") if ty else None


def _gossip(rows: list[dict], kinds: tuple[str, ...]) -> list[dict]:
    from .combat import gossip

    seen, out = set(), []
    for g in gossip(rows, kinds):  # exact repeats are same-turn reloads
        k = (g["turn"], g["type"], g["player_name"], tuple(g["extra"]))
        if k not in seen:
            seen.add(k)
            out.append(g)
    return out


# ---------------------------------------------------------------- events

def build(logs_by_age: dict[str, dict[str, list[dict]]], clock, majors: dict[int, str], minors: dict[str, dict[int, str]],
          text: dict[str, str], R: dict | None = None) -> list[dict]:
    """Diplomacy events in export's event shape ({kind, start, end, players, subject, source, precision, detail}).

    logs_by_age: log name -> age -> rows (export.merged_logs). majors: player id -> leader display name. minors: age ->
    id -> city-state / independent name (captures, snapshots), used when Independents has no CREATE TRIBE row."""
    R = R or EMPTY_RULES
    names = Names(majors, {a: tribes(rows) for a, rows in (logs_by_age.get("Independents") or {}).items()}, minors, text)
    name_to_id = {v: k for k, v in majors.items() if v}
    out: list[dict] = []
    met_majors: set[tuple[int, int]] = set()  # major pairs meet once per game (ids persist); re-logged at each age start
    ages = [a for a in clock.offset if a in (logs_by_age.get("DiplomacySummary") or {}) or a in (logs_by_age.get("Game_Gossip") or {})]
    for age in ages:
        out += _age(age, logs_by_age, clock, names, name_to_id, R, met_majors)
    _races(out)
    return out


def _ev(kind, start, end, players, subject, precision="exact", source="log:DiplomacySummary", **detail) -> dict:
    return {"kind": kind, "start": start, "end": end, "players": players, "subject": subject, "source": source,
            "precision": precision, "detail": {k: v for k, v in detail.items() if v not in (None, "")}}


def _age(age, logs_by_age, clock, names: Names, name_to_id, R, met_majors) -> list[dict]:
    out: list[dict] = []
    acts, by_name = R["actions"], R["by_name"]
    open_: dict[tuple[int, int, str], dict] = {}
    suz: dict[int, dict] = {}  # city-state id -> open suzerain span
    met_here: set[tuple[int, int]] = set()
    last_turn = clock.lengths.get(age, 0)
    at = lambda t: clock.at(age, t)
    nm = lambda pid, t: names(pid, age, t)

    def action_of(text: str) -> tuple[str, str]:
        """(type or the logged name, display name)."""
        typ = by_name.get(text) or FALLBACK_NAMES.get(text)
        if typ is None and text.startswith("LOC_DIPLOMACY_ACTION_") and text.endswith("_NAME"):
            typ = "DIPLOMACY_ACTION_" + text[len("LOC_DIPLOMACY_ACTION_"):-len("_NAME")]
        display = "Befriend independent" if typ == BEFRIEND else (text if not text.startswith("LOC_") else (typ or text))
        return typ or text, display

    def is_proposal(typ: str) -> bool | None:
        a = acts.get(typ)
        return None if a is None else "reject" in a["responses"]

    def respond(e: dict, response: str, t: int) -> None:
        d = e["detail"]
        if d.get("response"):
            return
        d["response"], d["response_turn"] = response, t
        a, b = d["initiator"], d["recipient"]
        out.append(_ev("diplo_response", at(t), at(t), [b, a], f"{nm(b, t)} {response} {d['action']} from {nm(a, t)}",
                       action=d["action"], action_type=d.get("action_type"), response=response, proposer=a, responder=b,
                       proposer_name=nm(a, t), responder_name=nm(b, t)))

    def open_action(t: int, a: int, b: int, typ: str, display: str, before_logs: bool = False) -> dict:
        key = (a, b, typ)
        old = open_.get(key)
        if old is not None:
            if old["start"]["turn"] == t:
                return old
            close(old, t, None, "restarted")
        kind = "befriend" if typ == BEFRIEND else "diplomacy_action"
        rk = names.kind(b, age, t)
        subject = f"Befriend {nm(b, t)} ({nm(a, t)})" if kind == "befriend" else f"{display}: {nm(a, t)} -> {nm(b, t)}"
        start = at(t)
        if before_logs and start:
            start = clock.at(age, t, 1, t)
        e = _ev(kind, start, None, [a, b], subject, "range" if before_logs else "exact",
                action=display, action_type=typ if typ.startswith("DIPLOMACY_ACTION_") else None,
                group=(acts.get(typ) or {}).get("group"), proposal=is_proposal(typ) if kind == "diplomacy_action" else None,
                initiator=a, recipient=b, initiator_name=nm(a, t), recipient_name=nm(b, t), recipient_kind=rk,
                started_before_logs=True if before_logs else None)
        e["detail"].update({"stages": [], "support": {}, "support_log": []})
        e["_key"] = key
        open_[key] = e
        out.append(e)
        return e

    def find(t: int, a: int, b: int, text: str) -> dict:
        typ, display = action_of(text)
        return open_.get((a, b, typ)) or open_action(t, a, b, typ, display, before_logs=True)

    def close(e: dict, t: int, result: str | None, reason: str | None = None) -> None:
        d = e["detail"]
        e["end"] = at(t)
        if result:
            d["outcome"] = result.lower()
        if reason:
            d["end_reason"] = reason
        if open_.get(e["_key"]) is e:
            del open_[e["_key"]]

    def gain(a: int, cs: int | None, t: int, via: str, **extra) -> dict | None:
        if cs is not None:
            cur = suz.get(cs)
            if cur is not None:
                if cur["detail"]["suzerain"] == a:
                    cur["detail"].update({k: v for k, v in extra.items() if v})
                    return cur
                lose(cs, t, "replaced", lost_to=a, lost_to_name=nm(a, t))
        csname = nm(cs, t) if cs is not None else (extra.get("capital") or "?")
        e = _ev("suzerain", at(t), None, [a] + ([cs] if cs is not None else []), f"{nm(a, t)} suzerain of {csname}",
                source=via, suzerain=a, suzerain_name=nm(a, t), city_state=cs, city_state_name=csname, **extra)
        out.append(e)
        if cs is not None:
            suz[cs] = e
        return e

    def lose(cs: int, t: int, reason: str, **extra) -> None:
        e = suz.pop(cs, None)
        if e is None:
            return
        e["end"] = at(t)
        e["detail"]["end_reason"] = reason
        e["detail"].update({k: v for k, v in extra.items() if v is not None})

    ds = summary_rows((logs_by_age.get("DiplomacySummary") or {}).get(age, []))
    gs = _gossip((logs_by_age.get("Game_Gossip") or {}).get(age, []), ("GOSSIP_BECOME_SUZERAIN", "GOSSIP_REJECT_DEAL"))
    ds_by_turn: dict[int, list[tuple]] = defaultdict(list)
    for r in ds:
        ds_by_turn[r[0]].append(r)
    gs_by_turn: dict[int, list[dict]] = defaultdict(list)
    for g in gs:
        if g["turn"] is not None:
            gs_by_turn[g["turn"]].append(g)

    for t in sorted(set(ds_by_turn) | set(gs_by_turn)):
        allied_now: set[tuple[int, int]] = set()
        for _, a, b, act, det in ds_by_turn.get(t, []):
            if act == "Met":
                ka, kb = names.kind(a, age, t), names.kind(b, age, t)
                if "major" not in (ka, kb) or "other" in (ka, kb):  # "other": ids that are neither (e.g. 63), unnamed
                    continue
                pair = tuple(sorted((a, b)))
                if ka == kb == "major":
                    if pair in met_majors or (t == 1 and age != "AGE_ANTIQUITY"):  # an age start re-logs old meetings
                        continue
                    met_majors.add(pair)
                elif pair in met_here:
                    continue
                met_here.add(pair)
                x, y = (a, b) if ka == "major" else (b, a)
                out.append(_ev("met", at(t), at(t), [x, y], f"{nm(x, t)} met {nm(y, t)}", player=x, other=y,
                               other_name=nm(y, t), other_kind=names.kind(y, age, t)))
            elif act == "Allied":
                allied_now.add(tuple(sorted((a, b))))
                for k in ((a, b, FORM_ALLIANCE), (b, a, FORM_ALLIANCE)):
                    if k not in open_:
                        continue
                    if t == 1 and open_[k]["start"]["turn"] == 1:  # an alliance carried into a new age
                        open_[k]["detail"]["carried_over"] = True
                    else:
                        respond(open_[k], "accepted", t)
            elif act == "City Capture" and b in suz:
                lose(b, t, "captured", captured_by=a, captured_by_name=nm(a, t))
            elif act == "Diplomacy Action Started":
                typ, display = action_of(det)
                if typ in SKIP or det in SKIP_NAMES or b < 0:
                    continue
                open_action(t, a, b, typ, display)
            elif act == "Diplomacy Action Enter Stage":
                m = _STAGE.match(det)
                if not m:
                    continue
                typ, _ = action_of(m.group("action"))
                if typ in SKIP or m.group("action") in SKIP_NAMES or b < 0:
                    continue
                e = find(t, a, b, m.group("action"))
                d = e["detail"]
                if not d["stages"] or d["stages"][-1]["stage"] != m.group("stage"):
                    d["stages"].append({"turn": t, "stage": m.group("stage")})
                d["stage"] = m.group("stage")
                if d.get("proposal") and not d.get("response"):
                    respond(e, "accepted", t)
                if e["kind"] == "befriend" and m.group("stage") == SUZERAIN_STAGE:
                    gain(a, b, t, "log:DiplomacySummary", befriend_turns=t - e["start"]["turn"] if e["precision"] == "exact" else None)
            elif act in ("Diplomacy Action Support Changed", "Diplomacy Action Opposition Changed"):
                m = _SUPPORT.match(det)
                if not m:
                    continue
                typ, _ = action_of(m.group("action"))
                if typ in SKIP or m.group("action") in SKIP_NAMES or b < 0:
                    continue
                e = find(t, a, b, m.group("action"))
                d, who, n = e["detail"], int(m.group("who")), int(m.group("n"))
                field = m.group("what")
                tally = d.setdefault(field, {})
                tally[str(who)] = tally.get(str(who), 0) + n
                d["support_log"].append({"turn": t, "player": who, field: n})
                if who == b and who != a:
                    respond(e, "supported" if field == "support" else "opposed", t)
            elif act == "Diplomacy Action Ended":
                m = _ENDED.match(det)
                if not m:
                    continue
                typ, _ = action_of(m.group("action"))
                if typ in SKIP or m.group("action") in SKIP_NAMES or b < 0:
                    continue
                e = find(t, a, b, m.group("action"))
                result = m.group("result")
                d = e["detail"]
                if d.get("proposal") and not d.get("response"):
                    quick = t <= e["start"]["turn"] + 1 and not d["stages"] and not d.get("started_before_logs")
                    if result == "Canceled" and quick and tuple(sorted((a, b))) not in allied_now:
                        respond(e, "rejected", t)
                    elif result == "Success" or d["stages"]:
                        respond(e, "accepted", t)
                close(e, t, result)
                if typ == INCORPORATE and result == "Success":
                    lose(b, t, "incorporated", incorporated_by=a, incorporated_by_name=nm(a, t))
                if e["kind"] == "befriend" and result == "Canceled":
                    winner = suz.get(b)
                    if winner is not None and winner["detail"]["suzerain"] != a:
                        d["lost_to"], d["lost_to_name"] = winner["detail"]["suzerain"], winner["detail"]["suzerain_name"]
        for g in gs_by_turn.get(t, []):
            pid = name_to_id.get(g["player_name"])
            if g["type"] == "GOSSIP_REJECT_DEAL":
                if pid is not None:
                    out.append(_ev("diplo_response", at(t), at(t), [pid], f"{g['player_name']} rejected a deal",
                                   source="log:Game_Gossip", action="Deal", response="rejected", responder=pid,
                                   responder_name=g["player_name"]))
                continue
            if pid is None:
                continue
            capital = g["extra"][0] if g["extra"] else None
            bonus = g["extra"][1] if len(g["extra"]) > 1 else None
            mine = [e for e in suz.values() if e["detail"]["suzerain"] == pid and e["start"]["turn"] == t]
            hit = next((e for e in mine if names.capital(e["detail"]["city_state"], age, t) == capital), None)
            if hit is None and len(mine) == 1 and not mine[0]["detail"].get("bonus"):
                hit = mine[0]
            if hit is not None:
                hit["detail"].update({k: v for k, v in (("capital", capital), ("bonus", bonus)) if v})
                continue
            cs = next((i for i in names.tribes.get(age, {}) if names.capital(i, age, t) == capital), None)
            gain(pid, cs, t, "log:Game_Gossip", capital=capital, bonus=bonus)

    end = clock.at(age, last_turn)
    for e in list(open_.values()):
        e["end"] = end
        e["detail"]["end_reason"] = "age_end_or_ongoing"
    for e in suz.values():
        e["end"] = end
        e["detail"]["end_reason"] = "age_end_or_ongoing"
    for e in out:
        e.pop("_key", None)
        d = e.get("detail") or {}
        for k in ("stages", "support_log"):
            if k in d and not d[k]:
                del d[k]
        if "support" in d and not d["support"]:
            del d["support"]
    return out


def _races(events: list[dict]) -> None:
    """Befriending races: each befriend event lists the other majors befriending the same independent at overlapping turns."""
    bef = [e for e in events if e["kind"] == "befriend" and e.get("start")]
    by_target: dict[tuple, list[dict]] = defaultdict(list)
    for e in bef:
        by_target[(e["start"]["age"], e["detail"]["recipient"])].append(e)
    for group in by_target.values():
        for e in group:
            s, en = e["start"]["g"], (e.get("end") or {}).get("g", 10 ** 9)
            rivals = []
            for o in group:
                if o is e or o["detail"]["initiator"] == e["detail"]["initiator"]:
                    continue
                os_, oe = o["start"]["g"], (o.get("end") or {}).get("g", 10 ** 9)
                if os_ <= en and oe >= s:
                    od = o["detail"]
                    rivals.append({"player": od["initiator"], "name": od["initiator_name"], "start_turn": o["start"]["turn"],
                                   "stage": od.get("stage"), "outcome": od.get("outcome"),
                                   "support": (od.get("support") or {}).get(str(od["initiator"]))})
            if rivals:
                e["detail"]["rivals"] = rivals


# ---------------------------------------------------------------- metrics

# metric -> event kind whose open spans it counts / whose starts it accumulates
def metric_series(events: list[dict], clock, cur_age: str, cur_turn: int) -> dict[tuple[int, str], dict[int, float]]:
    """(player, metric) -> {g: value} at every g the value changes, plus the current turn.

    suzerainties / befriend_actions_active: open suzerain spans / befriend actions (an age end closes them).
    suzerainties_total: suzerainties gained. diplomatic_actions_started_total: actions the player started (befriending,
    endeavors, sanctions, treaties, espionage, alliance proposals, war declarations), not counting ones already running
    when the logs begin. independents_met: distinct independents met this age (resets each age)."""
    cur_g = clock.offset[cur_age] + cur_turn
    delta: dict[tuple[int, str], dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for e in events:
        s, en, d = e.get("start") or {}, e.get("end") or {}, e.get("detail") or {}
        if not s:
            continue
        if e["kind"] in ("suzerain", "befriend"):
            pid = d.get("suzerain") if e["kind"] == "suzerain" else d.get("initiator")
            metric = "suzerainties" if e["kind"] == "suzerain" else "befriend_actions_active"
            if pid is None:
                continue
            delta[(pid, metric)][s["g"]] += 1
            if en:
                ongoing = d.get("end_reason") == "age_end_or_ongoing"
                if not ongoing:
                    delta[(pid, metric)][en["g"]] -= 1
                elif en.get("age") != cur_age:  # closed by the age transition: gone from the next age's first turn
                    delta[(pid, metric)][en["g"] + 1] -= 1
            if e["kind"] == "suzerain":
                delta[(pid, "suzerainties_total")][s["g"]] += 1
        if e["kind"] in ("diplomacy_action", "befriend") and not d.get("started_before_logs") and not d.get("carried_over") \
                and d.get("initiator") is not None:
            delta[(d["initiator"], "diplomatic_actions_started_total")][s["g"]] += 1
        elif e["kind"] == "war_declared" and d.get("declared_by") is not None:
            delta[(d["declared_by"], "diplomatic_actions_started_total")][s["g"]] += 1
    out: dict[tuple[int, str], dict[int, float]] = {}
    for key, per_g in delta.items():
        total, series = 0, {}
        for g in sorted(per_g):
            if g > cur_g:
                continue
            total += per_g[g]
            series[g] = total
        series[cur_g] = total
        out[key] = series
    # independents met per age
    met: dict[tuple[int, str], dict[int, set]] = defaultdict(lambda: defaultdict(set))
    for e in events:
        d = e.get("detail") or {}
        if e["kind"] == "met" and d.get("other_kind") == "independent" and e.get("start"):
            met[(d["player"], e["start"]["age"])][e["start"]["g"]].add(d["other"])
    for (pid, age), per_g in met.items():
        seen: set = set()
        series = out.setdefault((pid, "independents_met"), {})
        series.setdefault(clock.offset[age] + 1, 0)
        for g in sorted(per_g):
            seen |= per_g[g]
            series[g] = len(seen)
        if age == cur_age:
            series[cur_g] = len(seen)
    for (pid, metric), series in out.items():
        if metric == "independents_met" and not any(a == cur_age for p, a in met if p == pid):
            series[clock.offset[cur_age] + 1] = 0  # a new age without meetings yet
            series[cur_g] = 0
    return out
