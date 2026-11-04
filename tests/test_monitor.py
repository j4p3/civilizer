"""Offline tests for the live monitor: live, logs and ingest are faked; the store is a temp file."""

import json

import pytest

from civilizer import live, monitor, store


class Game:
    """Scripted Game.turn values; a LiveError instance in the script means the game is unavailable."""

    def __init__(self, script, age="AGE_MODERN", seed=42):
        self.script, self.age, self.seed = list(script), age, seed
        self.turn = None
        self.polls = self.snapshots = self.archives = self.refreshes = 0
        self.refresh_fails = 0

    def raw_eval(self, expr):
        assert expr == "Game.turn", "monitor may only poll Game.turn"
        self.polls += 1
        if not self.script:
            raise KeyboardInterrupt
        v = self.script.pop(0)
        if isinstance(v, Exception):
            raise v
        self.turn = v
        return v

    def snapshot(self, **kw):
        assert kw == {"detail": True, "map": True}
        self.snapshots += 1
        return {"game": {"seed": self.seed, "turn": self.turn, "age": self.age}}

    def archive(self):
        self.archives += 1

    def refresh(self, seed):
        self.refreshes += 1
        if self.refresh_fails:
            self.refresh_fails -= 1
            raise ValueError("ingest bug")
        with store.tx() as con:
            con.execute("INSERT OR REPLACE INTO games (game, current_age, current_turn, current_g) VALUES (?,?,?,?)",
                        (seed, self.age, self.turn, 300 + self.turn))
        return {"game": seed, "age": self.age, "turn": self.turn, "metrics": 1, "events": 2, "turns": 3}


@pytest.fixture
def game(tmp_path, monkeypatch):
    def make(script, **kw):
        g = Game(script, **kw)
        monkeypatch.setattr(store, "DB", tmp_path / "civilizer.db")
        monkeypatch.setattr(monitor, "SNAPSHOTS", tmp_path / "snapshots")
        monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
        monkeypatch.setattr(live, "raw_eval", g.raw_eval)
        monkeypatch.setattr(live, "snapshot", g.snapshot)
        monkeypatch.setattr(monitor.logs, "archive", g.archive)
        monkeypatch.setattr(monitor.ingest, "refresh", g.refresh)
        beats = []
        real = monitor.heartbeat
        monkeypatch.setattr(monitor, "heartbeat", lambda hb: (real(hb), beats.append(dict(hb))))
        g.beats, g.tmp = beats, tmp_path
        return g
    return make


def test_one_snapshot_and_refresh_per_turn_including_reloads(game):
    unavailable = live.LiveError("No in-game UI page")
    g = game([80, 80, 81, 81, unavailable, unavailable, 81, 79])
    monitor.run(interval=10)
    # 80, 81, 81 again after the game came back, 79 after a reload
    assert (g.snapshots, g.refreshes) == (4, 4)
    assert g.archives == 4 + 1  # once per snapshot, once (not twice) when the game went away
    assert len(list((g.tmp / "snapshots" / "42").glob("T*.json"))) >= 3  # same-second T0081 files may collide
    assert [b["state"] for b in g.beats] == ["watching"] * 4 + ["game_unavailable"] * 2 + ["watching"] * 2 + ["stopped"]
    hb = monitor.read_heartbeat()
    assert hb["state"] == "stopped" and hb["last_turn"] == 79 and hb["last_g"] == 379 and hb["last_age"] == "AGE_MODERN"
    assert set(hb) >= {"state", "pid", "seen_at", "last_turn", "last_age", "last_g", "last_refresh_at", "last_error", "interval"}
    assert hb["interval"] == 10 and hb["last_error"] is None


def test_ingest_error_keeps_running_and_does_not_resnapshot(game):
    g = game([80] * 5 + [81] * 3)
    g.refresh_fails = 10
    monitor.run()
    assert g.snapshots == 2  # one per turn despite the failures
    assert g.refreshes == 2 * monitor.MAX_RETRIES
    states = [b["state"] for b in g.beats]
    assert states[:-1] == ["error"] * 8
    assert g.beats[0]["last_error"] == "ValueError: ingest bug"


def test_refresh_retry_recovers(game):
    g = game([80, 80, 80])
    g.refresh_fails = 1
    monitor.run()
    assert (g.snapshots, g.refreshes) == (1, 2)
    assert [b["state"] for b in g.beats] == ["error", "watching", "watching", "stopped"]
    assert g.beats[1]["last_turn"] == 80 and g.beats[1]["last_error"] is None


def test_once_returns_status(game):
    g = game([81])
    assert monitor.run(once=True) == 0
    assert g.polls == 1 and g.snapshots == 1
    with store.tx() as con:
        assert con.execute("SELECT current_turn FROM games WHERE game='42'").fetchone()[0] == 81
    g = game([live.LiveError("not reachable")])
    assert monitor.run(once=True) == 1
    assert g.snapshots == 0 and g.archives == 1
    assert g.beats[0]["state"] == "game_unavailable" and "not reachable" in g.beats[0]["last_error"]
    assert json.loads(json.dumps(monitor.read_heartbeat()))["state"] == "stopped"
