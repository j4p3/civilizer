"""Civ VII .Civ7Save reader.

Format (credit: pydt/civ7-save-parser REVERSING.md, iqqmuT/civ7-save-editor):

    "CIV7" u32 version, then uncompressed groups of typed chunks, then the
    zlib-compressed game state as [u32 blockLen][deflate bytes]... blocks.

Each chunk is [u32 marker][u32 type][u32 pad][payload]. Markers are stable
FNV-style hashes of field names (shared with Civ VI for game-level fields).
The uncompressed header carries metadata (turn, age, settings, roster). The
compressed body is the full game database; only a few fields of it are mapped.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

COMPRESSED_DATA_START = bytes([0x00, 0x00, 0x01, 0x00, 0x78, 0x9C])
FXSBLKED = b"FXSBLKED"


def _m(b: list[int]) -> bytes:
    return bytes(b)


MARKERS = {
    "GAME_TURN": _m([0x9D, 0x2C, 0xE6, 0xBD]),
    "GAME_AGE": _m([0x84, 0x84, 0xC6, 0xD0]),
    "GAME_GUID": _m([0xD8, 0x40, 0xE5, 0xF4]),
    "GAMESPEED": _m([0x99, 0xB0, 0xD9, 0x05]),
    "MAPSIZE": _m([0x40, 0x5C, 0x83, 0x0B]),
    "DIFFICULTY": _m([0xA8, 0x16, 0xB4, 0x7F]),
    "RULESET": _m([0xDE, 0x25, 0x59, 0xC4]),
    "SAVE_NAME": _m([0xB0, 0x13, 0x4C, 0x1E]),
    "SAVE_TIMESTAMP": _m([0x64, 0x6D, 0xFE, 0x0E]),
    "APP_VERSION": _m([0xDA, 0x45, 0x05, 0x3A]),
    "HOST_LEADER": _m([0xA1, 0xB2, 0xB7, 0x6B]),
    "HOST_CIVILIZATION": _m([0xBB, 0xE4, 0x9A, 0xAF]),
    "MAP_DISPLAY_NAME": _m([0x27, 0x60, 0x4C, 0x58]),
    "ENABLED_MODS": _m([0x5C, 0xAE, 0x27, 0x84]),
    "MOD_ID": _m([0x76, 0x61, 0x2F, 0xE5]),
    "MOD_DISPLAY_NAME": _m([0x98, 0x26, 0x0B, 0xEA]),
    "MOD_ENABLED": _m([0x8C, 0xCE, 0x87, 0x4D]),
    # group3 per-player records
    "LEADER": _m([0x0F, 0xFB, 0x8C, 0xC1]),
    "CIVILIZATION": _m([0x76, 0x97, 0x40, 0xDE]),
    "CIV_LEVEL": _m([0x7A, 0x9C, 0xA0, 0x19]),
    "PLAYER_TYPE": _m([0xD4, 0x5F, 0x83, 0x28]),
    "TEAM_ID": _m([0xDE, 0xF6, 0x2D, 0x9B]),
    "ALIVE_FLAGS": _m([0x0E, 0xED, 0x6E, 0x29]),
    # compressed body (iqqmuT/civ7-save-editor)
    "GOLD": _m([0x23, 0x1E, 0x99, 0x37]),
    "INFLUENCE": _m([0x50, 0x3C, 0xA8, 0x4A]),
}
_NAME_BY_MARKER = {v: k for k, v in MARKERS.items()}

PLAYER_SLOT_MARKERS = [
    bytes.fromhex(h)
    for h in (
        "b861f0f4 2e51f783 9400fe1a 0230f96d a1a59df3 37959a84 "
        "8dc4931d 1bf4946a 8ae92bfa 1cd92c8d 32ca8cfa a4fa8b8d"
    ).split()
]

T_FLAGS12, T_UTF8, T_UTF16, T_NUM32 = 1, 2, 3, 8
T_ARRAY, T_NESTED, T_BYTES = 29, 30, 32


@dataclass
class Chunk:
    offset: int
    end: int
    marker: bytes
    type: int
    value: object

    @property
    def name(self) -> str:
        return _NAME_BY_MARKER.get(self.marker, self.marker.hex())


def _u16(d: bytes, o: int) -> int:
    return struct.unpack_from("<H", d, o)[0]


def _u32(d: bytes, o: int) -> int:
    return struct.unpack_from("<I", d, o)[0]


def parse_chunk(d: bytes, off: int, has_marker: bool = True) -> Chunk:
    marker = d[off : off + 4] if has_marker else b""
    typ = _u32(d, off + 4 if has_marker else off)
    ds = off + 12 if has_marker else off + 8
    if typ in (T_FLAGS12, 12):
        return Chunk(off, ds + 12, marker, typ, d[ds : ds + 12])
    if typ == 9:
        n = _u16(d, ds)
        return Chunk(off, ds + 8 + n * 4, marker, typ, d[ds + 8 : ds + 8 + n * 4])
    if typ in (10, 11, 17):
        n = _u16(d, ds)
        return Chunk(off, ds + 8 + n * 8, marker, typ, d[ds + 4 : ds + 8 + n * 8])
    if typ == T_NUM32:
        return Chunk(off, ds + 12, marker, typ, _u32(d, ds + 8))
    if typ == T_UTF8:
        n = _u16(d, ds)
        return Chunk(off, ds + 8 + n, marker, typ, d[ds + 8 : ds + 8 + n - 1].decode("utf-8", "replace"))
    if typ == T_UTF16:
        n = _u16(d, ds)
        raw = d[ds + 8 : ds + 8 + n * 2 - 2]
        return Chunk(off, ds + 8 + n * 2, marker, typ, raw.decode("utf-16-le", "replace"))
    if typ == T_ARRAY:
        subs = read_n(d, ds + 12, _u32(d, ds + 8))
        return Chunk(off, subs[-1].end if subs else ds + 12, marker, typ, subs)
    if typ == T_NESTED:
        n = _u32(d, ds + 8)
        items, end = [], ds + 12
        for _ in range(n):
            item = parse_chunk(d, end, has_marker=False)
            items.append(item.value if item.type == T_ARRAY else [item])
            end = item.end
        return Chunk(off, end, marker, typ, items)
    if typ == T_BYTES:
        n = _u32(d, ds + 4)
        return Chunk(off, ds + 8 + n, marker, typ, d[ds + 8 : ds + 8 + n])
    raise ValueError(f"unknown chunk type {typ} at offset {off}")


def read_n(d: bytes, off: int, n: int) -> list[Chunk]:
    out: list[Chunk] = []
    for _ in range(n):
        c = parse_chunk(d, out[-1].end if out else off)
        out.append(c)
    return out


def _find(chunks: list[Chunk], key: str) -> Chunk | None:
    m = MARKERS[key]
    return next((c for c in chunks if c.marker == m), None)


def decompress(data: bytes) -> bytes | None:
    """Inflate the length-prefixed zlib block stream holding the game database."""
    start = data.find(COMPRESSED_DATA_START)
    if start < 0:
        return None
    parts, pos = [], start
    n = _u32(data, pos)
    pos += 4
    while n > 1 and pos + n <= len(data):
        parts.append(data[pos : pos + n])
        pos += n
        if pos + 4 > len(data):
            break
        n = _u32(data, pos)
        pos += 4
    z = zlib.decompressobj()
    return z.decompress(b"".join(parts)) + z.flush(zlib.Z_SYNC_FLUSH)


@dataclass
class SavePlayer:
    slot: int | None
    leader: str
    civilization: str
    level: str
    is_human: bool
    team: int | None
    alive: bool | None


@dataclass
class SaveInfo:
    path: Path
    size: int
    turn: int | None = None
    age: str | None = None
    game_guid: str | None = None
    save_name: str | None = None
    saved_at: datetime | None = None
    game_speed: str | None = None
    map_size: str | None = None
    difficulty: str | None = None
    ruleset: str | None = None
    app_version: str | None = None
    host_leader: str | None = None
    host_civ: str | None = None
    map_name: str | None = None
    mods: list[dict] = field(default_factory=list)
    players: list[SavePlayer] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("players", "path")}
        d["path"] = str(self.path)
        d["saved_at"] = self.saved_at.isoformat() if self.saved_at else None
        d["players"] = [p.__dict__ for p in self.players]
        return d


def _en_text(v: object) -> object:
    """Display names are stored as {"LOC_KEY": [{"locale": .., "text": ..}, ...]}."""
    if isinstance(v, str) and v.startswith("{"):
        try:
            entries = next(iter(json.loads(v).values()))
            return next((e["text"] for e in entries if e.get("locale") == "en_US"), entries[0]["text"])
        except (ValueError, StopIteration, KeyError, IndexError, TypeError):
            pass
    return v


def read_header(path: str | Path) -> SaveInfo:
    """Parse the uncompressed header: metadata and player roster. Fast (no inflate)."""
    path = Path(path)
    d = path.read_bytes()
    if d[:4] != b"CIV7":
        raise ValueError(f"{path} is not a Civ7 save")
    info = SaveInfo(path=path, size=len(d))

    g1 = read_n(d, 12, _u32(d, 8))
    off = g1[-1].end
    g2 = read_n(d, off + 12, _u32(d, off + 8))
    off = g2[-1].end if g2 else off
    g3 = read_n(d, off + 8, _u32(d, off + 4))

    def s(key: str) -> object:
        c = _find(g1, key)
        return c.value if c else None

    info.turn = s("GAME_TURN")
    info.age = s("GAME_AGE")
    info.game_guid = s("GAME_GUID")
    info.save_name = s("SAVE_NAME")
    info.game_speed = s("GAMESPEED")
    info.map_size = s("MAPSIZE")
    info.difficulty = s("DIFFICULTY")
    info.ruleset = s("RULESET")
    info.app_version = s("APP_VERSION")
    info.host_leader = s("HOST_LEADER")
    info.host_civ = s("HOST_CIVILIZATION")
    info.map_name = _en_text(s("MAP_DISPLAY_NAME"))
    ts = _find(g1, "SAVE_TIMESTAMP")
    if ts and isinstance(ts.value, bytes) and len(ts.value) >= 12:
        epoch = struct.unpack_from("<Q", ts.value, 4)[0]  # [u32 count=8][u64 unix epoch]
        if 1_500_000_000 < epoch < 4_000_000_000:
            info.saved_at = datetime.fromtimestamp(epoch, timezone.utc)
    mods = _find(g1, "ENABLED_MODS")
    if mods and mods.type == T_NESTED:
        for rec in mods.value:
            mid = _find(rec, "MOD_ID")
            if mid and isinstance(mid.value, str):
                name = _find(rec, "MOD_DISPLAY_NAME")
                en = _find(rec, "MOD_ENABLED")
                info.mods.append(
                    {"id": mid.value, "name": name.value if name else "", "enabled": en.value == "1" if en else None}
                )

    for rec in g3:
        if rec.type != T_ARRAY:
            continue
        leader, civ, lvl = _find(rec.value, "LEADER"), _find(rec.value, "CIVILIZATION"), _find(rec.value, "CIV_LEVEL")
        if not (leader and leader.value and civ and civ.value):
            continue
        ptype, team, alive = _find(rec.value, "PLAYER_TYPE"), _find(rec.value, "TEAM_ID"), _find(rec.value, "ALIVE_FLAGS")
        info.players.append(
            SavePlayer(
                slot=PLAYER_SLOT_MARKERS.index(rec.marker) if rec.marker in PLAYER_SLOT_MARKERS else None,
                leader=leader.value,
                civilization=civ.value,
                level=lvl.value if lvl else "",
                is_human=bool(ptype and ptype.value == 3),
                team=team.value if team else None,
                alive=_u32(alive.value, 8) == 1 if alive and len(alive.value) >= 12 else None,
            )
        )
    return info


def _read_24bit(body: bytes, pos: int) -> int:
    v = _u32(body, pos)
    return (v >> 8) + (1 if (v & 0xFF) == 0xFF else 0)


def body_balances(body: bytes, marker_key: str) -> list[int]:
    """Values following each GOLD/INFLUENCE marker (one per player entity, in body order).

    Uses the civ7-save-editor navigation: marker -> next FXSBLKED -> +8 -> 24-bit value.
    Player order matches the body's player-record order, not necessarily slot ids.
    """
    marker = MARKERS[marker_key]
    out, pos = [], 0
    while (pos := body.find(marker, pos)) >= 0:
        blk = body.find(FXSBLKED, pos)
        if blk < 0:
            break
        out.append(_read_24bit(body, blk + 8))
        pos += 4
    return out


def body_strings(body: bytes, min_len: int = 6) -> list[str]:
    """Printable ASCII identifiers in the body (LOC_* keys, TYPE names, city names...)."""
    out, cur = [], bytearray()
    for b in body:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= min_len:
                out.append(cur.decode())
            cur.clear()
    return out
