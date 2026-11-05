"""`civilizer advisor follow`: stream one line per new turn, for an advisor agent to wake on and judge.

Reads the store only (read-only SQLite connection); never touches the running game. The monitor writes
`meta.last_refresh` after each refresh; this polls it and prints the turn's facts. It does not filter: the advisor
decides whether the turn calls for a comment (does the player have a new action to take?).

    TURN g27 T27 | decide: civic (none chosen), production (Carthage finished Warrior) | done: tech Writing, civic Shipsheds | new alerts: ... | events: 4 (2 local)
    RELOAD g22 -> g20: turns were replayed (reload?)
    GAME -1906453415: Ibn Battuta / Carthage, now followed
    MONITOR stopped: ...
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import advisor, store

DONE_KINDS = {"tech_research": "tech", "civic_research": "civic"}


def _ro(path: Path | None) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path or store.DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _meta(con, key: str):
    raw = store.get_meta(con, key)
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return raw


def turn_line(b: dict, prev_alerts: set[str]) -> str:
    """One line of facts for the turn: decisions the player now faces, what finished, new alerts, event counts."""
    me = b["game"].get("local_player_id")
    summary = b.get("local_summary") or {}
    mine = [e for e in b["events_since"] if me in (e.get("players") or [])]
    decide, done = [], []
    if summary and not summary.get("researching"):
        decide.append("research (none chosen)")
    if summary and not summary.get("civic"):
        decide.append("civic (none chosen)")
    for e in mine:
        if e["kind"] == "production" and e.get("phase") == "ended":
            decide.append(f"production ({e.get('city') or '?'} finished {e.get('subject')})")
        elif e["kind"] in DONE_KINDS and e.get("phase") == "ended":
            done.append(f"{DONE_KINDS[e['kind']]} {e.get('subject')}")
    parts = [f"TURN g{b['game']['current_g']} T{b['game']['current_turn']}"]
    if decide:
        parts.append("decide: " + ", ".join(decide))
    if done:
        parts.append("done: " + ", ".join(done))
    new_alerts = [a for a in b["alerts"] if a not in prev_alerts]
    if new_alerts:
        parts.append("new alerts: " + "; ".join(new_alerts))
    parts.append(f"events: {len(b['events_since'])} ({len(mine)} local)")
    return " | ".join(parts)


class Follower:
    """Remembers the last turn seen and the alerts already reported; `poll()` returns the lines to print."""

    def __init__(self, db: Path | None = None):
        self.db = db
        self.game = self.last_g = None
        self.prev_alerts: set[str] = set()
        self.monitor_state = self.last_error = None

    def poll(self) -> list[str]:
        try:
            con = _ro(self.db)
        except sqlite3.OperationalError:
            return []
        try:
            return self._poll(con)
        except (sqlite3.OperationalError, advisor.AdvisorError) as e:
            msg = f"ERROR {type(e).__name__}: {e}"
            if msg == self.last_error:
                return []
            self.last_error = msg
            return [msg]
        finally:
            con.close()

    def _poll(self, con) -> list[str]:
        out = []
        mon = _meta(con, "monitor")
        state = mon.get("state") if isinstance(mon, dict) else None
        if state != self.monitor_state and self.monitor_state is not None:
            out.append(f"MONITOR {state}: {mon.get('last_error') or 'state changed'}")
        self.monitor_state = state

        lr = _meta(con, "last_refresh")
        if not lr or lr.get("g") is None:
            return out
        g = lr["g"]
        if lr["game"] != self.game:
            b = advisor.briefing(con, lr["game"], None, 25)
            gi = b["game"]
            self.game, self.last_g = lr["game"], g
            self.prev_alerts = set(b["alerts"])
            return out + [f"GAME {self.game}: {gi['leader']} / {gi['civilization']}, {gi['current_age']} T{gi['current_turn']} "
                          f"(g{g}), now followed"]
        if g == self.last_g:
            return out
        if g < self.last_g:
            out.append(f"RELOAD g{self.last_g} -> g{g}: turns were replayed (reload?)")
            self.last_g = g
            return out
        b = advisor.briefing(con, self.game, self.last_g, 50)
        out.append(turn_line(b, self.prev_alerts))
        self.prev_alerts = set(b["alerts"])
        self.last_g = g
        return out


def follow(db: Path | None = None, interval: float = 5.0) -> None:
    f = Follower(db)
    while True:
        for line in f.poll():
            print(line, flush=True)
        time.sleep(interval)
