"""Offline tests for the whole-game export: log segmenting across reloads and ages, kept-branch merge."""

from pathlib import Path

from civilizer import export

STATS_HEADER = "Game Turn, Player, Cities\n"


def _session(tmp: Path, name: str, stats_turns: list[int], files: dict[str, str]) -> Path:
    d = tmp / name
    d.mkdir()
    (d / "Player_Stats.csv").write_text(STATS_HEADER + "".join(f"{t}, 0, 1\n" for t in stats_turns))
    for fn, body in files.items():
        (d / fn).write_text(body)
    return d


def test_segments_split_on_backwards_turns():
    assert export._segments([5, 5, 6, 7, 3, 4, 1, 2]) == [[5, 7], [3, 4], [1, 2]]


def test_session_running_into_next_age_and_restarting_it(tmp_path):
    # Exploration 116-140, new age at 1-3, then a reload of that age's turn 1 (still Modern)
    d = _session(tmp_path, "s1", list(range(116, 141)) + [1, 2, 3] + list(range(1, 24)), {})
    segs = export.session_segments(d, "AGE_MODERN")
    assert [(a, b, age) for a, b, age in segs] == [
        (116, 140, "AGE_EXPLORATION"),
        (1, 3, "AGE_MODERN"),
        (1, 23, "AGE_MODERN"),
    ]


def test_reload_drops_abandoned_turns_and_sparse_log_finds_its_segment(tmp_path):
    # turns 1-10, reload to 6, play to 8. A sparse log with rows at 9 (abandoned) and 7 (kept).
    stats = list(range(1, 11)) + [6, 7, 8]
    log = "Game Turn, Player, Item Purchased\n2, 0, A\n9, 0, ABANDONED\n7, 0, KEPT\n"
    d = _session(tmp_path, "s1", stats, {"CityPurchase.csv": log})
    segs = export.session_segments(d, "AGE_MODERN")
    rows = export.merged_logs("CityPurchase", [(d, segs)])["AGE_MODERN"]
    assert [r["Item Purchased"] for r in rows] == ["A", "KEPT"]


def test_clock_lays_ages_end_to_end():
    c = export.Clock({"AGE_ANTIQUITY": 100, "AGE_EXPLORATION": 120, "AGE_MODERN": 80})
    assert c.at("AGE_EXPLORATION", 1)["g"] == 101
    p = c.at("AGE_MODERN", 10, 5, 10)
    assert (p["g"], p["lo"], p["hi"]) == (230, 5, 10)


def test_capture_diff_brackets_new_items():
    caps = {"AGE_ANTIQUITY": [
        {"game": {"turn": 30}, "players": [{"id": 0, "techs": [{"node": "A", "name": "a"}]}]},
        {"game": {"turn": 60}, "players": [{"id": 0, "techs": [{"node": "A", "name": "a"}, {"node": "B", "name": "b"}]}]},
    ]}
    clock = export.Clock({"AGE_ANTIQUITY": 100})
    ev = export._diff_states("tech", caps, lambda p: {(n["node"], n["name"]) for n in p["techs"]}, clock, lambda i: i[1])
    by = {e["subject"]: e for e in ev}
    assert (by["a"]["start"]["lo"], by["a"]["start"]["hi"]) == (1, 30)
    assert (by["b"]["start"]["lo"], by["b"]["start"]["hi"]) == (31, 60)


def _snap(root: Path, seed: str, turn: int, stamp: str) -> None:
    d = root / seed
    d.mkdir(parents=True, exist_ok=True)
    (d / f"T{turn:04d}-{stamp}.json").write_text("{}")


def test_session_owners_split_two_games(tmp_path):
    snaps, arch = tmp_path / "snaps", tmp_path / "arch"
    dirs = [arch / s for s in ("20261101T090000", "20261101T120000", "20261102T100000", "20261102T110000")]
    _snap(snaps, "old", 50, "20261101T115000")  # during session 2
    _snap(snaps, "new", 1, "20261102T095500")  # during session 3
    _snap(snaps, "new", 2, "20261102T110300")  # just after session 4's last log write (within slack)
    owners = export.session_owners(dirs, snaps)
    # session 1 has no snapshots: it inherits the next attributed session's game
    assert [owners[d] for d in dirs] == ["old", "old", "new", "new"]


def test_game_meta_falls_back_to_snapshot_before_first_save():
    snap = {"game": {"difficulty": "LOC_DIFFICULTY_PRINCE_NAME", "local_player_id": 0},
            "me": {"leader": "Hatshepsut", "civilization": "Egypt"}}
    m = export._game_meta("42", None, None, [snap], [])
    assert (m["guid"], m["leader"], m["civilization"], m["difficulty"]) == (None, "Hatshepsut", "Egypt", "DIFFICULTY_PRINCE")
