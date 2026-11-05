"""Text rendering of the snapshot's revealed map around a point."""

from __future__ import annotations

ABBR = {
    "TERRAIN_OCEAN": "~~", "TERRAIN_COAST": "~ ", "TERRAIN_MOUNTAIN": "^^", "TERRAIN_HILL": "h ",
    "TERRAIN_FLAT": ". ", "TERRAIN_NAVIGABLE_RIVER": "r~",
}


def _plots(snap: dict) -> dict[tuple[int, int], dict]:
    m = snap["map"]
    lg, cols = m["legend"], m["columns"]
    out = {}
    for row in m["plots"]:
        d = dict(zip(cols, row))
        pick = lambda key, i: lg[key][i] if i >= 0 else None  # noqa: E731
        d["terrain"], d["feature"], d["resource"] = pick("terrain", d["terrain"]), pick("feature", d["feature"]), pick("resource", d["resource"])
        out[(d["x"], d["y"])] = d
    return out


def find_center(snap: dict, where: str) -> tuple[int, int]:
    if "," in where:
        x, y = where.split(",")
        return int(x), int(y)
    for c in (snap.get("settlements") or []) + (snap.get("known_foreign_settlements") or []):
        if c["name"].lower().startswith(where.lower()):
            return c["x"], c["y"]
    raise ValueError(f"no settlement named {where!r} in snapshot")


def render(snap: dict, where: str, radius: int = 4) -> str:
    if not snap.get("map"):
        return "snapshot has no map; take one with `civilizer live --map`"
    plots = _plots(snap)
    cx, cy = find_center(snap, where)
    owners = snap["map"]["legend"]["owner"]
    me = (snap.get("me") or {}).get("id")
    marks = {}
    for c in snap.get("settlements") or []:
        marks[(c["x"], c["y"])] = "@" + c["name"][:1].upper()
    for c in snap.get("known_foreign_settlements") or []:
        marks[(c["x"], c["y"])] = "#" + c["name"][:1].upper()
    for u in snap.get("units") or []:
        if u.get("x") is not None:
            marks.setdefault((u["x"], u["y"]), "u ")
    for u in snap.get("visible_enemy_units") or []:
        marks[(u["x"], u["y"])] = "E!"
    lines = [f"Map around {where} ({cx},{cy}), radius {radius}. North is up (y grows northward); rows are offset hex rows, odd rows shifted right.",
             "@X your settlement, #X foreign, u your unit, E! visible enemy, ~~ ocean, ~ coast, ^^ mountain, h hill, . flat, ?? unrevealed; owner suffix: * you, digit = player id mod 10",
             ""]
    detail = []
    for y in range(cy + radius, cy - radius - 1, -1):  # game y grows northward: print north first
        row = [" " if y % 2 == 0 else "   "]
        for x in range(cx - radius, cx + radius + 1):
            p = plots.get((x, y))
            if not p:
                row.append("??  ")
                continue
            cell = marks.get((x, y)) or ABBR.get(p["terrain"], p["terrain"][8:10].lower() if p["terrain"] else "??")
            own = "*" if p["owner"] == me else (str(p["owner"] % 10) if p["owner"] >= 0 else " ")
            row.append(f"{cell}{own} ")
            if p["resource"] or (p["feature"] and abs(x - cx) <= 2 and abs(y - cy) <= 2):
                who = "you" if p["owner"] == me else owners.get(str(p["owner"]), owners.get(p["owner"], "-")) if p["owner"] >= 0 else "unowned"
                detail.append(f"({x},{y}) {p['terrain'] or '?'} {p['feature'] or ''} {p['resource'] or ''} owner={who}{' visible' if p['vis'] == 2 else ''}")
        lines.append("".join(row))
    if detail:
        lines += ["", "Resources and features nearby:"] + [f"- {d}" for d in detail]
    return "\n".join(lines)
