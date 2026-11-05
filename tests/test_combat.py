"""Offline tests for combat rewards (combat.py) and their store wiring (ingest, advisor briefing)."""

import json

import pytest

from civilizer import advisor, advisor_cli, combat, export, ingest, store

DATA = """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <Units>
    <Row UnitType="UNIT_PRIVATEER" Domain="DOMAIN_SEA" CoreClass="CORE_CLASS_MILITARY"/>
    <Row UnitType="UNIT_CARRACK" Domain="DOMAIN_SEA" CoreClass="CORE_CLASS_MILITARY"/>
    <Row UnitType="UNIT_SWORDSMAN" Domain="DOMAIN_LAND" CoreClass="CORE_CLASS_MILITARY"/>
    <Row UnitType="UNIT_SCOUT" Domain="DOMAIN_LAND" CoreClass="CORE_CLASS_RECON"/>
    <Row UnitType="UNIT_MERCHANT" Domain="DOMAIN_LAND" CoreClass="CORE_CLASS_SUPPORT"/>
    <Row UnitType="UNIT_HWARANG" Domain="DOMAIN_LAND" CoreClass="CORE_CLASS_MILITARY"/>
  </Units>
  <Unit_Stats>
    <Row UnitType="UNIT_CARRACK" Combat="35"/>
    <Row UnitType="UNIT_SWORDSMAN" Combat="20"/>
  </Unit_Stats>
  <Unit_Costs>
    <Row UnitType="UNIT_MERCHANT" YieldType="YIELD_PRODUCTION" Cost="80"/>
  </Unit_Costs>
  <TypeTags>
    <Row Type="UNIT_PRIVATEER" Tag="UNIT_CLASS_PRIVATEER"/>
    <Row Type="UNIT_PRIVATEER" Tag="UNIT_CLASS_NAVAL"/>
    <Row Type="UNIT_CARRACK" Tag="UNIT_CLASS_NAVAL"/>
    <Row Type="UNIT_HWARANG" Tag="UNIT_CLASS_HWARANG"/>
  </TypeTags>
  <UnitClass_Abilities>
    <Row UnitAbilityType="ABILITY_PRIVATEER_GOLD" UnitClassType="UNIT_CLASS_PRIVATEER"/>
    <Row UnitAbilityType="ABILITY_HWARANG_CULTURE" UnitClassType="UNIT_CLASS_HWARANG"/>
  </UnitClass_Abilities>
  <UnitAbilities>
    <Row UnitAbilityType="ABILITY_PRIVATEER_GOLD"/>
    <Row UnitAbilityType="ABILITY_HWARANG_CULTURE" Inactive="true"/>
  </UnitAbilities>
  <UnitAbilityModifiers>
    <Row UnitAbilityType="ABILITY_PRIVATEER_GOLD" ModifierId="MOD_PRIVATEER_GOLD"/>
    <Row UnitAbilityType="ABILITY_HWARANG_CULTURE" ModifierId="CULTURE_KILLS_HWARANG"/>
  </UnitAbilityModifiers>
  <LeaderTraits>
    <Row LeaderType="LEADER_EDWARD_TEACH" TraitType="TRAIT_TEACH"/>
  </LeaderTraits>
  <TraitModifiers>
    <Row TraitType="TRAIT_TEACH" ModifierId="TEACH_GOLD_KILLS"/>
  </TraitModifiers>
  <Traditions>
    <Row TraditionType="TRADITION_MERRY_LIFE" Name="LOC_TRADITION_MERRY_LIFE_NAME"/>
  </Traditions>
  <TraditionModifiers>
    <Row TraditionType="TRADITION_MERRY_LIFE" ModifierId="MERRY_SUPPORT_GOLD"/>
  </TraditionModifiers>
  <CityStateTypes>
    <Row CityStateType="ECONOMIC" YieldType="YIELD_GOLD" DisperseRewardAmount="100"/>
    <Row CityStateType="CULTURAL" YieldType="YIELD_CULTURE" DisperseRewardAmount="100"/>
  </CityStateTypes>
  <Independents>
    <Row IndependentType="INDEPENDENT_X_1" CityStateType="ECONOMIC"/>
    <Row IndependentType="INDEPENDENT_X_2" CityStateType="CULTURAL"/>
  </Independents>
</Database>
"""

