"""`civilizer up` preflight: debugger setting and reuse of a running monitor."""

import os

from civilizer import monitor, up


def test_debugger_disabled_only_when_explicit(tmp_path, monkeypatch):
    f = tmp_path / "AppOptions.txt"
    monkeypatch.setattr(up, "APP_OPTIONS", f)
    assert not up.debugger_disabled()  # missing file
    f.write_text(";UI Remote Debugger, 0 = Disabled, 1 = Enabled\n;UIDebugger 1\n")
    assert not up.debugger_disabled()  # commented default
    f.write_text("[Debug]\nUIDebugger 0\n")
    assert up.debugger_disabled()


def test_monitor_running_needs_live_pid_and_fresh_heartbeat(monkeypatch):
    from civilizer import store

    other = os.getppid()  # a pid that exists and isn't ours
    hb = {"state": "watching", "pid": other, "seen_at": store.now()}
    monkeypatch.setattr(monitor, "read_heartbeat", lambda: hb)
    assert up.monitor_running() == other
    hb["state"] = "stopped"
    assert up.monitor_running() is None
    hb.update(state="watching", seen_at="2020-01-01T00:00:00")
    assert up.monitor_running() is None
    hb.update(seen_at=store.now(), pid=os.getpid())
    assert up.monitor_running() is None
