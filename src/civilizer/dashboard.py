"""Web dashboard over the central store (data/civilizer.db): `civilizer dashboard [--host] [--port]`.

Stdlib only. Every request opens its own read-only SQLite connection, so the dashboard never blocks or
writes to the store while the monitor appends turns; the page polls /api/version and refetches when the
token changes. The UI (web/index.html, app.js, style.css) is vanilla JS on the Foolscap design system
(github.com/j4p3/foolscap): its tokens, components and fonts, and its Observable Plot helper for figures, are
vendored under web/foolscap/ by scripts/vendor-foolscap.sh, so the page needs no CDN and works offline. Narrative prose is never generated here: the Story view shows the phases' facts and the
advisor's annotations (Claude writes those), with an empty state until they exist.

Endpoints (all GET, all JSON, optional ?game=<seed>, default the most recently refreshed game):
  /api/games                   every game in the store, newest first, for the home screen; monitor status
  /api/version                 cheap change token (refresh time, monitor heartbeat, advisor-table writes)
  /api/game                    game, ages, players, metric catalog, observed turns, monitor status
  /api/overview                stat tiles with recent trend, victory standings, active wars, phase outcomes
  /api/phases                  phase fact sheets + their annotations + effective outcome, intention notes
  /api/timeline?local=1&kinds=a,b&from=&to=   events for the swimlanes
  /api/metrics?metric=m&players=0,1&vis=visible|all   one series per player
  /api/turn/<g>                one turn: events, metric deltas vs previous observed turn, summary, notes
  /api/advisor                 profiles (with history), plan tree with progress, annotation log
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import store

WEB = Path(__file__).resolve().parent / "web"
MAJORS_MAX = 12  # palette slots: one fixed Foolscap chart color per major player id 0..11

TILE_METRICS = ["gold_per_turn", "science_per_turn", "culture_per_turn", "production_per_turn", "food_per_turn",
                "happiness_per_turn", "influence_per_turn", "gold_balance", "settlements", "population",
                "military_units", "trade_routes"]
PATHS = ["economic", "science", "culture", "military"]
DELTA_METRICS_SKIP = {"age_progress"}


class NotFound(Exception):
    pass


# ---------------------------------------------------------------- db helpers

def connect_ro(db: Path) -> sqlite3.Connection:
    """Read-only: the exporter and advisor own every write; a stray write from here would race the monitor."""
    if not Path(db).exists():
        raise NotFound(f"no store at {db}; run `civilizer refresh` or `civilizer monitor`")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _j(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _game(con, q: dict) -> sqlite3.Row | None:
    gid = q.get("game")
    if gid:
        return con.execute("SELECT * FROM games WHERE game=?", (gid,)).fetchone()
    return con.execute("SELECT * FROM games ORDER BY updated_at DESC LIMIT 1").fetchone()


def _need_game(con, q: dict) -> sqlite3.Row:
    gr = _game(con, q)
    if not gr:
        raise NotFound("no game in the store yet")
    return gr


def _event(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["players"], d["detail"] = _j(d["players"], []), _j(d["detail"], {})
    return d


def _ages(con, game: str, gr: sqlite3.Row) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM ages WHERE game=? ORDER BY idx", (game,)):
        a = dict(r)
        a["start_g"], a["end_g"] = a["g_offset"] + 1, a["g_offset"] + a["turns"]
        a["current"] = a["age"] == gr["current_age"]
        a["turns_max"] = gr["age_turns_max"] if a["current"] else a["turns"]
        out.append(a)
    return out


def _at(ages: list[dict], g: int) -> tuple[str | None, int | None]:
    for a in reversed(ages):
        if g > a["g_offset"]:
            return a["age"], g - a["g_offset"]
    return None, None


def _players(con, game: str) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM players WHERE game=? ORDER BY player_id", (game,)):
        d = dict(r)
        d["civilizations"] = _j(d["civilizations"], {})
        d["slot"] = d["player_id"] if 0 <= d["player_id"] < MAJORS_MAX else None
        out.append(d)
    return out


def _monitor(con) -> dict:
    """Monitor heartbeat with its age in seconds, so the header can tell a quiet game from a dead monitor."""
    hb = _j(store.get_meta(con, "monitor"), None) if "meta" in _tables(con) else None
    out = dict(hb) if isinstance(hb, dict) else {"state": "absent"}
    try:
        out["age_s"] = (datetime.now() - datetime.fromisoformat(out["seen_at"])).total_seconds() if out.get("seen_at") else None
    except (TypeError, ValueError):
        out["age_s"] = None
    interval = out.get("interval") or 10
    out["stale"] = out.get("state") == "watching" and (out["age_s"] is None or out["age_s"] > max(60, 3 * interval))
    out["last_refresh"] = _j(store.get_meta(con, "last_refresh"), None) if "meta" in _tables(con) else None
    return out


def _outcome(con, gr: sqlite3.Row) -> dict:
    """Over (someone won a victory), in progress, or unknown (no snapshot with victory data yet), with winner names."""
    d = dict(gr)
    names = {r[0]: r[1] for r in con.execute("SELECT player_id, leader FROM players WHERE game=?", (gr["game"],))}
    wins = _j(d.get("victories"), []) or []
    return {"status": d.get("status") or "unknown",
            "victories": [{"victory": v.get("victory"), "players": [{"id": p, "leader": names.get(p)} for p in v.get("players") or []]}
                          for v in wins]}


def _latest(con, game: str, metric: str, pid: int, g: int, lo: int = -10**9, vis: str = "all") -> tuple[float | None, int | None]:
    sql = "SELECT value, g FROM metrics WHERE game=? AND metric=? AND player_id=? AND g BETWEEN ? AND ?"
    if vis == "visible":
        sql += " AND visibility='visible'"
    r = con.execute(sql + " ORDER BY g DESC LIMIT 1", (game, metric, pid, lo, g)).fetchone()
    return (r[0], r[1]) if r else (None, None)


# ---------------------------------------------------------------- endpoints

def api_version(con, q: dict) -> dict:
    gr = _game(con, q)
    # The heartbeat is left out on purpose: it ticks every poll and would refetch everything with no new data.
    parts = [gr["updated_at"] if gr else None, gr["current_g"] if gr else None, store.get_meta(con, "last_refresh")]
    tabs = _tables(con)
    for t, col in (("annotations", "annotation_id"), ("goals", "goal_id"), ("strategies", "strategy_id"),
                   ("advisor_profiles", "version"), ("player_profiles", "version"), ("game_profiles", "seq"),
                   ("goal_metrics", "goal_id")):
        if t in tabs:
            parts.append(tuple(con.execute(f"SELECT count(*), max({col}) FROM {t}").fetchone()))
    if "goals" in tabs:
        parts.append(con.execute("SELECT group_concat(status || coalesce(resolved_g,'')) FROM goals").fetchone()[0])
    if "strategies" in tabs:
        parts.append(con.execute("SELECT group_concat(status) FROM strategies").fetchone()[0])
    data_token = hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:16]
    return {"token": data_token, "game": gr["game"] if gr else None, "current_g": gr["current_g"] if gr else None,
            "updated_at": gr["updated_at"] if gr else None, "monitor": _monitor(con)}


def api_games(con, q: dict) -> dict:
    mon = _monitor(con)
    out = []
    for r in con.execute("SELECT * FROM games ORDER BY updated_at DESC"):
        d, game = dict(r), r["game"]
        me = con.execute("SELECT leader, civilizations FROM players WHERE game=? AND player_id=?", (game, r["local_player_id"])).fetchone()
        d["leader_name"] = me["leader"] if me else None
        d["civilizations"] = _j(me["civilizations"], {}) if me else {}
        d["turns_observed"], d["first_g"] = con.execute("SELECT count(*), min(g) FROM turns WHERE game=?", (game,)).fetchone()
        d["ages"] = [a[0] for a in con.execute("SELECT age FROM ages WHERE game=? ORDER BY idx", (game,))]
        d["watching"] = mon.get("state") == "watching" and not mon["stale"] and str(mon.get("game")) == game
        d["outcome"] = _outcome(con, r)
        out.append(d)
    return {"games": out, "monitor": mon}


def api_game(con, q: dict) -> dict:
    gr = _game(con, q)
    if not gr:
        return {"game": None, "monitor": _monitor(con), "games": []}
    game = gr["game"]
    ages = _ages(con, game, gr)
    summary = con.execute("SELECT g, summary FROM turns WHERE game=? ORDER BY g DESC LIMIT 1", (game,)).fetchone()
    return {
        "game": dict(gr) | {"outcome": _outcome(con, gr)},
        "games": [dict(r) for r in con.execute("SELECT game, leader, civilization, current_age, current_g, updated_at FROM games "
                                               "ORDER BY updated_at DESC")],
        "ages": ages,
        "players": _players(con, game),
        "catalog": [dict(r) for r in con.execute("SELECT * FROM metric_catalog ORDER BY rowid")],
        "observed_g": [r[0] for r in con.execute("SELECT g FROM turns WHERE game=? ORDER BY g", (game,))],
        "first_g": con.execute("SELECT min(g) FROM metrics WHERE game=?", (game,)).fetchone()[0],
        "summary": _j(summary["summary"], {}) if summary else None,
        "summary_g": summary["g"] if summary else None,
        "monitor": _monitor(con),
    }


def _standings(con, game: str, cg: int, majors: list[int], local: int, vis: str) -> dict:
    out = {}
    for path in PATHS + ["score"]:
        m = path if path == "score" else f"vp_{path}"
        rows = []
        for p in majors:
            v, vg = _latest(con, game, m, p, cg, cg - 3, vis)
            if v is not None:
                rows.append({"player_id": p, "value": v, "g": vg})
        if not rows:
            continue
        rows.sort(key=lambda r: -r["value"])
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        target = _latest(con, game, f"{m}_target", local, cg, cg - 3)[0] if path != "score" else None
        me = next((r for r in rows if r["player_id"] == local), None)
        out[path] = {"metric": m, "target": target, "rows": rows, "rank": me["rank"] if me else None, "of": len(rows),
                     "value": me["value"] if me else None, "leader": rows[0]}
    return out


def _combat(con, game: str, a: int, b: int, lo: int, hi: int) -> tuple[int, int]:
    """(units a killed of b, units a lost to b) in [lo, hi]."""
    kills = losses = 0
    for r in con.execute("SELECT detail FROM events WHERE game=? AND kind='unit_killed' AND start_g BETWEEN ? AND ?", (game, lo, hi)):
        d = _j(r[0], {})
        if d.get("killer") == a and d.get("victim") == b:
            kills += 1
        elif d.get("victim") == a and d.get("killer") == b:
            losses += 1
    return kills, losses


def api_overview(con, q: dict) -> dict:
    gr = _need_game(con, q)
    game, cg, me = gr["game"], gr["current_g"], gr["local_player_id"]
    vis = "all" if q.get("vis") == "all" else "visible"
    cat = {r["metric"]: dict(r) for r in con.execute("SELECT * FROM metric_catalog")}
    majors = [p["player_id"] for p in _players(con, game)]
    tiles = []
    for m in TILE_METRICS:
        pts = [(r[0], r[1]) for r in con.execute("SELECT g, value FROM metrics WHERE game=? AND metric=? AND player_id=? AND g<=? "
                                                 "ORDER BY g DESC LIMIT 30", (game, m, me, cg))][::-1]
        if not pts:
            continue
        g_now, v_now = pts[-1]
        base = next(((g, v) for g, v in reversed(pts) if g <= g_now - 10), pts[0])
        ranked = []
        for p in majors:
            v, _ = _latest(con, game, m, p, cg, cg - 3, vis)
            if v is not None:
                ranked.append((p, v))
        ranked.sort(key=lambda kv: -kv[1])
        rank = next((i + 1 for i, (p, _) in enumerate(ranked) if p == me), None)
        tiles.append({"metric": m, "label": cat.get(m, {}).get("label", m), "unit": cat.get(m, {}).get("unit"),
                      "higher_is_better": cat.get(m, {}).get("higher_is_better"), "value": v_now, "g": g_now,
                      "delta": v_now - base[1] if base[0] != g_now else None, "delta_vs_g": base[0] if base[0] != g_now else None,
                      "rank": rank, "of": len(ranked), "trend": pts})
    if (s := next((t for t in tiles if t["metric"] == "settlements"), None)):
        s["cap"] = _latest(con, game, "settlement_cap", me, cg)[0]
    wars = []
    for r in con.execute("SELECT * FROM events WHERE game=? AND kind='war' AND local=1 AND precision='exact' AND "
                         "(end_g IS NULL OR end_g>=?) ORDER BY minor, start_g", (game, cg)):
        e = _event(r)
        opp = next((p for p in e["players"] if p != me), None)
        k, lost = _combat(con, game, me, opp, e["start_g"], cg) if opp is not None else (0, 0)
        wars.append({"event_key": e["event_key"], "opponent": opp, "major": opp in majors, "since_g": e["start_g"],
                     "turns": cg - e["start_g"] + 1, "war_type": e["detail"].get("war_type"),
                     "declared_by": e["detail"].get("declared_by"), "units_killed": k, "units_lost": lost,
                     "war_support": _latest(con, game, "war_support", opp, cg, e["start_g"])[0] if opp is not None else None})
    phases = [{"scope": r["scope"], "scope_key": r["scope_key"], "title": r["title"], "outcome": r["outcome"],
               "g_start": r["g_start"], "g_end": r["g_end"]} for r in con.execute(
        "SELECT * FROM phases WHERE game=? ORDER BY CASE scope WHEN 'game' THEN 0 WHEN 'age' THEN 1 WHEN 'war' THEN 2 ELSE 3 END, g_start",
        (game,))]
    _apply_outcomes(con, game, phases)
    blurbs = _annotations(con, game, "kind='blurb' AND scope='game'")
    return {"game": dict(gr), "visibility": vis, "tiles": tiles, "standings": _standings(con, game, cg, majors, me, vis),
            "wars": wars, "phases": phases, "blurb": blurbs[-1]["text"] if blurbs else None}


def _annotations(con, game: str, where: str = "1", args: tuple = ()) -> list[dict]:
    if "annotations" not in _tables(con):
        return []
    return [dict(r) for r in con.execute(f"SELECT * FROM annotations WHERE game=? AND {where} ORDER BY g, annotation_id",
                                         (game, *args))]


def _apply_outcomes(con, game: str, phases: list[dict]) -> None:
    """An advisor `outcome` annotation's outcome overrides the heuristic outcome of the phase it scopes."""
    latest = {}
    for a in _annotations(con, game, "kind='outcome' AND outcome IS NOT NULL AND scope IS NOT NULL"):
        latest[(a["scope"], a["scope_key"])] = a
    for p in phases:
        a = latest.get((p["scope"], p["scope_key"]))
        p["heuristic_outcome"] = p["outcome"] = store.outcome_of(p["outcome"])
        p["outcome_source"] = "advisor" if a else "heuristic"
        if a:
            p["outcome"] = a["outcome"]