EFFECTS = """<?xml version="1.0" encoding="utf-8"?>
<GameEffects xmlns="GameEffects">
  <Modifier id="MOD_PRIVATEER_GOLD" collection="COLLECTION_UNIT_COMBAT" effect="EFFECT_ADJUST_UNIT_POST_COMBAT_YIELD">
    <Argument name="YieldType">YIELD_GOLD</Argument>
    <Argument name="PercentDefeatedStrength">100</Argument>
  </Modifier>
  <Modifier id="CULTURE_KILLS_HWARANG" collection="COLLECTION_UNIT_COMBAT" effect="EFFECT_ADJUST_UNIT_POST_COMBAT_YIELD">
    <Argument name="YieldType">YIELD_CULTURE</Argument>
    <Argument name="PercentDefeatedStrength">25</Argument>
  </Modifier>
  <Modifier id="TEACH_GOLD_KILLS" collection="COLLECTION_PLAYER_UNITS" effect="EFFECT_ATTACH_MODIFIERS">
    <SubjectRequirements>
      <Requirement type="REQUIREMENT_UNIT_DOMAIN_MATCHES"><Argument name="UnitDomain">DOMAIN_SEA</Argument></Requirement>
    </SubjectRequirements>
    <Argument name="ModifierId">TEACH_GOLD_KILLS_MODIFIER</Argument>
  </Modifier>
  <Modifier id="TEACH_GOLD_KILLS_MODIFIER" collection="COLLECTION_UNIT_COMBAT" effect="EFFECT_ADJUST_UNIT_POST_COMBAT_YIELD">
    <SubjectRequirements>
      <Requirement type="REQUIREMENT_OPPONENT_UNIT_DOMAIN_MATCHES"><Argument name="UnitDomain">DOMAIN_SEA</Argument></Requirement>
    </SubjectRequirements>
    <Argument name="YieldType">YIELD_GOLD</Argument>
    <Argument name="PercentDefeatedStrength">50</Argument>
  </Modifier>
  <Modifier id="MERRY_SUPPORT_GOLD" collection="COLLECTION_PLAYER_COMBAT" effect="EFFECT_ADJUST_UNIT_POST_COMBAT_YIELD">
    <Argument name="YieldType">YIELD_GOLD</Argument>
    <Argument name="PercentDefeatedUnitCost">100</Argument>
    <Argument name="OnlyWhenDefeatedSupportUnit">true</Argument>
  </Modifier>
  <Modifier id="MEMENTO_GOLD" collection="COLLECTION_UNIT_COMBAT" effect="EFFECT_ADJUST_UNIT_POST_COMBAT_YIELD">
    <Argument name="YieldType">YIELD_GOLD</Argument>
    <Argument name="PercentDefeatedStrength">25</Argument>
  </Modifier>
</GameEffects>
"""


