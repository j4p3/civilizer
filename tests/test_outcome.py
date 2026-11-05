"""Game outcome: claimed victories -> games.status, and the v2 -> v3 store migration that added the columns."""

import sqlite3

from civilizer import ingest, store


def test_outcome_from_claimed_victories():
    assert ingest.outcome(None) == (None, [])  # snapshot predates the capture: unknown
    assert ingest.outcome([]) == ("in_progress", [])
    assert ingest.outcome([{"victory": "VICTORY_SCORE", "place": 2, "team": 1, "players": [1]}]) == ("in_progress", [])
    assert ingest.outcome([{"victory": "VICTORY_ECONOMIC_MODERN", "place": 1, "team": 0, "players": [0]}]) == (
        "over", [{"victory": "VICTORY_ECONOMIC_MODERN", "players": [0]}])


def test_migration_adds_outcome_columns(tmp_path):
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE games (game TEXT, guid TEXT, leader TEXT, civilization TEXT, difficulty TEXT, map TEXT, map_size TEXT, "
                "speed TEXT, app_version TEXT, local_player_id INTEGER, current_age TEXT, current_turn INTEGER, current_g INTEGER, "
                "age_turns_max INTEGER, updated_at TEXT, PRIMARY KEY (game))")
    old.execute("INSERT INTO games (game, updated_at) VALUES ('1', 'x')")
    old.commit()
    old.close()
    with store.tx(db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(games)")}
        assert {"status", "victories"} <= cols
        assert con.execute("SELECT game, status FROM games").fetchone()[:] == ("1", None)
