"""Extracted, searchable, link-preserving Civilopedia: an SQLite index (data/civilopedia.db) built from the install.

Why: answering "what is X / what does X unlock / which should I pick" needs the game's own data, connected. This
module reads the install once, read-only, and stores one page per Civilopedia entry with readable text, structured
facts and explicit edges, so the advisor can search, open, follow links and compare without grepping XML.

Sources (all under the install, Base/modules/* and DLC/*/modules; age-* modules are the age-specific databases):
  - Prose pages: data/civilopedia*.xml (sections, page layouts, chapters, search terms) + text/en_us/*.xml (and
    DLC/*/modules/text/**), assembled by pedia.build_index exactly like the in-game page model
    (Base/modules/base-standard/ui/civilopedia/model-civilopedia.js).
  - Gameplay pages: the same rows the in-game Civilopedia queries (CivilopediaPageQueries in civilopedia.xml) and
    its sidebars (ui/civilopedia/civilopedia-sidebar-panels.js): Constructibles (+ Buildings, Wonders, Improvements,
    Constructible_YieldChanges / _Maintenances / _Adjacencies / _ValidTerrains ..., TypeTags), Units (+ Unit_Stats,
    Unit_Costs, UnitUpgrades, UnitReplaces), ProgressionTreeNodes (+ ProgressionTreeNodeUnlocks / Prereqs),
    Traditions, Civilizations / Leaders (+ config CivilizationItems, LeaderItems, CivilizationUnlocks), resources,
    terrain, biomes, features, governments, beliefs, projects, unique quarters, legacy paths. Modifier descriptions
    come from the GameEffects XML (<Modifier><String context="Description">) and ModifierStrings rows.
  - Links: the only in-text link markup is [TIP:LOC_KEY]anchor[/TIP] (a glossary tooltip; 99% are
    LOC_PEDIA_CONCEPTS_<TERM>_TOOLTIP). Each tooltip becomes a `term` page (TERM:<TERM>) and every use a `mentions`
    edge with its anchor text. [icon:<TYPE>] tokens become words ("Gold", "Settlement Limit") and, when <TYPE> is a
    page, a mention edge too. Data rows add edges: unlocks, prereq, replaces, unique_to, belongs_to_quarter,
    upgrades_to, requires_building, placed_on, improves_resource, found_in, unlocks_civ, obsoletes, see_also.

Limits: the game builds its database per age and per owned DLC (modinfo ActionGroups, <Update>/<Delete> rows);
here every module is merged, so an item's age comes from its own Age column, its module (age-*), its civ or the tree
that unlocks it. <Update>/<Delete> rows are not applied. Numbers inside modifier descriptions are the game's text;
`civilizer rules` shows the raw modifier XML.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

from . import pedia, rules

GAME = rules.GAME
DB = rules.CACHE.parent.parent / "civilopedia.db"
INDEX_VERSION = 1

AGES = {"antiquity": "AGE_ANTIQUITY", "exploration": "AGE_EXPLORATION", "modern": "AGE_MODERN"}
MODULE_AGE = {"age-antiquity": "AGE_ANTIQUITY", "age-exploration": "AGE_EXPLORATION", "age-modern": "AGE_MODERN"}
TREE_AGE = {"AQ": "AGE_ANTIQUITY", "EX": "AGE_EXPLORATION", "MO": "AGE_MODERN"}
YIELD_TAGS = {"FOOD", "PRODUCTION", "GOLD", "SCIENCE", "CULTURE", "HAPPINESS", "DIPLOMACY", "MILITARY", "TRADE",
              "RELIGIOUS", "GREATWORK", "FORTIFICATION"}
# forward relation -> name of the same edge read backwards
INVERSE = {"unlocks": "unlocked_by", "prereq": "prereq_of", "replaces": "replaced_by", "unique_to": "has_unique",
           "belongs_to_quarter": "quarter_buildings", "upgrades_to": "upgrades_from", "requires_building": "required_by",
           "placed_on": "allows", "improves_resource": "improved_by", "found_in": "has", "unlocks_civ": "unlocked_by_civ",
           "obsoletes": "obsoleted_by", "mentions": "mentioned_by", "see_also": "see_also_of", "in_tree": "tree_of"}
REL_BY_NAME = {**{k: (k, "out") for k in INVERSE}, **{v: (k, "in") for k, v in INVERSE.items()}}
KINDS = ["concept", "term", "age", "civilization", "leader", "unit", "building", "wonder", "improvement", "quarter",
         "tech", "civic", "tradition", "policy", "government", "belief", "resource", "terrain", "biome", "feature",
         "natural_wonder", "project", "legacy_path", "route"]
SECTION_KIND = {"CONCEPTS": "concept", "AGES": "concept"}
PREFIX_KIND = [("NODE_TECH", "tech"), ("NODE_CIVIC", "civic"), ("CIVILIZATION_", "civilization"), ("LEADER_", "leader"),
               ("UNIT_", "unit"), ("WONDER_", "wonder"), ("BUILDING_", "building"), ("QUARTER_", "quarter"),
               ("IMPROVEMENT_", "improvement"), ("ROUTE_", "route"), ("GOVERNMENT_", "government"), ("RESOURCE_", "resource"),
               ("BIOME_", "biome"), ("FEATURE_", "feature"), ("TERRAIN_", "terrain"), ("AGE_", "age"), ("TRADITION_", "tradition")]


class PediaIndexError(ValueError):
    pass


# ================================================================ reading the install

def _game_root(root: Path | None) -> Path:
    return Path(root) if root else GAME


def modules(root: Path) -> list[tuple[str, Path, bool]]:
    """(module id, module dir, is_dlc): Base/modules/* then DLC/*/modules, sorted (later DLC text overrides base)."""
    out = [(p.name, p, False) for p in sorted((root / "Base" / "modules").glob("*")) if p.is_dir()]
    out += [(p.name, p / "modules", True) for p in sorted((root / "DLC").glob("*")) if (p / "modules").is_dir()]
    return out


def _data_files(mdir: Path) -> list[Path]:
    return [f for sub in ("data", "config") if (mdir / sub).is_dir() for f in sorted((mdir / sub).rglob("*.xml"))]


def _text_files(mdir: Path) -> list[Path]:
    out = []
    for f in sorted(mdir.rglob("*.xml")):
        rel = [p.lower() for p in f.relative_to(mdir).parts]
        if "en_us" in rel or (rel[0] == "text" and len(rel) == 2) or "en_us" in f.name.lower():
            out.append(f)
    return out


def _steam_build(root: Path) -> str | None:
    acf = root.parent.parent / "appmanifest_1295660.acf"
    try:
        m = re.search(r'"buildid"\s+"(\d+)"', acf.read_text())
        return m.group(1) if m else None
    except OSError:
        return None


def signature(root: Path | None = None) -> str:
    """Version key: index version, Steam build id, extractor source, and path/mtime/size of every file read."""
    root = _game_root(root)
    h = hashlib.sha1(f"{INDEX_VERSION}|{_steam_build(root)}".encode())
    for code in (Path(__file__), Path(pedia.__file__)):  # extractor changes rebuild too
        h.update(code.read_bytes())
    for _, mdir, _ in modules(root):
        for f in _data_files(mdir) + _text_files(mdir):
            try:
                st = f.stat()
            except OSError:
                continue
            h.update(f"{f.relative_to(root)}|{st.st_mtime_ns}|{st.st_size}".encode())
    return h.hexdigest()


def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


class Source:
    """Every data/config row of every module, merged; GameEffects modifiers; English text."""

    def __init__(self, root: Path):
        self.root = root
        self.tables: dict[str, list[dict]] = defaultdict(list)
        self.modifiers: dict[str, dict] = {}
        self.text: dict[str, str] = {}
        self.structure_files: list[Path] = []
        self.data_files: list[Path] = []
        for mid, mdir, dlc in modules(root):
            for f in _data_files(mdir):
                self._load_data(f, mid, dlc)
            for f in _text_files(mdir):
                self._load_text(f)
        self._index_cache: dict = {}

    def _load_data(self, f: Path, mid: str, dlc: bool) -> None:
        try:
            root = ET.parse(f).getroot()
        except (ET.ParseError, OSError):
            return
        if f.parent.name == "data" or "data" in f.parts:
            self.data_files.append(f)
        if f.name.startswith("civilopedia"):
            self.structure_files.append(f)
        kind = _tag(root)
        if kind == "GameEffects":
            for m in root.iter():
                if _tag(m) != "Modifier" or not m.get("id"):
                    continue
                d = self.modifiers.setdefault(m.get("id"), {"effect": m.get("effect"), "collection": m.get("collection"),
                                                            "args": {}, "strings": {}, "module": mid})
                for c in m:
                    if _tag(c) == "Argument" and c.get("name"):
                        d["args"][c.get("name")] = (c.text or "").strip()
                    elif _tag(c) == "String":
                        d["strings"].setdefault(c.get("context") or "Description", (c.text or "").strip())
            return
        if kind != "Database":
            return
        for table in root:
            tname = _tag(table)
            for r in table:
                if _tag(r) not in ("Row", "Replace", "InsertOrIgnore"):
                    continue
                d = dict(r.attrib)
                for c in r:
                    d[_tag(c)] = (c.text or "").strip()
                d["_m"], d["_dlc"] = mid, dlc
                self.tables[tname].append(d)

    def _load_text(self, f: Path) -> None:
        try:
            root = ET.parse(f).getroot()
        except (ET.ParseError, OSError):
            return
        for el in root.iter():
            tag = el.get("Tag")
            if not tag:
                continue
            lang = el.get("Language")
            if lang and not lang.lower().startswith("en"):
                continue
            t = next((c for c in el if _tag(c) == "Text"), None)
            text = t.text if t is not None else el.text
            if text and text.strip():
                self.text[tag] = text.strip()

    # ---- row access
    def rows(self, table: str) -> list[dict]:
        return self.tables.get(table, [])

    def by(self, table: str, col: str) -> dict[str, dict]:
        """Primary-key view: rows with the same key merge (first value of a column wins: base before DLC)."""
        key = ("by", table, col)
        if key not in self._index_cache:
            out: dict[str, dict] = {}
            for r in self.rows(table):
                k = r.get(col)
                if k is None:
                    continue
                cur = out.setdefault(k, {})
                for c, v in r.items():
                    cur.setdefault(c, v)
            self._index_cache[key] = out
        return self._index_cache[key]

    def multi(self, table: str, col: str) -> dict[str, list[dict]]:
        key = ("multi", table, col)
        if key not in self._index_cache:
            out: dict[str, list[dict]] = defaultdict(list)
            seen = set()
            for r in self.rows(table):
                k = r.get(col)
                if k is None:
                    continue
                sig = (k, tuple(sorted((c, v) for c, v in r.items() if not c.startswith("_"))))
                if sig in seen:
                    continue
                seen.add(sig)
                out[k].append(r)
            self._index_cache[key] = out
        return self._index_cache[key]


# ================================================================ markup -> readable text + links

ICON = re.compile(r"\[icon:([^\]]*)\]", re.I)
TIP = re.compile(r"\[TIP:([^\]]+)\](.*?)\[/TIP\]", re.I | re.S)


class Renderer:
    def __init__(self, text: dict[str, str], icon_names: dict[str, str]):
        self.text = text
        self.icons = icon_names

    def loc(self, key: str | None) -> str:
        return self.text.get(key, "") if key else ""

    def expand(self, s: str, depth: int = 0) -> str:
        if depth >= 3 or "{LOC_" not in s:
            return s
        return re.sub(r"\{(LOC_\w+)\}", lambda m: self.expand(self.text.get(m.group(1), ""), depth + 1), s)

    def render(self, s: str | None, links: list | None = None) -> str:
        """Game markup -> readable text. links (if given) collects (target, anchor, via) for TIP and icon links."""
        if not s:
            return ""
        s = self.expand(s)

        def icon(m):
            ident = m.group(1).strip()
            word = self.icons.get(ident) or self.icons.get(ident.upper(), "")
            if links is not None and ident.upper() in self.icons and not ident.upper().startswith("YIELD_"):
                links.append((ident.upper(), word, "icon"))
            rest = re.sub(r"^(?:\s|\[TIP:[^\]]*\]|\[/?[A-Za-z]+\])*", "", s[m.end():m.end() + 120]).lower()
            words = word.lower().split()
            if not word or any(rest.startswith(w[:3]) for w in (words[0], words[-1])):  # Gold/Gold, Cities/City
                return " "  # "+2[icon:YIELD_GOLD] Gold" already names the yield
            return f" {word} "

        s = ICON.sub(icon, s)

        def tip(m):
            anchor = re.sub(r"\[[^\]]*\]", "", m.group(2)).strip()
            if links is not None:
                links.append((m.group(1).strip(), anchor, "tip"))
            return m.group(2)

        s = TIP.sub(tip, s)
        s = re.sub(r"\[/?TIP(:[^\]]*)?\]", "", s, flags=re.I)
        s = re.sub(r"\[LI\]", "\n- ", s, flags=re.I)
        s = re.sub(r"\[NN\]", "\n\n", s, flags=re.I)
        s = re.sub(r"\[N\]", "\n", s, flags=re.I)
        s = re.sub(r"\[[BN]?LIST\]", "", s, flags=re.I)
        s = re.sub(r"\[/[BN]?LIST\]", "\n", s, flags=re.I)
        s = re.sub(r"\[/?[A-Za-z_]+(:[^\]]*)?\]", "", s)
        s = rules.TAG.sub("", s)
        s = re.sub(r"[ \t]+", " ", s)
        s = re.sub(r" +([.,;:!?)])", r"\1", s)
        s = re.sub(r"\( +", "(", s)
        s = re.sub(r" *\n *", "\n", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()


def one_line(s: str, n: int = 220) -> str:
    s = re.sub(r"\s*\n+\s*-?\s*", " ", s or "").strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + " ..."


def _pretty(ident: str) -> str:
    return ident.replace("_", " ").title()


def _truthy(v) -> bool:
    return str(v).lower() in ("true", "1")


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


# ================================================================ building the index

class Builder:
    def __init__(self, src: Source):
        self.s = src
        self.pages: dict[str, dict] = {}
        self.edges: list[tuple[str, str, str, str | None, str | None]] = []
        self.links: dict[str, list] = defaultdict(list)
        self.names: dict[str, str] = {}
        T = src
        self.yields = {k: re.sub(r"\[[^\]]*\]", "", T.text.get(r.get("Name"), "")).strip() or _pretty(k[6:])
                       for k, r in T.by("Yields", "YieldType").items()}
        self.ages_by_domain = {r["PlayerCivilizationDomain"]: k for k, r in T.by("Ages", "AgeType").items()
                               if r.get("PlayerCivilizationDomain")}
        # every typed row with a Name: used for names of things that have no page (districts, tags, ...)
        for table, col in [("Constructibles", "ConstructibleType"), ("Units", "UnitType"), ("Terrains", "TerrainType"),
                           ("Biomes", "BiomeType"), ("Features", "FeatureType"), ("Resources", "ResourceType"),
                           ("Districts", "DistrictType"), ("Civilizations", "CivilizationType"), ("Leaders", "LeaderType"),
                           ("ProgressionTreeNodes", "ProgressionTreeNodeType"), ("ProgressionTrees", "ProgressionTreeType"),
                           ("Traditions", "TraditionType"), ("Governments", "GovernmentType"), ("Beliefs", "BeliefType"),
                           ("UniqueQuarters", "UniqueQuarterType"), ("Projects", "ProjectType"), ("Ages", "AgeType"),
                           ("ResourceClasses", "ResourceClassType"), ("LegacyPaths", "LegacyPathType"),
                           ("Routes", "RouteType"), ("Traits", "TraitType"), ("DiplomacyActions", "DiplomacyActionType")]:
            for k, r in T.by(table, col).items():
                name = r.get("Name") or r.get("CivilizationName") or r.get("LeaderName")
                if name and name in T.text and name != "LOC_EMPTY":
                    self.names.setdefault(k, re.sub(r"\[icon:[^\]]*\]", "", T.text[name]).strip())
        icon_names = dict(self.yields)
        for k, v in self.names.items():
            icon_names.setdefault(k, v)
        for k in ("SETTLEMENT_LIMIT", "SPECIALIST", "TRADE_ROUTE", "CELEBRATION", "WAR_SUPPORT", "GROWTH_RATE",
                  "RELATIONSHIP", "ENDEAVOR", "SANCTIONS", "ESPIONAGE", "TRADITION", "GOVERNMENT", "WONDER",
                  "INDEPENDENT_POWER", "CITYSTATE"):
            if k not in icon_names:
                icon_names[k] = {"CITYSTATE": "City-State", "WAR_SUPPORT": "War Support", "WONDER": "Wonder"}.get(k, _pretty(k))
        icon_names.update({"YIELD_CITIES": "Cities", "YIELD_TOWNS": "Towns", "YIELD_POPULATION": "Population",
                           "YIELD_WAREHOUSE": "Warehouse", "CULTURE_VP": "Cultural Legacy Points",
                           "ECONOMIC_VP": "Economic Legacy Points", "SCIENCE_VP": "Scientific Legacy Points",
                           "MILITARY_VP": "Military Legacy Points", "CITY_URBAN": "Urban", "CITY_RURAL": "Rural",
                           "CITY_FORTIFIED": "Fortified District", "CITY_UNIQUE_QUARTER": "Unique Quarter",
                           "UNIT_ARMY_COMMANDER": "Army Commander", "YIELD_INFLUENCE": "Influence"})
        self.r = Renderer(T.text, icon_names)
        # traits -> civs / leaders, civ ages
        trait_civs: dict[str, set] = defaultdict(set)
        for r in T.rows("CivilizationTraits"):
            trait_civs[r["TraitType"]].add(r["CivilizationType"])
        trait_leaders: dict[str, set] = defaultdict(set)
        for r in T.rows("LeaderTraits"):
            trait_leaders[r["TraitType"]].add(r["LeaderType"])
        self.civ_age = {}
        for civ, r in T.by("Civilizations", "CivilizationType").items():
            age = self.ages_by_domain.get(r.get("Domain") or "") or T.by("LegacyCivilizations", "CivilizationType").get(civ, {}).get("Age")
            if not age:
                age = next((i.get("AgeType") for i in T.multi("CivilizationItems", "CivilizationType").get(civ, []) if i.get("AgeType")), None)
            if age:
                self.civ_age[civ] = age
        self.trait_owner: dict[str, str] = {}
        self.trait_age: dict[str, str] = {}
        for t, civs in trait_civs.items():
            if len(civs) == 1:
                self.trait_owner[t] = next(iter(civs))
            else:
                ages = {self.civ_age.get(c) for c in civs}
                if len(ages) == 1 and None not in ages:
                    self.trait_age[t] = ages.pop()
        for t, leaders in trait_leaders.items():
            if len(leaders) == 1 and t not in self.trait_owner:
                self.trait_owner[t] = next(iter(leaders))
        self.unlocked_by: dict[str, list[dict]] = defaultdict(list)
        for r in T.multi("ProgressionTreeNodeUnlocks", "TargetType").values():
            for u in r:
                self.unlocked_by[u["TargetType"]].append(u)

    # ---- helpers
    def name(self, ident: str | None) -> str:
        if not ident:
            return ""
        if ident in self.pages:
            return self.pages[ident]["title"]
        if ident in self.names:
            return self.r.render(self.names[ident])
        for k in (f"LOC_{ident}_NAME", f"LOC_{ident}"):
            if k in self.s.text:
                return self.r.render(self.s.text[k])
        return _pretty(re.sub(r"^(TERRAIN|BIOME|FEATURE|DISTRICT|RESOURCE|YIELD|UNIT|BUILDING|IMPROVEMENT)_", "", ident))

    def ref(self, ident: str) -> dict:
        return {"key": ident, "name": self.name(ident)}

    def txt(self, key: str | None, page: str | None = None) -> str:
        if not key or key not in self.s.text:
            return ""
        return self.r.render(self.s.text[key], self.links[page] if page else None)

    def edge(self, src: str, dst: str, rel: str, anchor: str | None = None, detail: dict | None = None) -> None:
        self.edges.append((src, dst, rel, anchor, json.dumps(detail, separators=(",", ":")) if detail else None))

    def add(self, key: str, kind: str, title: str, *, type_: str | None = None, module: str | None = None,
            dlc: bool = False, ages=(), summary: str = "", facts: dict | None = None, section: str | None = None,
            group: str | None = None, listed: bool = True, aliases=()) -> dict:
        p = self.pages.get(key)
        if p is None:
            p = self.pages[key] = {"key": key, "kind": kind, "title": title, "type": type_, "module": module, "dlc": dlc,
                                   "ages": set(), "summary": summary, "facts": {}, "chapters": [], "section": section,
                                   "group": group, "listed": listed, "aliases": set()}
        p["ages"] |= {a for a in ages if a}
        p["facts"].update({k: v for k, v in (facts or {}).items() if v not in (None, "", [], {})})
        p["aliases"] |= {a for a in aliases if a}
        return p

    def modifier_text(self, mid: str, page: str) -> dict:
        m = self.s.modifiers.get(mid) or {}
        desc = m.get("strings", {}).get("Description")
        if not desc:
            desc = next((r.get("Text") for r in self.s.multi("ModifierStrings", "ModifierId").get(mid, [])
                         if r.get("Context") == "Description"), None)
        out = {"modifier": mid}
        if desc:
            out["text"] = self.txt(desc, page)
        if m.get("effect"):
            out["effect"] = m["effect"]
        if m.get("args"):
            out["args"] = m["args"]
        return out

    def owner_of(self, trait: str | None) -> str | None:
        return self.trait_owner.get(trait or "")

    def unlock_facts(self, target: str) -> list[dict]:
        out = []
        for u in self.unlocked_by.get(target, []):
            node = u["ProgressionTreeNodeType"]
            d = self.ref(node) | {"system": "tech" if node.startswith("NODE_TECH") else "civic"}
            if _num(u.get("UnlockDepth") or 1) != 1:
                d["mastery"] = True
            if u.get("RequiredTraitType"):
                d["only_for"] = self.name(self.owner_of(u["RequiredTraitType"]) or u["RequiredTraitType"])
            out.append(d)
        return out

    # ---- gameplay pages
    def build_gameplay(self) -> None:
        T = self.s
        quarter_of = {}
        for q, r in T.by("UniqueQuarters", "UniqueQuarterType").items():
            for b in (r.get("BuildingType1"), r.get("BuildingType2")):
                if b:
                    quarter_of[b] = q
        civ_items: dict[str, list[str]] = defaultdict(list)
        for r in T.rows("CivilizationItems"):
            if r.get("Kind") != "KIND_TRAIT" and r["CivilizationType"] not in civ_items[r["Type"]]:
                civ_items[r["Type"]].append(r["CivilizationType"])

        def uniques(ident: str, trait: str | None) -> list[str]:
            out = list(civ_items.get(ident, []))
            owner = self.owner_of(trait)
            if owner and owner not in out:
                out.append(owner)
            return out

        # constructibles
        cons = T.by("Constructibles", "ConstructibleType")
        tags = T.multi("TypeTags", "Type")
        for k, r in cons.items():
            cls = r.get("ConstructibleClass", "")
            kind = {"BUILDING": "building", "WONDER": "wonder", "IMPROVEMENT": "improvement"}.get(cls, cls.lower() or "building")
            b = T.by("Buildings", "ConstructibleType").get(k, {})
            w = T.by("Wonders", "ConstructibleType").get(k, {})
            imp = T.by("Improvements", "ConstructibleType").get(k, {})
            if imp.get("DiscoveryType"):
                continue  # discoveries (goody huts): the in-game Civilopedia skips them too
            trait = b.get("TraitType") or imp.get("TraitType")
            civs = uniques(k, trait)
            ttags = [t["Tag"] for t in tags.get(k, [])]
            yields = [{"yield": self.yields.get(y["YieldType"], y["YieldType"]), "amount": _num(y["YieldChange"])}
                      for y in T.multi("Constructible_YieldChanges", "ConstructibleType").get(k, [])]
            maint = [{"yield": self.yields.get(y["YieldType"], y["YieldType"]), "cost": _num(y["Amount"])}
                     for y in T.multi("Constructible_Maintenances", "ConstructibleType").get(k, []) if _num(y["Amount"])]
            adj = []
            adj_rows = T.by("Adjacency_YieldChanges", "ID")
            for a in T.multi("Constructible_Adjacencies", "ConstructibleType").get(k, []):
                y = adj_rows.get(a["YieldChangeId"])
                if not y:
                    continue
                src = [self._adjacent(c, v) for c, v in y.items() if c.startswith("Adjacent") and v and v.lower() != "false"]
                d = {"yield": self.yields.get(y.get("YieldType"), y.get("YieldType")), "amount": _num(y.get("YieldChange")),
                     "per_adjacent": ", ".join(s for s in src if s) or None, "tiles_required": _num(y.get("TilesRequired"))}
                if _truthy(a.get("RequiresActivation")):
                    d["requires_activation"] = True
                if y.get("Age"):
                    d["age"] = y["Age"]
                adj.append({x: v for x, v in d.items() if v not in (None, "", 1) or x in ("amount",)})
            placement = {
                "terrain": [self.name(x["TerrainType"]) for x in T.multi("Constructible_ValidTerrains", "ConstructibleType").get(k, [])],
                "biome": [self.name(x["BiomeType"]) for x in T.multi("Constructible_ValidBiomes", "ConstructibleType").get(k, [])],
                "feature": [self.name(x["FeatureType"]) for x in T.multi("Constructible_ValidFeatures", "ConstructibleType").get(k, [])],
                "district": [self.name(x["DistrictType"]) for x in T.multi("Constructible_ValidDistricts", "ConstructibleType").get(k, [])],
                "resource": [self.name(x["ResourceType"]) for x in T.multi("Constructible_ValidResources", "ConstructibleType").get(k, [])],
            }
            for col in ("RiverPlacement", "AdjacentRiver", "AdjacentTerrain", "AdjacentDistrict", "AdjacentLake", "NoRiver",
                        "NoFeature", "RequiresHomeland", "RequiresDistantLands", "IslandSettlement", "ExistingDistrictOnly",
                        "InRailNetwork"):
                if r.get(col) and r[col].lower() != "false":
                    placement[_snake(col)] = True if _truthy(r[col]) else self.name(r[col])
            for col in ("AdjacentToLand", "AdjacentConstructible", "BuildOnFrontier", "RequiredConstructibleInSettlement",
                        "MustNotBeLake"):
                if w.get(col) and w[col].lower() != "false":
                    placement[_snake(col)] = True if _truthy(w[col]) else self.name(w[col])
            for col in ("CityBuildable", "TownBuildable", "OnePerSettlement", "BuildInLine", "SameAdjacentValid",
                        "MustBeAppealing", "MinimumPopulation"):
                if imp.get(col) and imp[col].lower() != "false":
                    placement[_snake(col)] = True if _truthy(imp[col]) else _num(imp[col])
            facts = {
                "class": cls.lower(), "category": [t.lower() for t in ttags if t in YIELD_TAGS],
                "tags": [t.lower() for t in ttags], "age": r.get("Age"),
                "cost": _num(r.get("Cost")) or None, "population": _num(r.get("Population")) or None,
                "base_yields": yields, "maintenance": maint, "adjacencies": adj,
                "placement": {x: v for x, v in placement.items() if v not in ([], None)},
                "unique_to": [self.ref(c) for c in civs], "quarter": self.ref(quarter_of[k]) if k in quarter_of else None,
                "unlocked_by": self.unlock_facts(k),
                "effects": [self.modifier_text(m["ModifierId"], k) for m in T.multi("ConstructibleModifiers", "ConstructibleType").get(k, [])],
                "max_world_instances": _num(w.get("MaxWorldInstances")) if w else None,
            }
            for col in ("Town", "Movable", "MultiplePerCity", "Purchasable", "Capital", "CapitalForbidden"):
                if b.get(col):
                    facts[_snake(col)] = _truthy(b[col])
            if r.get("Tooltip") and r.get("Tooltip") != r.get("Description"):
                facts["tooltip"] = self.txt(r["Tooltip"], k)
            ages = [r.get("Age") or MODULE_AGE.get(r["_m"])]
            self.add(k, kind, self.name(k) or _pretty(k), type_=k, module=r["_m"], dlc=r["_dlc"], ages=ages,
                     summary=self.txt(r.get("Description"), k), facts=facts)
            for c in civs:
                self.edge(k, c, "unique_to")
            if k in quarter_of:
                self.edge(k, quarter_of[k], "belongs_to_quarter")
            for x in T.multi("Constructible_ValidTerrains", "ConstructibleType").get(k, []):
                self.edge(k, x["TerrainType"], "placed_on")
            for x in T.multi("Constructible_ValidBiomes", "ConstructibleType").get(k, []):
                self.edge(k, x["BiomeType"], "placed_on")
            for x in T.multi("Constructible_ValidResources", "ConstructibleType").get(k, []):
                self.edge(k, x["ResourceType"], "improves_resource")

        # unique quarters
        for k, r in T.by("UniqueQuarters", "UniqueQuarterType").items():
            civs = uniques(k, r.get("TraitType"))
            bs = [x for x in (r.get("BuildingType1"), r.get("BuildingType2")) if x]
            self.add(k, "quarter", self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"],
                     ages=[cons.get(bs[0], {}).get("Age") if bs else None, MODULE_AGE.get(r["_m"])],
                     summary=self.txt(r.get("Description"), k),
                     facts={"buildings": [self.ref(x) for x in bs], "unique_to": [self.ref(c) for c in civs],
                            "tooltip": self.txt(r.get("Tooltip"), k) if r.get("Tooltip") != r.get("Description") else None,
                            "effects": [self.modifier_text(m["ModifierId"], k) for m in T.multi("UniqueQuarterModifiers", "UniqueQuarterType").get(k, [])]})
            for c in civs:
                self.edge(k, c, "unique_to")

        # units
        stats = T.by("Unit_Stats", "UnitType")
        upgrades = T.multi("UnitUpgrades", "Unit")
        replaces = T.multi("UnitReplaces", "CivUniqueUnitType")
        gp = T.by("GreatPersonIndividuals", "UnitType")
        for k, r in T.by("Units", "UnitType").items():
            civs = uniques(k, r.get("TraitType"))
            st = stats.get(k, {})
            facts = {
                "domain": (r.get("Domain") or "").replace("DOMAIN_", "").lower() or None,
                "formation": (r.get("FormationClass") or "").replace("FORMATION_CLASS_", "").lower() or None,
                "core_class": (r.get("CoreClass") or "").replace("CORE_CLASS_", "").lower() or None,
                "tier": _num(r.get("Tier")), "moves": _num(r.get("BaseMoves")), "sight": _num(r.get("BaseSightRange")),
                "combat": _num(st.get("Combat")), "ranged_combat": _num(st.get("RangedCombat")),
                "range": _num(st.get("Range")), "bombard": _num(st.get("Bombard")),
                "cost": [{"yield": self.yields.get(c["YieldType"], c["YieldType"]), "cost": _num(c["Cost"])}
                         for c in T.multi("Unit_Costs", "UnitType").get(k, [])],
                "maintenance": _num(r.get("Maintenance")),
                "tags": [t["Tag"].replace("UNIT_CLASS_", "").lower() for t in tags.get(k, [])],
                "upgrades_to": [self.ref(u["UpgradeUnit"]) for u in upgrades.get(k, [])],
                "replaces": [self.ref(u["ReplacesUnitType"]) for u in replaces.get(k, [])],
                "requires_building": [self.ref(u["ConstructibleType"]) for u in T.multi("Unit_RequiredConstructibles", "UnitType").get(k, [])],
                "unique_to": [self.ref(c) for c in civs], "unlocked_by": self.unlock_facts(k),
            }
            for col in ("FoundCity", "MakeTradeRoute", "CanPurchase", "MustPurchase", "CanTrain", "CanEarnExperience",
                        "ZoneOfControl", "CanCapture", "WMDCapable"):
                if r.get(col):
                    facts[_snake(col)] = _truthy(r[col])
            if k in gp:
                facts["great_person_effect"] = self.txt(gp[k].get("ActionEffectTextOverride"), k)
            age = MODULE_AGE.get(r["_m"]) or self.trait_age.get(r.get("TraitType") or "") or \
                (self.civ_age.get(civs[0]) if civs else None)
            self.add(k, "unit", self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"], ages=[age],
                     summary=self.txt(r.get("Description"), k), facts=facts)
            for c in civs:
                self.edge(k, c, "unique_to")
            for u in upgrades.get(k, []):
                self.edge(k, u["UpgradeUnit"], "upgrades_to")
            for u in replaces.get(k, []):
                self.edge(k, u["ReplacesUnitType"], "replaces")
            for u in T.multi("Unit_RequiredConstructibles", "UnitType").get(k, []):
                self.edge(k, u["ConstructibleType"], "requires_building")

        # techs and civics
        trees = T.by("ProgressionTrees", "ProgressionTreeType")
        civ_by_stem = {c.removeprefix("CIVILIZATION_"): c for c in T.by("Civilizations", "CivilizationType")}
        prereqs = T.multi("ProgressionTreePrereqs", "Node")
        leads = T.multi("ProgressionTreePrereqs", "PrereqNode")
        unlocks = T.multi("ProgressionTreeNodeUnlocks", "ProgressionTreeNodeType")
        for k, r in T.by("ProgressionTreeNodes", "ProgressionTreeNodeType").items():
            tree = trees.get(r.get("ProgressionTree"), {})
            system = "tech" if tree.get("SystemType") == "SYSTEM_TECH" or k.startswith("NODE_TECH") else "civic"
            m = re.match(r"TREE_(?:CIVICS|TECHS)_(AQ|EX|MO)_(\w+)$", r.get("ProgressionTree", ""))
            civ = civ_by_stem.get(m.group(2)) if m else None
            age = tree.get("AgeType") or (TREE_AGE.get(m.group(1)) if m else None) or MODULE_AGE.get(r["_m"])
            base, mastery = [], []
            for u in unlocks.get(k, []):
                tk, tt = u["TargetKind"], u["TargetType"]
                if tk == "KIND_MODIFIER":
                    item = {"kind": "modifier"} | self.modifier_text(tt, k)
                else:
                    item = {"kind": tk.replace("KIND_", "").lower()} | self.ref(tt)
                if u.get("RequiredTraitType"):
                    item["only_for"] = self.name(self.owner_of(u["RequiredTraitType"]) or u["RequiredTraitType"])
                if u.get("NotTraitType"):
                    item["not_for"] = self.name(self.owner_of(u["NotTraitType"]) or u["NotTraitType"])
                if u.get("RequiredGovernmentType"):
                    item["only_with_government"] = self.name(u["RequiredGovernmentType"])
                if _truthy(u.get("Hidden")):
                    item["hidden"] = True
                depth = _num(u.get("UnlockDepth") or 1)
                (base if depth == 1 else mastery).append(item)
                if tk != "KIND_MODIFIER":
                    detail = {x: item[x] for x in ("only_for", "not_for", "only_with_government") if item.get(x)}
                    self.edge(k, tt, "unlocks", None, detail | ({"mastery": True} if depth != 1 else {}) or None)
            facts = {"system": system, "tree": self.name(r.get("ProgressionTree")) if r.get("ProgressionTree") else None,
                     "tree_id": r.get("ProgressionTree"), "age": age, "cost": _num(r.get("Cost")),
                     "prereqs": [self.ref(p["PrereqNode"]) for p in prereqs.get(k, [])],
                     "prereq_format": tree.get("PrereqFormat"),
                     "leads_to": [self.ref(p["Node"]) for p in leads.get(k, [])],
                     "unlocks": base, "mastery_unlocks": mastery,
                     "unique_to": [self.ref(civ)] if civ else [], "repeatable": _truthy(r.get("Repeatable")) or None}
            title = self.name(k)
            self.add(k, system, title, type_=k, module=r["_m"], dlc=r["_dlc"], ages=[age], facts=facts,
                     summary=self.txt(r.get("Description"), k))
            for p in prereqs.get(k, []):
                self.edge(k, p["PrereqNode"], "prereq")
            if civ:
                self.edge(k, civ, "unique_to")

        # traditions and policies
        for k, r in T.by("Traditions", "TraditionType").items():
            owner = self.owner_of(r.get("TraitType"))
            civs = [owner] if owner else []
            kind = "tradition" if owner else "policy"
            facts = {"slot": (r.get("CultureSlotType") or "").replace("_CULTURE_SLOT", "").lower() or None,
                     "age": r.get("AgeType"), "unique_to": [self.ref(c) for c in civs],
                     "unlocked_by": self.unlock_facts(k), "crisis": _truthy(r.get("IsCrisis")) or None,
                     "obsoletes": [self.ref(r["ObsoletesTraditionType"])] if r.get("ObsoletesTraditionType") else [],
                     "effects": [self.modifier_text(m["ModifierId"], k) for m in T.multi("TraditionModifiers", "TraditionType").get(k, [])]}
            if not owner and r.get("TraitType"):
                facts["requires_trait"] = r["TraitType"]
            age = r.get("AgeType") or MODULE_AGE.get(r["_m"]) or self.trait_age.get(r.get("TraitType") or "")
            self.add(k, kind, self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"], ages=[age],
                     summary=self.txt(r.get("Description"), k), facts=facts)
            for c in civs:
                self.edge(k, c, "unique_to")
            if r.get("ObsoletesTraditionType"):
                self.edge(k, r["ObsoletesTraditionType"], "obsoletes")

        # civilizations
        citems = T.multi("CivilizationItems", "CivilizationType")
        for k, r in T.by("Civilizations", "CivilizationType").items():
            if r.get("StartingCivilizationLevelType", "CIVILIZATION_LEVEL_FULL_CIV") != "CIVILIZATION_LEVEL_FULL_CIV":
                continue
            if not (r.get("Name") or r.get("CivilizationName")):
                continue
            abilities, seen = [], set()
            for i in citems.get(k, []):
                if i.get("Kind") == "KIND_TRAIT":
                    d = {"name": self.txt(i.get("Name"), k), "description": self.txt(i.get("Description"), k),
                         "age": i.get("AgeType")}
                    if (d["name"], d["description"]) not in seen:
                        seen.add((d["name"], d["description"]))
                        abilities.append({x: v for x, v in d.items() if v})
            items = [self.ref(i["Type"]) | {"kind": i["Kind"].replace("KIND_", "").lower()} for i in citems.get(k, [])
                     if i.get("Kind") != "KIND_TRAIT"]
            nexts = [self.ref(u["Type"]) for u in T.multi("CivilizationUnlocks", "CivilizationType").get(k, [])
                     if u.get("Kind") == "KIND_CIVILIZATION"]
            facts = {"age": self.civ_age.get(k), "abilities": abilities, "unique_items": items,
                     "unlocks_civs": _dedupe(nexts),
                     "leader_affinity": _dedupe([self.ref(x["Leader"]) for x in T.rows("LeaderCivPriorities") if x["Civilization"] == k])}
            self.add(k, "civilization", self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"], ages=[self.civ_age.get(k)],
                     summary=self.txt(r.get("Description") or r.get("CivilizationDescription"), k), facts=facts)
            for u in nexts:
                self.edge(k, u["key"], "unlocks_civ")

        # leaders
        litems = T.multi("LeaderItems", "LeaderType")
        for k, r in T.by("Leaders", "LeaderType").items():
            if not _truthy(r.get("IsMajorLeader")) and not litems.get(k):
                continue
            abilities = [{"name": self.txt(i.get("Name"), k), "description": self.txt(i.get("Description"), k)}
                         for i in litems.get(k, []) if i.get("Kind") == "KIND_TRAIT"]
            nexts = _dedupe([self.ref(u["Type"]) for u in T.multi("LeaderUnlocks", "LeaderType").get(k, [])
                             if u.get("Kind") == "KIND_CIVILIZATION"])
            self.add(k, "leader", self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"],
                     summary=self.txt(r.get("LeaderDescription") or f"LOC_{k}_DESCRIPTION", k),
                     facts={"abilities": abilities, "unlocks_civs": nexts, "inherits": r.get("InheritFrom")})
            for u in nexts:
                self.edge(k, u["key"], "unlocks_civ")

        # governments, beliefs, projects, legacy paths, ages
        for k, r in T.by("Governments", "GovernmentType").items():
            self.add(k, "government", self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"], ages=[MODULE_AGE.get(r["_m"])],
                     summary=self.txt(r.get("Description"), k),
                     facts={"celebration": self.txt(r.get("CelebrationName"), k), "unlocked_by": self.unlock_facts(k)})
        for k, r in T.by("Beliefs", "BeliefType").items():
            self.add(k, "belief", self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"], ages=[MODULE_AGE.get(r["_m"])],
                     summary=self.txt(r.get("Description"), k),
                     facts={"class": (r.get("BeliefClassType") or "").replace("BELIEF_CLASS_", "").lower()})
        for k, r in T.by("Projects", "ProjectType").items():
            self.add(k, "project", self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"], ages=[MODULE_AGE.get(r["_m"])],
                     summary=self.txt(r.get("Description"), k),
                     facts={"cost": _num(r.get("Cost")), "town_only": _truthy(r.get("TownOnly")) or None,
                            "city_only": _truthy(r.get("CityOnly")) or None, "prereq_population": _num(r.get("PrereqPopulation")),
                            "prereq_building": self.ref(r["PrereqConstructible"]) if r.get("PrereqConstructible") else None,
                            "unlocked_by": self.unlock_facts(k)})
        for k, r in T.by("LegacyPaths", "LegacyPathType").items():
            age = r.get("Age") or r.get("AgeType")
            self.add(k, "legacy_path", self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"], ages=[age],
                     summary=self.txt(r.get("Description"), k),
                     facts={"class": (r.get("LegacyPathClassType") or "").replace("LEGACY_PATH_CLASS_", "").lower()})
        for k, r in T.by("Ages", "AgeType").items():
            self.add(k, "age", self.name(k), type_=k, module=r["_m"], ages=[k], summary=self.txt(r.get("Description"), k),
                     facts={"chronology": _num(r.get("ChronologyIndex"))})

        # map: resources, terrains, biomes, features
        rv = T.multi("Resource_ValidBiomes", "ResourceType")
        for k, r in T.by("Resources", "ResourceType").items():
            ages = [a["AgeType"] for a in T.multi("Resource_ValidAges", "ResourceType").get(k, [])]
            facts = {"class": self.name(r.get("ResourceClassType")), "ages": ages,
                     "yields": [{"yield": self.yields.get(y["YieldType"], y["YieldType"]), "amount": _num(y["YieldChange"])}
                                for y in T.multi("Resource_YieldChanges", "ResourceType").get(k, [])],
                     "found_on": _dedupe([{"biome": self.name(v.get("BiomeType")), "terrain": self.name(v.get("TerrainType")),
                                           **({"feature": self.name(v["FeatureType"])} if v.get("FeatureType") else {})} for v in rv.get(k, [])]),
                     "required_civ": [self.ref(c["CivilizationType"]) for c in T.multi("Resource_RequiredCivs", "ResourceType").get(k, [])]}
            self.add(k, "resource", self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"], ages=ages,
                     summary=self.txt(r.get("Tooltip"), k), facts=facts)
            for v in rv.get(k, []):
                for t in (v.get("BiomeType"), v.get("TerrainType"), v.get("FeatureType")):
                    if t:
                        self.edge(k, t, "found_in")
        for k, r in T.by("Terrains", "TerrainType").items():
            self.add(k, "terrain", self.name(k), type_=k, module=r["_m"], summary=self.txt(f"LOC_{k}_DESCRIPTION", k),
                     facts={x: (_truthy(r[c]) if c in ("Impassable", "Hills", "Water") else _num(r[c]))
                            for c, x in (("MovementCost", "movement_cost"), ("Impassable", "impassable"), ("Hills", "hills"),
                                         ("Water", "water"), ("Appeal", "appeal")) if r.get(c)})
        for k, r in T.by("Biomes", "BiomeType").items():
            self.add(k, "biome", self.name(k), type_=k, module=r["_m"], summary=self.txt(f"LOC_{k}_DESCRIPTION", k),
                     facts={"max_latitude": _num(r.get("MaxLatitude")),
                            "features": [self.ref(f["FeatureType"]) for f in T.rows("Feature_ValidBiomes") if f.get("BiomeType") == k]})
        for k, r in T.by("Features", "FeatureType").items():
            natural = not _truthy(r.get("Removable", "true"))
            facts = {"natural_wonder": natural or None, "removable": _truthy(r.get("Removable")),
                     "class": (r.get("FeatureClassType") or "").replace("FEATURE_CLASS_", "").lower() or None,
                     "impassable": _truthy(r.get("Impassable")) or None, "appeal": _num(r.get("Appeal")),
                     "defense_modifier": _num(r.get("DefenseModifier")), "adds_fresh_water": _truthy(r.get("AddsFreshWater")) or None,
                     "biomes": [self.name(f["BiomeType"]) for f in T.multi("Feature_ValidBiomes", "FeatureType").get(k, [])],
                     "terrains": [self.name(f["TerrainType"]) for f in T.multi("Feature_ValidTerrains", "FeatureType").get(k, [])]}
            self.add(k, "natural_wonder" if natural else "feature", self.name(k), type_=k, module=r["_m"], dlc=r["_dlc"],
                     summary=self.txt(r.get("Description") or r.get("Tooltip"), k), facts=facts)
            for f in T.multi("Feature_ValidBiomes", "FeatureType").get(k, []):
                self.edge(k, f["BiomeType"], "found_in")
            for f in T.multi("Feature_ValidTerrains", "FeatureType").get(k, []):
                self.edge(k, f["TerrainType"], "found_in")
        for k, r in T.by("Routes", "RouteType").items():
            if r.get("Name"):
                self.add(k, "route", self.name(k), type_=k, module=r["_m"], summary=self.txt(r.get("Description"), k))

        # ages for items that only an age-specific node unlocks (base-standard units, DLC content)
        for k, p in self.pages.items():
            # base-standard rows load in every age; only age-less DLC content takes the age of what unlocks it
            if not p["ages"] and p["module"] != "base-standard" and p["kind"] in ("unit", "building", "improvement",
                                                                                   "wonder", "project", "government"):
                ages = {self.pages[u["key"]]["facts"].get("age") for u in p["facts"].get("unlocked_by", []) if u["key"] in self.pages}
                p["ages"] |= {a for a in ages if a}
            if not p["ages"] and p["facts"].get("unique_to"):
                p["ages"] |= {self.civ_age[c["key"]] for c in p["facts"]["unique_to"] if c["key"] in self.civ_age}

    def _adjacent(self, col: str, v: str) -> str:
        if _truthy(v):
            return re.sub(r"(?<!^)([A-Z])", r" \1", col.removeprefix("Adjacent")).strip()
        if col == "AdjacentConstructibleTag":
            return v.replace("_", " ").title()
        return self.name(v)

    # ---- prose pages (concepts + chapters of gameplay pages)
    def build_prose(self) -> None:
        ix = pedia.build_index(self.s.text, self.s.structure_files, self.s.data_files, keep_keys=True)
        for p in ix["pages"]:
            pid = p["page_id"]
            target = pid if pid in self.pages else None
            if target is None:
                kind = SECTION_KIND.get(p["section"]) or next((k for pre, k in PREFIX_KIND if pid.startswith(pre)), "concept")
                if kind != "concept" and not p["listed"]:
                    kind = "concept"  # leftover text of removed content
                key = p["key"]
                self.add(key, kind, p["title"], section=p["section_name"], group=p["group"], listed=p["listed"],
                         module=None, aliases=p["terms"])
                target = key
            page = self.pages[target]
            page["section"] = page["section"] or p["section_name"]
            page["group"] = page["group"] or p["group"]
            page["aliases"] |= set(p["terms"]) | {p["key"]}
            if not p["listed"] and target == p["key"]:
                page["listed"] = False
            for c in p["chapters"]:
                text = "\n\n".join(self.txt(k, target) for k in c.get("keys", []))
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                if text:
                    page["chapters"].append({"title": c["title"], "text": text})
            if not page["summary"] and page["chapters"]:
                page["summary"] = page["chapters"][0]["text"]

    # ---- links -> edges, term pages
    def build_links(self) -> None:
        tip_anchor: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for page in list(self.links):
            for target, anchor, via in self.links[page]:
                if via == "icon":
                    if target in self.pages and target != page:
                        self.edge(page, target, "mentions", anchor, {"via": "icon"})
                    continue
                term = "TERM:" + re.sub(r"^LOC_(PEDIA_CONCEPTS_)?", "", target).removesuffix("_TOOLTIP")
                tip_anchor[target][anchor] += 1
                self.edge(page, term, "mentions", anchor)
        # term pages, from every tooltip that is linked (even from pages outside the index, like modifier text)
        concepts = {p["key"]: p for p in self.pages.values() if p["kind"] == "concept" and p["listed"]}
        for tip, anchors in tip_anchor.items():
            term = "TERM:" + re.sub(r"^LOC_(PEDIA_CONCEPTS_)?", "", tip).removesuffix("_TOOLTIP")
            if term in self.pages or tip not in self.s.text:
                continue
            title = max(anchors.items(), key=lambda kv: (kv[1], -len(kv[0])))[0] or _pretty(term[5:])
            title = title[:1].upper() + title[1:]
            links: list = []
            text = self.r.render(self.s.text[tip], links)
            self.add(term, "term", title, summary=text, section="Glossary", aliases={_pretty(term[5:])} | set(anchors))
            self.pages[term]["chapters"] = [{"title": "Tooltip", "text": text}]
            for target, anchor, via in links:
                if via == "tip":
                    t2 = "TERM:" + re.sub(r"^LOC_(PEDIA_CONCEPTS_)?", "", target).removesuffix("_TOOLTIP")
                    self.edge(term, t2, "mentions", anchor)
            stem = term[5:]
            variants = {stem, stem.rstrip("S"), stem + "S", stem.replace("CITIES", "CITY"), stem.replace("_", "")}
            for ck, c in concepts.items():
                cid = re.sub(r"_\d+$", "", ck.split(":", 1)[1])
                if cid in variants or _norm(c["title"]) in {_norm(title), _norm(_pretty(stem))}:
                    self.edge(term, ck, "see_also")

    NAME_LINK_KINDS = ("tech", "civic", "terrain", "biome", "feature", "natural_wonder", "building", "wonder", "improvement",
                       "unit", "government", "resource", "concept", "term", "legacy_path", "age", "quarter", "project",
                       "civilization", "leader")

    NAME_HEADS = {"Technology", "Tech", "Civic", "Age", "Unit", "Units", "Building", "Buildings", "Tile", "Tiles", "Biome",
                  "Terrain", "Resource", "Resources", "Wonder", "Ocean", "Quarter", "Improvement", "Improvements"}

    def build_name_links(self) -> None:
        """Concept and glossary prose rarely carries [TIP] markup, so link the titles of other pages it names
        (whole word, case-sensitive, longest first; edge detail via=name). Titles shared by several pages link to the
        listed, non-unique one first; one-word titles under 4 letters are skipped."""
        order = {k: i for i, k in enumerate(self.NAME_LINK_KINDS)}
        by_title: dict[str, str] = {}
        for k, p in sorted(self.pages.items(), key=lambda kv: (not kv[1]["listed"], bool(kv[1]["facts"].get("unique_to")),
                                                                 order.get(kv[1]["kind"], 99), len(kv[0]))):
            t = p["title"]
            if p["kind"] not in order or len(t) < 4 or not t[:1].isupper() or (p["kind"] == "term" and " " not in t):
                continue
            by_title.setdefault(t, k)
        if not by_title:
            return
        pat = re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in sorted(by_title, key=len, reverse=True)) + r")(?![\w-])")
        for k, p in self.pages.items():
            if p["kind"] not in ("concept", "term") or not p["listed"]:
                continue
            seen = set()
            for c in p["chapters"]:
                for m in pat.finditer(c["text"]):
                    nxt = re.match(r" ([A-Z][a-z]+)", c["text"][m.end():m.end() + 30])
                    if nxt and nxt.group(1) not in self.NAME_HEADS:
                        continue  # part of a longer proper name ("Rough Seas" is not the Rough terrain)
                    dst = by_title[m.group(1)]
                    if dst != k and dst not in seen:
                        seen.add(dst)
                        self.edge(k, dst, "mentions", m.group(1), {"via": "name"})

    def fix_unlock_kinds(self) -> None:
        for p in self.pages.values():
            for f in ("unlocks", "mastery_unlocks"):
                for u in p["facts"].get(f, []):
                    if u.get("key") in self.pages:
                        u["kind"] = self.pages[u["key"]]["kind"]

    def finish(self) -> tuple[list[dict], list[tuple]]:
        keys = set(self.pages)
        seen, edges = set(), []
        for e in self.edges:
            if e[0] in keys and e[1] in keys and e[0] != e[1] and e[:3] + (e[3],) not in seen:
                seen.add(e[:3] + (e[3],))
                edges.append(e)
        return list(self.pages.values()), edges


def _snake(s: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", s).lower()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _dedupe(items: list) -> list:
    out, seen = [], set()
    for i in items:
        k = json.dumps(i, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE pages (
  key TEXT PRIMARY KEY,      -- gameplay type id (BUILDING_COTHON, NODE_CIVIC_AQ_MAIN_CODE_OF_LAWS) or SECTION:PAGE / TERM:NAME
  kind TEXT NOT NULL,        -- concept, term, unit, building, wonder, tech, civic, tradition, policy, civilization, ...
  title TEXT NOT NULL,
  section TEXT, page_group TEXT,
  ages TEXT NOT NULL,        -- '|AGE_ANTIQUITY|' style list; '' = not age-bound
  module TEXT, dlc INTEGER,  -- source module (base-standard, age-antiquity, carthage, ...)
  listed INTEGER NOT NULL,   -- 0 = text of removed content, not in the current in-game Civilopedia
  summary TEXT, text TEXT,   -- readable (markup stripped, icons as words)
  chapters TEXT,             -- JSON [{title, text}]
  facts TEXT                 -- JSON structured facts from the data XML
);
CREATE TABLE aliases (alias TEXT NOT NULL, key TEXT NOT NULL);
CREATE INDEX aliases_alias ON aliases(alias);
CREATE TABLE edges (src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL, anchor TEXT, detail TEXT);
CREATE INDEX edges_src ON edges(src, rel);
CREATE INDEX edges_dst ON edges(dst, rel);
CREATE VIRTUAL TABLE pages_fts USING fts5(key UNINDEXED, title, aliases, summary, body, facts,
                                          tokenize = 'porter unicode61 remove_diacritics 2');
"""


