"""Civilizer: Civ VII game-state extraction for coaching."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import time

from . import export, ingest, live, logs, mapview, report, rules, save, store, timeline, verify

SNAPSHOTS = Path(__file__).resolve().parents[2] / "data" / "snapshots"


def _archive_snapshot(s: dict) -> Path:
    g = s.get("game") or {}
    d = SNAPSHOTS / str(g.get("seed", "unknown"))
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"T{g.get('turn', 0):04d}-{datetime.now():%Y%m%dT%H%M%S}.json"
    p.write_text(json.dumps(s, indent=1))
    return p


def cmd_status(a) -> int:
    try:
        page = live.game_page()
        print(f"live: in-game UI reachable ({page['url']})")
    except live.LiveError as e:
        print(f"live: unavailable — {e}")
    try:
        guid, tl = timeline.current_game()
        h = timeline.head(tl)
        print(f"saves: game {guid}, {len(tl)} saves, head = {h.path.name} (turn {h.turn}, {h.saved_at:%Y-%m-%d %H:%M})")
    except FileNotFoundError as e:
        print(f"saves: {e}")
    print(f"log sessions: {len(logs.sessions())} (archive: {logs.ARCHIVE})")
    return 0


def cmd_live(a) -> int:
    s = live.snapshot(detail=a.detail, omniscient=a.omniscient, map=a.map, victories=a.victories)
    if not a.no_archive:
        p = _archive_snapshot(s)
        print(f"<!-- snapshot archived: {p} -->", file=sys.stderr)
    print(json.dumps(s, indent=1) if a.json else report.live_report(s))
    return 0


def cmd_saves(a) -> int:
    infos = timeline.load_headers(timeline.find_saves())
    for guid, ss in timeline.games(infos).items():
        tl = timeline.timeline(ss)
        if not a.all and tl[-1].info.game_guid != timeline.current_game()[0]:
            continue
        i0 = tl[-1].info
        print(f"## game {guid} · {i0.host_leader} / {i0.host_civ} · {i0.difficulty}")
        for e in tl:
            i = e.info
            mark = "abandoned (reloaded past)" if e.abandoned else ""
            print(f"  {i.saved_at:%m-%d %H:%M}  T{i.turn:>3} {i.age.replace('AGE_', '')[:4]:4} {e.kind:6} {i.path.name:28} {mark}")
        print(f"  HEAD -> {tl[-1].info.path}")
    return 0


def _resolve_save(arg: str) -> Path:
    if arg == "head":
        return timeline.head(timeline.current_game()[1]).path
    return Path(arg).expanduser()


def cmd_save(a) -> int:
    p = _resolve_save(a.path)
    info = save.read_header(p)
    gold = infl = None
    if not a.header_only:
        body = save.decompress(p.read_bytes())
        if body:
            gold, infl = save.body_balances(body, "GOLD"), save.body_balances(body, "INFLUENCE")
            if a.dump_body:
                Path(a.dump_body).write_bytes(body)
                print(f"body ({len(body)} bytes) written to {a.dump_body}", file=sys.stderr)
    if a.json:
        d = info.to_dict()
        d["body_gold_by_player_id"], d["body_influence_by_player_id"] = gold, infl
        print(json.dumps(d, indent=1, default=str))
    else:
        print(report.save_report(info, gold, infl))
    return 0


def cmd_history(a) -> int:
    rows = logs.history(a.log, player=a.player if a.log.startswith("Player_") else None)
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print("no rows")
        return 0
    cols = a.cols.split(",") if a.cols else list(rows[0].keys())
    print("\t".join(cols))
    for r in rows[-a.last :] if a.last else rows:
        print("\t".join(str(r.get(c, "")) for c in cols))
    return 0


def cmd_archive_logs(a) -> int:
    print(logs.archive() or "no logs to archive")
    return 0


def cmd_report(a) -> int:
    """Best available view: live snapshot if the game is running, else head save + log history."""
    try:
        s = live.snapshot(detail=a.detail)
        _archive_snapshot(s)
        print(report.live_report(s))
        return 0
    except live.LiveError as e:
        print(f"_live game unavailable ({e}); falling back to latest save + logs_\n")
    guid, tl = timeline.current_game()
    h = timeline.head(tl)
    body = save.decompress(h.path.read_bytes())
    print(report.save_report(h, save.body_balances(body, "GOLD"), save.body_balances(body, "INFLUENCE")))
    # The save body's cities/units aren't decoded; reuse the live snapshot of the same state.
    snaps = _snapshots_for_turn(h.turn - 1) if h.turn else []
    if snaps:
        print(f"\n---\nFull detail from the archived live snapshot of this state ({snaps[0].name}):\n")
        print(report.live_report(json.loads(snaps[0].read_text())))
    else:
        print("\n_No archived live snapshot for this save's turn; run `civilizer live` while the game is open._")
    rows = logs.history("Player_Stats", player=0)[-8:]
    if rows:
        print("\n## Recent turns (Player_Stats log, you)")
        cols = ["turn", "cities", "towns", "settlement_cap", "gold_balance", "gold", "science", "culture", "happiness", "influence", "production"]
        print("| " + " | ".join(cols) + " |")
        print("|" + "---|" * len(cols))
        for r in rows:
            print("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return 0


def cmd_rules(a) -> int:
    hits = rules.lookup(a.query, max_types=a.max)
    if not hits:
        print("no match")
    for h in hits:
        print(f"## {h['name']}  ({h['type']})")
        if h["description"]:
            print(h["description"])
        for f, line in h["references"][: a.refs]:
            print(f"  {f}: {line[:400]}")
        print()
    return 0


def _snapshots_for_turn(turn: int, leader: str | None = None) -> list[Path]:
    """Archived snapshots of a given on-screen turn, oldest first."""
    out = []
    for p in sorted(SNAPSHOTS.glob(f"*/T{turn:04d}-*.json"), key=lambda p: p.name.split("-", 1)[1]):
        if leader is None or json.loads(p.read_text()).get("me", {}).get("leader") == leader:
            out.append(p)
    return out


def cmd_verify(a) -> int:
    if a.snapshot:
        s = json.loads(Path(a.snapshot).read_text())
    else:
        s = live.snapshot()
        _archive_snapshot(s)
        # Logs record turn-start values; mid-turn buys/placements change the live state,
        # so check the earliest snapshot of this turn against them.
        first = _snapshots_for_turn(s["game"]["turn"], s["me"].get("leader"))
        if first:
            s = json.loads(first[0].read_text())
    print(f"Snapshot turn {s['game']['turn']} captured {s.get('captured_at')} (logs = turn-start values)\n")
    print(verify.render(verify.check(s)))
    print(f"\nEmpire yield from non-settlement sources (yields report 'Other' row): {verify.other_sources(s)}")
    return 0


def _latest_snapshot(with_map: bool = False) -> dict:
    for p in sorted(SNAPSHOTS.glob("*/T*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        s = json.loads(p.read_text())
        if not with_map or s.get("map"):
            return s
    raise FileNotFoundError("no archived snapshot" + (" with a map; run `civilizer live --map`" if with_map else ""))


def cmd_map(a) -> int:
    if a.live:
        s = live.snapshot(map=True)
        _archive_snapshot(s)
    else:
        s = _latest_snapshot(with_map=True)
    print(f"(snapshot turn {s['game']['turn']}, {s.get('captured_at')})")
    print(mapview.render(s, a.where, a.radius))
    return 0


def cmd_watch(a) -> int:
    """Snapshot (with map) once per turn while the game runs; archive logs as they grow."""
    last = None
    print(f"watching every {a.interval}s; Ctrl-C to stop", file=sys.stderr)
    while True:
        try:
            turn = live.raw_eval("Game.turn")  # panel-system-bar.js reads Game.turn
            if turn != last:
                s = live.snapshot(detail=True, map=True)
                p = _archive_snapshot(s)
                logs.archive()
                print(f"{datetime.now():%H:%M:%S} turn {turn}: {p.name}", flush=True)
                last = turn
        except live.LiveError as e:
            if last is not None:
                logs.archive()
                print(f"{datetime.now():%H:%M:%S} game unavailable ({e}); logs archived", flush=True)
                last = None
        time.sleep(a.interval)


def cmd_capture(a) -> int:
    """Whole-game history from the loaded game (js/history.js), stored under data/history/<seed>/."""
    h = live.history()
    p = export.save_capture(h)
    g = h["game"]
    print(f"captured {g['age']} turn {g['turn']}: {p}")
    for e in h.get("errors") or []:
        print(f"  section error: {e}", file=sys.stderr)
    return 0


def cmd_export(a) -> int:
    out = export.export(seed=a.seed, out_root=Path(a.out))
    g = json.loads((out / "game.json").read_text())
    kinds: dict[str, int] = {}
    for e in g["events"]:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"exported {out}")
    print("  ages: " + ", ".join(f"{x['age']} {x['turns']}t" for x in g["ages"]))
    print("  events: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    print(f"  captures: {[(c['age'], c['turn']) for c in g['coverage']['captures']]}")
    return 0


def cmd_refresh(a) -> int:
    seed = a.seed or ingest.latest_seed()
    if not seed:
        print("no snapshots yet; run `civilizer live` with the game open", file=sys.stderr)
        return 1
    r = ingest.refresh(seed)
    print(f"store {store.DB}: game {r['game']} {r['age']} T{r['turn']} · {r['turns']} turns, {r['events']} events, {r['metrics']} metric rows")
    return 0


def cmd_schema(a) -> int:
    md = store.schema_markdown()
    if a.write:
        Path(a.write).write_text(md)
        print(f"wrote {a.write}")
    else:
        print(md)
    return 0


def cmd_monitor(a) -> int:
    from . import monitor

    return monitor.run(interval=a.interval, once=a.once) or 0


def cmd_up(a) -> int:
    from . import up

    return up.run(host=a.host, port=a.port, interval=a.interval, browser=not a.no_browser) or 0


def cmd_dashboard(a) -> int:
    from . import dashboard

    return dashboard.serve(host=a.host, port=a.port) or 0


def cmd_mcp(a) -> int:
    from . import mcp_server

    mcp_server.main()
    return 0


def cmd_advisor(a) -> int:
    from . import advisor_cli

    return advisor_cli.main(a.args) or 0


def cmd_pedia(a) -> int:
    from . import pedia

    return pedia.main(a.args) or 0


def cmd_eval(a) -> int:
    print(
        "WARNING: arbitrary JS in the live game. Unvalidated native calls can crash Civ VII.\n"
        "Only use call shapes that appear in the game's own UI code.",
        file=sys.stderr,
    )
    if not a.yes:
        print("refusing without --yes", file=sys.stderr)
        return 2
    print(json.dumps(live.raw_eval(a.expression), indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="civilizer", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="what sources are available").set_defaults(fn=cmd_status)

    p = sub.add_parser("live", help="snapshot the running game via the UI debugger")
    p.add_argument("--detail", action="store_true", help="include per-city yield tooltip breakdowns")
    p.add_argument("--omniscient", action="store_true", help="ignore fog of war / unmet players")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-archive", action="store_true")
    p.add_argument("--map", action="store_true", help="include every revealed plot (terrain/feature/resource/owner)")
    p.add_argument("--no-victories", dest="victories", action="store_false", help="skip GDP/Innovation/Tourism/Dominion points")
    p.set_defaults(fn=cmd_live)

    p = sub.add_parser("map", help="text map around a settlement name or x,y (from latest map snapshot)")
    p.add_argument("where")
    p.add_argument("--radius", type=int, default=4)
    p.add_argument("--live", action="store_true", help="take a fresh snapshot first")
    p.set_defaults(fn=cmd_map)

    p = sub.add_parser("watch", help="snapshot once per turn while the game runs (with map), archive logs")
    p.add_argument("--interval", type=int, default=20)
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("report", help="best available view (live, else save+logs)")
    p.add_argument("--detail", action="store_true")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("saves", help="save timeline with reload/branch detection")
    p.add_argument("--all", action="store_true", help="all games, not just the current one")
    p.set_defaults(fn=cmd_saves)

    p = sub.add_parser("save", help="read one save ('head' = newest save of current game)")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")
    p.add_argument("--header-only", action="store_true")
    p.add_argument("--dump-body", metavar="FILE", help="write the decompressed game database")
    p.set_defaults(fn=cmd_save)

    p = sub.add_parser("history", help="turn-by-turn history from archived + current logs")
    p.add_argument("--log", default="Player_Stats", help=f"one of: {', '.join(logs.HISTORY_LOGS)}")
    p.add_argument("--player", type=int, default=0)
    p.add_argument("--cols")
    p.add_argument("--last", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_history)

    sub.add_parser("archive-logs", help="copy current session logs aside (game wipes them on launch)").set_defaults(
        fn=cmd_archive_logs
    )

    p = sub.add_parser("rules", help="look up game rules by in-game name (text + data XML)")
    p.add_argument("query")
    p.add_argument("--max", type=int, default=3)
    p.add_argument("--refs", type=int, default=12)
    p.set_defaults(fn=cmd_rules)

    p = sub.add_parser("verify", help="cross-check a live snapshot against logs and the save file")
    p.add_argument("--snapshot", metavar="FILE", help="check an archived snapshot instead of taking one")
    p.set_defaults(fn=cmd_verify)

    sub.add_parser("capture", help="store whole-game history from the loaded game (run once per save to export)").set_defaults(
        fn=cmd_capture
    )

    p = sub.add_parser("export", help="whole-game timeline export (captures + snapshots + logs) to games/<seed>/")
    p.add_argument("--seed", help="game seed (default: newest capture)")
    p.add_argument("--out", default=str(export.GAMES))
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("refresh", help="export + ingest everything into the central store (data/civilizer.db)")
    p.add_argument("--seed", help="game seed (default: newest snapshot dir)")
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("schema", help="print the store's table/column/metric documentation (markdown)")
    p.add_argument("--write", metavar="FILE", help="write to a file instead (e.g. docs/SCHEMA.md)")
    p.set_defaults(fn=cmd_schema)

    p = sub.add_parser("up", help="start a play session: dashboard + monitor in one process (reuses running ones)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8777)
    p.add_argument("--interval", type=int, default=15, help="seconds between turn checks")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_up)

    p = sub.add_parser("monitor", help="per-turn live snapshot -> logs archive -> store refresh, while the game runs")
    p.add_argument("--interval", type=int, default=15, help="seconds between turn checks")
    p.add_argument("--once", action="store_true", help="one check + refresh, then exit")
    p.set_defaults(fn=cmd_monitor)

    p = sub.add_parser("dashboard", help="web dashboard over the store")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8777)
    p.set_defaults(fn=cmd_dashboard)

    sub.add_parser("mcp", help="MCP stdio server over the store (advisor tools)").set_defaults(fn=cmd_mcp)

    p = sub.add_parser("advisor", help="advisor profiles, strategies, goals, annotations, briefing (see `advisor --help`)")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_advisor)

    p = sub.add_parser("pedia", help="search the game's Civilopedia (see `pedia --help`)")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_pedia)

    p = sub.add_parser("eval", help="DEV: evaluate JS in the live game (can crash it)")
    p.add_argument("expression")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_eval)

    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("advisor", "pedia"):  # REMAINDER can't start with a flag; hand the rest over verbatim
        a = argparse.Namespace(args=argv[1:], fn=cmd_advisor if argv[0] == "advisor" else cmd_pedia)
    else:
        a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (live.LiveError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
