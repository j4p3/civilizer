"""Save-file discovery and timeline: which saves belong to which game, which is
the current head, and where the player reloaded (abandoned branches)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .save import SaveInfo, read_header

USER_DIR = Path.home() / "My Games" / "Sid Meier's Civilization VII"
SAVE_DIR = USER_DIR / "Saves"
LOG_DIR = USER_DIR / "Logs"


def find_saves(root: Path = SAVE_DIR) -> list[Path]:
    return sorted(root.rglob("*.Civ7Save"))


@dataclass
class TimelineEntry:
    info: SaveInfo
    abandoned: bool  # a later save rewound past this one (the player reloaded an earlier point)

    @property
    def kind(self) -> str:
        return "auto" if self.info.path.parent.name in ("auto", "prev") else "manual"


def load_headers(paths: list[Path]) -> list[SaveInfo]:
    out = []
    for p in paths:
        try:
            out.append(read_header(p))
        except (ValueError, IndexError, OSError):
            continue
    return out


def games(infos: list[SaveInfo]) -> dict[str, list[SaveInfo]]:
    by: dict[str, list[SaveInfo]] = {}
    for i in infos:
        by.setdefault(i.game_guid or "?", []).append(i)
    return by


def timeline(saves: list[SaveInfo]) -> list[TimelineEntry]:
    """Order one game's saves by wall-clock save time and mark abandoned branches.

    A save is abandoned if some later-written save has a turn <= its turn, i.e. the
    player loaded an earlier point and replayed, so this save's future never happened.
    Ages restart turn numbering, so we compare (age order, turn).
    """
    ages = {"AGE_ANTIQUITY": 0, "AGE_EXPLORATION": 1, "AGE_MODERN": 2}
    key = lambda s: (ages.get(s.age or "", 9), s.turn or 0)  # noqa: E731
    ordered = sorted(saves, key=lambda s: (s.saved_at.timestamp() if s.saved_at else s.path.stat().st_mtime))
    entries = []
    for i, s in enumerate(ordered):
        later = ordered[i + 1 :]
        abandoned = any(key(t) <= key(s) and t.path != s.path for t in later)
        entries.append(TimelineEntry(s, abandoned))
    return entries


def current_game(root: Path = SAVE_DIR) -> tuple[str, list[TimelineEntry]]:
    """The game with the most recently written save, and its timeline."""
    infos = load_headers(find_saves(root))
    if not infos:
        raise FileNotFoundError(f"no .Civ7Save files under {root}")
    newest = max(infos, key=lambda s: s.saved_at.timestamp() if s.saved_at else 0)
    guid = newest.game_guid or "?"
    return guid, timeline(games(infos)[guid])


def head(entries: list[TimelineEntry]) -> SaveInfo:
    return entries[-1].info