def _facts_text(v) -> str:
    """Searchable words from facts: names, texts (not keys or numbers)."""
    out = []
    if isinstance(v, dict):
        for k, x in v.items():
            if k not in ("key", "modifier", "effect", "args", "tree_id"):
                out.append(_facts_text(x))
    elif isinstance(v, list):
        out += [_facts_text(x) for x in v]
    elif isinstance(v, str):
        out.append(v)
    return " ".join(o for o in out if o)


def build(root: Path | None = None, db: Path | None = None, force: bool = False) -> dict:
    """Build the index from the install (or a fixture root) into db; skipped when the version key is unchanged."""
    root, db = _game_root(root), Path(db) if db else DB
    if not (root / "Base" / "modules").is_dir():
        raise PediaIndexError(f"Civ VII install not found at {root}")
    sig = signature(root)
    if not force and db.exists():
        try:
            with sqlite3.connect(db) as con:
                row = con.execute("SELECT value FROM meta WHERE key='signature'").fetchone()
            if row and row[0] == sig:
                return {"status": "up to date", "db": str(db), "signature": sig}
        except sqlite3.Error:
            pass
    t0 = time.time()
    b = Builder(Source(root))
    b.build_gameplay()
    b.build_prose()
    b.build_links()
    b.build_name_links()
    b.fix_unlock_kinds()
    pages, edges = b.finish()
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        for p in pages:
            chapters = p["chapters"] or ([{"title": "Summary", "text": p["summary"]}] if p["summary"] else [])
            body = "\n\n".join(c["text"] for c in chapters)
            ages = "|" + "|".join(sorted(p["ages"])) + "|" if p["ages"] else ""
            con.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (p["key"], p["kind"], p["title"], p["section"], p["group"], ages, p["module"], int(bool(p["dlc"])),
                         int(bool(p["listed"])), p["summary"], body, json.dumps(chapters, ensure_ascii=False),
                         json.dumps(p["facts"], ensure_ascii=False, separators=(",", ":"))))
            aliases = {p["title"], p["key"]} | ({p["type"]} if p["type"] else set()) | p["aliases"]
            con.executemany("INSERT INTO aliases VALUES (?,?)", [(_norm(a), p["key"]) for a in aliases if _norm(a)])
            con.execute("INSERT INTO pages_fts VALUES (?,?,?,?,?,?)",
                        (p["key"], p["title"], " ".join(sorted(a for a in p["aliases"] if not a.isupper())),
                         p["summary"], body, _facts_text(p["facts"])))
        con.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", edges)
        meta = {"signature": sig, "index_version": str(INDEX_VERSION), "steam_build": _steam_build(root) or "",
                "game_root": str(root), "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pages": str(len(pages)),
                "edges": str(len(edges)), "build_seconds": f"{time.time() - t0:.1f}"}
        con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
        con.commit()
    finally:
        con.close()
    os.replace(tmp, db)
    kinds: dict[str, int] = defaultdict(int)
    for p in pages:
        kinds[p["kind"]] += 1
    return {"status": "built", "db": str(db), "pages": len(pages), "edges": len(edges), "kinds": dict(kinds),
            "seconds": round(time.time() - t0, 1), "signature": sig}


