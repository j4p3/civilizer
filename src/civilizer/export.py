"""Whole-game export for an interactive per-turn timeline.

Sources, merged per age:
  - history captures (js/history.js, data/history/<seed>/): victory points per turn for every age,
    end-game summary datasets, and every major player's techs/civics/government/religion/settlements/
    wars at the captured turn. Capture each old save once to bracket events in ages without logs.
  - live snapshots (data/snapshots/<seed>/): the local player's research/civic/government per turn.
  - archived logs (data/archive/logs/): wars, city captures, religions, unit kills, production, diplomacy
    (diplomacy.py: actions, befriending independents, suzerainties, proposal responses, meetings).
  - save headers: game metadata and per-age rosters.

Output: games/<seed>/game.json (everything), events.jsonl, series_*.csv. See README "Game export".

Turn semantics: log turns are the turn whose end processed the event; snapshot/capture turns are the
on-screen turn. Every point carries `g`, a game-wide turn index (ages laid end to end) for plotting.
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import logs, timeline

ROOT = Path(__file__).resolve().parents[2]
HISTORY = ROOT / "data" / "history"
SNAPSHOTS = ROOT / "data" / "snapshots"
GAMES = ROOT / "games"
AGES = ["AGE_ANTIQUITY", "AGE_EXPLORATION", "AGE_MODERN"]
SCHEMA = "civilizer.game/1"


# ---------------------------------------------------------------- captures

def save_capture(h: dict) -> Path:
    g = h.get("game") or {}
    d = HISTORY / str(g.get("seed", "unknown"))
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{g.get('age', 'AGE_UNKNOWN')}-T{g.get('turn', 0):04d}-{datetime.now():%Y%m%dT%H%M%S}.json"
    p.write_text(json.dumps(h))
    return p


def _stamp(p: Path) -> str:
    m = re.search(r"(\d{8}T\d{6})", p.name)
    return m.group(1) if m else ""


def load_captures(seed: str) -> list[dict]:
    """Captures in age/turn order; for a repeated (age, turn) the latest capture wins."""
    best: dict[tuple, tuple[str, dict]] = {}
    for p in sorted((HISTORY / seed).glob("*.json")) if (HISTORY / seed).exists() else []:
        h = json.loads(p.read_text())
        g = h["game"]
        key = (AGES.index(g["age"]) if g["age"] in AGES else 9, g["turn"])
        if key not in best or _stamp(p) > best[key][0]:
            best[key] = (_stamp(p), h)
    return [best[k][1] for k in sorted(best)]


def load_snapshots(seed: str) -> list[dict]:
    """Snapshots on the kept branch: a later snapshot at turn t in the same age drops earlier ones at >= t."""
    files = sorted((SNAPSHOTS / seed).glob("T*.json"), key=_stamp) if (SNAPSHOTS / seed).exists() else []
    kept: list[dict] = []
    for p in files:
        try:
            s = json.loads(p.read_text())
        except ValueError:
            continue
        g = s.get("game") or {}
        age, turn = g.get("age"), g.get("turn")
        if age is None or turn is None:
            continue
        s["_stamp"] = _stamp(p)
        kept = [k for k in kept if not (k["game"]["age"] == age and k["game"]["turn"] >= turn)]
        kept.append(s)
    return sorted(kept, key=lambda s: (AGES.index(s["game"]["age"]), s["game"]["turn"]))


# ---------------------------------------------------------------- logs

def _sessions() -> list[Path]:
    """Archived log dirs, dropping any whose Player_Stats is a prefix of the next one (same session, archived again).
    The live Logs/ dir is left out on purpose: loading old saves to capture them writes fake reload
    branches there. Run `civilizer archive-logs` after real play, before exporting."""
    dirs = [d for d in logs.sessions(include_live=False) if (d / "Player_Stats.csv").exists()]
    out = []
    for i, d in enumerate(dirs):
        cur = (d / "Player_Stats.csv").read_bytes()
        nxt = (dirs[i + 1] / "Player_Stats.csv").read_bytes() if i + 1 < len(dirs) else None
        if nxt is not None and nxt.startswith(cur):
            continue
        out.append(d)
    return out


def _stamp_dt(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%dT%H%M%S")


def session_owners(dirs: list[Path], snapshots_root: Path = SNAPSHOTS, slack_s: int = 900) -> dict[Path, str | None]:
    """Which game (seed) each archived log session belongs to. Logs carry no game id, so: the seed of the
    snapshots taken during the session (after the previous archive, up to slack_s past this one, since the
    archive is stamped with the log's last write). Sessions without snapshots inherit the owner of the
    nearest later attributed session, else the nearest earlier one."""
    snaps = sorted((_stamp(p), p.parent.name) for p in snapshots_root.glob("*/T*.json")) if snapshots_root.exists() else []
    owners: dict[Path, str | None] = {}
    prev = ""
    for d in dirs:
        end = (_stamp_dt(d.name).timestamp() + slack_s) if re.match(r"\d{8}T\d{6}$", d.name) else float("inf")
        seen = [seed for st, seed in snaps if st > prev and _stamp_dt(st).timestamp() <= end]
        owners[d] = max(set(seen), key=seen.count) if seen else None
        prev = d.name
    known = [(i, o) for i, o in enumerate(owners.values()) if o]
    for i, d in enumerate(dirs):
        if owners[d] is None and known:
            later = [o for j, o in known if j > i]
            owners[d] = later[0] if later else known[-1][1]
    return owners


def save_timeline(seed: str, snaps: list[dict]) -> tuple[str | None, list]:
    """The save-file game matching this seed: the GUID with the most saves whose age and turn (autosaves are
    labeled up to one turn ahead) match a snapshot of this seed taken within 2 hours. (None, []) until the
    game has a matching save, e.g. a new game before its first autosave."""
    try:
        infos = timeline.load_headers(timeline.find_saves())
    except FileNotFoundError:
        return None, []
    marks = [(s["game"]["age"], s["game"]["turn"], datetime.fromisoformat(s["captured_at"].replace("Z", "+00:00")))
             for s in snaps if s.get("captured_at")]
    best, score = None, 0
    for guid, saves in timeline.games(infos).items():
        n = sum(any(a == i.age and abs((i.turn or 0) - t) <= 1 and i.saved_at and abs((i.saved_at - at).total_seconds()) < 7200
                    for a, t, at in marks) for i in saves)
        if n > score:
            best, score = guid, n
    return (best, timeline.timeline(timeline.games(infos)[best])) if best else (None, [])


def _session_last_age(d: Path, snaps_by_stamp: list[tuple[str, str]]) -> str | None:
    """Age at the end of a session: the last Historian row's age, else the last snapshot taken before the archive."""
    f = d / "Historian.csv"
    if f.exists():
        ages = [r.get("Age") for r in logs.read_table(f) if r.get("Age", "").startswith("AGE_")]
        if ages:
            return ages[-1]
    stamp = d.name if re.match(r"\d{8}T\d{6}$", d.name) else datetime.now().strftime("%Y%m%dT%H%M%S")
    before = [a for s, a in snaps_by_stamp if s <= stamp]
    return before[-1] if before else None


def _turn(row: dict) -> int | None:
    for k in ("Game Turn", "Turn", "turn"):
        v = row.get(k)
        if v not in (None, ""):
            try:
                return int(float(v))
            except ValueError:
                return None
    return None


def _segments(turns: list[int]) -> list[list[int]]:
    """[start, end] runs of a turn sequence; a turn going backwards starts a new run (reload or new age)."""
    segs: list[list[int]] = []
    for t in turns:
        if not segs or t < segs[-1][1]:
            segs.append([t, t])
        segs[-1][1] = t
    return segs


def session_segments(d: Path, last_age: str | None) -> list[tuple[int, int, str | None]]:
    """Player_Stats (one row per player per turn) defines a session's segments. One session can run into
    the next age without a relaunch, so a run starting at turn 1 after one that didn't is a new age;
    a run starting at 1 after another that did is a restart of the same age."""
    turns = [t for t in (_turn(r) for r in logs.read_table(d / "Player_Stats.csv")) if t is not None]
    segs = _segments(turns)
    advance = [i > 0 and s[0] == 1 and segs[i - 1][0] != 1 for i, s in enumerate(segs)]
    n = sum(advance)
    idx = (AGES.index(last_age) - n) if last_age in AGES else None
    out = []
    for (a, b), adv in zip(segs, advance):
        if idx is not None and adv:
            idx += 1
        out.append((a, b, AGES[idx] if idx is not None and 0 <= idx < len(AGES) else None))
    return out


def merged_logs(name: str, sessions: list[tuple[Path, list[tuple[int, int, str | None]]]]) -> dict[str, list[dict]]:
    """Rows of one log per age, on the kept branch. Each row is placed in the session's Player_Stats
    segment that contains its turn (sparse logs skip segments). A segment starting at turn s replaces
    everything earlier at turns >= s in that age."""
    by_age: dict[str, dict[int, list[dict]]] = defaultdict(dict)
    for d, ref in sessions:
        f = d / f"{name}.csv"
        if not f.exists() or not ref:
            continue
        groups: dict[int, list[dict]] = defaultdict(list)
        j, last = 0, None
        for r in logs.read_table(f):
            t = _turn(r)
            if t is None:
                continue
            if (last is not None and t < last) or t > ref[j][1]:
                nxt = next((k for k in range(j + (last is not None and t < last), len(ref)) if ref[k][0] <= t <= ref[k][1]), None)
                j = nxt if nxt is not None else min(j + 1, len(ref) - 1)
            groups[j].append(r)
            last = t
        for k in sorted(groups):
            age = ref[k][2]
            if age is None:
                continue
            start = ref[k][0]
            turns = by_age[age]
            for t in [t for t in turns if t >= start]:
                del turns[t]
            for r in groups[k]:
                turns.setdefault(_turn(r), []).append(r)
    return {age: [r for t in sorted(ts) for r in ts[t]] for age, ts in by_age.items()}


# ---------------------------------------------------------------- export

class Clock:
    """Maps (age, turn) to a game-wide index g; ages are laid end to end."""

    def __init__(self, lengths: dict[str, int]):
        self.lengths = {a: lengths.get(a, 0) for a in AGES}
        self.offset, acc = {}, 0
        for a in AGES:
            self.offset[a] = acc
            acc += self.lengths[a]

    def at(self, age: str, turn: int | None, lo: int | None = None, hi: int | None = None) -> dict | None:
        if turn is None or age not in self.offset:
            return None
        p = {"age": age, "turn": turn, "g": self.offset[age] + turn}
        if lo is not None and hi is not None and lo != hi:
            p["lo"], p["hi"] = lo, hi
        return p


def _loc_city(key: str, names: dict[str, str]) -> str:
    return names.get(key, key)


def _item_name(item: str, names: dict[str, str]) -> str:
    """BUILDING_X / PROJECT_X / UNIT_X -> display name via LOC_<TYPE>_NAME; LOC_ keys directly."""
    if item.startswith("LOC_"):
        return names.get(item, item)
    return names.get(f"LOC_{item}_NAME", item)


def _ev(kind: str, start: dict | None, end: dict | None, players: list[int], subject: str, source: str,
        precision: str = "exact", **detail) -> dict:
    e = {"kind": kind, "start": start, "end": end, "players": players, "subject": subject,
         "source": source, "precision": precision}
    if detail:
        e["detail"] = {k: v for k, v in detail.items() if v not in (None, "")}
    return e


def _diff_states(kind: str, captures_by_age: dict[str, list[dict]], key, clock: Clock, label) -> list[dict]:
    """Bracket changes between consecutive captures of an age: item first seen at capture b, absent at a
    -> happened in (a, b]. Items present at the age's first capture are dated (1, turn]."""
    out = []
    for age, caps in captures_by_age.items():
        prev: dict[int, set] = {}
        prev_turn = 0
        for cap in caps:
            t = cap["game"]["turn"]
            for p in cap["players"]:
                now = key(p)
                for item in sorted(now - prev.get(p["id"], set())):
                    lo = prev_turn + 1
                    out.append(_ev(kind, clock.at(age, t, lo, t), clock.at(age, t, lo, t), [p["id"]], label(item),
                                   f"capture:{age}", "range" if lo != t else "exact", item=item))
                prev[p["id"]] = now
            prev_turn = t
    return out


def export(seed: str | None = None, out_root: Path | None = None) -> Path:
    out_root = out_root or GAMES  # resolved at call time so tests/scratch runs can redirect export.GAMES
    captures = load_captures(seed) if seed else []
    if seed is None:
        seeds = sorted((HISTORY.glob("*") if HISTORY.exists() else []), key=lambda d: max((_stamp(f) for f in d.glob("*.json")), default=""))
        seed = seeds[-1].name if seeds else max(SNAPSHOTS.glob("*"), key=lambda d: d.stat().st_mtime).name
        captures = load_captures(seed)
    snaps = load_snapshots(seed)
    from .rules import text_index

    names = text_index()

    # ages present, from every source
    snap_stamps = sorted((s["_stamp"], s["game"]["age"]) for s in snaps)
    dirs = _sessions()
    owners = session_owners(dirs)
    sessions = [(d, session_segments(d, _session_last_age(d, snap_stamps))) for d in dirs if owners[d] in (seed, None)]
    L = {n: merged_logs(n, sessions) for n in
         ("Player_Stats", "DiplomacySummary", "Historian", "Game_Religion", "CityBuildQueue", "CityPurchase",
          "Player_Legacies", "CombatLog", "Game_Gossip", "Independents", "Game_TradeManager", "Player_Treasury")}

    # save metadata: the save-file game matching this seed (none yet for a brand-new game)
    guid, tl = save_timeline(seed, snaps)
    infos = [e.info for e in tl if not e.abandoned]
    head = infos[-1] if infos else None
    rosters = {}
    for i in infos:
        rosters[i.age] = [{"slot": p.slot, "leader": p.leader, "civilization": p.civilization, "is_human": p.is_human}
                          for p in i.players if p.level.endswith("FULL_CIV")]

    # age lengths
    lengths: dict[str, int] = defaultdict(int)
    for cap in captures:
        a = cap["game"]["age"]
        lengths[a] = max(lengths[a], cap["game"]["turn"])
        for vt in (cap.get("victories") or {}).values():
            for pa, per in (vt.get("past") or {}).items():
                lengths[pa] = max([lengths[pa]] + [len(h) for h in per.values()])
            lengths[a] = max([lengths[a]] + [len(h) for h in (vt.get("current") or {}).values()])
    for s in snaps:
        a = s["game"]["age"]
        lengths[a] = max(lengths[a], s["game"]["turn"])
    for i in infos:
        if i.age and i.turn:
            lengths[i.age] = max(lengths[i.age], i.turn)
    for rows_by_age in L.values():
        for a, rows in rows_by_age.items():
            lengths[a] = max([lengths[a]] + [_turn(r) or 0 for r in rows])
    clock = Clock(lengths)

    # players: majors from captures (ids persist across ages), names per age
    players: dict[int, dict] = {}
    for cap in captures:
        a = cap["game"]["age"]
        for p in cap["players"]:
            row = players.setdefault(p["id"], {"id": p["id"], "leader": p.get("leader"), "leader_type": p.get("leader_type"),
                                               "is_human": p.get("is_human"), "civilizations": {}})
            row["civilizations"][a] = p.get("civilization")
    # majors seen only in save rosters (no capture yet): leader per slot
    for a in AGES:
        for r in rosters.get(a, []):
            if r["slot"] is None:
                continue
            row = players.setdefault(r["slot"], {"id": r["slot"], "leader": names.get(f"LOC_{r['leader']}_NAME", r["leader"]),
                                                 "leader_type": r["leader"], "is_human": r["is_human"], "civilizations": {}})
            row["civilizations"].setdefault(a, names.get(f"LOC_{r['civilization']}_NAME", r["civilization"]))
    # majors seen only in snapshots (new game: no capture or save yet): the player and met rivals
    for snap in snaps:
        a, me = snap["game"]["age"], snap.get("me") or {}
        rows = [{"id": me.get("id", 0), "leader": me.get("leader"), "civilization": me.get("civilization"), "is_human": True}]
        rows += [p for p in snap.get("players") or [] if p.get("met")]
        for p in rows:
            if p.get("id") is None:
                continue
            row = players.setdefault(p["id"], {"id": p["id"], "leader": p.get("leader"), "leader_type": None,
                                               "is_human": p.get("is_human"), "civilizations": {}})
            row["civilizations"].setdefault(a, p.get("civilization"))
    minors: dict[str, dict[int, str]] = defaultdict(dict)
    for cap in captures:
        for m in cap.get("minors") or []:
            minors[cap["game"]["age"]][m["id"]] = m["name"]

    caps_by_age: dict[str, list[dict]] = defaultdict(list)
    for cap in captures:
        caps_by_age[cap["game"]["age"]].append(cap)

    events: list[dict] = []

    # ---- age spans
    for a in AGES:
        if clock.lengths[a]:
            events.append(_ev("age", clock.at(a, 1), clock.at(a, clock.lengths[a]), [], a, "derived"))

    # ---- wars, alliances, city captures (DiplomacySummary: status changes; turn 1 = state at session start)
    for age, rows in L["DiplomacySummary"].items():
        open_: dict[tuple, dict] = {}
        alli: dict[tuple, dict] = {}
        for r in rows:
            t, act = _turn(r), r.get("Action")
            try:
                a, b = int(r["Initiator"]), int(r["Recipient"])
            except (KeyError, ValueError):
                continue
            pair = tuple(sorted((a, b)))
            if act == "At War" and pair not in open_:
                open_[pair] = _ev("war", clock.at(age, t), None, list(pair), f"war {pair[0]}-{pair[1]}",
                                  "log:DiplomacySummary", war_type=r.get("Details"), declared_by=a)
                events.append(open_[pair])
            elif act == "Peace" and pair in open_:
                open_.pop(pair)["end"] = clock.at(age, t)
            elif act == "Allied" and pair not in alli:
                alli[pair] = _ev("alliance", clock.at(age, t), None, list(pair), f"alliance {pair[0]}-{pair[1]}", "log:DiplomacySummary")
                events.append(alli[pair])
            elif act == "Alliance Ends" and pair in alli:
                alli.pop(pair)["end"] = clock.at(age, t)
            elif act == "City Capture":
                events.append(_ev("settlement_captured", clock.at(age, t), clock.at(age, t), [a, b],
                                  _loc_city(r.get("Details", ""), names), "log:DiplomacySummary", captured_by=a, from_player=b))
            elif act == "Diplomacy Action Started" and r.get("Details") == "Declare War":
                events.append(_ev("war_declared", clock.at(age, t), clock.at(age, t), [a, b], f"{a} declares war on {b}",
                                  "log:DiplomacySummary", declared_by=a))
        # wars open at the age's last logged turn: an age transition ends them
        for e in list(open_.values()) + list(alli.values()):
            e["end"] = clock.at(age, clock.lengths[age])
            e["detail"] = {**e.get("detail", {}), "end_reason": "age_end_or_ongoing"}

    # wars seen only in captures (ages without logs): state at the captured turn
    logged_ages = set(L["DiplomacySummary"])
    for age, caps in caps_by_age.items():
        if age in logged_ages:
            continue
        for cap in caps:
            t = cap["game"]["turn"]
            seen = set()
            for p in cap["players"]:
                for q in p.get("at_war_with") or []:
                    pair = tuple(sorted((p["id"], q)))
                    if pair in seen:
                        continue
                    seen.add(pair)
                    events.append(_ev("war", clock.at(age, t), clock.at(age, t), list(pair), f"war {pair[0]}-{pair[1]}",
                                      f"capture:{age}", "state"))

    # ---- research and civics: captures (all players, bracketed), refined by snapshots (local, exact)
    tech_label = lambda item: item[1]
    events += _diff_states("tech", caps_by_age, lambda p: {(n["node"], n["name"]) for n in p.get("techs") or []}, clock, tech_label)
    events += _diff_states("civic", caps_by_age, lambda p: {(n["node"], n["name"]) for n in p.get("civics") or []}, clock, tech_label)
    for a in ("tech", "civic"):
        for e in events:
            if e["kind"] == a and isinstance(e.get("detail", {}).get("item"), tuple):
                e["detail"]["node"] = e["detail"].pop("item")[0]
    events += _local_progress(snaps, clock)

    # ---- government, religion: per-capture state, then snapshot exact turns for the local player
    events += _spans("government", caps_by_age, lambda p: p.get("government"), clock)
    events += _spans("religion", caps_by_age, lambda p: (p.get("religion") or {}).get("name") or None, clock)
    for age, rows in L["Game_Religion"].items():
        for r in rows:
            if r.get("Action") == "Found Religion":
                t = _turn(r)
                events.append(_ev("religion_founded", clock.at(age, t), clock.at(age, t), [int(r["Player"])],
                                  names.get(r.get("Belief", ""), r.get("Belief", "")), "log:Game_Religion", city=r.get("City")))

    # ---- settlements: first/last seen in captures (all players)
    events += _settlements(caps_by_age, clock)

    # ---- units killed (Historian: Player = owner of the lost unit, Opponent = killer)
    for age, rows in L["Historian"].items():
        for r in rows:
            if r.get("Type") not in ("UNIT_KILLED", "SHIP_SUNK"):
                continue
            t = _turn(r)
            try:
                victim, killer = int(r["Player"]), int(r["Opponent"])
            except (KeyError, ValueError):
                continue
            events.append(_ev("unit_killed", clock.at(age, t), clock.at(age, t), [victim, killer], r.get("Unit", ""),
                              "log:Historian", victim=victim, killer=killer, x=r.get("X"), y=r.get("Y"),
                              naval=r.get("Type") == "SHIP_SUNK"))

    # ---- production (projects, buildings, units, wonders): runs of the same item per city
    events += _production(L["CityBuildQueue"], clock, names)
    for age, rows in L["CityPurchase"].items():
        for r in rows:
            t = _turn(r)
            item = r.get("Item Purchased", "")
            events.append(_ev("purchase", clock.at(age, t), clock.at(age, t), [int(r["Player"])], _item_name(item, names),
                              "log:CityPurchase", city=_loc_city(r.get("City", ""), names), item=item))
    for age, rows in L["Player_Legacies"].items():
        for r in rows:
            t = _turn(r)
            events.append(_ev("legacy", clock.at(age, t), clock.at(age, t), [int(r["Player"])], r.get("Legacy", ""),
                              "log:Player_Legacies"))

    events += _diplomacy(L, clock, players, minors, snaps, names)

    majors = set(players)
    for e in events:
        if any(p not in majors for p in e["players"]):
            e["minor"] = True  # involves an independent power or city-state
    for i, e in enumerate(sorted(events, key=lambda e: ((e["start"] or {}).get("g", 0), e["kind"]))):
        e["id"] = i
    events.sort(key=lambda e: e["id"])

    # ---- series
    series = {"victory": _victory_series(captures), "stats": {}, "summary": None, "legacy_points": {}}
    for age, rows in L["Player_Stats"].items():
        series["stats"][age] = [{k: logs._num(v) if isinstance(v, str) else v for k, v in r.items()} for r in rows]
    if captures:
        last = captures[-1]
        series["summary"] = last.get("summary")
        series["legacy_points"] = {p["id"]: p.get("legacy_points") for p in last["players"]}

    meta = _game_meta(seed, guid, head, snaps, captures)
    combat_rewards = _combat_rewards(L, clock, players, rosters, snaps, names, meta)

    coverage = {
        "captures": [{"age": c["game"]["age"], "turn": c["game"]["turn"], "errors": c.get("errors") or []} for c in captures],
        "snapshots": {a: [s["game"]["turn"] for s in snaps if s["game"]["age"] == a] for a in AGES},
        "logs": {n: {a: [min(_turn(r) for r in rows), max(_turn(r) for r in rows)] for a, rows in by_age.items() if rows}
                 for n, by_age in L.items()},
    }

    game = {
        "schema": SCHEMA,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "game": meta,
        "ages": [{"age": a, "turns": clock.lengths[a], "g_offset": clock.offset[a]} for a in AGES],
        "players": sorted(players.values(), key=lambda p: p["id"]),
        "minors": {a: {str(k): v for k, v in m.items()} for a, m in minors.items()},
        "rosters": rosters,
        "saves": [{"age": i.age, "turn": i.turn, "name": i.path.name, "saved_at": i.saved_at.isoformat() if i.saved_at else None}
                  for i in infos],
        "coverage": coverage,
        "series": series,
        "events": events,
        "combat_rewards": combat_rewards["events"],
        "unexplained_gold": [{"age": a, "player": p, "turn": t, "value": v} for (a, p, t), v in combat_rewards["unexplained"].items()],
    }

    out = out_root / seed
    out.mkdir(parents=True, exist_ok=True)
    (out / "game.json").write_text(json.dumps(game, indent=1, default=_json_default))
    with (out / "events.jsonl").open("w") as fh:
        for e in events:
            fh.write(json.dumps(e, default=_json_default) + "\n")
    _write_csvs(out, game, clock)
    return out


def _game_meta(seed: str, guid: str | None, head, snaps: list[dict], captures: list[dict]) -> dict:
    """Game metadata from the newest matching save; from the newest snapshot until the game has one."""
    last = snaps[-1] if snaps else {}
    local = (captures[-1]["game"]["local_player_id"] if captures else (last.get("game") or {}).get("local_player_id")) or 0
    if head:
        return {"seed": seed, "guid": guid, "leader": head.host_leader, "civilization": head.host_civ, "difficulty": head.difficulty,
                "map": head.map_name, "map_size": head.map_size, "speed": head.game_speed, "app_version": head.app_version,
                "local_player_id": local}
    me, g = last.get("me") or {}, last.get("game") or {}
    diff = re.sub(r"^LOC_(.*)_NAME$", r"\1", g.get("difficulty") or "") or None
    return {"seed": seed, "guid": None, "leader": me.get("leader"), "civilization": me.get("civilization"), "difficulty": diff,
            "map": None, "map_size": None, "speed": None, "app_version": None, "local_player_id": local}


def _combat_rewards(L: dict, clock: Clock, players: dict[int, dict], rosters: dict, snaps: list[dict], names: dict[str, str],
                    meta: dict) -> dict:
    """combat.rewards() with each player's leader / civilization per age and the local player's slotted traditions."""
    from . import combat

    try:
        R = combat.load_rules()
    except OSError:  # game data not installed (tests, CI): log-only events, no kill rewards
        R = {"units": {}, "kill_rules": [], "independents": {}, "city_state_types": {}, "tradition_names": {}}
    by_name = defaultdict(set)
    for k, v in names.items():
        by_name[v].add(k)

    def type_of(display: str | None, prefix: str) -> str | None:
        hits = sorted(k[4:-5] for k in by_name.get(display or "", ()) if k.startswith(f"LOC_{prefix}_") and k.endswith("_NAME"))
        return hits[0] if hits else None

    local = meta.get("local_player_id") or 0
    trad_by_name = {names.get(loc, loc): t for t, loc in (R.get("tradition_names") or {}).items()}
    local_trads: dict[str, list[tuple[int, set]]] = defaultdict(list)
    notices: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for s in snaps:
        a, t, me = s["game"]["age"], s["game"]["turn"], s.get("me") or {}
        local_trads[a].append((t, {trad_by_name.get(n, n) for n in me.get("traditions") or []}))
        notices[(a, t)] += [n for n in (s.get("notifications") or {}).get("items") or []
                            if n.get("type") == "NOTIFICATION_DISPERSE_IP_REWARD"]

    def contexts(pid: int, age: str, turn: int) -> dict:
        p = players.get(pid) or {}
        leader = p.get("leader_type") or type_of(p.get("leader"), "LEADER")
        if pid == local and (meta.get("leader") or "").startswith("LEADER_"):
            leader = leader or meta["leader"]
        civ = next((r["civilization"] for r in rosters.get(age, []) if r["slot"] == pid), None) or \
            type_of((p.get("civilizations") or {}).get(age), "CIVILIZATION")
        trads = None
        if pid == local and local_trads.get(age):
            before = [ts for t, ts in local_trads[age] if t <= turn]
            trads = before[-1] if before else local_trads[age][0][1]
        return {"leader": leader, "civilization": civ, "traditions": trads}

    name_to_id = {p["leader"]: pid for pid, p in players.items() if p.get("leader")}
    return combat.rewards(L, clock, contexts, name_to_id, notices, R)


def _diplomacy(L: dict, clock: Clock, players: dict[int, dict], minors: dict[str, dict[int, str]], snaps: list[dict],
               names: dict[str, str]) -> list[dict]:
    """diplomacy.build() with leader names for majors and city-state names from captures and snapshots."""
    from . import diplomacy

    try:
        R = diplomacy.load_rules()
    except OSError:  # game data not installed (tests, CI)
        R = diplomacy.EMPTY_RULES
    known: dict[str, dict[int, str]] = defaultdict(dict)
    for s in snaps:
        for c in s.get("city_states") or []:
            if c.get("id") is not None and c.get("name"):
                known[s["game"]["age"]][c["id"]] = c["name"]
    for a, m in minors.items():
        known[a].update(m)
    majors = {pid: p.get("leader") for pid, p in players.items()}
    return diplomacy.build(L, clock, majors, known, names, R)


def _json_default(o):
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def _local_progress(snaps: list[dict], clock: Clock) -> list[dict]:
    """Local player's research/civic intervals from per-turn snapshots: from the first turn a node is
    being researched to the turn it stops being the active node."""
    out = []
    for field, kind in (("researching", "tech_research"), ("civic", "civic_research")):
        cur = None
        for s in snaps:
            me = s.get("me") or {}
            a, t = s["game"]["age"], s["game"]["turn"]
            r = me.get(field)
            key = (a, r.get("node"), r.get("name")) if r else None
            if cur and (key is None or key != cur["_key"]):
                cur["end"] = clock.at(a, t) if cur["_key"][0] == a else clock.at(cur["_key"][0], clock.lengths[cur["_key"][0]])
                out.append(cur)
                cur = None
            if key and cur is None:
                cur = _ev(kind, clock.at(a, t), None, [me.get("id", 0)], r.get("name"), "snapshot", node=r.get("node"))
                cur["_key"] = key
        if cur:
            out.append(cur)
    for e in out:
        e.pop("_key", None)
    return out


def _spans(kind: str, caps_by_age: dict[str, list[dict]], value, clock: Clock) -> list[dict]:
    """Intervals of a per-player value (government, religion) across captures within each age."""
    out = []
    for age, caps in caps_by_age.items():
        open_: dict[int, dict] = {}
        prev_turn = 0
        for cap in caps:
            t = cap["game"]["turn"]
            for p in cap["players"]:
                v = value(p)
                e = open_.get(p["id"])
                if e and e["subject"] != v:
                    e["end"] = clock.at(age, t, prev_turn, t)
                    del open_[p["id"]]
                    e = None
                if v and not e:
                    lo = prev_turn + 1 if prev_turn else 1
                    open_[p["id"]] = _ev(kind, clock.at(age, t, lo, t), None, [p["id"]], v, f"capture:{age}",
                                         "range" if lo != t else "exact")
                    out.append(open_[p["id"]])
            prev_turn = t
        for e in open_.values():
            e["end"] = clock.at(age, clock.lengths[age])
    return out


def _settlements(caps_by_age: dict[str, list[dict]], clock: Clock) -> list[dict]:
    """A settlement's span with one owner: first capture it appears in -> last one (or age end)."""
    out = []
    for age, caps in caps_by_age.items():
        open_: dict[tuple, dict] = {}
        prev_turn = 0
        for cap in caps:
            t = cap["game"]["turn"]
            here = set()
            for p in cap["players"]:
                for c in p.get("settlements") or []:
                    k = (p["id"], c["id"])
                    here.add(k)
                    if k not in open_:
                        lo = prev_turn + 1 if prev_turn else 1
                        open_[k] = _ev("settlement", clock.at(age, t, lo, t), None, [p["id"]], c["name"], f"capture:{age}",
                                       "range" if lo != t else "exact", type=c["type"], x=c["x"], y=c["y"],
                                       original_owner=c.get("original_owner"), capital=c.get("capital"))
                        out.append(open_[k])
                    open_[k]["detail"]["population"] = c.get("population")
            for k in [k for k in open_ if k not in here]:
                open_.pop(k)["end"] = clock.at(age, t, prev_turn, t)
            prev_turn = t
        for e in open_.values():
            e["end"] = clock.at(age, clock.lengths[age])
    return out


def _category(item: str) -> str:
    for pre, cat in (("PROJECT_", "project"), ("WONDER_", "wonder"), ("BUILDING_", "building"), ("UNIT_", "unit"),
                     ("IMPROVEMENT_", "improvement"), ("LOC_UNIT_", "unit")):
        if item.startswith(pre):
            return cat
    return "other"


def _production(rows_by_age: dict[str, list[dict]], clock: Clock, names: dict[str, str]) -> list[dict]:
    out = []
    for age, rows in rows_by_age.items():
        runs: dict[tuple, dict] = {}
        for r in rows:
            t = _turn(r)
            try:
                pl = int(r["Player"])
            except (KeyError, ValueError):
                continue
            city, item = r.get("City", ""), r.get("Current Item", "")
            k = (pl, city)
            run = runs.get(k)
            if run and run["detail"]["item"] != item:
                runs.pop(k)
                run = None
            if not item or item in ("NONE", "NO_ITEM"):
                continue
            if run is None:
                kind = "project" if _category(item) == "project" else "production"
                run = _ev(kind, clock.at(age, t), None, [pl], _item_name(item, names), "log:CityBuildQueue",
                          city=_loc_city(city, names), item=item, category=_category(item))
                runs[k] = run
                out.append(run)
            run["end"] = clock.at(age, t)
            try:  # Current Production already includes this turn's Production Added
                done = float(r.get("Current Production") or 0) >= float(r.get("Production Needed") or 1e18)
            except ValueError:
                done = False
            run["detail"]["completed"] = done
            if done:
                runs.pop(k)
    return out


def _victory_series(captures: list[dict]) -> dict:
    """victory[type][age][player_id] = per-turn points. A past age comes from the newest capture that has
    it; the current age of each capture from the newest capture made in that age."""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for cap in captures:  # oldest first, so newer captures overwrite
        a = cap["game"]["age"]
        for vt, d in (cap.get("victories") or {}).items():
            for pa, per in (d.get("past") or {}).items():
                if any(per.values()):
                    out[vt][pa] = per
            if any((d.get("current") or {}).values()):
                out[vt][a] = d["current"]
    return out


def _write_csvs(out: Path, game: dict, clock: Clock) -> None:
    with (out / "series_victory.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["victory", "age", "turn", "g", "player", "points"])
        for vt, ages in game["series"]["victory"].items():
            for a, per in ages.items():
                for pid, hist in per.items():
                    for i, v in enumerate(hist or []):
                        w.writerow([vt, a, i + 1, clock.offset.get(a, 0) + i + 1, pid, v])
    stats = game["series"]["stats"]
    if stats:
        cols = logs.PLAYER_STATS_COLS
        with (out / "series_stats.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["age", "g"] + cols)
            for a, rows in stats.items():
                for r in rows:
                    w.writerow([a, clock.offset.get(a, 0) + int(r.get("turn") or 0)] + [r.get(c) for c in cols])
    with (out / "events.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "kind", "start_age", "start_turn", "start_g", "start_lo", "start_hi", "end_age", "end_turn", "end_g",
                    "players", "subject", "precision", "source", "detail"])
        for e in game["events"]:
            s, en = e.get("start") or {}, e.get("end") or {}
            w.writerow([e["id"], e["kind"], s.get("age"), s.get("turn"), s.get("g"), s.get("lo"), s.get("hi"),
                        en.get("age"), en.get("turn"), en.get("g"), " ".join(map(str, e["players"])), e["subject"],
                        e["precision"], e["source"], json.dumps(e.get("detail") or {}, default=_json_default)])
