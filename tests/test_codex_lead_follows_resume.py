"""A codex Lead resumed from the desktop (`/resume` in the TUI) must stay the
conversation the phone mirrors — the bound session follows the rollout the
live codex process really has open, whatever switched it."""

from __future__ import annotations

import json
import os
import time
import types

from agent_takkub.remote import notify
from agent_takkub.spawn_engine import PaneState


def _orch(pid):
    pane = types.SimpleNamespace(
        session=types.SimpleNamespace(pid=pid),
        model=types.SimpleNamespace(provider_name="codex", spawn_ts=0.0),
    )
    ps = PaneState()
    orch = types.SimpleNamespace(
        _panes_by_project={"p": {"lead": pane}}, _pane_state={"p::lead": ps}
    )
    return orch, ps


def test_bound_session_follows_the_open_rollout(monkeypatch):
    live = {"sid": ("session-A", "C:/proj")}
    monkeypatch.setattr(notify, "_codex_live_session", lambda _pid: live["sid"])
    orch, ps = _orch(4242)
    assert notify._lead_session_uuid(orch, "p") == "session-A"
    live["sid"] = ("session-B", "C:/proj")  # user typed /resume on the desktop
    assert notify._lead_session_uuid(orch, "p") == "session-B"
    assert (ps.session_uuid_cwd, ps.session_provider) == ("C:/proj", "codex")


def test_no_open_rollout_yet_keeps_the_current_binding(monkeypatch):
    monkeypatch.setattr(notify, "_codex_live_session", lambda _pid: None)
    orch, ps = _orch(4242)
    ps.session_uuid = "resumed-by-cockpit"
    assert notify._lead_session_uuid(orch, "p") == "resumed-by-cockpit"


def test_non_codex_lead_is_left_alone(monkeypatch):
    monkeypatch.setattr(
        notify, "_codex_live_session", lambda _pid: (_ for _ in ()).throw(AssertionError)
    )
    orch, ps = _orch(4242)
    orch._panes_by_project["p"]["lead"].model.provider_name = "claude"
    ps.session_uuid = "claude-uuid"
    assert notify._lead_session_uuid(orch, "p") == "claude-uuid"


def test_newest_written_rollout_wins_when_two_are_open(monkeypatch, tmp_path):
    def rollout(name, sid, age):
        path = tmp_path / name
        meta = {"type": "session_meta", "payload": {"id": sid, "cwd": "C:/proj"}}
        path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        t = time.time() - age
        os.utime(path, (t, t))
        return path

    old = rollout("rollout-old.jsonl", "old", 60)
    new = rollout("rollout-new.jsonl", "new", 0)
    proc = types.SimpleNamespace(
        open_files=lambda: [
            types.SimpleNamespace(path=str(old)),
            types.SimpleNamespace(path=str(new)),
        ],
        children=lambda recursive=False: [],
    )
    import psutil

    monkeypatch.setattr(psutil, "Process", lambda _pid: proc)
    assert notify._open_codex_rollout(1) == ("new", "C:/proj")


def test_live_lookup_runs_off_the_ui_thread_and_is_cached(monkeypatch):
    calls = []

    def slow_lookup(pid):
        calls.append(pid)
        time.sleep(0.2)
        return ("sid", "cwd")

    monkeypatch.setattr(notify, "_open_codex_rollout", slow_lookup)
    monkeypatch.setattr(notify, "_live_rollout", {})
    monkeypatch.setattr(notify, "_live_rollout_inflight", set())
    t0 = time.monotonic()
    assert notify._codex_live_session(7) is None  # first call never blocks
    assert time.monotonic() - t0 < 0.1
    deadline = time.monotonic() + 3
    while notify._codex_live_session(7) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert notify._codex_live_session(7) == ("sid", "cwd")
    assert calls == [7]
