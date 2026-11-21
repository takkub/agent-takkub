"""#1044: main-thread I/O is bounded, deferred or skipped inside its throttle."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from agent_takkub import auto_issue_signals, bg_pool, graft_autobuild


def test_call_bounded_returns_default_then_hands_over_the_late_result():
    release = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        release.wait(5)
        return "value"

    started = time.monotonic()
    assert bg_pool.call_bounded("k-1044", slow, timeout=0.05, default="late") == "late"
    assert time.monotonic() - started < 1.0
    assert bg_pool.call_bounded("k-1044", slow, timeout=0.05, default="late") == "late"
    release.set()
    assert bg_pool.call_bounded("k-1044", slow, timeout=2, default="late") == "value"
    assert calls == [1], "a stuck read must not be started again while it runs"
    assert bg_pool.call_bounded("k-1044", lambda: "fresh", timeout=2) == "fresh"


def test_call_bounded_swallows_errors():
    def boom():
        raise OSError("disk")

    assert bg_pool.call_bounded("k-1044-err", boom, timeout=2, default=None) is None


def test_live_resync_leaves_before_touching_the_disk_inside_its_throttle(tmp_path, monkeypatch):
    monkeypatch.setattr(graft_autobuild, "_skip_env", lambda: False)
    monkeypatch.setattr(graft_autobuild, "_graft_cli", lambda: "graft")
    key = str(tmp_path.resolve())
    monkeypatch.setitem(graft_autobuild._resolved_cwd_keys, str(tmp_path), key)
    monkeypatch.setitem(graft_autobuild._last_live_resync, key, time.monotonic())

    def no_disk(*_a, **_k):
        raise AssertionError("resolve() ran inside the throttle window")

    monkeypatch.setattr(graft_autobuild.Path, "resolve", no_disk)
    graft_autobuild.resync_staging_only(str(tmp_path))


def test_git_lookup_is_cached(monkeypatch):
    seen = []
    monkeypatch.setattr(graft_autobuild, "_git_bin_cache", None)
    monkeypatch.setattr(
        graft_autobuild.shutil, "which", lambda name: seen.append(name) or "/usr/bin/git"
    )
    assert graft_autobuild._git_bin() == graft_autobuild._git_bin() == "/usr/bin/git"
    assert seen == ["git"]


def test_long_stall_report_names_its_call_site_and_dedups_per_site(tmp_path, monkeypatch):
    log = tmp_path / "events.log"
    now = datetime.now()

    def stall(frame):
        return json.dumps(
            {
                "ts": now.isoformat(timespec="seconds"),
                "event": "main_thread_stall",
                "duration_ms": 17000,
                "stack": ["pathlib.py:stat:1013", f"agent_takkub/{frame}"],
            }
        )

    log.write_text(stall("orchestrator.py:_evidence_stat_mtime:8589") + "\n", encoding="utf-8")
    (hit,) = auto_issue_signals.scan_for_signals(log, now=now)
    assert hit.site == "orchestrator.py:_evidence_stat_mtime"
    title, body = auto_issue_signals.build_issue(hit)
    assert "_evidence_stat_mtime" in title and "17000ms" in body

    reserved = []
    from agent_takkub import auto_issue_capture, issues

    monkeypatch.setattr(auto_issue_capture, "_auto_issue_suppressed", lambda: False)
    monkeypatch.setattr(auto_issue_signals, "auto_issue_enabled", lambda: True)
    monkeypatch.setattr(
        auto_issue_capture, "reserve_signature", lambda sig: reserved.append(sig) or True
    )
    monkeypatch.setattr(issues, "new_issue", lambda *a, **k: None)
    auto_issue_signals.file_signal_issue(hit)
    log.write_text(stall("role_messages.py:read:89") + "\n", encoding="utf-8")
    auto_issue_signals.file_signal_issue(auto_issue_signals.scan_for_signals(log, now=now)[0])
    assert len(set(reserved)) == 2, "a stall at a new call site is a new report"
