"""Offline tests for diplomacy tracking (diplomacy.py) and its store wiring (ingest metrics, briefing, follower)."""

import json

import pytest
from test_advisor import GAME, _insert, _set_turn, db  # noqa: F401  (db is a fixture)

from civilizer import advisor, advisor_cli, diplomacy, export, follow, ingest, store

DATA = """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <DiplomacyActions>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_GIVE_INFLUENCE_TOKEN" DiplomacyActionGroup="DIPLOMACY_ACTION_GROUP_PROJECT" Name="LOC_DIPLOMACY_ACTION_GIVE_INFLUENCE_TOKEN_NAME"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_RESEARCH_COLLABORATION" DiplomacyActionGroup="DIPLOMACY_ACTION_GROUP_ENDEAVOR" Name="LOC_DIPLOMACY_PROJECT_RESEARCH_COLLABORATION_NAME"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_DENOUNCE" DiplomacyActionGroup="DIPLOMACY_ACTION_GROUP_SANCTION" Name="LOC_DIPLOMACY_PROJECT_DENOUNCE_NAME"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_FORM_ALLIANCE" DiplomacyActionGroup="DIPLOMACY_ACTION_GROUP_UNIQUE" Name="LOC_DIPLOMACY_ACTION_FORM_ALLIANCE_NAME"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_CS_INCORPORATE" DiplomacyActionGroup="DIPLOMACY_ACTION_GROUP_PROJECT" Name="LOC_DIPLOMACY_PROJECT_CS_INCORPORATE_NAME"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_ESPIONAGE_STEAL_TECH" DiplomacyActionGroup="DIPLOMACY_ACTION_GROUP_ESPIONAGE" Name="LOC_DIPLOMACY_ESPIONAGE_STEAL_TECH_NAME"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_LAND_CLAIM" DiplomacyActionGroup="DIPLOMACY_ACTION_GROUP_UNIQUE" Name="LOC_DIPLOMACY_ACTION_LAND_CLAIM_NAME"/>
  </DiplomacyActions>
  <DiplomaticActionResponses>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_RESEARCH_COLLABORATION" DiplomaticResponseType="DIPLOMACY_RESPONSE_SUPPORT"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_RESEARCH_COLLABORATION" DiplomaticResponseType="DIPLOMACY_RESPONSE_ACCEPT"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_RESEARCH_COLLABORATION" DiplomaticResponseType="DIPLOMACY_RESPONSE_REJECT"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_DENOUNCE" DiplomaticResponseType="DIPLOMACY_RESPONSE_ACCEPT"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_DENOUNCE" DiplomaticResponseType="DIPLOMACY_RESPONSE_REJECT"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_FORM_ALLIANCE" DiplomaticResponseType="DIPLOMACY_RESPONSE_ACCEPT"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_FORM_ALLIANCE" DiplomaticResponseType="DIPLOMACY_RESPONSE_REJECT"/>
    <Row DiplomacyActionType="DIPLOMACY_ACTION_GIVE_INFLUENCE_TOKEN" DiplomaticResponseType="DIPLOMACY_RESPONSE_ACCEPT"/>
  </DiplomaticActionResponses>
</Database>
"""
TEXT = {"LOC_DIPLOMACY_PROJECT_RESEARCH_COLLABORATION_NAME": "Research Collaboration", "LOC_DIPLOMACY_PROJECT_DENOUNCE_NAME": "Denounce",
        "LOC_DIPLOMACY_ACTION_FORM_ALLIANCE_NAME": "Form Alliance", "LOC_DIPLOMACY_PROJECT_CS_INCORPORATE_NAME": "Incorporate",
        "LOC_DIPLOMACY_ESPIONAGE_STEAL_TECH_NAME": "Steal Technological Advancement", "LOC_DIPLOMACY_ACTION_LAND_CLAIM_NAME": "Land Claim",
        "LOC_DIPLOMACY_ACTION_GIVE_INFLUENCE_TOKEN_NAME": "Befriending - {1_villageName}",
        "LOC_INDEPENDENT_ANTIQUITY_24_MINOR_NAME": "Phrygian", "LOC_INDEPENDENT_ANTIQUITY_24_MAJOR_NAME": "Gordium",
        "LOC_INDEPENDENT_ANTIQUITY_5_MINOR_NAME": "Oponean", "LOC_INDEPENDENT_ANTIQUITY_5_MAJOR_NAME": "Opous",
        "LOC_INDEPENDENT_EXPLORATION_3_MINOR_NAME": "Tuareg"}


