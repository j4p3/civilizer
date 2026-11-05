"""Dashboard API: serve a small fixture store on an ephemeral port and check every endpoint returns JSON."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from civilizer import advisor, dashboard, store

GAME = "42"
OVER = "7"  # an older, finished game


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    db = tmp_path_factory.mktemp("store") / "civ.db"
    with store.tx(db) as con:
        con.execute("INSERT INTO games (game, leader, civilization, difficulty, map, local_player_id, current_age, current_turn, "
                    "current_g, age_turns_max, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (GAME, "LEADER_ADA_LOVELACE", "CIVILIZATION_AMERICA", "DIFFICULTY_EMPEROR", "Continents", 0, "AGE_EXPLORATION", 3, 13, 140,
                     store.now()))
        con.executemany("INSERT INTO ages VALUES (?,?,?,?,?)", [(GAME, "AGE_ANTIQUITY", 0, 10, 0), (GAME, "AGE_EXPLORATION", 1, 3, 10)])
        con.executemany("INSERT INTO players VALUES (?,?,?,?,?,?)", [
            (GAME, 0, "Ada Lovelace", "LEADER_ADA_LOVELACE", 1, json.dumps({"AGE_ANTIQUITY": "America"})),
            (GAME, 1, "Lafayette", "LEADER_LAFAYETTE", 0, json.dumps({"AGE_ANTIQUITY": "France"}))])
        for g in range(1, 14):
            age, turn = ("AGE_ANTIQUITY", g) if g <= 10 else ("AGE_EXPLORATION", g - 10)
            for p in (0, 1):
                con.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?,?)",
                            (GAME, g, age, turn, p, "gold_per_turn", 10.0 * g + p, "snapshot", "visible" if p == 0 or g > 5 else "omniscient"))
                con.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?,?)", (GAME, g, age, turn, p, "vp_economic", g * (2 - p), "snapshot", "visible"))
        con.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?,?)", (GAME, 13, "AGE_EXPLORATION", 3, 1, "war_support", -2, "snapshot", "visible"))
        con.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?)", (GAME, 13, "AGE_EXPLORATION", 3, "2026-01-01T00:00:00Z", "",
                                                                  json.dumps({"government": "Despotism", "at_war_with": [1]})))
        con.execute("INSERT INTO events (game, event_key, kind, subject, start_g, start_age, start_turn, end_g, players, local, minor, "
                    "precision, source, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (GAME, "w1", "war", "war 0-1", 12, "AGE_EXPLORATION", 2, None, "[0, 1]", 1, 0, "exact", "log", json.dumps({"declared_by": 1})))
        con.execute("INSERT INTO events (game, event_key, kind, subject, start_g, start_age, start_turn, end_g, players, local, minor, "
                    "precision, source, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (GAME, "k1", "unit_killed", "Warrior", 12, "AGE_EXPLORATION", 2, 12, "[1, 0]", 1, 0, "exact", "log",
                     json.dumps({"victim": 1, "killer": 0})))
        con.execute("INSERT INTO phases VALUES (?,?,?,?,?,?,?,?)", (GAME, "age", "AGE_ANTIQUITY", 1, 10, "AGE_ANTIQUITY", "went_awry", "{}"))
        con.execute("INSERT INTO games (game, leader, civilization, local_player_id, current_age, current_turn, current_g, status, victories, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (OVER, "LEADER_LAFAYETTE", "CIVILIZATION_FRANCE", 0, "AGE_MODERN", 140, 420, "over",
                                                              json.dumps([{"victory": "VICTORY_SCORE", "players": [1]}]), "2020-01-01T00:00:00"))
        con.execute("INSERT INTO players VALUES (?,?,?,?,?,?)", (OVER, 1, "Lafayette", "LEADER_LAFAYETTE", 0, "{}"))
        advisor.set_advisor_profile(con, GAME, "sarcastic")
        sid = advisor.add_strategy(con, GAME, "Money", ["economic"])["strategy_id"]
        advisor.add_goal(con, GAME, "Get rich", "long", strategy_id=sid, metrics=[{"metric": "gold_per_turn", "comparator": ">=", "target": 500}])
        advisor.annotate(con, GAME, "It went fine.", "outcome", outcome="partial", scope="age", scope_key="AGE_ANTIQUITY", g=10)
        advisor.annotate(con, GAME, "Old blurb.", "blurb", scope="game", scope_key=GAME, g=10)
        advisor.annotate(con, GAME, "France, but louder.", "blurb", scope="game", scope_key=GAME, g=12)
    srv = dashboard.make_server("127.0.0.1", 0, db)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        assert "json" in r.headers["Content-Type"]
        return json.loads(r.read())


@pytest.mark.parametrize("path,keys", [
    ("/api/games", {"games", "monitor"}),
    ("/api/version", {"token", "game", "current_g", "monitor"}),
    ("/api/game", {"game", "ages", "players", "catalog", "observed_g", "monitor"}),
    ("/api/overview", {"tiles", "standings", "wars", "phases"}),
    ("/api/phases", {"phases", "notes", "unscoped"}),
    ("/api/timeline?local=0", {"ages", "players", "events"}),
    ("/api/metrics?metric=gold_per_turn", {"metric", "catalog", "series", "visibility"}),
    ("/api/turn/12", {"g", "age", "turn", "events", "deltas", "summary", "annotations"}),
    ("/api/turn/latest", {"g", "events", "deltas"}),
    ("/api/advisor", {"advisor_profile", "player_profile", "plan", "annotations"}),
])
def test_endpoints_return_json(base, path, keys):
    assert keys <= set(get(base, path))


def test_advisor_outcome_overrides_heuristic_outcome(base):
    # the fixture's phase row carries a pre-v8 value ("went_awry"), as an old exporter process still writes it
    age = next(p for p in get(base, "/api/phases")["phases"] if p["scope_key"] == "AGE_ANTIQUITY")
    assert (age["outcome"], age["heuristic_outcome"], age["outcome_source"]) == ("partial", "failure", "advisor")
    assert age["annotations"][0]["text"] == "It went fine."


def test_visibility_filter_hides_omniscient_rival_values(base):
    vis = get(base, "/api/metrics?metric=gold_per_turn&players=0,1")
    assert [p[0] for p in vis["series"]["1"]] == list(range(6, 14)) and vis["omniscient_hidden"]["1"] == 5
    assert len(get(base, "/api/metrics?metric=gold_per_turn&players=1&vis=all")["series"]["1"]) == 13


def test_turn_view_and_plan(base):
    t = get(base, "/api/turn/12")
    assert (t["age"], t["turn"], t["prev_g"]) == ("AGE_EXPLORATION", 2, 11)
    assert {e["kind"] for e in t["events"]} == {"war", "unit_killed"}
    assert next(d for d in t["deltas"] if d["metric"] == "gold_per_turn")["delta"] == 10
    plan = get(base, "/api/advisor")["plan"]
    assert plan[0]["name"] == "Money" and plan[0]["goals"][0]["title"] == "Get rich"
    ov = get(base, "/api/overview")
    assert ov["wars"][0]["opponent"] == 1 and ov["wars"][0]["units_killed"] == 1 and ov["wars"][0]["war_support"] == -2
    assert ov["standings"]["economic"]["rank"] == 1
    assert ov["blurb"] == "France, but louder."  # latest game-scoped blurb wins


def test_games_list_and_game_param(base):
    g = get(base, "/api/games")["games"][0]
    assert (g["game"], g["leader_name"], g["turns_observed"], g["ages"]) == (GAME, "Ada Lovelace", 1, ["AGE_ANTIQUITY", "AGE_EXPLORATION"])
    assert g["watching"] is False
    assert get(base, f"/api/game?game={GAME}")["game"]["game"] == GAME
    assert get(base, "/api/game?game=nope")["game"] is None


def test_game_outcome(base):
    games = {g["game"]: g for g in get(base, "/api/games")["games"]}
    assert list(games) == [GAME, OVER]  # newest first
    assert games[GAME]["outcome"] == {"status": "unknown", "victories": []}
    assert games[OVER]["outcome"] == {"status": "over", "victories": [{"victory": "VICTORY_SCORE", "players": [{"id": 1, "leader": "Lafayette"}]}]}
    assert get(base, f"/api/game?game={OVER}")["game"]["outcome"]["status"] == "over"


def test_static_files_and_errors(base):
    for name in ("/", "/app.js", "/style.css", "/foolscap/foolscap.vars.css", "/foolscap/foolscap-plot.js", "/foolscap/plot.umd.min.js",
                 "/foolscap/fonts/lora-latin-400-normal.woff2"):
        with urllib.request.urlopen(base + name, timeout=10) as r:
            assert r.status == 200 and r.read()
    for bad in ("/api/nope", "/api/turn/abc", "/../pyproject.toml"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + bad, timeout=10)
        assert e.value.code == 404
