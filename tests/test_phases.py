"""War phase fact sheets (phases.py): casus belli, combatants, war support and notable events, on a temporary store
shaped after game -1906453415's war on Augustus (g111-147)."""

import json

import pytest

from civilizer import ingest, phases, rules, store

GAME = "424242"
ME, ROME, HAN, RIZAL = 0, 6, 7, 2
OPONEAN, HOHOKAM, GOGURYEO = 15, 25, 22


def _insert(con, table: str, **row) -> None:
    cols = [c for c, _, _ in store.TABLES[table][2]]
    con.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols])


def _ev(con, key: str, kind: str, subject: str, g: int, players: list, end_g: int | None = None, minor: int = 0, **detail) -> None:
    _insert(con, "events", game=GAME, event_key=key, kind=kind, subject=subject, start_g=g, start_age="AGE_ANTIQUITY", start_turn=g,
            end_g=g if end_g is None and kind not in ("war", "alliance") else end_g, players=json.dumps(players),
            local=int(ME in players), minor=minor, precision="exact", source="log", detail=json.dumps(detail))


def _metric(con, g: int, pid: int, metric: str, value: float) -> None:
    _insert(con, "metrics", game=GAME, g=g, age="AGE_ANTIQUITY", turn=g, player_id=pid, metric=metric, value=value,
            source="snapshot", visibility="visible")