def api_phases(con, q: dict) -> dict:
    gr = _need_game(con, q)
    game = gr["game"]
    phases = []
    for r in con.execute("SELECT * FROM phases WHERE game=? ORDER BY CASE scope WHEN 'game' THEN 0 WHEN 'age' THEN 1 "
                         "WHEN 'war' THEN 2 ELSE 3 END, g_start, scope_key", (game,)):
        d = dict(r)
        d["facts"] = _j(d["facts"], {})
        phases.append(d)
    _apply_outcomes(con, game, phases)
    by_scope: dict = {}
    for a in _annotations(con, game, "scope IS NOT NULL"):
        by_scope.setdefault((a["scope"], a["scope_key"]), []).append(a)
    for p in phases:
        p["annotations"] = by_scope.get((p["scope"], p["scope_key"]), [])
    return {"game": dict(gr), "phases": phases,
            "notes": _annotations(con, game, "kind IN ('intention','priority_change')"),
            "unscoped": _annotations(con, game, "scope IS NULL AND kind IN ('narrative','outcome')")}


def api_timeline(con, q: dict) -> dict:
    gr = _need_game(con, q)
    game = gr["game"]
    sql, args = "SELECT * FROM events WHERE game=?", [game]
    if q.get("local", "1") not in ("0", "false", ""):
        sql += " AND (local=1 OR kind='age')"
    if q.get("kinds"):
        kinds = [k for k in q["kinds"].split(",") if k]
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    if q.get("from"):
        sql += " AND coalesce(end_g, start_g) >= ?"
        args.append(int(q["from"]))
    if q.get("to"):
        sql += " AND start_g <= ?"
        args.append(int(q["to"]))
    events = [_event(r) for r in con.execute(sql + " ORDER BY start_g, kind", args)]
    return {"game": dict(gr), "ages": _ages(con, game, gr), "players": _players(con, game), "events": events}


