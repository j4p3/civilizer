"""Offline tests for the advisor layer (advisor.py, advisor_cli.py, mcp_server.py) on a temporary store."""

import asyncio
import json
import sqlite3

import pytest

from civilizer import advisor, advisor_cli, mcp_server, store

GAME = "424242"


def _insert(con, table: str, **row) -> None:
    cols = [c for c, _, _ in store.TABLES[table][2]]
    con.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols])


def _metric(con, g: int, pid: int, metric: str, value: float, visibility: str = "visible") -> None:
    _insert(con, "metrics", game=GAME, g=g, age="AGE_ANTIQUITY", turn=g, player_id=pid, metric=metric, value=value,
            source="snapshot", visibility=visibility)


def _set_turn(con, g: int) -> None:
    con.execute("UPDATE games SET current_g=?, current_turn=? WHERE game=?", (g, g, GAME))
    _insert(con, "turns", game=GAME, g=g, age="AGE_ANTIQUITY", turn=g, summary=json.dumps({"researching": {"name": "Writing"}}))


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB", tmp_path / "civilizer.db")
    with store.tx() as con:
        _insert(con, "games", game=GAME, leader="LEADER_X", civilization="CIVILIZATION_X", local_player_id=0,
                current_age="AGE_ANTIQUITY", current_turn=10, current_g=10, updated_at="2026-11-03T00:00:00")
        _insert(con, "ages", game=GAME, age="AGE_ANTIQUITY", idx=0, turns=10, g_offset=0)
        for pid, name in ((0, "Me"), (1, "Rival A"), (2, "Rival B")):
            _insert(con, "players", game=GAME, player_id=pid, leader=name, is_human=int(pid == 0),
                    civilizations=json.dumps({"AGE_ANTIQUITY": f"Civ {pid}"}))
        for g in range(1, 11):
            _insert(con, "turns", game=GAME, g=g, age="AGE_ANTIQUITY", turn=g, summary=json.dumps({"turn": g}))
            _metric(con, g, 0, "gold_per_turn", 10 + g)          # 11..20
            _metric(con, g, 1, "gold_per_turn", 30)
            _metric(con, g, 2, "gold_per_turn", 5)
            _metric(con, g, 0, "settlements", 3)
            _metric(con, g, 0, "units_lost_total", 1)
            _metric(con, g, 0, "unrest_settlements", 0)
            for pid, v in ((0, 2 * g), (1, 3 * g), (2, g)):
                _metric(con, g, pid, "vp_science", v)
        _metric(con, 10, 2, "vp_science", 999, "omniscient")    # fogged: must not affect visible standings
        ev = dict(game=GAME, start_age="AGE_ANTIQUITY", local=1, minor=0, precision="exact", source="snapshot")
        _insert(con, "events", **ev, event_key="war1", kind="war", subject="war 0-1", start_g=4, start_turn=4, end_g=10,
                players="[0, 1]", detail=json.dumps({"end_reason": "age_end_or_ongoing"}))
        _insert(con, "events", **ev, event_key="tech1", kind="tech", subject="Writing", start_g=10, start_turn=10,
                players="[0]", detail="{}")
        _insert(con, "events", **(ev | {"precision": "range"}), event_key="civic_old", kind="civic", subject="Mysticism",
                start_g=10, start_lo=1, start_hi=10, players="[0]", detail="{}")
        _insert(con, "events", **ev, event_key="prod1", kind="production", subject="Granary", start_g=7, end_g=10,
                players="[0]", detail=json.dumps({"city": "Rome", "completed": True}))
        _insert(con, "phases", game=GAME, scope="age", scope_key="AGE_ANTIQUITY", g_start=1, g_end=10, title="AGE_ANTIQUITY",
                outcome="ongoing", facts=json.dumps({"score": 1}))
        _insert(con, "phases", game=GAME, scope="war", scope_key="war1", g_start=4, g_end=10, title="war with player 1",
                outcome="failure", facts="{}")
    return store.DB


def _new_game(con, game: str = "777") -> str:
    _insert(con, "games", game=game, leader="LEADER_Y", civilization="CIVILIZATION_Y", local_player_id=0, current_age="AGE_ANTIQUITY",
            current_turn=1, current_g=1, updated_at="2026-11-04T00:00:00")
    _insert(con, "players", game=game, player_id=0, leader="Next", is_human=1, civilizations="{}")
    return game