@pytest.fixture
def con(tmp_path):
    with store.tx(tmp_path / "civ.db") as con:
        _insert(con, "games", game=GAME, local_player_id=ME, current_age="AGE_ANTIQUITY", current_turn=150, current_g=150,
                updated_at="2026-11-04T00:00:00")
        _insert(con, "ages", game=GAME, age="AGE_ANTIQUITY", idx=0, turns=160, g_offset=0)
        for pid, name in ((ME, "Ibn Battuta"), (RIZAL, "José Rizal"), (ROME, "Augustus"), (HAN, "Confucius")):
            _insert(con, "players", game=GAME, player_id=pid, leader=name, is_human=int(pid == ME), civilizations="{}")
        # the alliance event carries the session-start turn; the accepted proposal dates it
        _ev(con, "al07", "alliance", "alliance 0-7", 1, [ME, HAN], end_g=146)
        _ev(con, "al02", "alliance", "alliance 0-2", 1, [ME, RIZAL], end_g=144)
        _ev(con, "fa", "diplo_response", "Ibn Battuta accepted Form Alliance from Confucius", 88, [ME, HAN],
            action="Form Alliance", response="accepted", proposer=HAN, responder=ME)
        _ev(con, "old", "diplomacy_action", "Hinder Research: Ibn Battuta -> Augustus", 95, [ME, ROME], action="Hinder Research",
            group="sanction", initiator=ME, recipient=ROME, initiator_name="Ibn Battuta", recipient_name="Augustus")
        _ev(con, "den", "diplomacy_action", "Denounce: Ibn Battuta -> Augustus", 109, [ME, ROME], end_g=119, action="Denounce",
            group="sanction", initiator=ME, recipient=ROME, initiator_name="Ibn Battuta", recipient_name="Augustus",
            response="accepted", outcome="success")
        _ev(con, "denr", "diplo_response", "Augustus accepted Denounce from Ibn Battuta", 109, [ROME, ME], action="Denounce",
            response="accepted", proposer=ME, responder=ROME)
        _ev(con, "gift", "diplo_response", "Augustus rejected Gift from Ibn Battuta", 105, [ROME, ME], action="Gift",
            response="rejected", proposer=ME, responder=ROME)
        _ev(con, "rel", "relationship", "Augustus: Helpful -> Friendly", 111, [ME, ROME], previous="Helpful", relationship="Friendly")
        _ev(con, "noise", "diplomacy_action", "Hinder Civic Study: Augustus -> Confucius", 109, [ROME, HAN], action="Hinder Civic Study",
            initiator=ROME, recipient=HAN)
        _ev(con, "w67", "war", "war 6-7", 110, [ROME, HAN], end_g=120, war_type="Formal", declared_by=ROME)
        _ev(con, "war", "war", "war 0-6", 111, [ME, ROME], end_g=147, war_type="Surprise", declared_by=ME)
        _ev(con, "w2", "war", "war 2-6", 144, [RIZAL, ROME], end_g=147, war_type="Formal", declared_by=RIZAL)
        _ev(con, "s1", "war", "war 6-15", 111, [ROME, OPONEAN], end_g=147, minor=1, war_type="Suzerain War", declared_by=OPONEAN)
        _ev(con, "s2", "war", "war 0-25", 111, [ME, HOHOKAM], end_g=147, minor=1, war_type="Suzerain War", declared_by=HOHOKAM)
        _ev(con, "s3", "war", "war 0-22", 89, [ME, GOGURYEO], end_g=160, minor=1, war_type="Surprise", declared_by=GOGURYEO)
        _ev(con, "m1", "met", "Ibn Battuta met Goguryeo", 30, [ME, GOGURYEO], other=GOGURYEO, other_name="Goguryeo")
        _ev(con, "c1", "settlement_captured", "Ostia", 117, [ME, ROME], captured_by=ME, from_player=ROME)
        _ev(con, "c2", "settlement_captured", "Mediolanum", 127, [ME, ROME], captured_by=ME, from_player=ROME)
        _ev(con, "c3", "settlement_captured", "Mediolanum", 131, [GOGURYEO, ME], minor=1, captured_by=GOGURYEO, from_player=ME)
        _ev(con, "c4", "settlement_captured", "Ravenna", 130, [ME, ROME], captured_by=ME, from_player=ROME)
        _ev(con, "c5", "settlement_captured", "Roma", 147, [ME, ROME], captured_by=ME, from_player=ROME, capital=True)
        _ev(con, "c6", "settlement_captured", "Hippo", 140, [ROME, ME], captured_by=ROME, from_player=ME)
        for g, n in ((125, 3), (131, 4), (140, 3)):
            for i in range(n):
                _ev(con, f"k{g}{i}", "unit_killed", "Legion", g, [ROME, ME], victim=ROME, killer=ME)
        _ev(con, "l1", "unit_killed", "Quadrireme", 131, [ME, ROME], victim=ME, killer=ROME)
        for g in range(112, 151):
            razing = {"Mediolanum": 128 <= g <= 131, "Ravenna": 131 <= g <= 137}
            towns = ["Carthage", "Ostia"] + ["Mediolanum"] * (127 < g <= 131) + ["Ravenna"] * (130 < g <= 137) + ["Roma"] * (g > 147)
            summary = {"settlements": [{"name": n, "being_razed": razing.get(n, False)} for n in towns],
                       "city_states": [{"id": OPONEAN, "name": "Oponean"}, {"id": HOHOKAM, "name": "Hohokam"}]}
            _insert(con, "turns", game=GAME, g=g, age="AGE_ANTIQUITY", turn=g, summary=json.dumps(summary))
            if g <= 147:
                _metric(con, g, ROME, "settlements", 5 if g < 130 else 1)
                _metric(con, g, ROME, "war_support", 1 if 121 <= g <= 133 else -1 if 134 <= g <= 138 else 0)
        yield con


