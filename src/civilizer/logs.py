"""Civ VII per-session CSV logs (My Games/.../Logs) as turn-by-turn history.

The game truncates Logs/ at every launch, so `archive()` copies them aside and
`history()` merges every archived session plus the live directory. Where a turn
appears more than once (reloads), the last write wins: that is the branch the
player kept.
"""

from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path

from .timeline import LOG_DIR

ARCHIVE = Path(__file__).resolve().parents[2] / "data" / "archive" / "logs"

# Logs worth keeping as history (the rest are AI/engine internals or huge).
HISTORY_LOGS = [
    "Player_Stats", "Player_Treasury", "Player_Happiness", "Player_Legacies", "Player_Unlocks",
    "DiplomacySummary", "Game_TradeManager", "CityBuildQueue", "CityPurchase", "CombatLog",
    "Game_Religion", "Historian", "Game_PlayerScores", "AI_Victories", "Balance_Happiness",
    "Game_Gossip", "Game_RandomEvents", "Game_Greatworks", "Independents",
]

# Player_Stats has more data columns than header names; these are the real names
# (verified against Player_Treasury / Player_Happiness for the same turn).
PLAYER_STATS_COLS = [
    "turn", "player", "cities", "towns", "settlement_cap", "over_cap", "urban_pop", "rural_pop",
    "techs", "land_units", "naval_units", "tiles_owned", "tiles_improved", "gold_balance",
    "science", "culture", "gold", "production", "food", "happiness", "influence", "by_type_buildings",
]


def archive(src: Path = LOG_DIR, dest_root: Path = ARCHIVE) -> Path | None:
    """Copy the current session's logs aside, keyed by the session's last write time."""
    stats = src / "Player_Stats.csv"
    if not stats.exists():
        return None
    stamp = datetime.fromtimestamp(stats.stat().st_mtime).strftime("%Y%m%dT%H%M%S")
    dest = dest_root / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for name in HISTORY_LOGS:
        f = src / f"{name}.csv"
        if f.exists():
            shutil.copy2(f, dest / f.name)
    prune_superseded(dest_root)
    return dest


def _supersedes(new: Path, old: Path) -> bool:
    """Every log in `old` is a byte-prefix of the same log in `new`: the same session, archived earlier."""
    files = list(old.glob("*.csv"))
    if not files or not (old / "Player_Stats.csv").exists():
        return False
    for f in files:
        n = new / f.name
        if not n.exists() or n.stat().st_size < f.stat().st_size:
            return False
        with f.open("rb") as a, n.open("rb") as b:
            if a.read() != b.read(f.stat().st_size):
                return False
    return True


def prune_superseded(dest_root: Path = ARCHIVE) -> list[Path]:
    """Delete archive dirs fully contained in the next one. The monitor archives every turn, and each copy
    of a still-running session repeats the previous one; export already skips those (export._sessions)."""
    dirs = sorted(d for d in dest_root.iterdir() if d.is_dir()) if dest_root.exists() else []
    gone = []
    for old, new in zip(dirs, dirs[1:]):
        if _supersedes(new, old):
            shutil.rmtree(old)
            gone.append(old)
    return gone


def _read(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        return [[c.strip() for c in row] for row in csv.reader(fh) if row]


def _comma_names() -> list[list[str]]:
    """Display names containing commas (e.g. "Washington, D.C."), split as the CSV reader would."""
    from .rules import text_index

    names = {v for k, v in text_index().items() if k.startswith("LOC_CITY_NAME_") and "," in v}
    return [[c.strip() for c in n.split(",")] for n in names]


def _rejoin(row: list[str], width: int, names: list[list[str]]) -> list[str]:
    """The game writes unquoted names, so "Washington, D.C." becomes two fields. Merge them back."""
    i = 0
    while len(row) > width and i < len(row):
        for parts in names:
            n = len(parts)
            if row[i : i + n] == parts:
                row[i : i + n] = [", ".join(parts)]
                break
        i += 1
    return row


def read_table(path: Path) -> list[dict]:
    rows = _read(path)
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    if path.stem == "Player_Stats":
        header = PLAYER_STATS_COLS
    names = None
    out = []
    for r in rows[1:]:
        if len(r) > len(header) and path.stem != "Player_Stats":
            names = names if names is not None else _comma_names()
            r = _rejoin(r, len(header), names)
        d = {header[i] if i < len(header) else f"col{i}": v for i, v in enumerate(r)}
        out.append(d)
    return out


def sessions(include_live: bool = True) -> list[Path]:
    dirs = sorted(p for p in ARCHIVE.glob("*") if p.is_dir()) if ARCHIVE.exists() else []
    if include_live and LOG_DIR.exists():
        dirs.append(LOG_DIR)
    return dirs


def _num(v: str) -> float | str:
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except ValueError:
        return v


def history(name: str, player: int | None = None) -> list[dict]:
    """Merged rows of one log across all sessions, deduped by (turn, player, ...)."""
    merged: dict[tuple, dict] = {}
    plain: list[dict] = []
    for d in sessions():
        f = d / f"{name}.csv"
        if not f.exists():
            continue
        for row in read_table(f):
            row = {k: _num(v) for k, v in row.items()}
            turn = row.get("turn", row.get("Game Turn", row.get("Turn")))
            pl = row.get("player", row.get("Player"))
            if player is not None and pl != player:
                continue
            if name in ("Player_Stats", "Player_Treasury", "Player_Happiness"):
                merged[(turn, pl)] = row
            else:
                plain.append(row)
    if merged:
        return [merged[k] for k in sorted(merged, key=lambda k: (k[0], k[1]))]
    # event logs: drop exact duplicates that come from the same session archived twice
    seen, out = set(), []
    for r in plain:
        k = tuple(sorted(r.items()))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out