def test_profiles_are_reusable_versioned_and_changes_logged(db):
    with store.tx() as con:
        a1 = advisor.set_advisor_profile(con, GAME, "jocular")
        assert (a1["profile_id"], a1["version"], a1["name"]) == (1, 1, "Jocular")
        assert a1["priorities"] == ["growth", "happiness", "wonders"] and "jester" in a1["style_prompt"]
        a2 = advisor.set_advisor_profile(con, GAME, "terse_military", voice="clipped", priorities=["security"])
        assert a2["profile_id"] == 2 and a2["personality_description"]
        a3 = advisor.set_advisor_profile(con, GAME, "terse_military", voice="clipped", priorities=["security"])
        assert a3["profile_id"] == 2 and len(advisor.profile_history(con, GAME, "advisor")) == 2  # identical: no new link
        advisor.set_player_profile(con, GAME, ["builder"], "low", motives=["build"], name="JP")
        p = advisor.set_player_profile(con, GAME, ["aggressive", "whimsical"], "high", "wants a war", profile_id=1)
        assert (p["profile_id"], p["version"], p["name"]) == (1, 2, "JP")
        with pytest.raises(advisor.AdvisorError):
            advisor.set_advisor_profile(con, GAME, "grumpy")
        with pytest.raises(advisor.AdvisorError):
            advisor.set_player_profile(con, GAME, ["lazy"])
        with pytest.raises(advisor.AdvisorError):
            advisor.set_player_profile(con, GAME, motives=["nap"])
        h = advisor.history(con, GAME, "profile")
        assert [e["what"] for e in h].count("advisor_profile") == 2 and [e["what"] for e in h].count("player_profile") == 2
        changes = advisor.annotations(con, GAME, kind="priority_change")
        assert len(changes) == 1 and "builder" in changes[0]["text"] and "aggressive" in changes[0]["text"]

        # a new game reuses the saved profiles; editing there versions the profile without touching the old game
        g2 = _new_game(con)
        assert advisor.current_player_profile(con, g2) is None
        assert advisor.use_profile(con, g2, "player", 1)["version"] == 2
        assert advisor.set_player_profile(con, g2, ["cautious"], "low", profile_id=1)["version"] == 3
        assert advisor.current_player_profile(con, GAME)["version"] == 2
        saved = advisor.list_profiles(con, "player")
        assert len(saved) == 1 and {u["game"] for u in saved[0]["games"]} == {GAME, g2}


def test_setup_wizard_flow(db, monkeypatch):
    with store.tx() as con:
        w = advisor.setup_wizard(con, GAME)
        assert w["step"] == "advisor" and not w["multi_select"] and w["done"] == []
        keys = [o["key"] for o in w["options"]]
        assert keys[0] == "cautious_steward" and keys[-1] == "custom" and [o["n"] for o in w["options"]] == list(range(1, len(keys) + 1))

        c = advisor.create_advisor_preset(con, "pirate_queen", "Pirate queen", "Swaggering pirate.",
                                          "You are a pirate queen.\n\n- Say arr.", ["trade", "economy"])
        assert c["source"] == "custom" and (store.DB.parent / "advisors" / "pirate_queen.md").exists()
        with pytest.raises(advisor.AdvisorError):
            advisor.create_advisor_preset(con, "pirate_queen", "", "x", "y", ["trade"])  # exists
        with pytest.raises(advisor.AdvisorError):
            advisor.create_advisor_preset(con, "Bad Key", "", "x", "y", ["trade"])
        ap = advisor.set_advisor_profile(con, GAME, "pirate_queen")
        assert ap["priorities"] == ["trade", "economy"] and ap["style_prompt"].startswith("You are a pirate queen")

        w = advisor.setup_wizard(con, GAME)
        assert w["step"] == "player" and w["multi_select"] and w["done"] == ["advisor"]
        assert [o["key"] for o in w["options"]] == ["fight", "build", "explore", "mess_around", "story"]
        advisor.set_player_profile(con, GAME, ["whimsical"], motives=["story", "explore"], name="JP")

        w = advisor.setup_wizard(con, GAME)
        assert w["step"] == "strategy" and "just_win" in [o["key"] for o in w["options"]]
        r = advisor.choose_strategy(con, GAME, ["just_win"])
        assert "needs_victory_types" in r and not advisor.strategies(con, GAME)
        r = advisor.choose_strategy(con, GAME, ["just_win"], victory_types=["culture"])
        assert r["victory_types"] == ["culture"] and "vp_culture" in r["suggested_metrics"]
        assert advisor.choose_strategy(con, GAME, ["science", "not_sure"])["victory_types"] == ["science", "expansion", "score"]
        with pytest.raises(advisor.AdvisorError):
            advisor.choose_strategy(con, GAME, ["vibes"])
        assert advisor.setup_wizard(con, GAME)["step"] == "done"
        assert advisor.setup_wizard(con, GAME, "advisor")["step"] == "advisor"

        # next game: saved profiles come first
        g2 = _new_game(con)
        w = advisor.setup_wizard(con, g2)
        assert w["options"][0]["key"] == "profile:1" and "pirate_queen" in w["options"][0]["description"]
        assert any(o["key"] == "pirate_queen" for o in w["options"])
        advisor.use_profile(con, g2, "advisor", w["options"][0]["profile_id"])
        assert advisor.setup_wizard(con, g2)["options"][0]["key"] == "profile:1"


