"""Offline tests for `civilizer advisor follow` (follow.py) on a temporary store."""

import json

from test_advisor import GAME, _insert, _metric, _set_turn, db, v7_store  # noqa: F401  (db is a fixture)

from civilizer import advisor, follow, store


def _refresh(con, g: int) -> None:
    _set_turn(con, g)
    store.set_meta(con, "last_refresh", {"game": GAME, "at": "2026-11-03T00:00:00", "g": g})


def _run(db, steps):
    """Run follow for one poll per step; each step mutates the store before its poll. Returns printed lines."""
    f, lines = follow.Follower(db), []
    for step in steps:
        with store.tx() as con:
            step(con)
        lines += f.poll()
    return lines


def test_follow_reports_every_turn_with_decisions_and_new_alerts(db):
    def lose_unit(con):
        _metric(con, 11, 0, "units_lost_total", 2)
        _refresh(con, 11)

    def built(con):
        ev = dict(game=GAME, start_age="AGE_ANTIQUITY", local=1, minor=0, precision="exact", source="snapshot")
        _insert(con, "events", **ev, event_key="w", kind="production", subject="Warrior", start_g=10, start_turn=10, end_g=12,
                end_turn=12, players="[0]", detail=json.dumps({"city": "Rome", "completed": True}))
        _insert(con, "events", **ev, event_key="t", kind="tech_research", subject="Pottery", start_g=5, start_turn=5, end_g=12,
                end_turn=12, players="[0]", detail="{}")
        _refresh(con, 12)

    lines = _run(db, [lambda con: _refresh(con, 10), lose_unit, built, lambda con: _refresh(con, 13)])
    assert lines[0].startswith(f"GAME {GAME}: ")
    assert lines[1].startswith("TURN g11 T11 | decide: civic (none chosen) | new alerts: Lost 1 unit(s)")
    assert "production (Rome finished Warrior)" in lines[2] and "done: tech Pottery" in lines[2]
    assert "Lost 1 unit" not in lines[2]  # an alert is reported once, not every turn it stays true
    assert lines[3].startswith("TURN g13 T13")  # every turn gets a line; the advisor decides whether to speak
    assert len(lines) == 4


def test_follow_flags_reloads(db):
    lines = _run(db, [lambda con, g=g: _refresh(con, g) for g in (10, 11, 12)])
    assert lines[2].startswith("TURN g12")
    assert _run(db, [lambda con: _refresh(con, 12), lambda con: _refresh(con, 11)])[1] == \
        "RELOAD g12 -> g11: turns were replayed (reload?)"


def test_follow_never_writes_the_store(db):
    with store.tx() as con:
        _refresh(con, 10)
    con = follow._ro(db)
    try:
        con.execute("INSERT INTO meta VALUES ('x', 'y')")
        raise AssertionError("read-only connection accepted a write")
    except Exception as e:
        assert "readonly" in str(e)
    finally:
        con.close()


def test_unset_victory_targets_do_not_alert(db):
    with store.tx() as con:
        b = advisor.briefing(con, GAME, None, 25)
    standings = {"vp_culture": {"rank": 1, "of": 3, "value": 0, "target": -1}}
    assert not [a for a in advisor._alerts(b["state"], standings, []) if "victory target" in a]
    assert json.dumps(b)  # briefing stays JSON-able


def test_follow_on_a_store_migrated_from_verdicts(tmp_path, monkeypatch):
    """A v7 store with verdict annotations: after migration the briefing (recent_annotations) and follow still work."""
    path = tmp_path / "v7.db"
    con = v7_store(path)
    con.execute("INSERT INTO games (game, local_player_id, current_age, current_turn, current_g, updated_at) VALUES (?,?,?,?,?,?)",
                (GAME, 0, "AGE_ANTIQUITY", 10, 10, "2026-11-03T00:00:00"))
    con.execute("INSERT INTO ages VALUES (?,?,?,?,?)", (GAME, "AGE_ANTIQUITY", 0, 10, 0))
    con.execute("INSERT INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (GAME, 1, 9, "AGE_ANTIQUITY", 9, "player", "outcome", "Fleet dropped", "went_awry", None, None, None, None, None, "x"))
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB", path)
    lines = _run(path, [lambda con: _refresh(con, 10), lambda con: _refresh(con, 11)])
    assert lines[0].startswith(f"GAME {GAME}: ") and lines[1].startswith("TURN g11")
    with store.tx() as con:
        notes = advisor.briefing(con, GAME)["recent_annotations"]
    assert notes[0]["outcome"] == "failure" and "verdict" not in notes[0]
