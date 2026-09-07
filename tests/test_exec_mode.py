"""Tests for exec_mode: SOLO/PARALLEL derived from the team preset (#515),
plus the machine-capacity telemetry (unaffected by that diet)."""

from __future__ import annotations

import pytest

from agent_takkub import exec_mode, team_preset


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(team_preset, "_BASE_DIR", tmp_path)


def test_default_project_is_parallel():
    """ "auto" (the default preset — see `team_preset.BUILTIN_PRESETS`/
    `_resolve`) resolves to PARALLEL."""
    assert exec_mode.current() == exec_mode.PARALLEL
    assert exec_mode.is_parallel() is True


def test_solo_lead_preset_is_solo():
    team_preset.set_current("solo-lead", "proj")
    assert exec_mode.current("proj") == exec_mode.SOLO
    assert exec_mode.is_parallel("proj") is False


def test_full_preset_is_parallel():
    team_preset.set_current("full", "proj")
    assert exec_mode.current("proj") == exec_mode.PARALLEL
    assert exec_mode.is_parallel("proj") is True


def test_custom_preset_honours_its_own_exec_mode():
    team_preset.set_current(
        "custom",
        "proj",
        custom={"roles": {}, "checker": None, "exec_mode": "solo"},
    )
    assert exec_mode.is_parallel("proj") is False


def test_archives_legacy_exec_mode_file_once(tmp_path, monkeypatch):
    """A pre-#515 `exec-mode.json` is moved to `backups/`, never deleted."""
    from agent_takkub import config

    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    legacy = tmp_path / "exec-mode.json"
    legacy.write_text('{"mode": "parallel"}', encoding="utf-8")  # real prod shape

    exec_mode.current()

    assert not legacy.exists()
    assert (tmp_path / "backups" / "exec-mode.json").is_file()


def test_max_fanout_is_bounded():
    assert isinstance(exec_mode.MAX_FANOUT, int)
    assert 2 <= exec_mode.MAX_FANOUT <= 16


def test_machine_fanout_cap_bounds():
    cap = exec_mode.machine_fanout_cap()
    assert isinstance(cap, int)
    assert 1 <= cap <= exec_mode.MAX_FANOUT


def test_machine_caps_use_stable_total_ram_headroom(monkeypatch):
    """Regression for #117: a transient low available sample must not yield cap 1."""
    # Reported host: 8 logical cores, 16 GB total. Even if available temporarily
    # dips to 1 GB, the stable 25%-of-total baseline provides 4 GB of headroom,
    # so CPU remains the tighter budget at four panes.
    monkeypatch.setattr(exec_mode.os, "cpu_count", lambda: 8)

    class _VM:
        total = 16 * 1024**3
        available = 1 * 1024**3

    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM())
    assert exec_mode.machine_fanout_cap() == exec_mode.MAX_FANOUT
    assert exec_mode.machine_total_pane_cap() == 4


def test_machine_fanout_cap_limited_by_low_ram(monkeypatch):
    # Tiny host: 25% of 2 GB is one 0.5 GB pane, so RAM remains the tighter cap.
    monkeypatch.setattr(exec_mode.os, "cpu_count", lambda: 32)

    class _VM:
        total = 2 * 1024**3
        available = 128 * 1024**2

    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM())
    assert exec_mode.machine_fanout_cap() == 1


def test_machine_fanout_cap_capped_at_max(monkeypatch):
    # Huge machine → still never exceeds MAX_FANOUT.
    monkeypatch.setattr(exec_mode.os, "cpu_count", lambda: 64)

    class _VM:
        total = 256 * 1024**3
        available = 256 * 1024**3

    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM())
    assert exec_mode.machine_fanout_cap() == exec_mode.MAX_FANOUT