def test_v3_profiles_migrate_to_reusable(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE advisor_profiles (game TEXT, version INTEGER, personality TEXT, voice TEXT, priorities TEXT,
        quirks TEXT, valid_from_g INTEGER, created_at TEXT, PRIMARY KEY (game, version));
        CREATE TABLE player_profiles (game TEXT, version INTEGER, playstyle TEXT, risk_tolerance TEXT, notes TEXT,
        valid_from_g INTEGER, created_at TEXT, PRIMARY KEY (game, version));
        INSERT INTO advisor_profiles VALUES ('A', 1, 'jocular', '', '["growth"]', '', 3, 't1'), ('A', 2, 'sarcastic', '', '["economy"]', '', 9, 't2'),
                                            ('B', 1, 'scholarly', 'footnotes', '["science"]', '', 1, 't3');
        INSERT INTO player_profiles VALUES ('A', 1, '["builder"]', 'low', 'tall', 3, 't1');""")
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB", path)
    with store.tx() as con:
        assert [r["personality"] for r in advisor.profile_history(con, "A", "advisor")] == ["sarcastic", "jocular"]
        assert advisor.profile_history(con, "B", "advisor")[0]["profile_id"] == 2
        assert advisor.profile_history(con, "A", "player")[0]["playstyle"] == ["builder"]


@pytest.mark.parametrize("cmp,cur,base,target,rank,expect", [
    (">=", 15, 10, 20, None, (0.5, False)),
    (">=", 25, 10, 20, None, (1.0, True)),
    (">=", 5, 10, 20, None, (0.0, False)),        # went backwards
    (">=", -5, -20, 0, None, (0.75, False)),      # negative income climbing to zero
    (">=", 10, 30, 20, None, (0.5, False)),       # baseline already past target: ratio fallback
    ("<=", 6, 10, 2, None, (0.5, False)),
    ("<=", 2, 10, 2, None, (1.0, True)),
    ("gain>=", 15, 10, 10, None, (0.5, False)),
    ("gain>=", 25, 10, 10, None, (1.0, True)),
    ("gain<=", 11, 10, 2, None, (1.0, True)),     # lost 1 of at most 2
    ("gain<=", 13, 10, 2, None, (0.0, False)),    # limit broken
    ("rank<=", None, None, 1, 1, (1.0, True)),
    ("rank<=", None, None, 1, 4, (0.25, False)),
    (">=", None, 10, 20, None, (None, None)),
])
def test_progress_semantics(cmp, cur, base, target, rank, expect):
    p, met = advisor._progress(cmp, cur, base, target, rank)
    assert met == expect[1]
    assert (p is None and expect[0] is None) or p == pytest.approx(expect[0])


def test_goal_tree_metrics_and_rollup(db):
    with store.tx() as con:
        s = advisor.add_strategy(con, GAME, "Gold rush", ["economic"])
        assert "gold_per_turn" in s["suggested_metrics"]
        long = advisor.add_goal(con, GAME, "Richest civ", "long", strategy_id=s["strategy_id"],
                                metrics=[{"metric": "gold_per_turn", "comparator": "rank<=", "target": 1}])
        short = advisor.add_goal(con, GAME, "30 gold a turn", parent_goal_id=long["goal_id"], deadline_g=12,
                                 metrics=[{"metric": "gold_per_turn", "comparator": ">=", "target": 30},
                                          {"metric": "units_lost_total", "comparator": "gain<=", "target": 1}])
        with pytest.raises(advisor.AdvisorError):
            advisor.link_metric(con, GAME, short["goal_id"], "not_a_metric")
        # two turns later: gold 20 -> 25, one more unit lost
        for g, gold in ((11, 22), (12, 25)):
            _metric(con, g, 0, "gold_per_turn", gold)
            _metric(con, g, 1, "gold_per_turn", 30)
        _metric(con, 12, 0, "units_lost_total", 2)
        _set_turn(con, 12)
        tree = advisor.goal_tree(con, GAME)
    assert len(tree) == 1 and tree[0]["name"] == "Gold rush"
    lg = tree[0]["goals"][0]
    sg = lg["children"][0]
    assert sg["strategy_id"] == s["strategy_id"]  # inherited from the parent
    by = {m["metric"]: m for m in sg["metrics"]}
    assert by["gold_per_turn"]["progress"] == pytest.approx(0.5)  # 20 -> 25 of 20 -> 30
    assert by["units_lost_total"]["met"] is True and by["units_lost_total"]["gain"] == 1
    assert sg["progress"] == pytest.approx(0.75) and sg["turns_left"] == 0
    rank = lg["metrics"][0]
    assert (rank["rank"], rank["of"], rank["met"]) == (2, 3, False)
    assert lg["metric_progress"] == pytest.approx(0.5) and lg["children_progress"] == pytest.approx(0.75)
    assert lg["progress"] == pytest.approx((0.5 + 0.75) / 2)


def test_goal_changes_are_recorded(db):
    with store.tx() as con:
        gid = advisor.add_goal(con, GAME, "Expand", metrics=[{"metric": "settlements", "comparator": ">=", "target": 5}])["goal_id"]
        advisor.update_goal(con, GAME, gid, priority=1, deadline_g=20, note="rival is boxing us in")
        advisor.link_metric(con, GAME, gid, "settlements", ">=", 6)
        advisor.set_goal_status(con, GAME, gid, "abandoned", "no land left")
        with pytest.raises(advisor.AdvisorError):
            advisor.update_goal(con, GAME, gid)
        h = advisor.history(con, GAME, "goal")
        g = dict(con.execute("SELECT * FROM goals WHERE game=? AND goal_id=?", (GAME, gid)).fetchone())
    texts = " | ".join(e["text"] for e in h)
    assert [e["what"] for e in h] == ["intention", "priority_change", "priority_change", "outcome"]
    assert "priority 2 -> 1" in texts and "was >= 5" in texts and h[-1]["outcome"] == "changed_plans"
    assert (g["status"], g["resolved_g"], g["priority"]) == ("abandoned", 10, 1)


def test_annotation_scope_and_phase_outcome_override(db):
    with store.tx() as con:
        advisor.annotate(con, GAME, "We bled for nothing.", "narrative", scope="war", scope_key="war1")
        advisor.annotate(con, GAME, "Actually it bought time.", "outcome", outcome="partial", scope="war", scope_key="war1")
        aid = advisor.annotate(con, GAME, "Whole game so far", "narrative", scope="game")
        assert con.execute("SELECT scope_key FROM annotations WHERE annotation_id=?", (aid,)).fetchone()[0] == GAME
        for bad in (dict(scope="war", scope_key="nope"), dict(scope="age"), dict(scope="era", scope_key="x"), dict(scope_key="x")):
            with pytest.raises(advisor.AdvisorError):
                advisor.annotate(con, GAME, "x", "narrative", **bad)
        gid = advisor.add_goal(con, GAME, "g")["goal_id"]
        a = advisor.annotations(con, GAME, goal_id=gid)[0]
        assert (a["scope"], a["scope_key"]) == ("goal", str(gid))
        ph = {p["scope"]: p for p in advisor.phases(con, GAME)}
    war, age = ph["war"], ph["age"]
    assert (war["heuristic_outcome"], war["advisor_outcome"], war["effective_outcome"]) == ("failure", "partial", "partial")
    assert not war["needs_narrative"] and len(war["annotations"]) == 2
    assert age["effective_outcome"] == "ongoing" and age["needs_narrative"] and age["facts"] == {"score": 1}


def test_briefing_shape(db):
    with store.tx() as con:
        b0 = advisor.briefing(con)
        assert len(b0["setup_needed"]) == 3 and all("setup_wizard" in m for m in b0["setup_needed"])
        advisor.set_advisor_profile(con, GAME, "sarcastic")
        advisor.set_player_profile(con, GAME, ["efficient"])
        gid = advisor.add_goal(con, GAME, "Science lead", "long", deadline_g=11,
                               metrics=[{"metric": "vp_science", "comparator": "rank<=", "target": 1}])["goal_id"]
        b = advisor.briefing(con)
    assert "setup_needed" not in b and b["since_g"] == 9
    assert b["game"]["leader"] == "Me" and b["game"]["civilization"] == "Civ 0"
    assert b["advisor_profile"]["personality"] == "sarcastic" and b["player_profile"]["playstyle"] == ["efficient"]
    assert "vizier" in b["advisor_profile"]["style_prompt"]
    assert b["state"]["gold_per_turn"] == {"value": 20, "delta": 1}
    # visible values only: rival 2's omniscient 999 is ignored, rival 1 (30) leads
    assert b["standings"]["vp_science"] == {"rank": 2, "of": 3, "value": 20, "leader": "Rival A", "leader_value": 30}
    kinds = {e["kind"] for e in b["events_since"]}
    assert kinds == {"tech", "production"}  # ongoing war and backfilled range civic are not news
    plan_goal = b["plan"][0]["goals"][0]
    assert plan_goal["goal_id"] == gid and plan_goal["metrics"] == ["vp_science rank<= 1: rank 2 [50%]"]
    assert any("due in 1 turn" in a for a in b["alerts"])
    assert len(json.dumps(b)) < 8000


def test_cli_roundtrip(db, capsys):
    run = lambda *a: advisor_cli.main(list(a))
    assert run("profile", "advisor", "set", "scholarly", "--voice", "footnotes") == 0
    assert run("strategy", "add", "Big", "brain", "--victory", "science") == 0
    assert run("goal", "add", "Top science", "--horizon", "long", "--strategy", "1", "--metric", "vp_science rank<=1") == 0
    assert run("goal", "add", "More gold", "--parent", "1", "--metric", "gold_per_turn>=40*2") == 0
    assert run("goal", "link", "2", "units_lost_total", "gain<=", "0") == 0
    assert run("annotate", "Going", "tall", "--kind", "intention", "--author", "player") == 0
    assert run("goal", "status", "9", "achieved") == 1  # unknown goal: error, not a traceback
    capsys.readouterr()
    assert run("goal", "list") == 0
    out = capsys.readouterr().out
    assert "Strategy 1: Big brain" in out and "#2 (short, p2) More gold" in out and "gold_per_turn >= 40" in out
    assert run("brief") == 0 and "scholarly" in capsys.readouterr().out
    assert run("history", "--json") == 0
    assert len(json.loads(capsys.readouterr().out)) >= 5
    assert advisor_cli.parse_metric("score@2 <= 500") == {"metric": "score", "comparator": "<=", "target": 500, "player_id": 2}


def test_outcome_aliases_validation_and_rejudge(db, capsys):
    with store.tx() as con:
        gid = advisor.add_goal(con, GAME, "Raid fleet")["goal_id"]
        _set_turn(con, 8)
        assert advisor.set_goal_status(con, GAME, gid, "abandoned", "no targets")["outcome"] == "changed_plans"
        for old, new in store.OUTCOME_ALIASES.items():
            advisor.annotate(con, GAME, "x", "outcome", outcome=old, goal_id=gid)
            assert advisor.annotations(con, GAME, goal_id=gid)[-1]["outcome"] == new
        for bad in ("neutral", "great", "Success"):
            with pytest.raises(advisor.AdvisorError, match="unknown outcome"):
                advisor.set_goal_status(con, GAME, gid, "abandoned", outcome=bad)
        with pytest.raises(advisor.AdvisorError, match="unknown outcome"):
            advisor.annotate(con, GAME, "x", "outcome", outcome="went_sideways")
        sid = advisor.add_strategy(con, GAME, "Pirates", ["military"])["strategy_id"]
        assert advisor.set_strategy_status(con, GAME, sid, "abandoned")["outcome"] == "changed_plans"
        _set_turn(con, 10)
    run = lambda *a: advisor_cli.main(list(a))
    # same status again re-judges: resolved_g / ended_g and the resolution stay
    assert run("goal", "status", str(gid), "abandoned", "--outcome", "failure") == 0
    assert run("goal", "status", str(gid), "abandoned", "--verdict", "mixed") == 0
    assert run("strategy", "status", str(sid), "abandoned", "--verdict", "went_awry") == 0
    assert run("annotate", "Fine", "--kind", "outcome", "--goal", str(gid), "--outcome", "success") == 0
    capsys.readouterr()
    assert run("goal", "status", str(gid), "abandoned", "--outcome", "meh") == 1
    assert "unknown outcome 'meh'" in capsys.readouterr().out
    with store.tx() as con:
        g = con.execute("SELECT status, resolved_g, resolution FROM goals WHERE game=? AND goal_id=?", (GAME, gid)).fetchone()
        s = con.execute("SELECT status, ended_g FROM strategies WHERE game=? AND strategy_id=?", (GAME, sid)).fetchone()
        outs = [a["outcome"] for a in advisor.annotations(con, GAME, kind="outcome")]
    assert tuple(g) == ("abandoned", 8, "no targets") and tuple(s) == ("abandoned", 8)
    assert outs[-4:] == ["failure", "partial", "failure", "success"]


def v7_store(path) -> sqlite3.Connection:
    """An empty schema-v7 store: annotations and phases still have a verdict column."""
    old_ddl = store.ddl().replace("outcome TEXT,\n  goal_id", "verdict TEXT,\n  goal_id").replace(
        "title TEXT,\n  outcome TEXT", "title TEXT,\n  verdict TEXT")
    assert old_ddl.count("verdict TEXT") == 2
    con = sqlite3.connect(path)
    con.executescript(old_ddl)
    return con


def test_migrate_verdict_to_outcome(tmp_path):
    """A v7 store (verdict columns, old values) migrates in place; an old process's late phase writes still read."""
    path = tmp_path / "old.db"
    con = v7_store(path)
    rows = [("went_well", "outcome"), ("went_awry", "outcome"), ("mixed", "outcome"), ("ongoing", "outcome"),
            ("neutral", "outcome"), (None, "intention")]
    con.executemany("INSERT INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(GAME, i, 1, "AGE_ANTIQUITY", 1, "player", k, "t", v, None, None, None, None, None, "x")
                     for i, (v, k) in enumerate(rows, 1)])
    con.execute("INSERT INTO phases VALUES (?,?,?,?,?,?,?,?)", (GAME, "age", "AGE_ANTIQUITY", 1, 2, "a", "went_well", "{}"))
    con.execute("INSERT INTO meta VALUES ('schema_version', '7')")
    con.commit()
    con.close()
    with store.tx(path) as con:
        got = [r[0] for r in con.execute("SELECT outcome FROM annotations ORDER BY annotation_id")]
        assert got == ["success", "failure", "partial", "ongoing", None, None]
        assert con.execute("SELECT outcome FROM phases").fetchone()[0] == "success"
        assert store.get_meta(con, "schema_version") == str(store.SCHEMA_VERSION)
        assert {r[0] for r in con.execute("SELECT value FROM vocab WHERE field='outcome'")} == set(store.VOCAB["outcome"])
        # an old exporter keeps writing verdict values positionally into the renamed column
        con.execute("INSERT OR REPLACE INTO phases VALUES (?,?,?,?,?,?,?,?)", (GAME, "age", "AGE_ANTIQUITY", 1, 2, "a", "mixed", "{}"))
    with store.tx(path) as con:  # idempotent: a second connect changes nothing
        assert [r[0] for r in con.execute("SELECT outcome FROM annotations ORDER BY annotation_id")] == got
        assert store.outcome_of(con.execute("SELECT outcome FROM phases").fetchone()[0]) == "partial"