# ================================================================ querying

_CHECKED: dict[str, float] = {}
RECHECK_SECONDS = 600


def connect(db: Path | None = None, root: Path | None = None) -> sqlite3.Connection:
    """Open the index read-only, building it first if missing or (checked every 10 min) stale after a patch."""
    db = Path(db) if db else DB
    if not db.exists() or time.time() - _CHECKED.get(str(db), 0) > RECHECK_SECONDS:
        if root is None and db.exists():
            try:  # re-check against the install this index was built from
                with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
                    row = con.execute("SELECT value FROM meta WHERE key='game_root'").fetchone()
                root = Path(row[0]) if row else None
            except sqlite3.Error:
                root = None
        if db.exists() and not (_game_root(root) / "Base" / "modules").is_dir():
            pass  # install gone (moved, uninstalled): keep serving the existing index
        else:
            build(root, db)
        _CHECKED[str(db)] = time.time()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _age(age: str | None) -> str | None:
    if not age:
        return None
    a = age.strip().upper()
    return AGES.get(a.lower().removeprefix("age_"), a if a.startswith("AGE_") else f"AGE_{a}")


KIND_ALIASES = {"civ": "civilization", "civs": "civilization", "civilisation": "civilization", "technology": "tech",
                "technologies": "tech", "techs": "tech", "civics": "civic", "policies": "policy", "natural wonder": "natural_wonder",
                "glossary": "term", "terms": "term", "concepts": "concept", "quarters": "quarter", "resources": "resource",
                "traditions": "tradition", "leaders": "leader", "units": "unit", "buildings": "building", "wonders": "wonder",
                "improvements": "improvement", "governments": "government", "beliefs": "belief", "projects": "project"}


