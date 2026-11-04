"""`civilizer advisor ...`: the advisor service from the shell, mirroring the MCP tools (mcp_server.py).

Writes only advisor tables (profiles, strategies, goals, goal_metrics, annotations) in data/civilizer.db.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from . import advisor, store

METRIC_SPEC = re.compile(r"^(\w+)(?:@(\d+))?\s*(gain>=|gain<=|rank<=|>=|<=)\s*(-?[\d.]+)(?:\s*\*\s*([\d.]+))?$")


def parse_metric(spec: str) -> dict:
    """'gold_per_turn>=100', 'units_lost_total gain<=2', 'vp_economic rank<=1', 'score@3<=500' (rival 3), '...*2' (weight)."""
    m = METRIC_SPEC.match(spec.strip())
    if not m:
        raise advisor.AdvisorError(f"bad metric spec {spec!r}; e.g. gold_per_turn>=100, units_lost_total gain<=2, vp_science rank<=1")
    metric, pid, cmp, target, weight = m.groups()
    return {"metric": metric, "comparator": cmp, "target": float(target)} | ({"player_id": int(pid)} if pid else {}) | (
        {"weight": float(weight)} if weight else {})


def _csv(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _pct(p) -> str:
    return "  ?" if p is None else f"{p * 100:3.0f}%"


def _num(v) -> str:
    return "-" if v is None else f"{v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:,.1f}"


# ---------------------------------------------------------------- rendering

def _goal_lines(g: dict, depth: int = 0) -> list[str]:
    pad = "    " * depth
    st = "" if g["status"] == "active" else f" [{g['status']}]"
    due = f" due g{g['deadline_g']}" + (" OVERDUE" if g.get("overdue") else "") if g.get("deadline_g") else ""
    out = [f"{pad}  {_pct(g['progress'])}  #{g['goal_id']} ({g['horizon']}, p{g['priority']}) {g['title']}{st}{due}"]
    for m in g["metrics"]:
        who = "" if m["local"] else f" (player {m['player_id']})"
        cur = "no data" if m["current"] is None else (f"rank {m['current']}/{m.get('of')}" if m["comparator"] == "rank<=" else _num(m["current"]))
        gain = f", gain {m['gain']:+,.0f}" if "gain" in m else ""
        out.append(f"{pad}         - {m['metric']}{who} {m['comparator']} {m['target']:g}: {cur}{gain} "
                   f"(baseline {_num(m['baseline'])}) {'met' if m['met'] else _pct(m['progress']).strip()}")
    for c in g.get("children", []):
        out += _goal_lines(c, depth + 1)
    return out


def render_tree(tree: list[dict]) -> str:
    if not tree:
        return "No strategies or goals yet."
    out = []
    for s in tree:
        head = f"Strategy {s['strategy_id']}: {s['name']}" if s["strategy_id"] else s["name"]
        out.append(f"{head} ({', '.join(s['victory_types']) or '-'}) [{s['status']}]")
        if s.get("description"):
            out.append(f"  {s['description']}")
        for g in s["goals"]:
            out += _goal_lines(g)
        if not s["goals"]:
            out.append("  (no goals)")
    return "\n".join(out)


def render_brief(b: dict) -> str:
    gi = b["game"]
    out = [f"{gi['leader']} / {gi['civilization']}  {gi['current_age']} turn {gi['current_turn']} (g{gi['current_g']}); "
           f"changes since g{b['since_g']}"]
    ap, pp = b["advisor_profile"], b["player_profile"]
    out.append(f"Advisor: {ap['personality']} v{ap['version']} (priorities {', '.join(ap['priorities'])})"
               + (f"; voice: {ap['voice']}" if ap["voice"] else "") if ap else "Advisor: (not set)")
    out.append(f"Player: {', '.join(pp['playstyle'])}, risk {pp['risk_tolerance']}" + (f"; {pp['notes']}" if pp["notes"] else "")
               if pp else "Player: (not set)")
    for s in b.get("setup_needed", []):
        out.append(f"  setup: {s}")
    out.append("\nPlan:")
    if not b["plan"]:
        out.append("  (none)")
    for s in b["plan"]:
        out.append(f"  Strategy {s['strategy_id']}: {s['name']} ({', '.join(s['victory_types'])})" if s["strategy_id"] else f"  {s['name']}")
        stack = [(g, 1) for g in reversed(s["goals"])]
        while stack:
            g, d = stack.pop()
            out.append(f"  {'  ' * d}{_pct(g['progress'])} #{g['goal_id']} {g['title']}"
                       + (f" (due g{g['deadline_g']}{', OVERDUE' if g.get('overdue') else ''})" if g.get("deadline_g") else ""))
            out += [f"  {'  ' * d}       {m}" for m in g.get("metrics", [])]
            stack += [(c, d + 1) for c in reversed(g.get("children", []))]
    out.append("\nState:")
    row = []
    for k, v in b["state"].items():
        row.append(f"{k}={_num(v['value'])}" + (f" ({v['delta']:+,.1f})" if v.get("delta") else ""))
    out += ["  " + "  ".join(row[i:i + 5]) for i in range(0, len(row), 5)]
    if b["standings"]:
        out.append("Standings: " + "; ".join(
            f"{k} #{s['rank']}/{s['of']} {_num(s['value'])}" + (f"/{_num(s['target'])}" if s.get("target") else "")
            + (f" (leader {s['leader']} {_num(s['leader_value'])})" if s.get("leader") else "") for k, s in b["standings"].items()))
    if b["alerts"]:
        out.append("\nAlerts:")
        out += [f"  ! {a}" for a in b["alerts"]]
    if b["events_since"]:
        out.append("\nEvents:")
        for e in b["events_since"]:
            extra = ", ".join(f"{k}={e[k]}" for k in ("city", "category", "war_type", "victim", "killer") if k in e)
            out.append(f"  g{e['g']} {e['kind']} {e['phase']}: {e['subject']}" + (f" ({extra})" if extra else ""))
        if b.get("events_omitted"):
            out.append(f"  (+ omitted: {', '.join(f'{k} {n}' for k, n in b['events_omitted'].items())})")
    if b["recent_annotations"]:
        out.append("\nRecent notes:")
        out += [f"  g{a['g']} [{a['author']}/{a['kind']}{'/' + a['verdict'] if a.get('verdict') else ''}] {a['text']}"
                for a in b["recent_annotations"]]
    return "\n".join(out)


def render_history(rows: list[dict]) -> str:
    if not rows:
        return "No advisor history yet."
    return "\n".join(f"g{r['g']:<4} {r['what']:<16} {r['text']}" + (f" [{r['verdict']}]" if r.get("verdict") else "") for r in rows)


def render_phases(rows: list[dict]) -> str:
    out = []
    for p in rows:
        v = p["effective_verdict"] + (f" (heuristic {p['heuristic_verdict']})" if p["advisor_verdict"] else "")
        out.append(f"{p['scope']:<5} {p['scope_key']:<16} g{p['g_start']}-{p['g_end']}  {p['title']}: {v}"
                   + ("" if not p["needs_narrative"] else "  [no narrative yet]"))
        out += [f"        [{a['kind']}{'/' + a['verdict'] if a['verdict'] else ''}] {a['text']}" for a in p["annotations"]]
    return "\n".join(out) or "No phases (run `civilizer refresh`)."


# ---------------------------------------------------------------- CLI

def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--game", help="game id (map seed); default: most recently updated")
    common.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", help="JSON output")
    ap = argparse.ArgumentParser(prog="civilizer advisor", description="Advisor profiles, strategy, goals, annotations, briefing.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("brief", parents=[common], help="per-turn briefing")
    p.add_argument("--since", type=int, help="compare against this g (default: previous observed turn)")
    p.add_argument("--max-events", type=int, default=25)

    p = sub.add_parser("profile", parents=[common], help="advisor / player profiles")
    p.add_argument("which", choices=["advisor", "player", "show"])
    p.add_argument("action", nargs="?", choices=["set", "show"], default="show")
    p.add_argument("value", nargs="?", help="advisor: personality; player: comma-separated playstyles")
    p.add_argument("--voice", default="")
    p.add_argument("--priorities", help="advisor priorities, comma-separated (default: the personality's)")
    p.add_argument("--quirks", default="")
    p.add_argument("--risk", default="medium", choices=["low", "medium", "high"])
    p.add_argument("--notes", default="")

    p = sub.add_parser("strategy", parents=[common], help="strategies")
    p.add_argument("action", choices=["add", "status", "list"])
    p.add_argument("args", nargs="*", help="add: NAME; status: ID STATUS")
    p.add_argument("--victory", help="add: comma-separated victory types (science,culture,economic,military,expansion,score)")
    p.add_argument("--description", default="")
    p.add_argument("--note", default="")
    p.add_argument("--verdict")

    p = sub.add_parser("goal", parents=[common], help="goals and their metrics")
    p.add_argument("action", choices=["add", "link", "unlink", "update", "status", "list"])
    p.add_argument("args", nargs="*", help="add: TITLE; link: ID 'METRIC CMP TARGET'; unlink: ID METRIC; update: ID; status: ID STATUS")
    p.add_argument("--horizon", default="short", choices=["short", "long"])
    p.add_argument("--strategy", type=int)
    p.add_argument("--parent", type=int)
    p.add_argument("--priority", type=int)
    p.add_argument("--deadline", type=int, help="game-wide turn g (-1 clears on update)")
    p.add_argument("--title")
    p.add_argument("--description")
    p.add_argument("--metric", action="append", default=[], help="add: e.g. 'gold_per_turn>=100' (repeatable)")
    p.add_argument("--player", type=int, help="link/unlink: whose metric (default local)")
    p.add_argument("--weight", type=float, default=1.0)
    p.add_argument("--resolution", default="")
    p.add_argument("--verdict")
    p.add_argument("--note", default="")
    p.add_argument("--active", action="store_true", help="list: active only")

    p = sub.add_parser("annotate", parents=[common], help="record a note (intention, priority_change, narrative, outcome, ...)")
    p.add_argument("text", nargs="+")
    p.add_argument("--kind", default="observation")
    p.add_argument("--author", default="advisor", choices=["advisor", "player"])
    p.add_argument("--verdict")
    p.add_argument("--goal", type=int)
    p.add_argument("--strategy", type=int)
    p.add_argument("--event", help="events.event_key")
    p.add_argument("--scope", choices=advisor.SCOPES)
    p.add_argument("--scope-key")
    p.add_argument("--g", type=int, help="turn the note is about (default now)")

    p = sub.add_parser("notes", parents=[common], help="list annotations")
    p.add_argument("--kind")
    p.add_argument("--scope")
    p.add_argument("--scope-key")
    p.add_argument("--goal", type=int)
    p.add_argument("--since", type=int)
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("phases", parents=[common], help="phase fact sheets with heuristic and advisor verdicts")
    p.add_argument("--scope", choices=advisor.SCOPES)

    p = sub.add_parser("history", parents=[common], help="change log of profiles, strategies and goals")
    p.add_argument("what", nargs="?", choices=["profile", "strategy", "goal"])

    p = sub.add_parser("vocab", parents=[common], help="allowed values (personality, playstyle, strategy_focus, ...)")
    p.add_argument("field", nargs="?")
    return ap


def _need(args: list[str], n: int, usage: str) -> list[str]:
    if len(args) < n:
        raise advisor.AdvisorError(f"usage: {usage}")
    return args


def run(a, con) -> object:
    """Execute one command; returns a JSON-able result or a string already rendered."""
    gid = advisor.game_row(con, a.game)["game"]
    match a.cmd:
        case "brief":
            b = advisor.briefing(con, gid, a.since, a.max_events)
            return b if a.json else render_brief(b)
        case "profile":
            if a.which == "show" or a.action == "show":
                d = {"advisor": advisor.current_advisor_profile(con, gid), "player": advisor.current_player_profile(con, gid)}
                if a.which in ("advisor", "player"):
                    d = d[a.which]
                return d
            if not a.value:
                raise advisor.AdvisorError(f"usage: profile {a.which} set VALUE")
            if a.which == "advisor":
                return advisor.set_advisor_profile(con, gid, a.value, a.voice, _csv(a.priorities) or None, a.quirks)
            return advisor.set_player_profile(con, gid, _csv(a.value), a.risk, a.notes)
        case "strategy":
            if a.action == "add":
                name = " ".join(_need(a.args, 1, "strategy add NAME --victory economic,expansion"))
                return advisor.add_strategy(con, gid, name, _csv(a.victory), a.description)
            if a.action == "status":
                sid, status = _need(a.args, 2, "strategy status ID STATUS")[:2]
                return advisor.set_strategy_status(con, gid, int(sid), status, a.note, a.verdict)
            rows = advisor.goal_tree(con, gid)
            return rows if a.json else render_tree(rows)
        case "goal":
            args = a.args
            if a.action == "add":
                title = " ".join(_need(args, 1, "goal add TITLE [--metric 'gold_per_turn>=100']"))
                return advisor.add_goal(con, gid, title, a.horizon, a.strategy, a.parent, a.priority or 2, a.deadline,
                                        a.description or "", [parse_metric(m) for m in a.metric])
            if a.action == "link":
                _need(args, 2, "goal link ID 'METRIC CMP TARGET'")
                m = parse_metric(" ".join(args[1:]))
                if a.player is not None:
                    m["player_id"] = a.player
                m.setdefault("weight", a.weight)
                return advisor.link_metric(con, gid, int(args[0]), **m)
            if a.action == "unlink":
                _need(args, 2, "goal unlink ID METRIC")
                advisor.unlink_metric(con, gid, int(args[0]), args[1], a.player)
                return "ok"
            if a.action == "update":
                _need(args, 1, "goal update ID [--title ..] [--priority N] [--deadline G] [--parent ID] [--strategy ID]")
                return advisor.update_goal(con, gid, int(args[0]), a.title, a.description, a.priority, a.deadline, a.parent,
                                           a.strategy, a.note)
            if a.action == "status":
                _need(args, 2, "goal status ID STATUS [--resolution ..] [--verdict ..]")
                return advisor.set_goal_status(con, gid, int(args[0]), args[1], a.resolution, a.verdict)
            rows = advisor.goal_tree(con, gid, a.active)
            return rows if a.json else render_tree(rows)
        case "annotate":
            return {"annotation_id": advisor.annotate(con, gid, " ".join(a.text), a.kind, a.author, a.verdict, a.goal, a.strategy,
                                                      a.event, a.g, a.scope, a.scope_key)}
        case "notes":
            rows = advisor.annotations(con, gid, a.kind, a.scope, a.scope_key, a.goal, a.since, a.limit)
            return rows if a.json else "\n".join(
                f"#{r['annotation_id']} g{r['g']} [{r['author']}/{r['kind']}{'/' + r['verdict'] if r['verdict'] else ''}"
                f"{' ' + r['scope'] + ':' + r['scope_key'] if r['scope'] else ''}] {r['text']}" for r in rows) or "No notes."
        case "phases":
            rows = advisor.phases(con, gid, a.scope, with_facts=a.json)
            return rows if a.json else render_phases(rows)
        case "history":
            rows = advisor.history(con, gid, a.what)
            return rows if a.json else render_history(rows)
        case "vocab":
            q = "SELECT field, value, description, extra FROM vocab" + (" WHERE field=?" if a.field else "") + " ORDER BY field"
            rows = [dict(r) for r in con.execute(q, (a.field,) if a.field else ())]
            return rows if a.json else "\n".join(f"{r['field']:<17} {r['value']:<18} {r['description']}"
                                                 + (f"  {r['extra']}" if r["extra"] else "") for r in rows)


def main(argv: list[str]) -> int:
    a = _parser().parse_args(argv)
    try:
        with store.tx(a.db) as con:
            res = run(a, con)
    except advisor.AdvisorError as e:
        print(f"error: {e}")
        return 1
    if isinstance(res, str):
        print(res)
    else:
        print(json.dumps(res, indent=1, default=str, ensure_ascii=False))
    return 0
