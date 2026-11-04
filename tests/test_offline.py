"""Offline tests: save parsing, timeline branch detection, log parsing, report rendering."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from civilizer import logs, report, save, timeline

SAVES = sorted(timeline.find_saves()) if timeline.SAVE_DIR.exists() else []
needs_saves = pytest.mark.skipif(not SAVES, reason="no local Civ VII saves")


@needs_saves
def test_header_roster_and_metadata():
    info = save.read_header(SAVES[-1])
    assert info.turn and info.turn > 0
    assert info.age.startswith("AGE_")
    assert info.saved_at and info.saved_at.year >= 2025
    humans = [p for p in info.players if p.is_human]
    assert len(humans) == 1 and humans[0].slot is not None


@needs_saves
def test_body_decompresses_and_balances_index_by_player_id():
    p = SAVES[-1]
    info = save.read_header(p)
    body = save.decompress(p.read_bytes())
    assert body and len(body) > 1_000_000
    gold = save.body_balances(body, "GOLD")
    full = [x for x in info.players if x.level == "CIVILIZATION_LEVEL_FULL_CIV"]
    assert len(gold) >= max(x.slot for x in full) + 1


def _info(turn, age, minute):
    i = save.SaveInfo(path=Path(f"/x/{age}{turn}-{minute}.Civ7Save"), size=0, turn=turn, age=age, game_guid="g")
    i.saved_at = datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc)
    return i


def test_timeline_marks_reloaded_branch():
    a = _info(10, "AGE_EXPLORATION", 1)
    b = _info(12, "AGE_EXPLORATION", 2)  # played ahead...
    c = _info(11, "AGE_EXPLORATION", 3)  # ...then reloaded turn 11
    d = _info(12, "AGE_EXPLORATION", 4)
    tl = timeline.timeline([a, b, c, d])
    assert [e.abandoned for e in tl] == [False, True, False, False]


def test_timeline_age_change_is_not_a_reload():
    a = _info(120, "AGE_ANTIQUITY", 1)
    b = _info(1, "AGE_EXPLORATION", 2)
    assert not any(e.abandoned for e in timeline.timeline([a, b]))


def test_player_stats_columns(tmp_path):
    f = tmp_path / "Player_Stats.csv"
    f.write_text(
        "Game Turn, Player, Cities\n"
        "111, 0, 3, 10, 13, 0, 91, 85, 14, 10, 8, 314, 85, 920.1, 306.0, 146.8, 392.3, 334.0, 393.8, 288.8,  45.0, 836.0, 477.0\n"
    )
    row = logs.read_table(f)[0]
    assert row["gold_balance"] == "920.1" and row["gold"] == "392.3" and row["influence"] == "45.0"


def test_live_report_renders_minimal_snapshot():
    s = {"game": {"turn": 5, "age": "AGE_ANTIQUITY"}, "me": {"id": 0, "leader": "L", "civilization": "C"}, "settlements": [], "errors": []}
    out = report.live_report(s)
    assert out.startswith("# Turn 5")


def test_map_render_marks_settlement_and_owner():
    from civilizer import mapview

    snap = {
        "me": {"id": 0},
        "settlements": [{"name": "Testville", "x": 5, "y": 5}],
        "map": {
            "columns": ["x", "y", "vis", "terrain", "feature", "resource", "owner", "river"],
            "legend": {"terrain": ["TERRAIN_FLAT", "TERRAIN_OCEAN"], "feature": [], "resource": ["RESOURCE_FURS"], "owner": {"0": "Me"}},
            "plots": [[5, 5, 2, 0, -1, -1, 0, 0], [6, 5, 1, 1, -1, -1, -1, 0], [4, 5, 2, 0, -1, 0, 0, 0]],
        },
    }
    out = mapview.render(snap, "Testville", radius=1)
    assert "@T*" in out and "~~" in out and "RESOURCE_FURS owner=you" in out


def test_unquoted_comma_city_name_is_rejoined(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "_comma_names", lambda: [["Washington", "D.C."]])
    f = tmp_path / "Game_TradeManager.csv"
    f.write_text(
        "Turn, Event, Initiating Player City, Resource City, Attached Resources, Resource City Yields\n"
        "118, Route Started, Mitiyana, Washington, D.C.,Kaolin:4448 Iron:4639 , 18\n"
    )
    row = logs.read_table(f)[0]
    assert row["Resource City"] == "Washington, D.C." and row["Resource City Yields"] == "18"


def test_report_explains_no_resources_route():
    s = {
        "game": {"turn": 5, "age": "AGE_EXPLORATION"}, "me": {"id": 0, "leader": "L", "civilization": "C"},
        "settlements": [], "errors": [],
        "trade": {"routes": {"established": [], "available": [], "unavailable": [
            {"from": "New Orleans", "to": "Mparntwe", "to_owner": 5, "domain": "land", "imports": [], "why": ["NO_RESOURCES"]}]}},
    }
    assert "New Orleans→Mparntwe" in report.live_report(s) and "no resources to trade" in report.live_report(s)


def test_prune_superseded_keeps_distinct_sessions(tmp_path):
    from civilizer import logs

    def arch(name, stats, extra=""):
        d = tmp_path / name
        d.mkdir()
        (d / "Player_Stats.csv").write_text(stats)
        (d / "CombatLog.csv").write_text(extra)
        return d

    a = arch("20261101T100000", "h\n1\n", "x\n")
    b = arch("20261101T100100", "h\n1\n2\n", "x\ny\n")  # same session, later: supersedes a
    c = arch("20261102T090000", "h\n1\n", "z\n")  # new session (relaunch): not a prefix-extension of b
    assert logs.prune_superseded(tmp_path) == [a]
    assert b.exists() and c.exists()
