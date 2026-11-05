"""Indexed Civilopedia: extraction from a small fixture install, plus a smoke check against the real install."""

import os
import sqlite3
from pathlib import Path

import pytest

from civilizer import civilopedia as cp
from civilizer import rules

FIXTURE = {
    "Base/modules/base-standard/data/civilopedia.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <CivilopediaSections>
    <Row SectionID="CONCEPTS" Name="LOC_PEDIA_CONCEPTS_TITLE" SortIndex="1"/>
    <Row SectionID="BUILDINGS" Name="LOC_PEDIA_BUILDINGS_TITLE" SortIndex="2"/>
  </CivilopediaSections>
  <CivilopediaPages>
    <Row SectionID="CONCEPTS" PageID="BIOME_4" PageLayoutID="Concept" Name="LOC_PEDIA_CONCEPTS_PAGE_BIOME_4_TITLE"/>
    <Row SectionID="CONCEPTS" PageID="PILLAGING_1" PageLayoutID="Concept" Name="LOC_PEDIA_CONCEPTS_PAGE_PILLAGING_1_TITLE"/>
  </CivilopediaPages>
  <CivilopediaPageLayoutChapters>
    <Row PageLayoutID="Concept" ChapterID="CONTENT" SortIndex="10"/>
  </CivilopediaPageLayoutChapters>
</Database>""",
    "Base/modules/base-standard/data/core.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <Yields>
    <Row YieldType="YIELD_GOLD" Name="LOC_YIELD_GOLD"/>
    <Row YieldType="YIELD_PRODUCTION" Name="LOC_YIELD_PRODUCTION"/>
    <Row YieldType="YIELD_HAPPINESS" Name="LOC_YIELD_HAPPINESS"/>
  </Yields>
  <Terrains><Row TerrainType="TERRAIN_COAST" Name="LOC_TERRAIN_COAST_NAME" MovementCost="1" Water="true"/></Terrains>
  <Districts><Row DistrictType="DISTRICT_URBAN" Name="LOC_DISTRICT_URBAN_NAME"/></Districts>
</Database>""",
    "Base/modules/base-standard/data/units.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <Units>
    <Row UnitType="UNIT_MERCHANT" Name="LOC_UNIT_MERCHANT_NAME" Description="LOC_UNIT_MERCHANT_DESCRIPTION" BaseSightRange="2"
         BaseMoves="4" Domain="DOMAIN_LAND" CoreClass="CORE_CLASS_CIVILIAN" FormationClass="FORMATION_CLASS_CIVILIAN" MakeTradeRoute="true"/>
  </Units>
  <Unit_Costs><Row UnitType="UNIT_MERCHANT" YieldType="YIELD_PRODUCTION" Cost="40"/></Unit_Costs>
</Database>""",
    "Base/modules/age-antiquity/config/config.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <Ages><Row AgeType="AGE_ANTIQUITY" Name="LOC_AGE_ANTIQUITY_NAME" PlayerCivilizationDomain="AntiquityAgeCivilizations" ChronologyIndex="10"/></Ages>
</Database>""",
    "Base/modules/age-antiquity/data/constructibles.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <Constructibles>
    <Row ConstructibleType="BUILDING_MARKET" Name="LOC_BUILDING_MARKET_NAME" Description="LOC_BUILDING_MARKET_DESCRIPTION" ConstructibleClass="BUILDING" Cost="90" Population="1" Age="AGE_ANTIQUITY"/>
    <Row ConstructibleType="BUILDING_BASILICA" Name="LOC_BUILDING_BASILICA_NAME" Description="LOC_BUILDING_BASILICA_DESCRIPTION" ConstructibleClass="BUILDING" Cost="135" Population="1" Age="AGE_ANTIQUITY"/>
  </Constructibles>
  <Buildings>
    <Row ConstructibleType="BUILDING_MARKET" Movable="false"/>
    <Row ConstructibleType="BUILDING_BASILICA" Movable="false" TraitType="TRAIT_ROME"/>
  </Buildings>
  <Constructible_YieldChanges><Row ConstructibleType="BUILDING_MARKET" YieldType="YIELD_GOLD" YieldChange="3"/></Constructible_YieldChanges>
  <TypeTags>
    <Row Type="BUILDING_MARKET" Tag="GOLD"/>
    <Row Type="BUILDING_BASILICA" Tag="DIPLOMACY"/>
  </TypeTags>
</Database>""",
    "Base/modules/age-antiquity/data/civilizations.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <Civilizations>
    <Row CivilizationType="CIVILIZATION_ROME" Name="LOC_CIVILIZATION_ROME_NAME" Description="LOC_CIVILIZATION_ROME_DESCRIPTION" StartingCivilizationLevelType="CIVILIZATION_LEVEL_FULL_CIV" ApexAge="AGE_ANTIQUITY"/>
  </Civilizations>
  <CivilizationTraits><Row CivilizationType="CIVILIZATION_ROME" TraitType="TRAIT_ROME"/></CivilizationTraits>
</Database>""",
    "Base/modules/age-antiquity/data/progression-trees.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <ProgressionTrees>
    <Row ProgressionTreeType="TREE_TECHS_AQ" AgeType="AGE_ANTIQUITY" SystemType="SYSTEM_TECH" Name="LOC_TREE_TECHS_AQ_NAME"/>
    <Row ProgressionTreeType="TREE_CIVICS_AQ_MAIN" AgeType="AGE_ANTIQUITY" SystemType="SYSTEM_CULTURE" Name="LOC_TREE_CIVICS_AQ_MAIN_NAME"/>
  </ProgressionTrees>
  <ProgressionTreeNodes>
    <Row ProgressionTreeNodeType="NODE_TECH_AQ_POTTERY" ProgressionTree="TREE_TECHS_AQ" Cost="40" Name="LOC_TECH_POTTERY_NAME"/>
    <Row ProgressionTreeNodeType="NODE_TECH_AQ_IRRIGATION" ProgressionTree="TREE_TECHS_AQ" Cost="125" Name="LOC_TECH_IRRIGATION_NAME"/>
    <Row ProgressionTreeNodeType="NODE_CIVIC_AQ_MAIN_CHIEFDOM" ProgressionTree="TREE_CIVICS_AQ_MAIN" Cost="90" Name="LOC_CIVIC_CHIEFDOM_NAME"/>
    <Row ProgressionTreeNodeType="NODE_CIVIC_AQ_MAIN_CODE_OF_LAWS" ProgressionTree="TREE_CIVICS_AQ_MAIN" Cost="245" Name="LOC_CIVIC_CODE_OF_LAWS_NAME"/>
  </ProgressionTreeNodes>
  <ProgressionTreeNodeUnlocks>
    <Row ProgressionTreeNodeType="NODE_TECH_AQ_POTTERY" TargetKind="KIND_CONSTRUCTIBLE" TargetType="BUILDING_MARKET" UnlockDepth="1"/>
    <Row ProgressionTreeNodeType="NODE_TECH_AQ_IRRIGATION" TargetKind="KIND_MODIFIER" TargetType="MOD_AQ_SETTLEMENT_CAP_INCREASE" UnlockDepth="1"/>
    <Row ProgressionTreeNodeType="NODE_CIVIC_AQ_MAIN_CHIEFDOM" TargetKind="KIND_CONSTRUCTIBLE" TargetType="BUILDING_BASILICA" UnlockDepth="1" RequiredTraitType="TRAIT_ROME"/>
    <Row ProgressionTreeNodeType="NODE_CIVIC_AQ_MAIN_CODE_OF_LAWS" TargetKind="KIND_UNIT" TargetType="UNIT_MERCHANT" UnlockDepth="1"/>
  </ProgressionTreeNodeUnlocks>
  <ProgressionTreePrereqs>
    <Row Node="NODE_TECH_AQ_IRRIGATION" PrereqNode="NODE_TECH_AQ_POTTERY"/>
    <Row Node="NODE_CIVIC_AQ_MAIN_CODE_OF_LAWS" PrereqNode="NODE_CIVIC_AQ_MAIN_CHIEFDOM"/>
  </ProgressionTreePrereqs>
</Database>""",
    "Base/modules/age-antiquity/data/progression-trees-tech-gameeffects.xml": """<?xml version="1.0" encoding="utf-8"?>
<GameEffects xmlns="GameEffects">
  <Modifier id="MOD_AQ_SETTLEMENT_CAP_INCREASE" collection="COLLECTION_OWNER" effect="EFFECT_PLAYER_ADJUST_SETTLEMENT_CAP" permanent="true">
    <Argument name="Amount">1</Argument>
    <String context="Description">LOC_MOD_AQ_SETTLEMENT_CAP_INCREASE_DESCRIPTION</String>
  </Modifier>
</GameEffects>""",
    "Base/modules/base-standard/text/en_us/Text.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <EnglishText>
    <Row Tag="LOC_PEDIA_CONCEPTS_TITLE"><Text>General Concepts</Text></Row>
    <Row Tag="LOC_PEDIA_BUILDINGS_TITLE"><Text>Buildings</Text></Row>
    <Row Tag="LOC_YIELD_GOLD"><Text>[icon:YIELD_GOLD] Gold</Text></Row>
    <Row Tag="LOC_YIELD_PRODUCTION"><Text>[icon:YIELD_PRODUCTION] Production</Text></Row>
    <Row Tag="LOC_YIELD_HAPPINESS"><Text>[icon:YIELD_HAPPINESS] Happiness</Text></Row>
    <Row Tag="LOC_TERRAIN_COAST_NAME"><Text>Coast</Text></Row>
    <Row Tag="LOC_DISTRICT_URBAN_NAME"><Text>Urban</Text></Row>
    <Row Tag="LOC_AGE_ANTIQUITY_NAME"><Text>Antiquity</Text></Row>
    <Row Tag="LOC_UNIT_MERCHANT_NAME"><Text>Merchant</Text></Row>
    <Row Tag="LOC_UNIT_MERCHANT_DESCRIPTION"><Text>Civilian Unit who can establish a [TIP:LOC_PEDIA_CONCEPTS_TRADE_ROUTE_TOOLTIP]Trade Route[/TIP].</Text></Row>
    <Row Tag="LOC_BUILDING_MARKET_NAME"><Text>Market</Text></Row>
    <Row Tag="LOC_BUILDING_MARKET_DESCRIPTION"><Text>+3[icon:YIELD_GOLD] Gold. Antiquity Gold Building.</Text></Row>
    <Row Tag="LOC_BUILDING_BASILICA_NAME"><Text>Basilica</Text></Row>
    <Row Tag="LOC_BUILDING_BASILICA_DESCRIPTION"><Text>Roman Unique Diplomacy Building.</Text></Row>
    <Row Tag="LOC_CIVILIZATION_ROME_NAME"><Text>Rome</Text></Row>
    <Row Tag="LOC_CIVILIZATION_ROME_DESCRIPTION"><Text>Romans.</Text></Row>
    <Row Tag="LOC_TECH_POTTERY_NAME"><Text>Pottery</Text></Row>
    <Row Tag="LOC_TECH_IRRIGATION_NAME"><Text>Irrigation</Text></Row>
    <Row Tag="LOC_CIVIC_CHIEFDOM_NAME"><Text>Chiefdom</Text></Row>
    <Row Tag="LOC_CIVIC_CODE_OF_LAWS_NAME"><Text>Code of Laws</Text></Row>
    <Row Tag="LOC_MOD_AQ_SETTLEMENT_CAP_INCREASE_DESCRIPTION"><Text>+1 [icon:SETTLEMENT_LIMIT] [TIP:LOC_PEDIA_CONCEPTS_SETTLEMENT_LIMIT_TOOLTIP]Settlement Limit[/TIP].</Text></Row>
    <Row Tag="LOC_PEDIA_CONCEPTS_SETTLEMENT_LIMIT_TOOLTIP"><Text>The number of Settlements your Civilization can administer.</Text></Row>
    <Row Tag="LOC_PEDIA_CONCEPTS_TRADE_ROUTE_TOOLTIP"><Text>A connection that exchanges goods.</Text></Row>
    <Row Tag="LOC_PEDIA_CONCEPTS_NAVAL_UNITS_TOOLTIP"><Text>Units that move on water.</Text></Row>
    <Row Tag="LOC_PEDIA_CONCEPTS_PAGE_BIOME_4_TITLE"><Text>Ocean</Text></Row>
    <Row Tag="LOC_PEDIA_CONCEPTS_PAGE_BIOME_4_CHAPTER_CONTENT_PARA_1"><Text>Only [TIP:LOC_PEDIA_CONCEPTS_NAVAL_UNITS_TOOLTIP]Naval Units[/TIP] may cross the deep Ocean.[NN]Tiles next to it yield +1[icon:YIELD_GOLD] Gold. Researching the Irrigation Technology does not help here.</Text></Row>
    <Row Tag="LOC_PEDIA_CONCEPTS_PAGE_PILLAGING_1_TITLE"><Text>Pillaging</Text></Row>
    <Row Tag="LOC_PEDIA_CONCEPTS_PAGE_PILLAGING_1_CHAPTER_CONTENT_PARA_1"><Text>Units can pillage a [TIP:LOC_PEDIA_CONCEPTS_TRADE_ROUTE_TOOLTIP]Trade Route[/TIP] or a Market.[N]The Merchant cannot.</Text></Row>
  </EnglishText>
</Database>""",
    "DLC/carthage/modules/data/constructibles.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <Constructibles>
    <Row ConstructibleType="BUILDING_COTHON" Name="LOC_BUILDING_COTHON_NAME" Description="LOC_BUILDING_COTHON_DESCRIPTION" ConstructibleClass="BUILDING" Cost="135" Population="1" Age="AGE_ANTIQUITY"/>
    <Row ConstructibleType="BUILDING_DOCKYARD" Name="LOC_BUILDING_DOCKYARD_NAME" Description="LOC_BUILDING_DOCKYARD_DESCRIPTION" ConstructibleClass="BUILDING" Cost="135" Population="1" Age="AGE_ANTIQUITY"/>
  </Constructibles>
  <Buildings>
    <Row ConstructibleType="BUILDING_COTHON" Movable="false" TraitType="TRAIT_CARTHAGE"/>
    <Row ConstructibleType="BUILDING_DOCKYARD" Movable="false" TraitType="TRAIT_CARTHAGE"/>
  </Buildings>
  <Constructible_Maintenances><Row ConstructibleType="BUILDING_COTHON" YieldType="YIELD_HAPPINESS" Amount="2"/></Constructible_Maintenances>
  <Constructible_ValidTerrains>
    <Row ConstructibleType="BUILDING_COTHON" TerrainType="TERRAIN_COAST"/>
    <Row ConstructibleType="BUILDING_DOCKYARD" TerrainType="TERRAIN_COAST"/>
  </Constructible_ValidTerrains>
  <Constructible_ValidDistricts><Row ConstructibleType="BUILDING_COTHON" DistrictType="DISTRICT_URBAN"/></Constructible_ValidDistricts>
  <TypeTags>
    <Row Type="BUILDING_COTHON" Tag="PRODUCTION"/>
    <Row Type="BUILDING_COTHON" Tag="UNIQUE"/>
    <Row Type="BUILDING_DOCKYARD" Tag="GOLD"/>
    <Row Type="BUILDING_DOCKYARD" Tag="UNIQUE"/>
  </TypeTags>
  <Constructible_YieldChanges>
    <Row ConstructibleType="BUILDING_COTHON" YieldType="YIELD_PRODUCTION" YieldChange="2"/>
    <Row ConstructibleType="BUILDING_DOCKYARD" YieldType="YIELD_GOLD" YieldChange="2"/>
  </Constructible_YieldChanges>
  <UniqueQuarters>
    <Row UniqueQuarterType="QUARTER_PUNIC_PORT" BuildingType1="BUILDING_COTHON" BuildingType2="BUILDING_DOCKYARD" Name="LOC_QUARTER_PUNIC_PORT_NAME" Description="LOC_QUARTER_PUNIC_PORT_DESCRIPTION" TraitType="TRAIT_CARTHAGE"/>
  </UniqueQuarters>
</Database>""",
    "DLC/carthage/modules/data/civilizations.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <Civilizations>
    <Row CivilizationType="CIVILIZATION_CARTHAGE" Name="LOC_CIVILIZATION_CARTHAGE_NAME" Description="LOC_CIVILIZATION_CARTHAGE_DESCRIPTION" StartingCivilizationLevelType="CIVILIZATION_LEVEL_FULL_CIV" ApexAge="AGE_ANTIQUITY"/>
  </Civilizations>
  <CivilizationTraits><Row CivilizationType="CIVILIZATION_CARTHAGE" TraitType="TRAIT_CARTHAGE"/></CivilizationTraits>
</Database>""",
    "DLC/carthage/modules/data/progression-trees-culture-unique.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <ProgressionTrees>
    <Row ProgressionTreeType="TREE_CIVICS_AQ_CARTHAGE" AgeType="AGE_ANTIQUITY" SystemType="SYSTEM_CULTURE" Name="LOC_TREE_CIVICS_AQ_CARTHAGE_NAME"/>
  </ProgressionTrees>
  <ProgressionTreeNodes>
    <Row ProgressionTreeNodeType="NODE_CIVIC_AQ_CARTHAGE_SHIPSHEDS" ProgressionTree="TREE_CIVICS_AQ_CARTHAGE" Cost="150" Name="LOC_CIVIC_SHIPSHEDS_NAME"/>
    <Row ProgressionTreeNodeType="NODE_CIVIC_AQ_CARTHAGE_RED_SLIP" ProgressionTree="TREE_CIVICS_AQ_CARTHAGE" Cost="150" Name="LOC_CIVIC_RED_SLIP_NAME"/>
  </ProgressionTreeNodes>
  <ProgressionTreeNodeUnlocks>
    <Row ProgressionTreeNodeType="NODE_CIVIC_AQ_CARTHAGE_SHIPSHEDS" TargetKind="KIND_CONSTRUCTIBLE" TargetType="BUILDING_COTHON" UnlockDepth="1"/>
    <Row ProgressionTreeNodeType="NODE_CIVIC_AQ_CARTHAGE_RED_SLIP" TargetKind="KIND_CONSTRUCTIBLE" TargetType="BUILDING_DOCKYARD" UnlockDepth="1"/>
  </ProgressionTreeNodeUnlocks>
</Database>""",
    "DLC/carthage/modules/config/config.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <Civilizations><Row Domain="AntiquityAgeCivilizations" CivilizationType="CIVILIZATION_CARTHAGE" CivilizationName="LOC_CIVILIZATION_CARTHAGE_NAME"/></Civilizations>
  <CivilizationItems>
    <Row CivilizationDomain="AntiquityAgeCivilizations" AgeType="AGE_ANTIQUITY" CivilizationType="CIVILIZATION_CARTHAGE" Type="TRAIT_CARTHAGE_ABILITY" Kind="KIND_TRAIT" Name="LOC_TRAIT_CARTHAGE_ABILITY_NAME" Description="LOC_TRAIT_CARTHAGE_ABILITY_DESCRIPTION"/>
    <Row CivilizationDomain="AntiquityAgeCivilizations" CivilizationType="CIVILIZATION_CARTHAGE" Type="BUILDING_COTHON" Kind="KIND_BUILDING" Name="LOC_BUILDING_COTHON_NAME" Description="LOC_BUILDING_COTHON_DESCRIPTION"/>
  </CivilizationItems>
</Database>""",
    "DLC/carthage/modules/text/en_us/Text.xml": """<?xml version="1.0" encoding="utf-8"?>
<Database>
  <EnglishText>
    <Row Tag="LOC_BUILDING_COTHON_NAME"><Text>Cothon</Text></Row>
    <Row Tag="LOC_BUILDING_COTHON_DESCRIPTION"><Text>Carthaginian Unique Production [TIP:LOC_PEDIA_CONCEPTS_BUILDINGS_TOOLTIP]Building[/TIP]. +2[icon:YIELD_PRODUCTION] Production. Must be placed on Coast.</Text></Row>
    <Row Tag="LOC_BUILDING_DOCKYARD_NAME"><Text>Dockyard</Text></Row>
    <Row Tag="LOC_BUILDING_DOCKYARD_DESCRIPTION"><Text>Carthaginian Unique Gold Building. +2 [icon:YIELD_GOLD] Gold. Must be placed on Coast.</Text></Row>
    <Row Tag="LOC_PEDIA_CONCEPTS_BUILDINGS_TOOLTIP"><Text>Constructed in a District.</Text></Row>
    <Row Tag="LOC_QUARTER_PUNIC_PORT_NAME"><Text>Punic Port</Text></Row>
    <Row Tag="LOC_QUARTER_PUNIC_PORT_DESCRIPTION"><Text>Carthaginian Unique Quarter.</Text></Row>
    <Row Tag="LOC_CIVILIZATION_CARTHAGE_NAME"><Text>Carthage</Text></Row>
    <Row Tag="LOC_CIVILIZATION_CARTHAGE_DESCRIPTION"><Text>Can only have 1 [icon:YIELD_CITIES] City.</Text></Row>
    <Row Tag="LOC_TRAIT_CARTHAGE_ABILITY_NAME"><Text>Phoenician Heritage</Text></Row>
    <Row Tag="LOC_TRAIT_CARTHAGE_ABILITY_DESCRIPTION"><Text>[LIST][LI]Can only have 1 City.[/LIST]</Text></Row>
    <Row Tag="LOC_CIVIC_SHIPSHEDS_NAME"><Text>Shipsheds</Text></Row>
    <Row Tag="LOC_CIVIC_RED_SLIP_NAME"><Text>Wisdom of Tanit</Text></Row>
    <Row Tag="LOC_PEDIA_BUILDINGS_PAGE_BUILDING_COTHON_CHAPTER_HISTORY_TITLE"><Text>Historical Context</Text></Row>
    <Row Tag="LOC_PEDIA_BUILDINGS_PAGE_BUILDING_COTHON_CHAPTER_HISTORY_PARA_1"><Text>The cothon was the inner harbor of Carthage.</Text></Row>
  </EnglishText>
  <LocalizedText>
    <Replace Tag="LOC_BUILDING_COTHON_NAME" Language="de_DE"><Text>Kothon</Text></Replace>
  </LocalizedText>
</Database>""",
}


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    root = tmp_path_factory.mktemp("install") / "Civ7"
    for rel, xml in FIXTURE.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(xml)
    db = root.parent / "civilopedia.db"
    res = cp.build(root, db)
    assert res["status"] == "built"
    return root, db


