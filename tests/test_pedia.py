"""Civilopedia: markup cleaning offline; search against the installed game when present."""

import pytest

from civilizer import pedia, rules


def test_clean_markup():
    idx = {"LOC_X": "inner [icon:YIELD_GOLD] gold"}
    raw = ("+9[icon:YIELD_PRODUCTION] Production for [TIP:LOC_TT]Resources[/TIP].[NN]Options:[BLIST][LI][B]Farm[/B] town"
           "[LI]Mine town[/BLIST] {LOC_X}")
    assert pedia.clean(raw, idx) == "+9 Production for Resources.\n\nOptions:\n- Farm town\n- Mine town\ninner gold"


@pytest.mark.skipif(not rules.GAME.exists(), reason="Civ VII not installed")
def test_search_finds_concepts_and_gameplay_pages():
    r = pedia.search("war weariness", limit=1, max_chars=300)["results"][0]
    assert r["key"] == "CONCEPTS:WARWEARINESS_1" and r["chapters"] and len(r["chapters"][0]["text"]) <= 310
    assert "[" not in r["chapters"][0]["text"]
    exact = pedia.search("BUILDINGS:BUILDING_STEEL_MILL")["results"][0]
    assert exact["title"] == "Steel Mill" and {c["chapter"] for c in exact["chapters"]} >= {"Summary", "Historical Context"}
    listing = pedia.search("", section="governments")
    assert listing["pages"] and all(p["key"].startswith("GOVERNMENT:") for p in listing["pages"])
    with pytest.raises(pedia.PediaError):
        pedia.search("x", section="nope")
