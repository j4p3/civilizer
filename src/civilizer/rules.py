"""Game-rules lookup over the installed Civ VII data: English text + data XML.

`lookup("Tariffs")` finds LOC keys whose English text matches, derives the game
type (LOC_TRADITION_TARIFFS_NAME -> TRADITION_TARIFFS), and returns its
description plus the XML rows/modifier blocks that reference it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

GAME = Path.home() / ".local/share/Steam/steamapps/common/Sid Meier's Civilization VII"
CACHE = Path(__file__).resolve().parents[2] / "data" / "cache" / "en_text.json"
TAG = re.compile(r"<[^>]+>")


def _text_files() -> list[Path]:
    roots = [GAME / "Base" / "modules", GAME / "DLC"]
    out = []
    for r in roots:
        out += list(r.rglob("en_us/*.xml")) + list(r.rglob("*en_US*.xml"))
    out += list((GAME / "DLC").glob("*/modules/text/*.xml"))  # some DLCs keep English text outside en_us/
    return sorted(set(out))


def text_index(rebuild: bool = False) -> dict[str, str]:
    files = _text_files()
    if CACHE.exists() and not rebuild and CACHE.stat().st_mtime >= max((f.stat().st_mtime for f in files), default=0):
        return json.loads(CACHE.read_text())  # rebuilt when a patch touches any text file
    idx: dict[str, str] = {}
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for row in root.iter():
            tag = row.get("Tag")
            if not tag:
                continue
            t = row.find("Text")
            text = t.text if t is not None else row.text
            if text:
                idx[tag] = text.strip()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(idx))
    return idx


def _data_files() -> list[Path]:
    return [p for r in (GAME / "Base" / "modules", GAME / "DLC") for p in r.rglob("data/**/*.xml")]


def capital_names() -> dict[str, str]:
    """Civilization display name (short and full, e.g. 'Rome' and 'Roman Empire') -> its capital's name ('Roma'),
    from the Civilizations rows' CapitalName. Empty when the game is not installed."""
    names, out = text_index(), {}
    for f in _data_files():
        if not f.name.startswith("civilizations"):
            continue
        try:
            rows = ET.parse(f).getroot().iter("Row")
        except ET.ParseError:
            continue
        for r in rows:
            if (cap := names.get(r.get("CapitalName") or "")):
                for k in ("Name", "FullName"):
                    if (civ := names.get(r.get(k) or "")):
                        out[civ] = cap
    return out


def references(type_id: str, limit: int = 40) -> list[tuple[str, str]]:
    """XML lines (and whole <Modifier> blocks) that mention type_id or its modifiers."""
    stem = re.sub(r"^(TRADITION|BUILDING|UNIT|IMPROVEMENT|WONDER|POLICY|CIVIC|TECH|NODE_\w+?)_", "", type_id)
    pat = re.compile(rf"\b({re.escape(type_id)}|{re.escape(stem)}_MOD\w*)\b")
    out: list[tuple[str, str]] = []
    for f in _data_files():
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        i = 0
        while i < len(lines):
            ln = lines[i]
            if pat.search(ln):
                block = [ln.strip()]
                if "<Modifier " in ln and "/>" not in ln:
                    j = i + 1
                    while j < len(lines) and "</Modifier>" not in lines[j]:
                        block.append(lines[j].strip())
                        j += 1
                    i = j
                out.append((f.relative_to(GAME).as_posix(), " ".join(block)))
                if len(out) >= limit:
                    return out
            i += 1
    return out


def lookup(query: str, max_types: int = 5) -> list[dict]:
    idx = text_index()
    q = query.lower()
    names = [(k, v) for k, v in idx.items() if k.endswith("_NAME") and q in TAG.sub("", v).lower()]
    names.sort(key=lambda kv: (TAG.sub("", kv[1]).lower() != q, len(kv[1])))
    res = []
    for key, name in names[:max_types]:
        type_id = key.removeprefix("LOC_").removesuffix("_NAME")
        res.append(
            {
                "type": type_id,
                "name": TAG.sub("", name),
                "description": idx.get(f"LOC_{type_id}_DESCRIPTION"),
                "references": references(type_id),
            }
        )
    return res