def api_metrics(con, q: dict) -> dict:
    gr = _need_game(con, q)
    game = gr["game"]
    metric = q.get("metric") or "gold_per_turn"
    cat = con.execute("SELECT * FROM metric_catalog WHERE metric=?", (metric,)).fetchone()
    players = ([int(p) for p in q["players"].split(",") if p.strip().lstrip("-").isdigit()] if q.get("players")
               else [p["player_id"] for p in _players(con, game)])
    vis = "all" if q.get("vis") == "all" else "visible"
    series, hidden = {}, {}
    for p in players:
        rows = con.execute("SELECT g, value, visibility FROM metrics WHERE game=? AND metric=? AND player_id=? ORDER BY g",
                           (game, metric, p)).fetchall()
        series[str(p)] = [[r[0], r[1], r[2][0]] for r in rows if vis == "all" or r[2] == "visible"]
        hidden[str(p)] = sum(1 for r in rows if r[2] != "visible") if vis == "visible" else 0
    return {"metric": metric, "catalog": dict(cat) if cat else None, "visibility": vis, "series": series,
            "omniscient_hidden": hidden, "ages": _ages(con, game, gr)}


def api_turn(con, q: dict, g: int | None) -> dict:
    gr = _need_game(con, q)
    game, me = gr["game"], gr["local_player_id"]
    g = gr["current_g"] if g is None else g
    ages = _ages(con, game, gr)
    age, turn = _at(ages, g)
    cat = {r["metric"]: dict(r) for r in con.execute("SELECT * FROM metric_catalog")}
    started = [_event(r) | {"phase": "start"} for r in con.execute(
        "SELECT * FROM events WHERE game=? AND start_g=? ORDER BY local DESC, kind", (game, g))]
    ended = [_event(r) | {"phase": "end"} for r in con.execute(
        "SELECT * FROM events WHERE game=? AND end_g=? AND end_g<>start_g AND kind IN "
        "('age','war','alliance','government','policy','production','project','tech_research','civic_research') "
        "ORDER BY local DESC, kind", (game, g))] if g != gr["current_g"] else []  # open intervals "end" at the current turn
    prev_g = con.execute("SELECT max(g) FROM metrics WHERE game=? AND player_id=? AND g<?", (game, me, g)).fetchone()[0]
    now = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT metric, value, visibility FROM metrics WHERE game=? AND player_id=? AND g=?", (game, me, g))}
    before = {r[0]: r[1] for r in con.execute(
        "SELECT metric, value FROM metrics WHERE game=? AND player_id=? AND g=?", (game, me, prev_g))} if prev_g is not None else {}
    order = list(cat)
    deltas = []
    for m, (v, vis) in sorted(now.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 999):
        if m in DELTA_METRICS_SKIP:
            continue
        p = before.get(m)
        c = cat.get(m, {})
        deltas.append({"metric": m, "label": c.get("label", m), "unit": c.get("unit"), "higher_is_better": c.get("higher_is_better"),
                       "value": v, "prev": p, "delta": None if p is None else v - p, "visibility": vis})
    srow = con.execute("SELECT g, summary, captured_at FROM turns WHERE game=? AND g<=? ORDER BY g DESC LIMIT 1", (game, g)).fetchone()
    nav = {k: con.execute(sql, (game, g)).fetchone()[0] for k, sql in {
        "prev_observed": "SELECT max(g) FROM turns WHERE game=? AND g<?", "next_observed": "SELECT min(g) FROM turns WHERE game=? AND g>?"}.items()}
    return {"g": g, "age": age, "turn": turn, "current_g": gr["current_g"], "observed": srow is not None and srow["g"] == g,
            "events": started + ended, "prev_g": prev_g, "deltas": deltas,
            "summary": _j(srow["summary"], {}) if srow else None, "summary_g": srow["g"] if srow else None,
            "captured_at": srow["captured_at"] if srow else None,
            "annotations": _annotations(con, game, "g=?", (g,)), **nav}


