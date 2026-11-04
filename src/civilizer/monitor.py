"""Live monitor: one snapshot per game turn -> snapshot archive -> logs archive -> store refresh.

Polls only `Game.turn` between turns (the same read panel-system-bar.js does), so the game sees one cheap
evaluation every `interval` seconds and one vetted snapshot.js run per turn. A heartbeat goes into the
store's `meta` table under key `monitor` on every poll so the dashboard can tell a quiet game from a dead
monitor. Errors never end the loop: an ingest bug should cost one turn's refresh, not the watcher.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import ingest, live, logs, store
from .export import SNAPSHOTS

HEARTBEAT = "monitor"
MAX_RETRIES = 3  # refresh attempts per turn; a persistent ingest bug then waits for the next turn


def archive_snapshot(s: dict) -> Path:
    """Same layout as civilizer._archive_snapshot (data/snapshots/<seed>/T####-stamp.json), kept here to avoid a
    cycle with the CLI module."""
    g = s.get("game") or {}
    d = SNAPSHOTS / str(g.get("seed", "unknown"))
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"T{g.get('turn', 0):04d}-{datetime.now():%Y%m%dT%H%M%S}.json"
    p.write_text(json.dumps(s, indent=1))
    return p


def heartbeat(hb: dict) -> None:
    hb["seen_at"] = store.now()
    try:
        with store.tx() as con:
            store.set_meta(con, HEARTBEAT, hb)
    except Exception as e:  # a locked/broken store must not stop the watcher either
        print(f"{datetime.now():%H:%M:%S} heartbeat write failed: {e}", file=sys.stderr, flush=True)


def read_heartbeat() -> dict | None:
    with store.tx() as con:
        v = store.get_meta(con, HEARTBEAT)
    return json.loads(v) if v else None


def take_snapshot() -> str:
    """Snapshot the live game and archive it and the logs. Returns the seed."""
    s = live.snapshot(detail=True, map=True)
    archive_snapshot(s)
    logs.archive()
    return str((s.get("game") or {}).get("seed"))


def refresh(seed: str) -> dict:
    """ingest.refresh plus timing and the game-wide turn index the dashboard shows."""
    t0 = time.monotonic()
    r = ingest.refresh(seed)
    r["seconds"] = round(time.monotonic() - t0, 2)
    with store.tx() as con:
        row = con.execute("SELECT current_g FROM games WHERE game=?", (r["game"],)).fetchone()
    r["g"] = row[0] if row else None
    return r


def run(interval: int = 15, once: bool = False) -> int:
    """Poll forever (or one pass with once=True). Returns 0 when the last pass ingested a turn, else 1."""
    hb = {"state": "watching", "pid": os.getpid(), "seen_at": None, "last_turn": None, "last_age": None, "last_g": None,
          "last_refresh_at": None, "last_error": None, "interval": interval, "game": None}
    last_turn = None          # turn last snapshotted in this process
    pending: str | None = None  # seed whose refresh failed; retried without touching the game
    retries = 0
    logs_archived = False     # archived once since the game became unavailable
    rc = 1

    def _term(*_):
        raise KeyboardInterrupt
    prev_term = signal.signal(signal.SIGTERM, _term)
    if not once:
        print(f"monitor: polling Game.turn every {interval}s, store {store.DB}; Ctrl-C to stop", file=sys.stderr, flush=True)
    try:
        while True:
            polled = False
            try:
                turn = live.raw_eval("Game.turn")  # panel-system-bar.js reads Game.turn
                polled, logs_archived = True, False
                # a reload to an earlier turn also differs: the kept-branch rule in export/ingest drops the old turns
                if turn != last_turn:
                    last_turn = turn  # set first: a failed snapshot is not retried until the turn changes
                    pending, retries = take_snapshot(), 0
                if pending and retries < MAX_RETRIES:
                    retries += 1
                    r = refresh(pending)
                    pending = None
                    hb.update(state="watching", last_turn=r["turn"], last_age=r["age"], last_g=r["g"], game=r["game"],
                              last_refresh_at=store.now(), last_error=None)
                    print(f"{datetime.now():%H:%M:%S} {r['age']} T{r['turn']} (g {r['g']}): refreshed in {r['seconds']}s · "
                          f"{r['turns']} turns, {r['events']} events, {r['metrics']} metric rows", flush=True)
                    rc = 0
                elif not pending:
                    hb.update(state="watching")
            except Exception as e:
                rc = 1
                if isinstance(e, live.LiveError) and not polled:  # main menu, loading screen, game closed
                    hb.update(state="game_unavailable", last_error=str(e))
                    last_turn, pending = None, None  # whatever loads next gets a fresh snapshot
                    if not logs_archived:
                        logs_archived = True
                        try:
                            logs.archive()
                            print(f"{datetime.now():%H:%M:%S} game unavailable ({e}); logs archived", flush=True)
                        except OSError as oe:
                            print(f"{datetime.now():%H:%M:%S} game unavailable; log archive failed: {oe}", file=sys.stderr, flush=True)
                else:  # snapshot or ingest failure: log it and keep watching
                    traceback.print_exc(file=sys.stderr)
                    hb.update(state="error", last_error=f"{type(e).__name__}: {e}")
                    print(f"{datetime.now():%H:%M:%S} error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            heartbeat(hb)
            if once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, prev_term)
    hb["state"] = "stopped"
    heartbeat(hb)
    return rc
