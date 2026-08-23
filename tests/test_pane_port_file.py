"""Tests for `_apply_port_file`, the helper that stamps the effective
cli_server port-file path into every pane's env.

Bug this fixes: a prod (single-instance) cockpit spawns a teammate pane
whose PATH resolves `takkub` to a dev-repo copy of the CLI (installed
ahead of it on PATH). In single-instance mode nothing sets
`TAKKUB_PORT_FILE` in the host process env, so the allowlist-copy path in
`_build_pane_env`/`_build_lead_env` used to omit the key entirely — the
pane's `takkub` CLI then fell back to whatever `runtime/port` its own
DATA_HOME resolved to (the dev repo's), which is a different cockpit's
port file → WinError 10061 connection refused, Lead can never spawn a
teammate.

`config._effective_port_file_for_app()` honours a `TAKKUB_PORT_FILE`
override when it was derived by THIS process itself (multi-instance mode,
marked via `_TAKKUB_AUTO_PORT_FILE`) or resolves under this instance's own
DATA_HOME, and falls back to this process's own `RUNTIME_DIR/port`
otherwise (single-instance, or a foreign override leaked in from a
different cockpit instance — #354) — exactly the value every spawned pane
should receive regardless of mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import config
from agent_takkub.orchestrator import _apply_port_file, _build_lead_env, _build_pane_env


@pytest.fixture
def runtime_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config's runtime dir at an isolated tmp path for this test."""
    runtime_dir = tmp_path / "runtime"
    port_file = runtime_dir / "port"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(config, "PORT_FILE", port_file)
    return port_file


class TestApplyPortFile:
    def test_sets_default_port_file_when_no_override(
        self, runtime_port: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        env: dict[str, str] = {}
        _apply_port_file(env)
        assert env["TAKKUB_PORT_FILE"] == str(runtime_port)

    def test_honours_multi_instance_override(
        self, runtime_port: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Same-DATA_HOME multi-instance mode: `_restart_env.configure_
        # multi_instance_port_file` sets BOTH TAKKUB_PORT_FILE and the
        # `_TAKKUB_AUTO_PORT_FILE` provenance marker together — reproduce
        # that pairing here rather than the override alone (#354 guard
        # rejects an override with no marker that isn't under DATA_HOME).
        override = tmp_path / "instance-42" / "port"
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(override))
        monkeypatch.setenv("_TAKKUB_AUTO_PORT_FILE", str(override))
        env: dict[str, str] = {}
        _apply_port_file(env)
        assert env["TAKKUB_PORT_FILE"] == str(override)

    def test_foreign_override_with_no_provenance_marker_is_rejected(
        self, runtime_port: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # #354: an override inherited from a DIFFERENT cockpit instance's
        # PANE (no _TAKKUB_AUTO_PORT_FILE marker, not under this instance's
        # own DATA_HOME, but carrying the TAKKUB_ROLE marker pane_env.py
        # stamps into every spawned pane) must NOT be stamped into a
        # newly-spawned pane — that would send the new pane's `takkub`
        # calls to the wrong cockpit. Without TAKKUB_ROLE this same path
        # is indistinguishable from a legitimate plain-shell user override
        # and must be honoured instead (config._PANE_MARKER_ENV).
        foreign = tmp_path / "other-instance" / "port"
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(foreign))
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        monkeypatch.delenv("_TAKKUB_AUTO_PORT_FILE", raising=False)
        env: dict[str, str] = {}
        _apply_port_file(env)
        assert env["TAKKUB_PORT_FILE"] == str(runtime_port)

    def test_overwrites_stale_value_already_in_env(
        self, runtime_port: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        env = {"TAKKUB_PORT_FILE": str(tmp_path / "stale" / "port")}
        _apply_port_file(env)
        assert env["TAKKUB_PORT_FILE"] == str(runtime_port)

    def test_no_return_value(self, runtime_port: Path) -> None:
        env: dict[str, str] = {}
        result = _apply_port_file(env)
        assert result is None


class TestBuildPaneEnvStampsPortFile:
    def test_single_instance_pane_env_gets_default_port_file(
        self, runtime_port: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reproduces the prod bug: single-instance mode never sets
        # TAKKUB_PORT_FILE in the host env, so before this fix the pane
        # env simply omitted the key.
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        env = _build_pane_env()
        assert env["TAKKUB_PORT_FILE"] == str(runtime_port)

    def test_multi_instance_pane_env_gets_override_value(
        self, runtime_port: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        override = tmp_path / "instance-7" / "port"
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(override))
        monkeypatch.setenv("_TAKKUB_AUTO_PORT_FILE", str(override))
        env = _build_pane_env()
        assert env["TAKKUB_PORT_FILE"] == str(override)


class TestBuildLeadEnvStampsPortFile:
    def test_single_instance_lead_env_gets_default_port_file(
        self, runtime_port: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Lead is the one running `takkub assign` — if its own env lacks
        # the right port file, it can never spawn a teammate at all.
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        env = _build_lead_env()
        assert env["TAKKUB_PORT_FILE"] == str(runtime_port)

    def test_multi_instance_lead_env_gets_override_value(
        self, runtime_port: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        override = tmp_path / "instance-3" / "port"
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(override))
        monkeypatch.setenv("_TAKKUB_AUTO_PORT_FILE", str(override))
        env = _build_lead_env()
        assert env["TAKKUB_PORT_FILE"] == str(override)