def _kinds(kind: str | list | None) -> list[str]:
    if not kind:
        return []
    ks = kind if isinstance(kind, list) else re.split(r"[,|]+", kind)
    return [KIND_ALIASES.get(k.strip().lower(), k.strip().lower().replace(" ", "_")) for k in ks if k.strip()]


def _ages_out(ages: str) -> list[str] | str:
    return [a for a in ages.split("|") if a] or "any"


def _hit(r, query_norm: str = "") -> dict:
    d = {"key": r["key"], "title": r["title"], "kind": r["kind"], "age": _ages_out(r["ages"])}
    if r["kind"] in ("tech", "civic"):  # their prose is history; what they unlock says more in one line
        f = json.loads(r["facts"] or "{}")
        d["unlocks"] = [_flat(u) for u in f.get("unlocks", [])]
        if f.get("unique_to"):
            d["unique_to"] = [u["name"] for u in f["unique_to"]]
    else:
        d["summary"] = one_line(r["summary"] or "")
    if not r["listed"]:
        d["note"] = "not in the current in-game Civilopedia (removed content)"
    return d


def _fts_query(q: str, mode: str) -> str:
    toks = re.findall(r"\w+", q.lower())
    if not toks:
        return ""
    parts = [f'"{t}"' for t in toks[:-1]] + [f'"{toks[-1]}"*']
    return (" OR " if mode == "or" else " ").join(parts)