def test_war_phase_facts(con):
    war = next(p for p in phases.build(con, GAME) if p["scope"] == "war" and p["scope_key"] == "war")
    f = war["facts"]
    assert f["opponent_name"] == "Augustus"
    cb = f["casus_belli"]
    assert cb["declared_by"] == {"id": ME, "name": "Ibn Battuta"} and cb["war_type"] == "Surprise"
    # actions in the 10-turn run-up (their responses folded in), stray responses, attitude changes; not other pairs
    assert cb["run_up"] == [
        {"g": 105, "what": "Gift", "by": "Ibn Battuta", "response": "rejected"},
        {"g": 109, "what": "Denounce", "group": "sanction", "by": "Ibn Battuta", "response": "accepted", "outcome": "success"},
        {"g": 111, "what": "attitude Helpful -> Friendly", "by": "Augustus"}]
    assert cb["joined_ally_war"] == {"ally": {"id": HAN, "name": "Confucius"}, "ally_war_start_g": 110,
                                     "declared_by": {"id": ROME, "name": "Augustus"}, "war_type": "Formal", "allied_since_g": 88}

    us, them = f["combatants"]["us"], f["combatants"]["them"]
    assert [m["name"] for m in us["majors"]] == ["Ibn Battuta", "Confucius", "José Rizal"]
    assert us["majors"][1] == {"id": HAN, "name": "Confucius", "since_g": 110, "allied": True}
    assert us["city_states"] == [{"id": OPONEAN, "name": "Oponean", "since_g": 111}]
    # Goguryeo's war began before ours and is not a suzerain war: not a combatant
    assert them == {"majors": [{"id": ROME, "name": "Augustus"}], "city_states": [{"id": HOHOKAM, "name": "Hohokam", "since_g": 111}]}

    ws = f["war_support"]
    assert ws["series"][0] == [112, 0] and ws["series"][-1] == [147, 0] and len(ws["series"]) == 36
    assert (ws["first"], ws["last"], ws["min"], ws["max"]) == (0, 0, -1, 1)

    nb = f["notable"]
    assert [(s["g"], s["event"], s["settlement"]) for s in nb["settlements"]] == [
        (117, "captured", "Ostia"), (127, "captured", "Mediolanum"), (128, "razing", "Mediolanum"), (130, "captured", "Ravenna"),
        (131, "lost", "Mediolanum"), (131, "razing", "Ravenna"), (138, "razed", "Ravenna"), (140, "lost", "Hippo"),
        (147, "captured", "Roma")]
    lost = {s["settlement"]: s for s in nb["settlements"] if s["event"] == "lost"}
    assert lost["Mediolanum"] == {"g": 131, "settlement": "Mediolanum", "event": "lost", "to": "Goguryeo", "third_party": True,
                                  "while_razing": True}
    assert lost["Hippo"]["to"] == "Augustus" and "third_party" not in lost["Hippo"]
    assert nb["peak_kill_turns"] == [{"g": 131, "killed": 4, "lost": 1}, {"g": 125, "killed": 3, "lost": 0},
                                     {"g": 140, "killed": 3, "lost": 0}]
    assert nb["upsets"] == [{"g": 140, "what": "founded_lost", "settlement": "Hippo", "player": "Augustus"},
                            {"g": 147, "what": "capital_taken", "settlement": "Roma", "player": "Augustus"},
                            {"g": 147, "what": "eliminated", "player": "Augustus"}]


def test_no_ally_war_and_no_war_support(con):
    con.execute("DELETE FROM events WHERE event_key='w67'")
    con.execute("DELETE FROM metrics WHERE metric='war_support'")
    f = next(p for p in phases.build(con, GAME) if p["scope_key"] == "war")["facts"]
    assert f["casus_belli"]["joined_ally_war"] is None and f["war_support"] is None
    assert [m["id"] for m in f["combatants"]["us"]["majors"]] == [ME, RIZAL]


def test_digest_keeps_war_support_and_razing():
    snap = {"game": {"turn": 120, "age": "AGE_ANTIQUITY"}, "me": {"id": 0},
            "settlements": [{"name": "Aquileia", "being_razed": True}],
            "players": [{"id": 6, "met": True, "at_war": True, "war_support": {"we_declared": True, "our_support": -2}},
                        {"id": 7, "met": True, "at_war": False}]}
    d = ingest.digest(snap)
    assert d["metrics"]["6"]["war_support"] == -2 and "war_support" not in d["metrics"].get("7", {})
    assert d["summary"]["settlements"][0]["being_razed"] is True


def test_capital_captures_are_tagged(monkeypatch):
    monkeypatch.setattr(rules, "capital_names", lambda: {"Rome": "Roma", "Carthage": "Carthage"})
    ev = lambda n, frm: {"kind": "settlement_captured", "subject": n, "start": {"age": "AGE_ANTIQUITY"},
                         "detail": {"captured_by": 0, "from_player": frm}}
    events = [ev("Roma", 6), ev("Ostia", 6), ev("Roma", 7)]
    ingest._tag_capitals(events, [{"id": 6, "civilizations": {"AGE_ANTIQUITY": "Rome"}}, {"id": 7, "civilizations": {}}])
    assert [e["detail"].get("capital") for e in events] == [True, None, None]