def test_render_icons_tips_and_lists():
    r = cp.Renderer({"LOC_X": "inner"}, {"YIELD_GOLD": "Gold", "SETTLEMENT_LIMIT": "Settlement Limit", "UNIT_MERCHANT": "Merchant"})
    links = []
    out = r.render("+2[icon:YIELD_GOLD] Gold, +1[icon:YIELD_GOLD]. +1 [icon:SETTLEMENT_LIMIT] [TIP:LOC_PEDIA_CONCEPTS_"
                   "SETTLEMENT_LIMIT_TOOLTIP]Settlement Limit[/TIP].[N]Buy a [icon:UNIT_MERCHANT]:[LIST][LI][B]One[/B][LI]{LOC_X}[/LIST]", links)
    assert out == "+2 Gold, +1 Gold. +1 Settlement Limit.\nBuy a Merchant:\n- One\n- inner"
    assert ("LOC_PEDIA_CONCEPTS_SETTLEMENT_LIMIT_TOOLTIP", "Settlement Limit", "tip") in links
    assert ("UNIT_MERCHANT", "Merchant", "icon") in links


def test_cothon_dockyard_facts(fx):
    _, db = fx
    p = cp.page("Cothon", db=db)
    f = p["facts"]
    assert p["key"] == "BUILDING_COTHON" and p["kind"] == "building" and p["dlc"] and p["age"] == ["AGE_ANTIQUITY"]
    assert f["category"] == ["production"]
    assert f["placement"]["terrain"] == ["Coast"]
    assert [u["key"] for u in f["unique_to"]] == ["CIVILIZATION_CARTHAGE"]
    assert f["unlocked_by"] == [{"key": "NODE_CIVIC_AQ_CARTHAGE_SHIPSHEDS", "name": "Shipsheds", "system": "civic"}]
    assert f["base_yields"] == [{"yield": "Production", "amount": 2}] and f["maintenance"] == [{"yield": "Happiness", "cost": 2}]
    assert f["quarter"]["key"] == "QUARTER_PUNIC_PORT"
    assert p["summary"] == "Carthaginian Unique Production Building. +2 Production. Must be placed on Coast."
    assert [c["title"] for c in p["chapters"]] == ["Summary", "Historical Context"]
    assert p["links"]["mentions"] == [{"key": "TERM:BUILDINGS", "title": "Building", "kind": "term"}]  # anchor == title
    assert p["incoming"]["unlocked_by"][0]["title"] == "Shipsheds"
    d = cp.page("BUILDING_DOCKYARD", db=db)
    assert d["facts"]["category"] == ["gold"] and d["facts"]["unlocked_by"][0]["name"] == "Wisdom of Tanit"
    assert cp.page("BUILDINGS:BUILDING_COTHON", db=db)["key"] == "BUILDING_COTHON"  # old pedia key form


