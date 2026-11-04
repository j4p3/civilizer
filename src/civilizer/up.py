"""`civilizer up`: everything a play session needs in one process: the dashboard (in a thread) and the monitor.

Reuses what is already running instead of doubling it: a dashboard already bound to the port is left alone,
and a live monitor (fresh heartbeat from a running pid) means this process only serves the dashboard.
"""

from __future__ import annotations

import os
import re
import threading
import webbrowser
from datetime import datetime

from . import dashboard, monitor
from .timeline import LOG_DIR

APP_OPTIONS = LOG_DIR.parent / "AppOptions.txt"  # My Games/Sid Meier's Civilization VII/


def debugger_disabled() -> bool:
    """True if AppOptions.txt explicitly turns the UI remote debugger off (the default, commented-out
    `;UIDebugger 1`, leaves it on)."""
    try:
        text = APP_OPTIONS.read_text(errors="replace")
    except OSError:
        return False
    return bool(re.search(r"^\s*UIDebugger\s+0\b", text, re.M))


def monitor_running(max_age_s: int = 120) -> int | None:
    """Pid of another monitor with a recent heartbeat, if any."""
    try:
        hb = monitor.read_heartbeat() or {}
    except Exception:
        return None
    pid = hb.get("pid")
    if hb.get("state") == "stopped" or not pid or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
        age = (datetime.now() - datetime.fromisoformat(hb["seen_at"])).total_seconds()
    except (OSError, KeyError, ValueError):
        return None
    return pid if age < max_age_s else None


def run(host: str = "127.0.0.1", port: int = 8777, interval: int = 15, browser: bool = True) -> int:
    if debugger_disabled():
        print(f"warning: {APP_OPTIONS} sets `UIDebugger 0`; set it to 1 (or comment it out) and restart the game, "
              "or the monitor can't read it", flush=True)
    url = f"http://{host}:{port}/"
    try:
        srv = dashboard.make_server(host, port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"dashboard: {url}", flush=True)
    except OSError:
        srv = None
        print(f"dashboard: port {port} in use; assuming it's already running at {url}", flush=True)
    if browser:
        webbrowser.open(url)
    other = monitor_running()
    try:
        if other:
            print(f"monitor: already running (pid {other}); serving the dashboard only. Ctrl-C to stop.", flush=True)
            threading.Event().wait()
        print("monitor: start or load a game in Civ VII; turns are recorded as you end them.", flush=True)
        return monitor.run(interval=interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if srv:
            srv.shutdown()