def _call(name: str, args: dict | None = None):
    from mcp import Client

    async def go():
        async with Client(mcp_server.server) as c:
            return await c.call_tool(name, args or {})
    return asyncio.run(go())


def _data(res):
    assert not res.is_error, res.content
    sc = res.structured_content
    if isinstance(sc, dict) and set(sc) == {"result"}:
        return sc["result"]
    return sc if sc is not None else json.loads(res.content[0].text)


def test_mcp_tools_end_to_end(db):
    from mcp import Client

    async def names():
        async with Client(mcp_server.server) as c:
            return {t.name for t in (await c.list_tools()).tools}
    assert {"briefing", "query", "phases", "annotate", "add_goal", "civilopedia", "game_rules", "setup_wizard", "choose_strategy",
            "create_advisor_preset", "use_profile", "saved_profiles"} <= asyncio.run(names())
    assert _data(_call("setup_wizard"))["step"] == "advisor"

    assert _data(_call("set_advisor_profile", {"personality": "zealous"}))["version"] == 1
    assert _data(_call("set_player_profile", {"playstyle": ["cautious"], "motives": ["build"], "risk_tolerance": "low"}))["version"] == 1
    assert _data(_call("saved_profiles", {"role": "player"}))[0]["motives"] == ["build"]
    assert _data(_call("use_profile", {"role": "player", "profile_id": 1}))["profile_id"] == 1
    sid = _data(_call("add_strategy", {"name": "Wide", "victory_types": ["expansion"]}))["strategy_id"]
    gid = _data(_call("add_goal", {"title": "Ten cities", "horizon": "long", "strategy_id": sid,
                                   "metrics": [{"metric": "settlements", "comparator": ">=", "target": 10}]}))["goal_id"]
    child = _data(_call("add_goal", {"title": "Two more", "parent_goal_id": gid}))["goal_id"]
    _data(_call("link_metric", {"goal_id": child, "metric": "settlements", "comparator": "gain>=", "target": 2}))
    _data(_call("update_goal", {"goal_id": child, "priority": 1}))
    _data(_call("annotate", {"text": "Antiquity story", "kind": "narrative", "scope": "age", "scope_key": "AGE_ANTIQUITY"}))
    _data(_call("annotate", {"text": "Went fine", "kind": "outcome", "outcome": "success", "scope": "age",
                             "scope_key": "AGE_ANTIQUITY"}))
    assert _data(_call("set_goal_status", {"goal_id": child, "status": "failed"}))["outcome"] == "failure"
    # old parameter name and old values still work, and re-sending the status only re-judges it
    assert _data(_call("set_goal_status", {"goal_id": child, "status": "failed", "verdict": "mixed"}))["outcome"] == "partial"
    _data(_call("set_strategy_status", {"strategy_id": sid, "status": "active", "note": "still on"}))
    _data(_call("unlink_metric", {"goal_id": child, "metric": "settlements"}))

    b = _data(_call("briefing"))
    assert b["advisor_profile"]["personality"] == "zealous" and b["plan"][0]["name"] == "Wide"
    tree = _data(_call("goals"))
    assert tree[0]["goals"][0]["children"][0]["status"] == "failed"
    ph = {p["scope"]: p for p in _data(_call("phases", {"scope": "age"}))}
    assert ph["age"]["advisor_outcome"] == "success" and ph["age"]["effective_outcome"] == "success"
    assert len(_data(_call("annotations", {"kind": "narrative"}))) == 1
    assert {e["what"] for e in _data(_call("advisor_history"))} >= {"advisor_profile", "intention", "outcome"}
    assert _data(_call("profiles"))["player"]["risk_tolerance"] == "low"
    assert _data(_call("metric_history", {"metric": "gold_per_turn", "last": 3}))[-1]["value"] == 20
    assert [e["kind"] for e in _data(_call("timeline", {"kinds": ["war"]}))] == ["war"]
    assert _data(_call("query", {"sql": "SELECT count(*) AS n FROM goals"}))[0]["n"] == 2
    assert len(_data(_call("vocabulary", {"field": "outcome"}))) == 5
    assert "## goals" in _call("schema").content[0].text

    for name, args, msg in (("query", {"sql": "DELETE FROM goals"}, "only SELECT"),
                            ("add_goal", {"title": "x", "parent_goal_id": 99}, "no goal 99"),
                            ("annotate", {"text": "x", "kind": "gossip"}, "unknown annotation_kind")):
        res = _call(name, args)
        assert res.is_error and msg in res.content[0].text