def test_unlocks_and_settlement_cap(fx):
    _, db = fx
    m = cp.links("Merchant", "unlocked_by", db=db)
    assert [x["title"] for x in m["links"]] == ["Code of Laws"] and m["direction"] == "in"
    assert cp.links("Merchant", "unlocks", "in", db=db)["links"] == m["links"]
    col = cp.links("Code of Laws", "unlocks", db=db)
    assert [x["key"] for x in col["links"]] == ["UNIT_MERCHANT"]
    irr = cp.page("Irrigation", db=db)["facts"]
    assert irr["unlocks"] == [{"kind": "modifier", "modifier": "MOD_AQ_SETTLEMENT_CAP_INCREASE", "text": "+1 Settlement Limit.",
                               "effect": "EFFECT_PLAYER_ADJUST_SETTLEMENT_CAP", "args": {"Amount": "1"}}]
    assert irr["prereqs"] == [{"key": "NODE_TECH_AQ_POTTERY", "name": "Pottery"}]
    assert cp.links("Irrigation", "mentions", db=db)["links"][0]["key"] == "TERM:SETTLEMENT_LIMIT"
    bas = cp.page("Chiefdom", db=db)["facts"]["unlocks"][0]
    assert bas["key"] == "BUILDING_BASILICA" and bas["kind"] == "building" and bas["only_for"] == "Rome"


