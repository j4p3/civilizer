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


def _stage(stage: str | None) -> str:
    """DIPLOMACY_INFLUENCE_INDEPENDENT_FRIENDLY_FIRST_GIFT -> friendly first gift (befriend stages); other stages as logged."""
    if not stage:
        return "?"
    s = stage.removeprefix("DIPLOMACY_INFLUENCE_INDEPENDENT_")
    return s.replace("_", " ").lower() if s != stage else stage


def render_brief(b: dict) -> str:
    gi = b["game"]
    out = [f"{gi['leader']} / {gi['civilization']}  {gi['current_age']} turn {gi['current_turn']} (g{gi['current_g']}); "
           f"changes since g{b['since_g']}"]
    ap, pp = b["advisor_profile"], b["player_profile"]
    out.append(f"Advisor: {ap['name']} ({ap['personality']}, profile {ap['profile_id']} v{ap['version']}; priorities {', '.join(ap['priorities'])})"
               + (f"; voice: {ap['voice']}" if ap["voice"] else "") if ap else "Advisor: (not set)")
    out.append(f"Player: {pp['name']} (profile {pp['profile_id']} v{pp['version']}): {advisor._player_phrase(pp)}" + (f"; {pp['notes']}" if pp["notes"] else "")
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
            extra = ", ".join(f"{k}={_stage(e[k]) if k == 'stage' else e[k]}"
                              for k in ("city", "category", "war_type", "victim", "killer", "group", "stage", "response",
                                        "outcome", "bonus", "lost_to_name", "end_reason", "turn_influence_spent") if k in e)
            out.append(f"  g{e['g']} {e['kind']} {e['phase']}: {e['subject']}" + (f" ({extra})" if extra else ""))
        if b.get("events_omitted"):
            out.append(f"  (+ omitted: {', '.join(f'{k} {n}' for k, n in b['events_omitted'].items())})")
    dp = b.get("diplomacy") or {}
    if dp:
        out.append("\nDiplomacy:")
        if dp.get("suzerain_of"):
            out.append(f"  suzerain of: {', '.join(dp['suzerain_of'])}")
        for p in dp.get("befriending", []):
            rivals = "; ".join(f"{r['name']} (since T{r['start_turn']}{', ' + _stage(r['stage']) if r.get('stage') else ''})"
                               for r in p.get("rivals", []))
            out.append(f"  befriending since g{p['since_g']}: {p['subject']}, stage {_stage(p.get('stage'))}, "
                       f"support {p.get('support') or 0}" + (f"; also befriending it: {rivals}" if rivals else ""))
        for p in dp.get("progress", []):
            out.append(f"  progress: {p['subject']}" + (f", entered {', '.join(map(_stage, p['stages']))}" if p.get("stages") else "")
                       + (f", support +{p['support_added']}" if p.get("support_added") else ""))
    cr = b.get("combat_rewards_since") or {}
    if cr.get("by_kind"):
        out.append("\nCombat rewards:")
        for kind, c in cr["by_kind"].items():
            ys = ", ".join(f"{_num(v)} {y.removeprefix('YIELD_').lower()}" for y, v in c["yields"].items())
            out.append(f"  {kind} x{c['count']}" + (f": {ys}" if ys else "")
                       + (f"; {c['unknown_amount']} amount not logged by the game" if c.get("unknown_amount") else ""))
    if cr.get("other_gold"):
        out.append("\nOther gold (unexplained treasury change, not attributed):")
        for t in cr["other_gold"]["turns"]:
            ev = ", ".join(f"{k} x{n}" for k, n in (t.get("combat_events") or {}).items())
            out.append(f"  g{t['g']}: {t['gold']:+,.0f}" + (f" (combat events that turn: {ev})" if ev else ""))
    if b["recent_annotations"]:
        out.append("\nRecent notes:")
        out += [f"  g{a['g']} [{a['author']}/{a['kind']}{'/' + a['outcome'] if a.get('outcome') else ''}] {a['text']}"
                for a in b["recent_annotations"]]
    return "\n".join(out)


def render_history(rows: list[dict]) -> str:
    if not rows:
        return "No advisor history yet."
    return "\n".join(f"g{r['g']:<4} {r['what']:<16} {r['text']}" + (f" [{r['outcome']}]" if r.get("outcome") else "") for r in rows)


def render_phases(rows: list[dict]) -> str:
    out = []
    for p in rows:
        v = p["effective_outcome"] + (f" (heuristic {p['heuristic_outcome']})" if p["advisor_outcome"] else "")
        out.append(f"{p['scope']:<5} {p['scope_key']:<16} g{p['g_start']}-{p['g_end']}  {p['title']}: {v}"
                   + ("" if not p["needs_narrative"] else "  [no narrative yet]"))
        out += [f"        [{a['kind']}{'/' + a['outcome'] if a['outcome'] else ''}] {a['text']}" for a in p["annotations"]]
    return "\n".join(out) or "No phases (run `civilizer refresh`)."


# ---------------------------------------------------------------- CLI

OUTCOME_HELP = ("success, partial, failure, changed_plans, ongoing (status default: achieved -> success, failed -> failure, "
                "abandoned -> changed_plans); old verdicts went_well/went_awry/mixed are accepted")


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
    p.add_argument("value", nargs="?", help="advisor: preset key; player: comma-separated playstyles")
    p.add_argument("--motives", help="player: comma-separated motives (fight,build,explore,mess_around,story)")
    p.add_argument("--name", help="profile name")
    p.add_argument("--profile", type=int, help="new version of this saved profile id")
    p.add_argument("--voice", default="")
    p.add_argument("--priorities", help="advisor priorities, comma-separated (default: the personality's)")
    p.add_argument("--quirks", default="")
    p.add_argument("--risk", default="medium", choices=["low", "medium", "high"])
    p.add_argument("--notes", default="")

    p = sub.add_parser("profiles", parents=[common], help="saved (reusable) advisor / player profiles")
    p.add_argument("role", nargs="?", choices=["advisor", "player"])

    p = sub.add_parser("use", parents=[common], help="link a saved profile to the game")
    p.add_argument("role", choices=["advisor", "player"])
    p.add_argument("profile_id", type=int)
    p.add_argument("--version", type=int)

    p = sub.add_parser("setup", parents=[common], help="setup wizard: the next question (advisor, player, strategy)")
    p.add_argument("step", nargs="?", choices=advisor.WIZARD_STEPS)

    p = sub.add_parser("preset", parents=[common], help="create a custom advisor preset (data/advisors/KEY.md)")
    p.add_argument("key")
    p.add_argument("--label", default="")
    p.add_argument("--description", required=True)
    p.add_argument("--prompt-file", type=Path, required=True, help="file with the style prompt body")
    p.add_argument("--priorities", required=True, help="comma-separated advisor priorities")
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("pick-strategy", parents=[common], help="adopt a strategy from wizard presets")
    p.add_argument("presets", help="comma-separated: science,culture,economic,military,expansion,score,just_win,not_sure")
    p.add_argument("--victory", help="victory types (needed for just_win alone)")
    p.add_argument("--name")
    p.add_argument("--notes", default="")

    p = sub.add_parser("strategy", parents=[common], help="strategies")
    p.add_argument("action", choices=["add", "status", "list"])
    p.add_argument("args", nargs="*", help="add: NAME; status: ID STATUS")
    p.add_argument("--victory", help="add: comma-separated victory types (science,culture,economic,military,expansion,score)")
    p.add_argument("--description", default="")
    p.add_argument("--note", default="")
    p.add_argument("--outcome", "--verdict", dest="outcome", help=OUTCOME_HELP)

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
    p.add_argument("--outcome", "--verdict", dest="outcome", help=OUTCOME_HELP)
    p.add_argument("--note", default="")
    p.add_argument("--active", action="store_true", help="list: active only")

    p = sub.add_parser("annotate", parents=[common], help="record a note (intention, priority_change, narrative, outcome, ...)")
    p.add_argument("text", nargs="+")
    p.add_argument("--kind", default="observation")
    p.add_argument("--author", default="advisor", choices=["advisor", "player"])
    p.add_argument("--outcome", "--verdict", dest="outcome", help=OUTCOME_HELP)
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

    p = sub.add_parser("phases", parents=[common], help="phase fact sheets with heuristic and advisor outcomes")
    p.add_argument("--scope", choices=advisor.SCOPES)

    p = sub.add_parser("history", parents=[common], help="change log of profiles, strategies and goals")
    p.add_argument("what", nargs="?", choices=["profile", "strategy", "goal"])

    p = sub.add_parser("follow", parents=[common], help="stream one line of facts per new turn (read-only; for an advisor agent)")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between store polls")

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
                return advisor.set_advisor_profile(con, gid, a.value, a.voice, _csv(a.priorities) or None, a.quirks, a.name, a.profile)
            return advisor.set_player_profile(con, gid, _csv(a.value), a.risk, a.notes, _csv(a.motives), a.name, a.profile)
        case "profiles":
            rows = advisor.list_profiles(con, a.role)
            return rows if a.json else "\n".join(
                f"{r['role']:<7} #{r['profile_id']} v{r['version']} {r['name']}: "
                + (r["personality"] if r["role"] == "advisor" else advisor._player_phrase(r))
                + f"  (games: {', '.join(u['leader'] or u['game'] for u in r['games']) or '-'})" for r in rows) or "No saved profiles."
        case "use":
            return advisor.use_profile(con, gid, a.role, a.profile_id, a.version)
        case "setup":
            return advisor.setup_wizard(con, gid, a.step)
        case "preset":
            return advisor.create_advisor_preset(con, a.key, a.label, a.description, a.prompt_file.read_text(), _csv(a.priorities),
                                                 a.overwrite)
        case "pick-strategy":
            return advisor.choose_strategy(con, gid, _csv(a.presets), _csv(a.victory) or None, a.name, a.notes)
        case "strategy":
            if a.action == "add":
                name = " ".join(_need(a.args, 1, "strategy add NAME --victory economic,expansion"))
                return advisor.add_strategy(con, gid, name, _csv(a.victory), a.description)
            if a.action == "status":
                sid, status = _need(a.args, 2, "strategy status ID STATUS")[:2]
                return advisor.set_strategy_status(con, gid, int(sid), status, a.note, a.outcome)
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
                _need(args, 2, "goal status ID STATUS [--resolution ..] [--outcome ..]")
                return advisor.set_goal_status(con, gid, int(args[0]), args[1], a.resolution, a.outcome)
            rows = advisor.goal_tree(con, gid, a.active)
            return rows if a.json else render_tree(rows)
        case "annotate":
            return {"annotation_id": advisor.annotate(con, gid, " ".join(a.text), a.kind, a.author, a.outcome, a.goal, a.strategy,
                                                      a.event, a.g, a.scope, a.scope_key)}
        case "notes":
            rows = advisor.annotations(con, gid, a.kind, a.scope, a.scope_key, a.goal, a.since, a.limit)
            return rows if a.json else "\n".join(
                f"#{r['annotation_id']} g{r['g']} [{r['author']}/{r['kind']}{'/' + r['outcome'] if r['outcome'] else ''}"
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
    if a.cmd == "follow":
        from .follow import follow
        try:
            follow(a.db, a.interval)
        except KeyboardInterrupt:
            pass
        return 0
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
