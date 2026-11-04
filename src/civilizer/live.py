"""Live game state via the Civ VII UI remote debugger (Coherent Gameface, CDP).

The game listens on 127.0.0.1:9444 by default (AppOptions.txt `UIDebugger 1`).
We send Runtime.evaluate to the in-game UI page and run js/snapshot.js, which
reads state through the same JS API the game's own screens use.

Native bindings do not validate arguments, so a malformed call can crash the
game. Only the vetted snapshot script runs by default; `raw_eval` exists for
development and must be used with Firaxis-precedented calls only.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from importlib.resources import files

import websockets

HOST = "127.0.0.1"
PORT = 9444
GAME_PAGE = "fs://game/root-game.html"


class LiveError(RuntimeError):
    pass


def list_pages(host: str = HOST, port: int = PORT) -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json", timeout=3) as r:
            return json.load(r)
    except OSError as e:
        raise LiveError(f"Civ VII UI debugger not reachable on {host}:{port} (is the game running?): {e}") from e


def game_page(host: str = HOST, port: int = PORT) -> dict:
    pages = list_pages(host, port) or []  # null while the game is shutting down
    for p in pages:
        if p.get("url") == GAME_PAGE:
            return p
    urls = ", ".join(p.get("url", "?") for p in pages) or "none"
    raise LiveError(f"No in-game UI page (at main menu or loading?). Pages: {urls}")


async def _evaluate(ws_url: str, expression: str, timeout: float) -> object:
    async with websockets.connect(ws_url, max_size=None, open_timeout=timeout) as ws:
        await ws.send(
            json.dumps(
                {"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True}}
            )
        )
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if msg.get("id") != 1:
                continue
            if "error" in msg:
                raise LiveError(f"CDP error: {msg['error']}")
            res = msg["result"]
            if "exceptionDetails" in res:
                d = res["exceptionDetails"]
                raise LiveError(f"JS exception: {d.get('exception', {}).get('description') or d.get('text')}")
            return res["result"].get("value")


def raw_eval(expression: str, timeout: float = 30.0) -> object:
    page = game_page()
    # The advertised URL ("ws://.../json/devtools/page/0") is wrong for Cohtml; this form works.
    ws_url = f"ws://{HOST}:{PORT}/devtools/page/{page['id']}"
    return asyncio.run(_evaluate(ws_url, expression, timeout))


def snapshot(detail: bool = False, omniscient: bool = False, map: bool = False, victories: bool = True) -> dict:
    """Full screen-equivalent state of the running game for the local player.

    omniscient=False (default) limits rivals to what the UI would show the player:
    unmet civs are anonymous, foreign cities only if revealed, enemy units only if visible.
    """
    return _run("snapshot.js", {"detail": detail, "omniscient": omniscient, "map": map, "victories": victories})


def history() -> dict:
    """Whole-game history from the running game (js/history.js): victory points per turn for this
    and past ages, end-game summary datasets, and every major player's techs, civics, government,
    religion, settlements and wars. Omniscient."""
    return _run("history.js", {}, timeout=120.0)


def _run(name: str, opts: dict, timeout: float = 30.0) -> dict:
    script = files("civilizer").joinpath(f"js/{name}").read_text()
    value = raw_eval(script.replace("__OPTIONS__", json.dumps(opts)), timeout=timeout)
    if not isinstance(value, str):
        raise LiveError(f"unexpected {name} result: {value!r}")
    return json.loads(value)