def search(query: str, kind: str | list | None = None, age: str | None = None, limit: int = 10,
           db: Path | None = None) -> dict:
    """Ranked pages for words in title, aliases, summary, text and facts; filters by kind and age (age-less items
    count as every age)."""
    con = connect(db)
    try:
        kinds, a = _kinds(kind), _age(age)
        where, args = [], []
        if kinds:
            where.append(f"p.kind IN ({','.join('?' * len(kinds))})")
            args += kinds
        if a:
            where.append("(p.ages = '' OR instr(p.ages, ?) > 0)")
            args.append(f"|{a}|")
        cond = (" AND " + " AND ".join(where)) if where else ""
        qn = _norm(query)
        rows = []
        for mode in ("and", "or"):
            fq = _fts_query(query, mode)
            if not fq:
                break
            rows = con.execute(
                f"SELECT p.*, bm25(pages_fts, 0, 12.0, 6.0, 3.0, 1.0, 0.5) AS rank FROM pages_fts f JOIN pages p ON p.key = f.key "
                f"WHERE pages_fts MATCH ?{cond} ORDER BY rank LIMIT 200", [fq, *args]).fetchall()
            if rows:
                break
        exact = {r["key"] for r in con.execute("SELECT key FROM aliases WHERE alias = ?", (qn,))}

        def score(r):
            s = -r["rank"]
            t = _norm(r["title"])
            if r["key"] in exact or t == qn:
                s += 1000
            elif qn and t.startswith(qn):
                s += 50
            if not r["listed"]:
                s /= 3
            if r["kind"] == "term":
                s *= 0.8
            return s

        rows = sorted(rows, key=score, reverse=True)
        out = {"query": query, "results": [_hit(r, qn) for r in rows[:max(1, limit)]]}
        if kind:
            out["kind"] = kinds
        if a:
            out["age"] = a
        if len(rows) > limit:
            out["more"] = len(rows) - limit
        if not rows:
            out["hint"] = "no match; try fewer or other words, or drop the kind/age filter"
        return out
    finally:
        con.close()