def _history(con, role: str, game: str) -> list[dict]:
    if "game_profiles" not in _tables(con):
        return []
    from . import advisor
    return advisor.profile_history(con, game, role)


def api_advisor(con, q: dict) -> dict:
    gr = _need_game(con, q)
    game = gr["game"]
    vocab = {}
    for r in con.execute("SELECT field, value, description FROM vocab"):
        vocab.setdefault(r[0], {})[r[1]] = r[2]
    advisors, players = _history(con, "advisor", game), _history(con, "player", game)
    plan, plan_error = [], None
    try:
        from . import advisor
        plan = advisor.goal_tree(con, game)
    except Exception as e:  # advisor.py is evolving in parallel; keep the rest of the page alive
        plan_error = f"{type(e).__name__}: {e}"
    return {"game": dict(gr), "advisor_profile": advisors[0] if advisors else None, "advisor_history": advisors,
            "player_profile": players[0] if players else None, "player_history": players,
            "plan": plan, "plan_error": plan_error, "vocab": vocab,
            "annotations": _annotations(con, game)[::-1]}


ROUTES = {"/api/games": api_games, "/api/version": api_version, "/api/game": api_game, "/api/overview": api_overview, "/api/phases": api_phases,
          "/api/timeline": api_timeline, "/api/metrics": api_metrics, "/api/advisor": api_advisor}


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    db: Path = store.DB
    server_version = "civilizer-dashboard"

    def log_message(self, fmt, *args):  # polling every 10 s would flood the terminal
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, default=str, separators=(",", ":")).encode(), "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        path = u.path.rstrip("/") or "/"
        if path.startswith("/api/"):
            try:
                con = connect_ro(self.db)
                try:
                    if path.startswith("/api/turn"):
                        tail = path[len("/api/turn"):].strip("/")
                        if tail and not tail.lstrip("-").isdigit() and tail != "latest":
                            raise NotFound(f"bad turn {tail!r}")
                        out = api_turn(con, q, int(tail) if tail and tail != "latest" else None)
                    elif path in ROUTES:
                        out = ROUTES[path](con, q)
                    else:
                        raise NotFound(f"no endpoint {path}")
                finally:
                    con.close()
                self._json(200, out)
            except NotFound as e:
                self._json(404, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=4)})
            return
        name = "index.html" if path == "/" else path.lstrip("/")
        f = (WEB / name).resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/javascript")) else ""))


def make_server(host: str = "127.0.0.1", port: int = 8777, db: Path | None = None) -> ThreadingHTTPServer:
    """Server bound to (host, port) over `db`; port 0 picks a free port (tests)."""
    handler = type("BoundHandler", (Handler,), {"db": Path(db) if db else store.DB})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(host: str = "127.0.0.1", port: int = 8777, db: Path | None = None) -> int:
    srv = make_server(host, port, db)
    print(f"civilizer dashboard on http://{host}:{srv.server_address[1]}/  (store: {db or store.DB}; Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