@pytest.fixture(scope="module")
def R(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("gamedata")
    (d / "units.xml").write_text(DATA)
    (d / "units-gameeffects.xml").write_text(EFFECTS)
    return combat.build_rules([d / "units.xml", d / "units-gameeffects.xml"])


ME = {"leader": "LEADER_ADA_LOVELACE", "civilization": "CIVILIZATION_AMERICA", "traditions": set()}


def test_rules_resolve_grants_and_skip_what_logs_cannot_tell(R):
    names = {r["modifier"] for r in R["kill_rules"]}
    assert names == {"MOD_PRIVATEER_GOLD", "TEACH_GOLD_KILLS_MODIFIER", "MERRY_SUPPORT_GOLD"}
    skipped = {s["modifier"]: s["why"] for s in R["skipped_rules"]}
    assert "inactive ability ABILITY_HWARANG_CULTURE" in skipped["CULTURE_KILLS_HWARANG"]  # switched on by a civic
    assert "MEMENTO_GOLD" in skipped  # not attached to anything the logs reveal
    assert R["units"]["UNIT_CARRACK"] == {"tags": ["UNIT_CLASS_NAVAL"], "domain": "DOMAIN_SEA",
                                          "core_class": "CORE_CLASS_MILITARY", "combat": 35}
    assert R["independents"]["INDEPENDENT_X_1"] == "ECONOMIC"
    assert R["city_state_types"]["ECONOMIC"] == {"yield": "YIELD_GOLD", "base": 100}


def test_kill_rewards_follow_unit_leader_tradition_and_requirements(R):
    # Privateer ability: 100% of the defeated unit's logged strength
    assert [(r["modifier"], r["amount"]) for r in combat.kill_rewards("UNIT_PRIVATEER", "UNIT_CARRACK", 35, ME, R)] == [
        ("MOD_PRIVATEER_GOLD", 35.0)]
    # Edward Teach: naval killer vs naval victim only, 50%; stacks with the Privateer ability
    teach = ME | {"leader": "LEADER_EDWARD_TEACH"}
    got = {r["modifier"]: r["amount"] for r in combat.kill_rewards("UNIT_PRIVATEER", "UNIT_CARRACK", 35, teach, R)}
    assert got == {"MOD_PRIVATEER_GOLD": 35.0, "TEACH_GOLD_KILLS_MODIFIER": 17.5}
    got = {r["modifier"] for r in combat.kill_rewards("UNIT_PRIVATEER", "UNIT_SWORDSMAN", 20, teach, R)}
    assert got == {"MOD_PRIVATEER_GOLD"}  # land victim: no Teach gold
    # strength falls back to Unit_Stats when the log has none
    assert combat.kill_rewards("UNIT_PRIVATEER", "UNIT_SWORDSMAN", None, ME, R)[0]["amount"] == 20.0
    # tradition: only when slotted; support units only; amount from production cost
    merry = ME | {"traditions": {"TRADITION_MERRY_LIFE"}}
    assert [(r["modifier"], r["amount"]) for r in combat.kill_rewards("UNIT_SWORDSMAN", "UNIT_MERCHANT", 10, merry, R)] == [
        ("MERRY_SUPPORT_GOLD", 80.0)]
    assert combat.kill_rewards("UNIT_SWORDSMAN", "UNIT_SCOUT", 10, merry, R) == []
    assert combat.kill_rewards("UNIT_SWORDSMAN", "UNIT_MERCHANT", 10, ME | {"traditions": None}, R) == []  # unknown
    assert combat.kill_rewards("UNIT_SWORDSMAN", "UNIT_CARRACK", 35, ME, R) == []  # no reward source
    assert combat.kill_rewards("UNIT_SECRET", "UNIT_CARRACK", 35, ME, R) == []  # unknown unit type


def _rows(text: str) -> list[dict]:
    lines = [l for l in text.strip().splitlines()]
    head = [h.strip() for h in lines[0].split(",")]
    out = []
    for l in lines[1:]:
        vals = [v.strip() for v in l.split(",")]
        out.append({head[i] if i < len(head) else f"col{i}": v for i, v in enumerate(vals)})
    return out


COMBAT_LOG = """Turn, SourceType, Location, AttPlayer, DefPlayer, CombatType, Attacker, Defender, AttStr, DefStr, AttStrMod, DefStrMod, AttDmg, DefDmg, Destroyed, HealAmount, attHealth, defHealth
116,Unit vs Unit,(44)(54),0,21,Melee,(26)UNIT_PRIVATEER,(25)UNIT_CARRACK,35,35,10,-6,19,65,Defender,0,(38)100,(0)100
117,Unit vs Unit,(40)(20),8,0,Melee,(3)UNIT_SWORDSMAN,(26)UNIT_PRIVATEER,20,35,0,0,100,0,Attacker,0,(0)100,(100)100
118,Unit vs Unit,(40)(21),0,8,Melee,(26)UNIT_PRIVATEER,(3)UNIT_SWORDSMAN,35,20,0,0,10,10,N/A,0,(90)100,(90)100
118,Unit vs Location,(20)(6),0,19,Ranged,(32)UNIT_PRIVATEER,(-1)LOC_DISTRICT_CITY_CENTER_NAME,65,50,20,0,0,100,District,0,(100)100,(0)100
"""


def test_combat_log_kills_both_directions():
    # 116: the attacking Privateer sinks a Carrack; 117: a Swordsman attacks the Privateer and is destroyed (the
    # defender gets the kill); 118: no one destroyed; city-centre attacks are not unit kills
    kills = combat.combat_kills(_rows(COMBAT_LOG))
    assert [(k["turn"], k["killer"], k["killer_unit"], k["victim"], k["victim_unit"], k["victim_strength"], k["x"], k["y"])
            for k in kills] == [(116, 0, "UNIT_PRIVATEER", 21, "UNIT_CARRACK", 35.0, 44, 54),
                                (117, 0, "UNIT_PRIVATEER", 8, "UNIT_SWORDSMAN", 20.0, 40, 20)]


def test_gossip_handles_unquoted_leader_names_with_commas():
    rows = [{"Game Turn": "67", "Player": "Xerxes", "Civilization": "the Achaemenid", "Plot X": "Achaemenid Persia",
             "Plot Y": "23", "Type": "6", "col6": "GOSSIP_PILLAGE", "col7": ""},
            {"Game Turn": "7", "Player": "Ada Lovelace", "Civilization": "America", "Plot X": "28", "Plot Y": "45",
             "Type": "GOSSIP_INDEPENDENT_DISPERSE", "col6": "Independent Dispersed=", "col7": "Dispersing Unit=UNIT_CUIRASSIER"},
            {"Game Turn": "8", "Player": "Ada Lovelace", "Civilization": "America", "Plot X": "1", "Plot Y": "2",
             "Type": "GOSSIP_TRAIN_UNIT", "col6": "Merchant"}]
    got = combat.gossip(rows, ("GOSSIP_PILLAGE", "GOSSIP_INDEPENDENT_DISPERSE"))
    assert [(g["turn"], g["type"], g["player_name"], g["civ"], g["x"], g["y"]) for g in got] == [
        (67, "GOSSIP_PILLAGE", "Xerxes, the Achaemenid", "Achaemenid Persia", 23, 6),
        (7, "GOSSIP_INDEPENDENT_DISPERSE", "Ada Lovelace", "America", 28, 45)]
    assert got[1]["extra"] == ["Independent Dispersed=", "Dispersing Unit=UNIT_CUIRASSIER"]


def test_treasury_residual_and_notification_parsing():
    rows = [{"Turn": "6", "Player": "0", "Gold Balance": "228.5", "Gold Yield": "187.1"},
            {"Turn": "7", "Player": "0", "Gold Balance": "363.7", "Gold Yield": "133.2"},
            {"Turn": "8", "Player": "0", "Gold Balance": "1964.7", "Gold Yield": "149.0"},
            {"Turn": "10", "Player": "0", "Gold Balance": "5.0", "Gold Yield": "1.0"}]
    assert combat.treasury_residuals(rows) == {(0, 6): -51.9, (0, 7): 1467.8}  # no turn 11: no residual for 10
    s = "The Independent Power of Lakota has been dispersed. You have received 1,250 [icon:YIELD_GOLD] Gold."
    assert combat.notification_reward(s) == ("YIELD_GOLD", 1250.0)
    assert combat.notification_reward("... You have received 60 Culture.") == ("YIELD_CULTURE", 60.0)
    assert combat.notification_reward("Command Units") is None


def _logs(R) -> dict:
    age = "AGE_MODERN"
    gossip_rows = [
        {"Game Turn": "7", "Player": "Ada Lovelace", "Civilization": "America", "Plot X": "28", "Plot Y": "45",
         "Type": "GOSSIP_INDEPENDENT_DISPERSE", "col6": "Independent Dispersed=", "col7": "Dispersing Unit=UNIT_CUIRASSIER"},
        {"Game Turn": "9", "Player": "Ada Lovelace", "Civilization": "America", "Plot X": "50", "Plot Y": "50",
         "Type": "GOSSIP_PILLAGE", "col6": ""},
        {"Game Turn": "9", "Player": "Ada Lovelace", "Civilization": "America", "Plot X": "51", "Plot Y": "50",
         "Type": "GOSSIP_PILLAGE", "col6": ""},
        {"Game Turn": "11", "Player": "Ada Lovelace", "Civilization": "America", "Plot X": "10", "Plot Y": "10",
         "Type": "GOSSIP_PILLAGE", "col6": ""},
        {"Game Turn": "12", "Player": "Ada Lovelace", "Civilization": "America", "Plot X": "5", "Plot Y": "5",
         "Type": "GOSSIP_INDEPENDENT_DISPERSE", "col6": "Dispersing Unit=UNIT_FRIGATE"},
        {"Game Turn": "9", "Player": "Austrian", "Civilization": "Villages", "Plot X": "9", "Plot Y": "8",
         "Type": "GOSSIP_PILLAGE", "col6": ""},
    ]
    treasury = []
    for t, bal, y in ((7, 100, 50), (8, 1650, 50), (9, 1700, 50), (10, 1900, 50), (11, 1950, 50), (12, 1000, 50), (13, 1050, 50)):
        treasury.append({"Turn": str(t), "Player": "0", "Gold Balance": str(bal), "Gold Yield": str(y)})
    return {
        "CombatLog": {"AGE_EXPLORATION": _rows(COMBAT_LOG)},
        "Game_Gossip": {age: gossip_rows},
        "Independents": {age: [{"Turn": "7", "Tribe": "INDEPENDENT_X_1", "Action": "DISPERSE TRIBE", "Other": "28 46"},
                               {"Turn": "12", "Tribe": "INDEPENDENT_X_2", "Action": "DISPERSE TRIBE", "Other": "6 5"}]},
        "Player_Treasury": {age: treasury},
        "CityPurchase": {age: [{"Game Turn": "11", "Player": "0", "City": "X", "Item Purchased": "BUILDING_Y"}]},
    }


def test_rewards_assembles_kills_dispersals_pillage_and_unexplained_gold(R):
    clock = export.Clock({"AGE_EXPLORATION": 140, "AGE_MODERN": 20})
    out = combat.rewards(_logs(R), clock, lambda pid, age, turn: ME, {"Ada Lovelace": 0},
                         notices={("AGE_MODERN", 12): [{"type": "NOTIFICATION_DISPERSE_IP_REWARD",
                                                        "summary": "... dispersed. You have received 75 [icon:YIELD_CULTURE] Culture."}]},
                         R=R)
    ev = out["events"]
    kill = [e for e in ev if e["kind"] == "kill"]
    assert [(e["player_id"], e["yield"], e["amount"], e["basis"], e["target"], e["target_owner"]) for e in kill] == [
        (0, "YIELD_GOLD", 35.0, "rules", "UNIT_CARRACK", 21), (0, "YIELD_GOLD", 20.0, "rules", "UNIT_SWORDSMAN", 8)]
    assert kill[0]["g"] == 116 and kill[0]["modifier"] == "MOD_PRIVATEER_GOLD"
    d7, d12 = [e for e in ev if e["kind"] == "disperse"]
    # economic camp: gold yield type known from the camp, amount not logged: never filled from the treasury
    assert (d7["yield"], d7["amount"], d7["basis"], d7["target"], d7["x"], d7["y"]) == (
        "YIELD_GOLD", None, "unknown", "INDEPENDENT_X_1", 28, 46)
    assert d7["unit"] == "UNIT_CUIRASSIER" and d7["city_state_type"] == "ECONOMIC" and d7["base_amount"] == 100
    # notification seen: exact
    assert (d12["yield"], d12["amount"], d12["basis"]) == ("YIELD_CULTURE", 75.0, "notification")
    pil = [e for e in ev if e["kind"] == "pillage"]
    assert len(pil) == 3  # the independent's pillage is not a major's reward
    assert [(e["turn"], e["yield"], e["amount"], e["basis"]) for e in pil] == [
        (9, None, None, "unknown"), (9, None, None, "unknown"), (11, None, None, "unknown")]
    assert not any("treasury" in k for e in ev for k in e)
    # unexplained gold: per turn, not attributed; turn 11 had a purchase (no prices logged) so it is left out
    assert out["unexplained"] == {("AGE_MODERN", 0, 7): 1500.0, ("AGE_MODERN", 0, 8): 0.0, ("AGE_MODERN", 0, 9): 150.0,
                                  ("AGE_MODERN", 0, 10): 0.0, ("AGE_MODERN", 0, 12): 0.0}

    inc = combat.cumulative(ev)
    assert dict(inc[(0, "combat_gold_total")]) == {116: 35.0, 117: 20.0}
    assert dict(inc[(0, "kill_gold_total")]) == {116: 35.0, 117: 20.0}
    assert dict(inc[(0, "pillages_total")]) == {149: 2, 151: 1}
    assert dict(inc[(0, "combat_culture_total")]) == {152: 75.0}
    assert dict(inc[(0, "independents_dispersed_total")]) == {147: 1, 152: 1}
    assert not any(m.endswith("_gold_total") and m not in ("combat_gold_total", "kill_gold_total") for _, m in inc)


def test_exact_combat_gold_is_subtracted_from_unexplained_gold(R):
    clock = export.Clock({"AGE_EXPLORATION": 140})
    logs = {"CombatLog": {"AGE_EXPLORATION": _rows(COMBAT_LOG)},
            "Game_Gossip": {"AGE_EXPLORATION": [{"Game Turn": "116", "Player": "Ada Lovelace", "Civilization": "America",
                                                 "Plot X": "44", "Plot Y": "55", "Type": "GOSSIP_PILLAGE", "col6": ""}]},
            "Player_Treasury": {"AGE_EXPLORATION": [{"Turn": "116", "Player": "0", "Gold Balance": "0", "Gold Yield": "10"},
                                                    {"Turn": "117", "Player": "0", "Gold Balance": "100", "Gold Yield": "10"}]}}
    out = combat.rewards(logs, clock, lambda *a: ME, {"Ada Lovelace": 0}, R=R)
    pil = next(e for e in out["events"] if e["kind"] == "pillage")
    assert (pil["amount"], pil["basis"]) == (None, "unknown")
    assert out["unexplained"] == {("AGE_EXPLORATION", 0, 116): 55.0}  # 90 minus the Privateer's 35 kill gold


def test_ingest_metrics_zero_baseline_and_rows(tmp_path, R, monkeypatch):
    monkeypatch.setattr(store, "DB", tmp_path / "civilizer.db")
    clock = export.Clock({"AGE_EXPLORATION": 140, "AGE_MODERN": 20})
    out = combat.rewards(_logs(R), clock, lambda *a: ME, {"Ada Lovelace": 0}, R=R)
    rows = {}

    def put(age, turn, pid, metric, value, source, vis=None):
        rows[(clock.offset[age] + int(turn), pid, metric)] = value

    unexplained = [{"age": a, "player": p, "turn": t, "value": v} for (a, p, t), v in out["unexplained"].items()]
    unexplained.append({"age": "AGE_MODERN", "player": 0, "turn": 13, "value": -40.0})  # an upgrade: not netted
    ingest._combat_metrics(out["events"], unexplained, put, clock, "AGE_MODERN", 15, 0)
    assert rows[(1, 0, "combat_gold_total")] == 0  # baseline from the first turn for goals
    assert rows[(116, 0, "combat_gold_total")] == 35.0
    assert rows[(155, 0, "combat_gold_total")] == 55.0  # current turn carries the total; exact amounts only
    assert rows[(155, 0, "kill_gold_total")] == 55.0
    assert rows[(155, 0, "trade_routes_plundered_total")] == 0  # no plunder yet: still present
    assert rows[(155, 0, "pillages_total")] == 3
    assert rows[(147, 0, "unexplained_gold")] == 1500.0 and rows[(153, 0, "unexplained_gold")] == -40.0
    assert (151, 0, "unexplained_gold") not in rows  # purchase turn
    assert rows[(1, 0, "unexplained_gold_total")] == 0
    assert rows[(147, 0, "unexplained_gold_total")] == 1500.0 and rows[(149, 0, "unexplained_gold_total")] == 1650.0
    assert rows[(155, 0, "unexplained_gold_total")] == 1650.0
    removed = ("pillage_gold_total", "plunder_gold_total", "disperse_gold_total", "treasury_residual")
    assert not any(m in removed for _, _, m in rows)
    with store.tx() as con:
        ingest._insert_rewards(con, "g1", out["events"])
        r = con.execute("SELECT * FROM combat_rewards WHERE kind='kill'").fetchone()
        assert (r["g"], r["player_id"], r["yield_type"], r["amount"], r["basis"], r["unit"], r["target"], r["target_owner"]) == (
            116, 0, "YIELD_GOLD", 35.0, "rules", "UNIT_PRIVATEER", "UNIT_CARRACK", 21)
        assert json.loads(r["detail"])["modifier"] == "MOD_PRIVATEER_GOLD"
        keys = [x[0] for x in con.execute("SELECT reward_id FROM combat_rewards")]
        assert len(keys) == len(set(keys)) == len(out["events"])
        assert con.execute("SELECT count(*) FROM combat_rewards WHERE kind<>'kill' AND amount IS NOT NULL "
                           "AND basis<>'notification'").fetchone()[0] == 0
        catalog = {x[0] for x in con.execute("SELECT metric FROM metric_catalog")}
        assert {"unexplained_gold", "unexplained_gold_total", "kill_gold_total"} <= catalog
        assert not catalog & set(removed)


def test_briefing_reports_combat_rewards_since(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB", tmp_path / "civilizer.db")
    with store.tx() as con:
        con.execute("INSERT INTO games (game, local_player_id, current_age, current_turn, current_g) VALUES ('g1', 0, 'AGE_MODERN', 10, 10)")
        con.execute("INSERT INTO players (game, player_id, leader, is_human, civilizations) VALUES ('g1', 0, 'Me', 1, '{}')")
        for g in (9, 10):
            con.execute("INSERT INTO turns (game, g, age, turn, summary) VALUES ('g1', ?, 'AGE_MODERN', ?, '{}')", (g, g))
        rows = [("a", 10, "kill", "YIELD_GOLD", 35.0, "rules"), ("b", 10, "kill", "YIELD_SCIENCE", 35.0, "rules"),
                ("c", 9, "pillage", None, None, "unknown"), ("d", 10, "pillage", None, None, "unknown"),
                ("f", 9, "pillage", None, None, "unknown"), ("e", 5, "kill", "YIELD_GOLD", 99.0, "rules")]
        for rid, g, kind, y, amt, basis in rows:
            con.execute("INSERT INTO combat_rewards (game, reward_id, g, age, turn, player_id, kind, yield_type, amount, basis, source) "
                        "VALUES ('g1', ?, ?, 'AGE_MODERN', ?, 0, ?, ?, ?, ?, 'log:CombatLog')", (rid, g, g, kind, y, amt, basis))
        for g, v in ((8, 500.0), (9, 120.0), (10, 0.0)):
            con.execute("INSERT INTO metrics VALUES ('g1', ?, 'AGE_MODERN', ?, 0, 'unexplained_gold', ?, 'log:Player_Treasury', 'visible')",
                        (g, g, v))
        b = advisor.briefing(con, "g1")
    assert b["since_g"] == 9
    assert b["combat_rewards_since"] == {
        "by_kind": {"kill": {"count": 2, "yields": {"YIELD_GOLD": 35.0, "YIELD_SCIENCE": 35.0}},
                    "pillage": {"count": 1, "yields": {}, "unknown_amount": 1}},
        # turn 9's value (known one turn late) with that turn's combat events; turn 8 is outside, turn 10 is zero
        "other_gold": {"gained": 120.0, "lost": 0, "turns": [{"g": 9, "gold": 120.0, "combat_events": {"pillage": 2}}]}}
    text = advisor_cli.render_brief(b)
    assert "kill x2: 35 gold, 35 science" in text and "pillage x1; 1 amount not logged by the game" in text
    assert "Other gold (unexplained treasury change, not attributed):" in text
    assert "g9: +120 (combat events that turn: pillage x2)" in text
    assert "pillage gold" not in text.lower()


@pytest.mark.skipif(not combat.rules.GAME.exists(), reason="game data not installed")
def test_real_game_rules_cover_known_kill_rewards():
    R = combat.load_rules()
    names = {r["modifier"] for r in R["kill_rules"]}
    assert "MOD_PRIVATEER_GOLD_ON_UNIT_DEFEAT" in names
    assert combat.kill_rewards("UNIT_PRIVATEER", "UNIT_CARRACK", 35, ME, R)[0]["amount"] == 35.0
    if "YI_MOD_SCIENCE_KILLS_MODIFIER" in names:  # DLC leader
        got = combat.kill_rewards("UNIT_CARRACK", "UNIT_CARRACK", 35, ME | {"leader": "LEADER_YI_SUN_SIN"}, R)
        assert ("YIELD_SCIENCE", 35.0) in {(r["yield"], r["amount"]) for r in got}