def _resolve(con, key_or_title: str) -> tuple[sqlite3.Row | None, list[dict]]:
    """Exact key (any case, SECTION:TYPE accepted), alias/title, then best search hit. Returns (row, alternatives)."""
    q = key_or_title.strip()
    r = con.execute("SELECT * FROM pages WHERE key = ? COLLATE NOCASE", (q,)).fetchone()
    if r:
        return r, []
    if ":" in q:
        r = con.execute("SELECT * FROM pages WHERE key = ? COLLATE NOCASE", (q.split(":", 1)[1],)).fetchone()
        if r:
            return r, []
    keys = [x["key"] for x in con.execute("SELECT DISTINCT key FROM aliases WHERE alias = ?", (_norm(q),))]
    if keys:
        rows = [con.execute("SELECT * FROM pages WHERE key = ?", (k,)).fetchone() for k in keys]
        order = {k: i for i, k in enumerate(KINDS)}
        rows.sort(key=lambda r: (not r["listed"], _norm(r["title"]) != _norm(q), r["kind"] == "term",
                                 order.get(r["kind"], 99) if r["kind"] not in ("concept",) else 50, len(r["key"])))
        return rows[0], [{"key": x["key"], "title": x["title"], "kind": x["kind"]} for x in rows[1:8]]
    return None, []


