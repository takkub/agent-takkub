"""Regression tests for #905 (preset deny text), #908 (git read-only on a
shared tree) and #909 (busy_machine release condition), 2026-10-23."""

from __future__ import annotations

import json
import time

from agent_takkub import pane_guard, team_preset

# --- #908 -------------------------------------------------------------------


def test_git_version_and_init_allowed_on_shared_tree() -> None:
    for cmd in ("git --version", "git -C sub --version", "git init", "git init app"):
        assert pane_guard.classify(cmd, "backend", cwd=".").allowed, cmd


def test_git_init_shapes_that_reach_existing_state_still_denied() -> None:
    for cmd in ("git init --separate-git-dir=/tmp/x", "git init --template=/tmp/t"):
        assert not pane_guard.classify(cmd, "backend", cwd=".").allowed, cmd


def test_shared_tree_deny_says_chain_did_not_run() -> None:
    verdict = pane_guard.classify("mkdir x && git gc", "backend", cwd=".")
    assert not verdict.allowed
    assert verdict.rule == "git_shared_default_deny:gc"
    assert "&&" in verdict.reason


# --- #909 -------------------------------------------------------------------


def _state(tmp_path, **fields):
    path = tmp_path / "machine-state.json"
    path.write_text(json.dumps({"ts": time.time(), "working_panes": [], **fields}), "utf-8")
    return path


def test_single_build_ignores_sticky_overload_latch(tmp_path) -> None:
    path = _state(tmp_path, ram_percent=60.0, cpu_percent=30.0, overloaded=True)
    busy, _ = pane_guard._is_machine_busy_from_snapshot(
        "frontend", machine_state_path=path, check_working_panes=False
    )
    assert not busy
    busy, reason = pane_guard._is_machine_busy_from_snapshot(
        "frontend", machine_state_path=path, check_working_panes=True
    )
    assert busy and "CPU 30%" in reason


def test_busy_reason_names_live_values_and_release(tmp_path) -> None:
    path = _state(tmp_path, ram_percent=60.0, cpu_percent=95.0, overloaded=False)
    busy, reason = pane_guard._is_machine_busy_from_snapshot(
        "frontend", machine_state_path=path, check_working_panes=False
    )
    assert busy
    assert "CPU 95%" in reason and "ปลดเมื่อ" in reason


# --- #905 -------------------------------------------------------------------


def test_extra_position_deny_does_not_suggest_team_full() -> None:
    cfg = {**team_preset.BUILTIN_PRESETS["full"], "preset": "full"}
    ok, msg = team_preset._can_spawn_from_cfg("docs", cfg)
    assert not ok
    assert "--team full" not in msg and "Settings" in msg


def test_core_position_under_pair_still_suggests_team_full() -> None:
    cfg = {**team_preset.BUILTIN_PRESETS["full"], "preset": "custom"}
    cfg["roles"] = dict.fromkeys(cfg["roles"], False)
    ok, msg = team_preset._can_spawn_from_cfg("backend", cfg)
    assert not ok and "--team full" in msg