@pytest.fixture(scope="module")
def R(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("gamedata")
    (d / "diplomacy-actions.xml").write_text(DATA)
    return diplomacy.build_rules([d / "diplomacy-actions.xml"], TEXT)


def test_rules_map_logged_names_to_types_groups_and_proposals(R):
    assert R["by_name"]["Research Collaboration"] == "DIPLOMACY_ACTION_RESEARCH_COLLABORATION"
    assert R["by_name"]["LOC_DIPLOMACY_ACTION_GIVE_INFLUENCE_TOKEN_NAME"] == diplomacy.BEFRIEND  # logged as the raw key
    a = R["actions"]
    assert a["DIPLOMACY_ACTION_DENOUNCE"]["group"] == "sanction"
    assert a["DIPLOMACY_ACTION_RESEARCH_COLLABORATION"]["responses"] == ["support", "accept", "reject"]
    assert a["DIPLOMACY_ACTION_ESPIONAGE_STEAL_TECH"] == {"group": "espionage", "name": "LOC_DIPLOMACY_ESPIONAGE_STEAL_TECH_NAME",
                                                          "responses": []}


def _ds(text: str) -> list[dict]:
    """DiplomacySummary lines 'turn, initiator, recipient, action, details' as read_table rows."""
    out = []
    for line in text.strip().splitlines():
        t, a, b, act, det = [x.strip() for x in line.split(",", 4)]
        out.append({"Game Turn": t, "Initiator": a, "Recipient": b, "Action": act, "Details": det, "Mayhem": "0.0"})
    return out


def test_same_turn_reload_keeps_the_last_pass():
    rows = _ds("""
31, 0, 1, Met,
32, 4, 5, Alliance Ends,
32, 4, 7, Diplomacy Action Ended, Denounce result: Success
32, 4, 5, Alliance Ends,
32, 4, 7, Diplomacy Action Ended, Denounce result: Success
32, 0, 2, Diplomacy Action Started, Denounce
33, 0, 1, Peace,
""")
    assert diplomacy.summary_rows(rows) == [
        (31, 0, 1, "Met", ""), (32, 4, 5, "Alliance Ends", ""), (32, 4, 7, "Diplomacy Action Ended", "Denounce result: Success"),
        (32, 0, 2, "Diplomacy Action Started", "Denounce"), (33, 0, 1, "Peace", "")]


TOKEN = "LOC_DIPLOMACY_ACTION_GIVE_INFLUENCE_TOKEN_NAME"
SUMMARY = f"""
1, 0, -1, Diplomacy Action Started, Land Claim
4, 0, 27, Met,
4, 27, 0, Met,
4, 6, 63, Met,
5, 0, 1, Met,
5, 1, 0, Met,
18, 0, 27, Diplomacy Action Started, {TOKEN}
18, 0, 27, Diplomacy Action Enter Stage, {TOKEN} Entering Stage DIPLOMACY_INFLUENCE_INDEPENDENT_VERY_HOSTILE
18, 0, 27, Diplomacy Action Support Changed, {TOKEN} - P 0 increased support by 2
19, 1, 27, Diplomacy Action Started, {TOKEN}
19, 1, 27, Diplomacy Action Support Changed, {TOKEN} - P 1 increased support by 2
20, 0, 1, Diplomacy Action Started, Research Collaboration
20, 0, 1, Diplomacy Action Support Changed, Research Collaboration - P 0 increased support by 1
20, 0, 1, Diplomacy Action Support Changed, Research Collaboration - P 1 increased support by 1
20, 0, 1, Diplomacy Action Enter Stage, Research Collaboration Entering Stage DIPLOMACY_RESEARCH_COLLABORATION_COMPLETE
21, 1, 0, Diplomacy Action Started, Denounce
21, 1, 0, Diplomacy Action Support Changed, Denounce - P 1 increased support by 1
21, 2, 0, Diplomacy Action Started, Steal Technological Advancement
22, 0, 2, Diplomacy Action Started, Form Alliance
22, 0, 2, Diplomacy Action Ended, Form Alliance result: Canceled
23, 0, 1, Diplomacy Action Started, Form Alliance
23, 0, 1, Allied,
24, 0, 27, Diplomacy Action Enter Stage, {TOKEN} Entering Stage DIPLOMACY_INFLUENCE_INDEPENDENT_FRIENDLY
24, 0, 27, Diplomacy Action Support Changed, {TOKEN} - P 0 increased support by 1
26, 0, 27, Diplomacy Action Enter Stage, {TOKEN} Entering Stage DIPLOMACY_INFLUENCE_INDEPENDENT_SUZERAIN
26, 0, 27, Diplomacy Action Ended, {TOKEN} result: Success
26, 1, 27, Diplomacy Action Ended, {TOKEN} result: Canceled
28, 1, 15, Diplomacy Action Started, {TOKEN}
30, 0, 27, Diplomacy Action Started, Incorporate
32, 0, 27, Diplomacy Action Ended, Incorporate result: Success
"""
GOSSIP = [{"Game Turn": "26", "Player": "Ibn Battuta", "Civilization": "Carthage", "Plot X": "32", "Plot Y": "52",
           "Type": "GOSSIP_BECOME_SUZERAIN", "col6": "Gordium", "col7": "Gbara"},
          {"Game Turn": "27", "Player": "Rival A", "Civilization": "Rome", "Plot X": "-1", "Plot Y": "-1",
           "Type": "GOSSIP_REJECT_DEAL", "col6": ""}]
TRIBES = [{"Turn": "2", "Tribe": "INDEPENDENT_ANTIQUITY_24", "Action": "CREATE TRIBE 27", "Other": "32 52"},
          {"Turn": "2", "Tribe": "INDEPENDENT_ANTIQUITY_5", "Action": "CREATE TRIBE 15", "Other": "44 46"},
          {"Turn": "2", "Tribe": "INDEPENDENT_ANTIQUITY_24", "Action": "CREATE UNIT", "Other": "UNIT_WARRIOR:1"}]
MAJORS = {0: "Ibn Battuta", 1: "Rival A", 2: "Rival B"}


def _build(R, clock=None, extra_ages=None):
    clock = clock or export.Clock({"AGE_ANTIQUITY": 40})
    L = {"DiplomacySummary": {"AGE_ANTIQUITY": _ds(SUMMARY)} | (extra_ages or {}), "Game_Gossip": {"AGE_ANTIQUITY": GOSSIP},
         "Independents": {"AGE_ANTIQUITY": TRIBES}}
    return diplomacy.build(L, clock, MAJORS, {}, TEXT, R)


def _one(ev, kind, **match):
    hits = [e for e in ev if e["kind"] == kind and all(e["detail"].get(k) == v for k, v in match.items())]
    assert len(hits) == 1, (kind, match, hits)
    return hits[0]


def test_befriend_race_suzerain_and_incorporation(R):
    ev = _build(R)
    mine = _one(ev, "befriend", initiator=0)
    d = mine["detail"]
    assert mine["subject"] == "Befriend Phrygian (Ibn Battuta)" and mine["players"] == [0, 27]
    assert (mine["start"]["turn"], mine["end"]["turn"], mine["precision"]) == (18, 26, "exact")
    assert [s["stage"].rsplit("_", 1)[-1] for s in d["stages"]] == ["HOSTILE", "FRIENDLY", "SUZERAIN"]
    assert d["support"] == {"0": 3} and d["outcome"] == "success" and d["recipient_kind"] == "independent"
    assert d["rivals"] == [{"player": 1, "name": "Rival A", "start_turn": 19, "stage": None, "outcome": "canceled", "support": 2}]
    theirs = _one(ev, "befriend", initiator=1, recipient=27)
    assert theirs["detail"]["outcome"] == "canceled" and theirs["detail"]["lost_to"] == 0  # lost the race
    # suzerainty from the SUZERAIN stage, enriched by the gossip row (capital + chosen bonus); incorporation ends it
    suz = _one(ev, "suzerain", suzerain=0)
    assert suz["subject"] == "Ibn Battuta suzerain of Phrygian" and suz["players"] == [0, 27]
    assert (suz["start"]["turn"], suz["end"]["turn"]) == (26, 32)
    assert {k: suz["detail"][k] for k in ("capital", "bonus", "end_reason", "befriend_turns")} == {
        "capital": "Gordium", "bonus": "Gbara", "end_reason": "incorporated", "befriend_turns": 8}
    # still running at the age's last turn
    open_ = _one(ev, "befriend", initiator=1, recipient=15)
    assert open_["detail"]["end_reason"] == "age_end_or_ongoing" and open_["subject"] == "Befriend Oponean (Rival A)"


def test_proposals_responses_sanctions_and_meetings(R):
    ev = _build(R)
    rc = _one(ev, "diplomacy_action", action="Research Collaboration")
    assert rc["detail"]["group"] == "endeavor" and rc["detail"]["proposal"] is True and rc["detail"]["response"] == "supported"
    den = _one(ev, "diplomacy_action", action="Denounce")
    assert den["players"] == [1, 0] and den["detail"]["group"] == "sanction"
    spy = _one(ev, "diplomacy_action", action="Steal Technological Advancement")
    assert spy["detail"]["group"] == "espionage" and spy["detail"]["proposal"] is False
    responses = {(e["players"][0], e["detail"]["action"], e["detail"]["response"], e["start"]["turn"])
                 for e in ev if e["kind"] == "diplo_response"}
    assert responses == {(1, "Research Collaboration", "supported", 20), (2, "Form Alliance", "rejected", 22),
                         (1, "Form Alliance", "accepted", 23), (1, "Deal", "rejected", 27)}
    assert not [e for e in ev if e["detail"].get("action") in ("Land Claim", "Declare War")]  # automatic / export's war events
    met = sorted((e["players"][0], e["players"][1], e["detail"]["other_kind"]) for e in ev if e["kind"] == "met")
    assert met == [(0, 1, "major"), (0, 27, "independent")]  # once per pair; unnamed id 63 skipped


def test_major_meetings_are_not_repeated_at_an_age_start(R):
    clock = export.Clock({"AGE_ANTIQUITY": 40, "AGE_EXPLORATION": 10})
    ev = _build(R, clock, {"AGE_EXPLORATION": _ds("1, 0, 1, Met,\n1, 1, 0, Met,\n3, 0, 2, Met,")})
    met = [(e["start"]["age"], e["players"]) for e in ev if e["kind"] == "met"]
    assert ("AGE_EXPLORATION", [0, 2]) in met and ("AGE_EXPLORATION", [0, 1]) not in met


def test_metric_series_counts_and_resets(R):
    clock = export.Clock({"AGE_ANTIQUITY": 40, "AGE_EXPLORATION": 10})
    ev = _build(R, clock)
    ev.append({"kind": "war_declared", "start": clock.at("AGE_ANTIQUITY", 25), "end": None, "players": [0, 2],
               "detail": {"declared_by": 0}})
    s = diplomacy.metric_series(ev, clock, "AGE_EXPLORATION", 5)
    assert s[(0, "suzerainties")] == {26: 1, 32: 0, 45: 0}
    assert s[(0, "suzerainties_total")] == {26: 1, 45: 1}
    assert s[(0, "befriend_actions_active")] == {18: 1, 26: 0, 45: 0}
    # rival 1's open befriending was closed by the age transition: gone from the next age's first turn
    assert s[(1, "befriend_actions_active")] == {19: 1, 26: 0, 28: 1, 41: 0, 45: 0}
    # befriend, research collaboration, two alliance proposals, incorporate, war declaration
    assert s[(0, "diplomatic_actions_started_total")] == {18: 1, 20: 2, 22: 3, 23: 4, 25: 5, 30: 6, 45: 6}
    assert s[(0, "independents_met")] == {1: 0, 4: 1, 41: 0, 45: 0}  # resets in the new age


def test_digest_counts_visible_suzerainties_and_relationships():
    snap = {"game": {"turn": 5, "age": "AGE_MODERN", "local_player_id": 0}, "me": {"id": 0, "leader": "Ada Lovelace"},
            "players": [{"id": 1, "met": True, "leader": "Lafayette", "relationship": "Hostile", "relationship_level": -60},
                        {"id": 2, "met": False}],
            "city_states": [{"id": 8, "name": "Lakota", "suzerain": "Ada Lovelace"}, {"id": 9, "name": "Irish", "suzerain": 0},
                            {"id": 10, "name": "Tlingit", "suzerain": "Lafayette"}, {"id": 11, "name": "Finnish", "suzerain": None},
                            {"id": 12, "name": "Shoshone", "suzerain": "unmet civ"}]}
    d = ingest.digest(snap)
    assert d["metrics"]["0"]["suzerainties"] == 2  # by name and by id (older snapshots)
    assert d["metrics"]["1"] == {"suzerainties": 1, "relationship": -60}
    assert "2" not in d["metrics"]
    assert d["summary"]["relationships"] == {"1": {"leader": "Lafayette", "relationship": "Hostile"}}
    assert len(d["summary"]["city_states"]) == 5
    assert "suzerainties" not in ingest.digest({"game": {}, "me": {"id": 0}})["metrics"]["0"]  # section missing: unknown


def _digest(turn, bal, inc, rel=None, age="AGE_ANTIQUITY"):
    return {"game": {"age": age, "turn": turn}, "player_id": 0,
            "metrics": {"0": {"influence_balance": bal, "influence_per_turn": inc}},
            "summary": {"relationships": rel or {}}}


def test_influence_spent_attached_and_relationship_events():
    ds = [_digest(17, 160, 10), _digest(18, 170, 10, {"1": {"leader": "Rival A", "relationship": "Neutral"}}),
          _digest(19, 10, 10, {"1": {"leader": "Rival A", "relationship": "Friendly"}}), _digest(21, 30, 10)]
    spent = ingest.influence_spent(ds)
    assert spent == [("AGE_ANTIQUITY", 17, 0), ("AGE_ANTIQUITY", 18, 170)]  # no turn 20: no value for 19
    clock = export.Clock({"AGE_ANTIQUITY": 30})
    ev = [{"kind": "befriend", "start": clock.at("AGE_ANTIQUITY", 18), "players": [0, 27], "precision": "exact",
           "detail": {"initiator": 0}}]
    ingest._attach_influence(ev, spent, 0)
    assert ev[0]["detail"]["turn_influence_spent"] == 170
    rel = ingest._relationship_events(ds, clock, 0)
    assert [(e["start"]["turn"], e["players"], e["subject"]) for e in rel] == [(19, [0, 1], "Rival A: Neutral -> Friendly")]
    rows = {}
    ingest._diplomacy_metrics([], spent, lambda age, t, pid, m, v, src, vis=None: rows.__setitem__((t, pid, m), v), clock,
                              "AGE_ANTIQUITY", 21, 0)
    assert rows[(18, 0, "influence_spent")] == 170 and rows[(21, 0, "influence_spent_total")] == 170
    assert rows[(1, 0, "suzerainties")] == 0 and rows[(21, 0, "independents_met")] == 0  # zero baselines for goals


def test_rival_espionage_against_the_player_stays_out_of_the_local_view():
    spy = {"kind": "diplomacy_action", "players": [2, 0], "detail": {"group": "espionage", "initiator": 2}}
    mine = {"kind": "diplomacy_action", "players": [0, 2], "detail": {"group": "espionage", "initiator": 0}}
    den = {"kind": "diplomacy_action", "players": [2, 0], "detail": {"group": "sanction", "initiator": 2}}
    assert (ingest._is_local(spy, 0), ingest._is_local(mine, 0), ingest._is_local(den, 0)) == (False, True, True)


def _event(con, key, kind, subject, g, players, detail, end_g=None, minor=0):
    _insert(con, "events", game=GAME, event_key=key, kind=kind, subject=subject, start_g=g, start_age="AGE_ANTIQUITY",
            start_turn=g, end_g=end_g, end_age="AGE_ANTIQUITY" if end_g else None, end_turn=end_g, players=json.dumps(players),
            local=1, minor=minor, precision="exact", source="log:DiplomacySummary", detail=json.dumps(detail))


def test_briefing_and_follower_on_diplomacy(db):
    stage = "DIPLOMACY_INFLUENCE_INDEPENDENT_"
    with store.tx() as con:
        _event(con, "bef", "befriend", "Befriend Phrygian (Me)", 8, [0, 27],
               {"initiator": 0, "recipient": 27, "stage": stage + "FRIENDLY", "support": {"0": 3},
                "stages": [{"turn": 8, "stage": stage + "VERY_HOSTILE"}, {"turn": 11, "stage": stage + "FRIENDLY"}],
                "support_log": [{"turn": 8, "player": 0, "support": 2}, {"turn": 11, "player": 0, "support": 1}],
                "rivals": [{"player": 1, "name": "Rival A", "start_turn": 9, "stage": stage + "FRIENDLY", "support": 2}],
                "end_reason": "age_end_or_ongoing"}, end_g=11, minor=1)
        _event(con, "rc", "diplomacy_action", "Research Collaboration: Me -> Rival A", 11, [0, 1],
               {"initiator": 0, "recipient": 1, "group": "endeavor", "action": "Research Collaboration"})
        _set_turn(con, 11)
    with store.connect() as con:
        b = advisor.briefing(con, GAME, 10)
    assert b["diplomacy"]["befriending"][0]["rivals"][0]["name"] == "Rival A"
    assert b["diplomacy"]["progress"] == [{"g": 8, "kind": "befriend", "subject": "Befriend Phrygian (Me)",
                                           "stages": [stage + "FRIENDLY"], "support_added": 1}]
    text = advisor_cli.render_brief(b)
    assert "befriending since g8: Befriend Phrygian (Me), stage friendly, support 3; also befriending it: Rival A (since T9, friendly)" in text
    assert "progress: Befriend Phrygian (Me), entered friendly, support +1" in text

    with store.tx() as con:
        _event(con, "suz", "suzerain", "Me suzerain of Phrygian", 12, [0, 27], {"suzerain": 0, "city_state_name": "Phrygian",
                                                                               "bonus": "Gbara"}, minor=1)
        _event(con, "den", "diplomacy_action", "Denounce: Rival B -> Me", 12, [2, 0],
               {"initiator": 2, "recipient": 0, "group": "sanction", "action": "Denounce"})
        _event(con, "resp", "diplo_response", "Rival A rejected Form Alliance from Me", 12, [1, 0],
               {"response": "rejected", "proposer": 0, "responder": 1, "action": "Form Alliance"})
        _event(con, "resp2", "diplo_response", "Me accepted Denounce from Rival B", 12, [0, 2],
               {"response": "accepted", "proposer": 2, "responder": 0, "action": "Denounce"})
        _event(con, "resp3", "diplo_response", "Rival B accepted Open Borders from Me", 12, [2, 0],
               {"response": "accepted", "proposer": 0, "responder": 2, "action": "Open Borders"})
        _set_turn(con, 12)
    with store.connect() as con:
        b = advisor.briefing(con, GAME, 11)
    kinds = {e["subject"] for e in b["events_since"]}
    assert {"Me suzerain of Phrygian", "Denounce: Rival B -> Me", "Rival A rejected Form Alliance from Me"} <= kinds
    assert b["diplomacy"]["suzerain_of"] == ["Phrygian"]
    assert "suzerain of: Phrygian" in advisor_cli.render_brief(b)


@pytest.mark.skipif(not diplomacy.rules.GAME.exists(), reason="game data not installed")
def test_real_game_rules_cover_logged_action_names(tmp_path, monkeypatch):
    monkeypatch.setattr(diplomacy, "CACHE", tmp_path / "diplomacy_rules.json")
    monkeypatch.setattr(diplomacy, "_RULES", None)
    R = diplomacy.load_rules()
    for name in ("Research Collaboration", "Steal Technological Advancement", "Hinder Finances", "Reconciliation", "Open Markets",
                 "Local Festival", "Form Alliance", "Incorporate", "Counter Spy", "LOC_DIPLOMACY_ACTION_GIVE_INFLUENCE_TOKEN_NAME"):
        assert name in R["by_name"], name
    assert R["actions"]["DIPLOMACY_ACTION_DENOUNCE"]["group"] == "sanction"