def _edge_rows(con, key: str, rel: str | None, directions: list[str]) -> list[dict]:
    """Edges touching key. rel is a stored (forward) relation or None; 'in' edges are labelled with the inverse name."""
    out = []
    for d in directions:
        col, other = ("src", "dst") if d == "out" else ("dst", "src")
        sql = (f"SELECT e.rel, e.anchor, e.detail, p.key, p.title, p.kind, p.ages FROM edges e "
               f"JOIN pages p ON p.key = e.{other} WHERE e.{col} = ?")
        args = [key]
        if rel:
            sql += " AND e.rel = ?"
            args.append(rel)
        for r in con.execute(sql + " ORDER BY e.rel, p.kind, p.title", args):
            item = {"rel": r["rel"] if d == "out" else INVERSE.get(r["rel"], r["rel"]), "key": r["key"],
                    "title": r["title"], "kind": r["kind"], "age": _ages_out(r["ages"])}
            if r["anchor"] and r["anchor"] != r["title"]:
                item["anchor"] = r["anchor"]
            if r["detail"]:
                item.update(json.loads(r["detail"]))
            out.append(item)
    return out


def page(key_or_title: str, max_chars: int = 6000, max_per_rel: int = 40, db: Path | None = None) -> dict:
    """Full page: readable chapters, structured facts, outgoing links and incoming links (unlocked_by, has_unique,
    prereq_of, ...; mentions only counted)."""
    con = connect(db)
    try:
        r, alts = _resolve(con, key_or_title)
        if r is None:
            hits = search(key_or_title, limit=5, db=db)["results"]
            if not hits:
                raise PediaIndexError(f"no page for {key_or_title!r}")
            r = con.execute("SELECT * FROM pages WHERE key = ?", (hits[0]["key"],)).fetchone()
            alts = [{"key": h["key"], "title": h["title"], "kind": h["kind"]} for h in hits[1:]]
        chapters, used = [], 0
        for c in json.loads(r["chapters"] or "[]"):
            text = c["text"]
            if max_chars and used + len(text) > max_chars:
                text = text[:max(0, max_chars - used)].rsplit(" ", 1)[0] + " ..."
                chapters.append({"title": c["title"], "text": text, "truncated": True})
                break
            chapters.append(c)
            used += len(text)
        out_links = _edge_rows(con, r["key"], None, ["out"])
        in_links = [i for i in _edge_rows(con, r["key"], None, ["in"]) if i["rel"] != "mentioned_by"]
        n_mentioned = con.execute("SELECT count(*) FROM edges WHERE dst = ? AND rel = 'mentions'", (r["key"],)).fetchone()[0]
        d = {"key": r["key"], "title": r["title"], "kind": r["kind"], "age": _ages_out(r["ages"]),
             "section": r["section"], "group": r["page_group"], "module": r["module"], "dlc": bool(r["dlc"]),
             "summary": r["summary"], "facts": json.loads(r["facts"] or "{}"), "chapters": chapters,
             "links": _compact_links(out_links), "incoming": _compact_links(in_links, max_per_rel)}
        if n_mentioned:
            d["incoming"]["mentioned_by"] = f"{n_mentioned} pages (pedia_links rel=mentioned_by)"
        if not r["listed"]:
            d["note"] = "not in the current in-game Civilopedia (removed content); may be outdated"
        if alts:
            d["also_matches"] = alts
        return {k: v for k, v in d.items() if v not in (None, "", [], {})}
    finally:
        con.close()


def _compact_links(items: list[dict], max_per_rel: int = 0) -> dict[str, list]:
    """Group by relation; one entry per target (anchors merged)."""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for i in items:
        x = {k: v for k, v in i.items() if k not in ("rel", "age", "anchor")}
        cur = out[i["rel"]].setdefault(i["key"], x)
        if i.get("anchor"):
            cur.setdefault("anchors", [])
            if i["anchor"] not in cur["anchors"]:
                cur["anchors"].append(i["anchor"])
    res: dict[str, list] = {}
    for rel, d in out.items():
        vals = list(d.values())
        if max_per_rel and len(vals) > max_per_rel:
            vals = vals[:max_per_rel] + [{"more": len(vals) - max_per_rel}]
        res[rel] = vals
    return res


def links(key: str, relation: str | None = None, direction: str = "out", kind: str | None = None,
          db: Path | None = None) -> dict:
    """Graph neighbours of a page. relation: forward (unlocks, prereq, unique_to, ...) or inverse name (unlocked_by,
    prereq_of, has_unique, ...); direction out | in | both."""
    if direction not in ("out", "in", "both"):
        raise PediaIndexError("direction must be out, in or both")
    con = connect(db)
    try:
        r, _ = _resolve(con, key)
        if r is None:
            raise PediaIndexError(f"no page for {key!r}")
        rel = None
        if relation:
            if relation not in REL_BY_NAME:
                raise PediaIndexError(f"unknown relation {relation!r}; one of: {', '.join(sorted(REL_BY_NAME))}")
            rel, natural = REL_BY_NAME[relation]
            if natural == "in" and direction != "both":
                direction = "in"  # an inverse name (unlocked_by, prereq_of, ...) reads the stored edge backwards
        items = _edge_rows(con, r["key"], rel, ["out", "in"] if direction == "both" else [direction])
        if kind:
            ks = _kinds(kind)
            items = [i for i in items if i["kind"] in ks]
        return {"key": r["key"], "title": r["title"], "kind": r["kind"], "direction": direction,
                **({"relation": relation} if relation else {}), "count": len(items), "links": items}
    finally:
        con.close()


COMPARE_SKIP = {"tags", "tree_id", "effect", "args"}


def _flat(v):
    """Compact a fact value for side-by-side reading: refs -> names, yields -> '+2 Gold'."""
    if isinstance(v, list):
        return [_flat(x) for x in v] if v else None
    if isinstance(v, dict):
        if "yield" in v and "cost" in v:
            return f"{v['cost']} {v['yield']}"
        if "yield" in v and "amount" in v:
            s = f"{v['amount']:+g} {v['yield']}" if isinstance(v["amount"], (int, float)) else f"{v['amount']} {v['yield']}"
            if v.get("per_adjacent"):
                s += f" per adjacent {v['per_adjacent']}"
            return s
        if "name" in v and "key" in v:
            extra = [f"{k}={v[k]}" for k in ("only_for", "not_for") if v.get(k)] + (["mastery"] if v.get("mastery") else [])
            return v["name"] + (f" ({', '.join(extra)})" if extra else "")
        if "text" in v and "modifier" in v:
            return v["text"]
        return {k: _flat(x) for k, x in v.items() if k not in COMPARE_SKIP and x not in (None, [], "")}
    return v