def test_concept_pages_have_text_and_links(fx):
    _, db = fx
    o = cp.page("Ocean", db=db)
    assert o["key"] == "CONCEPTS:BIOME_4" and o["kind"] == "concept"
    text = o["chapters"][0]["text"]
    assert "[" not in text and "Naval Units may cross the deep Ocean." in text and "+1 Gold." in text
    assert o["links"]["mentions"][0]["key"] == "NODE_TECH_AQ_IRRIGATION" and o["links"]["mentions"][0]["via"] == "name"
    assert {x["key"] for x in o["links"]["mentions"]} == {"NODE_TECH_AQ_IRRIGATION", "TERM:NAVAL_UNITS"}
    p = cp.page("pillaging", db=db)
    assert p["chapters"][0]["text"] == "Units can pillage a Trade Route or a Market.\nThe Merchant cannot."
    assert {x["key"] for x in p["links"]["mentions"]} >= {"TERM:TRADE_ROUTE", "BUILDING_MARKET", "UNIT_MERCHANT"}
    term = cp.page("TERM:TRADE_ROUTE", db=db)
    assert term["title"] == "Trade Route" and term["summary"] == "A connection that exchanges goods."
    assert {x["key"] for x in cp.links("TERM:TRADE_ROUTE", "mentioned_by", db=db)["links"]} == {"UNIT_MERCHANT", "CONCEPTS:PILLAGING_1"}


