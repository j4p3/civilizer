"""Presets for the setup wizard: advisor personalities (style-prompt files), player motives, strategy presets.

Advisor presets are markdown files with a small frontmatter (label, description, priorities) and a body that is the
style prompt Claude follows when it speaks as that advisor. Built-ins ship in `advisors/` next to this module; custom
ones are written by `create_advisor()` to `data/advisors/` (next to the store, not checked in) and shadow built-ins
of the same key.
"""

from __future__ import annotations

import re
from pathlib import Path

BUILTIN_DIR = Path(__file__).resolve().parent / "advisors"
KEY = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class PresetError(ValueError):
    pass


def custom_dir() -> Path:
    from . import store  # store imports this module at load time

    return store.DB.parent / "advisors"


def parse(text: str) -> tuple[dict, str]:
    """'---\\nkey: value\\n---\\nbody' -> (meta, body). priorities is a comma-separated list."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, text.strip()
    meta = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep:
            meta[k.strip()] = v.strip()
    meta["priorities"] = [p.strip() for p in meta.get("priorities", "").split(",") if p.strip()]
    return meta, m.group(2).strip()


def _load(path: Path, source: str) -> dict:
    meta, body = parse(path.read_text())
    return {"key": path.stem, "label": meta.get("label") or path.stem.replace("_", " ").capitalize(),
            "description": meta.get("description", ""), "priorities": meta["priorities"], "prompt": body,
            "file": str(path), "source": source}


def builtin_advisors() -> dict[str, dict]:
    return {p.stem: _load(p, "builtin") for p in sorted(BUILTIN_DIR.glob("*.md"))}


def advisors() -> dict[str, dict]:
    """All advisor presets, built-in first, then custom (a custom file overrides a built-in of the same key)."""
    out = builtin_advisors()
    d = custom_dir()
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            out[p.stem] = _load(p, "custom")
    return out


def advisor(key: str) -> dict | None:
    return advisors().get(key)


def create_advisor(key: str, label: str, description: str, prompt: str, priorities: list[str], overwrite: bool = False) -> dict:
    """Write data/advisors/<key>.md. Priorities are validated by the caller (vocab advisor_priority)."""
    if not KEY.match(key):
        raise PresetError("key must be 2-40 chars of lowercase letters, digits and _, starting with a letter")
    if not prompt.strip() or not description.strip():
        raise PresetError("description and prompt are required")
    path = custom_dir() / f"{key}.md"
    if path.exists() and not overwrite:
        raise PresetError(f"custom advisor {key!r} exists ({path}); pass overwrite=true to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    one_line = lambda s: " ".join(s.split())
    path.write_text(f"---\nlabel: {one_line(label or key)}\ndescription: {one_line(description)}\n"
                    f"priorities: {', '.join(priorities)}\n---\n{prompt.strip()}\n")
    return _load(path, "custom")


# motive -> (label, description, follow-up angles Claude can turn into its own questions)
MOTIVES: dict[str, tuple[str, str, list[str]]] = {
    "fight": ("Fight", "War, conquest, armies and commanders.",
              ["early rush or late, decisive wars?", "conquer and keep cities, or raze and move on?",
               "how many units are you willing to lose for a city?"]),
    "build": ("Build", "Cities, wonders, districts, a thriving economy.",
              ["tall (few big cities) or wide (many settlements)?", "which wonders or eras excite you?",
               "do you enjoy optimizing yields, or just watching things grow?"]),
    "explore": ("Explore", "Scouting, the map, the Distant Lands, discoveries.",
                ["land or sea first?", "do you want to race for the Distant Lands / treasure fleets?",
                 "settle what you find, or just see it?"]),
    "mess_around": ("Mess around", "Low stakes, try odd ideas, see what happens.",
                    ["any experiment you want to try this game (a weird civ swap, all-towns, only wonders)?",
                     "should I stay quiet unless something is on fire, or pitch wild ideas?"]),
    "story": ("Make a story", "Play in character and remember the game as a narrative.",
              ["who is your leader in this story: a hero, a schemer, a reluctant ruler?",
               "any rival you want as the nemesis or the old friend?",
               "should I narrate turning points as they happen?"]),
}

# strategy preset -> (label, description, victory types, guidance for Claude when proposing starter goals)
STRATEGIES: dict[str, tuple[str, str, list[str], str]] = {
    "science": ("Science", "Out-research everyone; the space race in the Modern Age.", ["science"],
                "Science legacy path each age. Start goals on science_per_turn and vp_science rank."),
    "culture": ("Culture", "Wonders, civics and the culture legacy path.", ["culture"],
                "Culture legacy path each age. Start goals on culture_per_turn, vp_culture and triumphs_done."),
    "economic": ("Economic", "Gold, trade routes and the economic legacy path.", ["economic"],
                 "Economic legacy path each age. Start goals on gold_per_turn, trade_routes and vp_economic."),
    "military": ("Military", "Conquest and the military legacy path.", ["military"],
                 "Military legacy path / domination. Start goals on settlements_captured_total with a units_lost_total "
                 "gain<= limit sized to the player's risk tolerance."),
    "expansion": ("Expansion", "Go wide: settle fast, grow population and land.", ["expansion"],
                  "Start goals on settlements vs settlement_cap and population; watch happiness and unrest."),
    "score": ("Score", "Balanced play to maximize score.", ["score"], "Start goals on score rank."),
    "just_win": ("Just win", "No preference: the advisor picks the strongest path for this civ and map, and "
                 "re-checks it every age.", [],
                 "Pick victory_types yourself from the leader/civ abilities (civilopedia LEADERS / CIVILIZATIONS, "
                 "game_rules) and the start; tell the player which path and why, then call choose_strategy again with "
                 "victory_types. Revisit at each age transition."),
    "not_sure": ("Not sure yet", "Keep options open: expand and grow, decide once the map and rivals are known.",
                 ["expansion", "score"],
                 "Set a short-term expansion goal for the first ~30 turns and a reminder goal to pick a path. Re-run "
                 "the strategy step of setup_wizard when the player has a feel for the game."),
}