def compare(keys: list[str], fields: list[str] | None = None, db: Path | None = None) -> dict:
    """Side-by-side facts for a few pages: one row per fact, one column per page (missing = null)."""
    if not keys:
        raise PediaIndexError("give 2+ keys or titles")
    con = connect(db)
    try:
        items, facts = [], []
        for k in keys[:8]:
            r, _ = _resolve(con, k)
            if r is None:
                raise PediaIndexError(f"no page for {k!r}")
            items.append({"key": r["key"], "title": r["title"], "kind": r["kind"], "age": _ages_out(r["ages"]),
                          "summary": one_line(r["summary"] or "", 300)})
            facts.append(json.loads(r["facts"] or "{}"))
        names: list[str] = []
        for f in facts:
            names += [n for n in f if n not in names and n not in COMPARE_SKIP]
        if fields:
            names = [n for n in fields if n in names]
        rows = {n: [_flat(f.get(n)) for f in facts] for n in names}
        same = [n for n, vs in rows.items() if len({json.dumps(v, sort_keys=True) for v in vs}) == 1]
        return {"items": items, "facts": {n: v for n, v in rows.items() if n not in same},
                **({"same": {n: rows[n][0] for n in same}} if same else {})}
    finally:
        con.close()


def filter_pages(kind: str | list | None = None, age: str | None = None, civ: str | None = None,
                 category: str | None = None, unlocked_by: str | None = None, include_unique: bool = True,
                 limit: int = 200, db: Path | None = None) -> dict:
    """Pages of a kind available in an age to a civ: generic items plus that civ's uniques (other civs' uniques
    dropped). category matches building yield categories (gold, production, science, ...). unlocked_by keeps items a
    given tech/civic unlocks."""
    con = connect(db)
    try:
        kinds, a = _kinds(kind), _age(age)
        sql, args = "SELECT * FROM pages WHERE listed = 1", []
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        if a:
            sql += " AND (ages = '' OR instr(ages, ?) > 0)"
            args.append(f"|{a}|")
        civ_key = None
        if civ:
            r, _ = _resolve(con, civ if civ.upper().startswith("CIVILIZATION_") or ":" in civ else civ)
            if r is None or r["kind"] not in ("civilization", "leader"):
                r = con.execute("SELECT * FROM pages WHERE key = ?", ("CIVILIZATION_" + civ.upper().replace(" ", "_"),)).fetchone()
            if r is None:
                raise PediaIndexError(f"no civilization {civ!r}")
            civ_key = r["key"]
        node_targets = None
        if unlocked_by:
            r, _ = _resolve(con, unlocked_by)
            if r is None:
                raise PediaIndexError(f"no page for {unlocked_by!r}")
            node_targets = {e["dst"] for e in con.execute("SELECT dst FROM edges WHERE src = ? AND rel = 'unlocks'", (r["key"],))}
        out = []
        for r in con.execute(sql + " ORDER BY kind, title", args):
            f = json.loads(r["facts"] or "{}")
            owners = {u["key"] for u in f.get("unique_to", [])}
            if owners and (not include_unique or (civ_key and civ_key not in owners) or (not civ_key and civ)):
                continue
            if node_targets is not None and r["key"] not in node_targets:
                continue
            if category and category.lower() not in f.get("category", []):
                continue
            item = _hit(r)
            if owners:
                item["unique_to"] = [u["name"] for u in f["unique_to"]]
            if f.get("category"):
                item["category"] = f["category"]
            if f.get("unlocked_by"):
                item["unlocked_by"] = [_flat(u) for u in f["unlocked_by"]]
            out.append(item)
        return {"filters": {k: v for k, v in {"kind": kinds, "age": a, "civ": civ_key, "category": category,
                                              "unlocked_by": unlocked_by}.items() if v},
                "count": len(out), "results": out[:limit], **({"more": len(out) - limit} if len(out) > limit else {})}
    finally:
        con.close()


MAIN_TREE = re.compile(r"^TREE_(TECHS_(AQ|EX|MO)|CIVICS_(AQ|EX|MO)_MAIN)$")


def reachable(known: list[str], steps: int = 1, system: str | None = None, civ: str | None = None,
              age: str | None = None, db: Path | None = None) -> dict:
    """Techs/civics researchable within `steps` steps of the known (researched) ones, with what each unlocks.
    Step 1 = researchable now. Covers the age's main tech and civic trees plus civ's unique civics. Prereqs are all
    required unless the tree's PrereqFormat is OR; nodes without prereqs (tree roots) are step 1. The age comes from
    the known nodes, else from `age`."""
    con = connect(db)
    try:
        have = set()
        for k in known:
            r, _ = _resolve(con, k)
            if r is None or r["kind"] not in ("tech", "civic"):
                raise PediaIndexError(f"not a tech or civic: {k!r}")
            have.add(r["key"])
        civ_key = None
        if civ:
            r, _ = _resolve(con, civ)
            if r is None or r["kind"] != "civilization":
                r = con.execute("SELECT * FROM pages WHERE key = ?", ("CIVILIZATION_" + civ.upper().replace(" ", "_"),)).fetchone()
            if r is None:
                raise PediaIndexError(f"no civilization {civ!r}")
            civ_key, civ_name = r["key"], r["title"]
        else:
            civ_name = None
        nodes = {r["key"]: (r, json.loads(r["facts"] or "{}"))
                 for r in con.execute("SELECT * FROM pages WHERE kind IN ('tech','civic')")}
        ages = {a for k in have for a in nodes[k][0]["ages"].split("|") if a} or ({_age(age)} if age else set())
        if not ages:
            raise PediaIndexError("give known techs/civics or an age")

        def in_scope(r, f) -> bool:
            if system and r["kind"] != system:
                return False
            if not any(f"|{a}|" in r["ages"] for a in ages):
                return False
            owners = {u["key"] for u in f.get("unique_to", [])}
            if owners:
                return civ_key in owners
            return bool(MAIN_TREE.match(f.get("tree_id") or ""))

        out, frontier = [], set(have)
        for step in range(1, max(1, steps) + 1):
            new = []
            for k, (r, f) in nodes.items():
                if k in frontier or not in_scope(r, f):
                    continue
                pre = [p["key"] for p in f.get("prereqs", [])]
                if not pre or (any if f.get("prereq_format") == "OR" else all)(p in frontier for p in pre):
                    new.append((k, r, f))
            if not new:
                break
            for k, r, f in sorted(new, key=lambda x: (x[1]["kind"], x[2].get("cost") or 0, x[1]["title"])):
                unl = [_flat(u) for u in f.get("unlocks", []) if not u.get("only_for") or u["only_for"] == civ_name]
                item = {"step": step, "key": k, "title": r["title"], "kind": r["kind"], "cost": f.get("cost"),
                        "unlocks": [u for u in unl if u]}
                if f.get("mastery_unlocks"):
                    item["mastery_unlocks"] = [_flat(u) for u in f["mastery_unlocks"]]
                out.append(item)
            frontier |= {k for k, _, _ in new}
        return {"known": sorted(have), "ages": sorted(ages), "steps": steps, "count": len(out), "results": out}
    finally:
        con.close()


def info(db: Path | None = None) -> dict:
    con = connect(db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        meta["kinds"] = dict(con.execute("SELECT kind, count(*) FROM pages GROUP BY kind ORDER BY 2 DESC").fetchall())
        meta["relations"] = dict(con.execute("SELECT rel, count(*) FROM edges GROUP BY rel ORDER BY 2 DESC").fetchall())
        return meta
    finally:
        con.close()


# ================================================================ CLI

COMMANDS = ("build", "search", "page", "links", "compare", "filter", "reachable", "info")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="civilizer pedia", description="Indexed Civilopedia (data/civilopedia.db); "
                                 "JSON output. `civilizer pedia <words>` without a subcommand is the older full-text reader.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build", help="build the index (skipped if the game files are unchanged)")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("search", help="ranked hits with one-line summaries")
    p.add_argument("query", nargs="+")
    p.add_argument("--kind", "-k", help="unit, building, wonder, tech, civic, tradition, policy, civilization, concept, ...")
    p.add_argument("--age", "-a", help="antiquity | exploration | modern")
    p.add_argument("--limit", "-n", type=int, default=10)
    p = sub.add_parser("page", help="full text, facts and links of one page (key or title)")
    p.add_argument("key", nargs="+")
    p.add_argument("--max-chars", type=int, default=6000)
    p = sub.add_parser("links", help="graph neighbours")
    p.add_argument("key", nargs="+")
    p.add_argument("--rel", "-r", help="unlocks, unlocked_by, prereq, prereq_of, unique_to, has_unique, mentions, ...")
    p.add_argument("--dir", "-d", default="out", choices=["out", "in", "both"])
    p.add_argument("--kind", "-k")
    p = sub.add_parser("compare", help="side-by-side facts (keys or titles, quote multi-word titles)")
    p.add_argument("keys", nargs="+")
    p.add_argument("--field", "-f", action="append")
    p = sub.add_parser("filter", help="e.g. --kind building --age antiquity --civ carthage")
    p.add_argument("--kind", "-k")
    p.add_argument("--age", "-a")
    p.add_argument("--civ", "-c")
    p.add_argument("--category", help="building yield category: gold, production, science, culture, food, happiness, ...")
    p.add_argument("--unlocked-by", "-u")
    p.add_argument("--no-unique", action="store_true")
    p.add_argument("--limit", "-n", type=int, default=200)
    p = sub.add_parser("reachable", help="techs/civics researchable within N steps of the known ones, with unlocks")
    p.add_argument("known", nargs="*", help="researched techs/civics (keys or quoted titles); none = tree roots of --age")
    p.add_argument("--age", "-a")
    p.add_argument("--steps", "-s", type=int, default=1)
    p.add_argument("--system", choices=["tech", "civic"])
    p.add_argument("--civ", "-c")
    sub.add_parser("info", help="index version, counts per kind and relation")
    a = ap.parse_args(argv)
    try:
        match a.cmd:
            case "build":
                res = build(force=a.force)
                _CHECKED[str(DB)] = time.time()
            case "search":
                res = search(" ".join(a.query), a.kind, a.age, a.limit)
            case "page":
                res = page(" ".join(a.key), a.max_chars)
            case "links":
                res = links(" ".join(a.key), a.rel, a.dir, a.kind)
            case "compare":
                res = compare(a.keys, a.field)
            case "filter":
                res = filter_pages(a.kind, a.age, a.civ, a.category, a.unlocked_by, not a.no_unique, a.limit)
            case "reachable":
                res = reachable(a.known, a.steps, a.system, a.civ, a.age)
            case _:
                res = info()
    except PediaIndexError as e:
        print(f"error: {e}")
        return 1
    print(json.dumps(res, indent=1, ensure_ascii=False))
    return 0