def test_search_filters_and_ranking(fx):
    _, db = fx
    r = cp.search("cothon", db=db)["results"]
    assert r[0]["key"] == "BUILDING_COTHON" and "Production" in r[0]["summary"]
    assert {x["key"] for x in cp.search("gold building", kind="building", db=db)["results"][:2]} == {"BUILDING_MARKET", "BUILDING_DOCKYARD"}
    hit = cp.search("code of laws", db=db)["results"][0]
    assert hit["key"] == "NODE_CIVIC_AQ_MAIN_CODE_OF_LAWS" and hit["unlocks"] == ["Merchant"]
    assert cp.search("kothon", db=db)["results"] == []  # German text is not indexed
    assert all(x["kind"] == "civic" for x in cp.search("laws", kind="civics", db=db)["results"])


def test_filter_compare_reachable(fx):
    _, db = fx
    f = cp.filter_pages(kind="building", age="antiquity", civ="carthage", db=db)
    keys = [x["key"] for x in f["results"]]
    assert "BUILDING_COTHON" in keys and "BUILDING_MARKET" in keys and "BUILDING_BASILICA" not in keys
    assert [x["key"] for x in cp.filter_pages(kind="building", category="gold", civ="Carthage", db=db)["results"]] == \
        ["BUILDING_DOCKYARD", "BUILDING_MARKET"]
    c = cp.compare(["Cothon", "Dockyard", "Market"], db=db)
    assert c["facts"]["category"] == [["production"], ["gold"], ["gold"]]
    assert c["facts"]["base_yields"] == [["+2 Production"], ["+2 Gold"], ["+3 Gold"]]
    assert c["same"]["age"] == "AGE_ANTIQUITY"
    r = cp.reachable(["Pottery", "Chiefdom"], steps=2, civ="carthage", db=db)
    got = {(x["step"], x["title"]) for x in r["results"]}
    assert got == {(1, "Irrigation"), (1, "Code of Laws"), (1, "Shipsheds"), (1, "Wisdom of Tanit")}
    assert {x["title"] for x in cp.reachable([], age="antiquity", db=db)["results"]} == {"Pottery", "Chiefdom"}


def test_version_key_rebuilds(fx):
    root, db = fx
    assert cp.build(root, db)["status"] == "up to date"
    f = root / "Base/modules/base-standard/text/en_us/Text.xml"
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert cp.build(root, db)["status"] == "built"
    with sqlite3.connect(db) as con:
        assert dict(con.execute("SELECT key, value FROM meta"))["signature"] == cp.signature(root)


def test_errors(fx):
    _, db = fx
    with pytest.raises(cp.PediaIndexError):
        cp.links("Cothon", "bogus", db=db)
    with pytest.raises(cp.PediaIndexError):
        cp.reachable(["Cothon"], db=db)
    with pytest.raises(cp.PediaIndexError):
        cp.build(Path("/nonexistent"), db.parent / "x.db")


# ---------------------------------------------------------------- real install (smoke)

@pytest.fixture(scope="module")
def real(tmp_path_factory):
    if not (rules.GAME / "Base" / "modules").is_dir():
        pytest.skip("Civ VII not installed")
    db = tmp_path_factory.mktemp("real") / "civilopedia.db"
    cp.build(db=db)
    return db


def test_real_install_cases(real):
    cothon = cp.page("Cothon", db=real)["facts"]
    assert cothon["category"] == ["production"] and "Coastal Marine" in cothon["placement"]["terrain"]
    assert [u["name"] for u in cothon["unlocked_by"]] == ["Shipsheds"] and cothon["unique_to"][0]["key"] == "CIVILIZATION_CARTHAGE"
    assert cp.page("Dockyard", db=real)["facts"]["category"] == ["gold"]
    assert "Code of Laws" in [x["title"] for x in cp.links("Merchant", "unlocked_by", db=real)["links"]]
    irr = cp.page("Irrigation", db=real)["facts"]["unlocks"]
    assert any(u.get("text") == "+1 Settlement Limit." for u in irr)
    for concept in ("Ocean", "Pillaging"):
        p = cp.page(concept, db=real)
        assert p["kind"] == "concept" and p["chapters"] and "[" not in p["chapters"][0]["text"]
        assert p.get("links", {}).get("mentions"), concept
    assert cp.search("cothon", db=real)["results"][0]["key"] == "BUILDING_COTHON"
    with sqlite3.connect(real) as con:
        assert con.execute("SELECT count(*) FROM pages WHERE text LIKE '%[icon%' OR text LIKE '%[TIP%'").fetchone()[0] == 0
