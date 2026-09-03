"""Tests for smoke_gate.py — running-stack smoke test for the Node qa-gate
(#475). `docker compose ps` and the smoke script itself are always faked
(never a real docker daemon on CI); `find_smoke_script`/`_find_compose_file`
use real tmp dirs since that's exactly what's under test there."""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_takkub import smoke_gate


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _write_pkg(root: Path, scripts: dict) -> dict:
    pkg = {"scripts": scripts}
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    return pkg


# ---------------------------------------------------------------------------
# find_smoke_script
# ---------------------------------------------------------------------------


def test_find_smoke_script_root(tmp_path: Path) -> None:
    pkg = _write_pkg(tmp_path, {"smoke": "playwright test smoke"})
    assert smoke_gate.find_smoke_script(tmp_path, pkg) == (tmp_path, "smoke")


def test_find_smoke_script_priority_order(tmp_path: Path) -> None:
    pkg = _write_pkg(tmp_path, {"test:smoke": "x", "smoke": "y"})
    assert smoke_gate.find_smoke_script(tmp_path, pkg) == (tmp_path, "smoke")


def test_find_smoke_script_none_when_absent(tmp_path: Path) -> None:
    pkg = _write_pkg(tmp_path, {"test": "vitest run"})
    assert smoke_gate.find_smoke_script(tmp_path, pkg) is None


def test_find_smoke_script_in_workspace_package(tmp_path: Path) -> None:
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n", encoding="utf-8")
    pkg = _write_pkg(tmp_path, {"test": "vitest run"})
    api = tmp_path / "apps" / "api"
    api.mkdir(parents=True)
    (api / "package.json").write_text(
        json.dumps({"scripts": {"e2e:smoke": "vitest run smoke"}}), encoding="utf-8"
    )

    assert smoke_gate.find_smoke_script(tmp_path, pkg) == (api, "e2e:smoke")


# ---------------------------------------------------------------------------
# run_smoke_check — the wiring: script + stack presence gate whether/how it runs
# ---------------------------------------------------------------------------


def test_no_step_when_no_smoke_script(tmp_path: Path) -> None:
    pkg = {"scripts": {"test": "vitest run"}}
    assert smoke_gate.run_smoke_check(tmp_path, pkg, "npm", os.environ.copy()) is None


def test_no_step_when_disabled_via_env(tmp_path: Path, monkeypatch) -> None:
    pkg = {"scripts": {"smoke": "vitest run smoke"}}
    (tmp_path / "docker-compose.yml").write_text("", encoding="utf-8")
    called = []
    monkeypatch.setattr(
        smoke_gate.subprocess,
        "run",
        lambda cmd, **kw: called.append(cmd) or _FakeCompleted(0),
    )
    env = os.environ.copy()
    env["TAKKUB_QA_SMOKE"] = "0"

    result = smoke_gate.run_smoke_check(tmp_path, pkg, "npm", env)

    assert result is None
    assert not called, "TAKKUB_QA_SMOKE=0 must not shell out at all"


def test_skips_visibly_when_no_compose_file(tmp_path: Path) -> None:
    pkg = {"scripts": {"smoke": "vitest run smoke"}}
    finding = smoke_gate.run_smoke_check(tmp_path, pkg, "npm", os.environ.copy())
    assert finding is not None
    assert finding.ok is True
    assert finding.skipped is True
    assert "stack" in finding.detail.lower()


def test_skips_visibly_when_stack_not_running(tmp_path: Path, monkeypatch) -> None:
    pkg = {"scripts": {"smoke": "vitest run smoke"}}
    (tmp_path / "docker-compose.yml").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        smoke_gate.subprocess, "run", lambda cmd, **kw: _FakeCompleted(0, stdout="")
    )

    finding = smoke_gate.run_smoke_check(tmp_path, pkg, "npm", os.environ.copy())

    assert finding.ok is True
    assert finding.skipped is True


def test_runs_and_passes_when_stack_running(tmp_path: Path, monkeypatch) -> None:
    pkg = {"scripts": {"smoke": "vitest run smoke"}}
    (tmp_path / "docker-compose.yml").write_text("", encoding="utf-8")
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "compose" in cmd:
            return _FakeCompleted(0, stdout="gateway\n")
        return _FakeCompleted(0, stdout="3 passed")

    monkeypatch.setattr(smoke_gate.subprocess, "run", fake_run)

    finding = smoke_gate.run_smoke_check(tmp_path, pkg, "npm", os.environ.copy())

    assert finding.ok is True
    assert finding.skipped is False
    assert len(calls) == 2, "compose ps, then the smoke script itself"


def test_runs_and_fails_the_gate_when_the_script_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    pkg = {"scripts": {"smoke": "vitest run smoke"}}
    (tmp_path / "docker-compose.yml").write_text("", encoding="utf-8")

    def fake_run(cmd, **kw):
        if "compose" in cmd:
            return _FakeCompleted(0, stdout="gateway\n")
        return _FakeCompleted(1, stdout="", stderr="1 failed")

    monkeypatch.setattr(smoke_gate.subprocess, "run", fake_run)

    finding = smoke_gate.run_smoke_check(tmp_path, pkg, "npm", os.environ.copy())

    assert finding.ok is False
    assert finding.skipped is False


def test_smoke_timeout_env_override_is_used(tmp_path: Path, monkeypatch) -> None:
    pkg = {"scripts": {"smoke": "vitest run smoke"}}
    (tmp_path / "docker-compose.yml").write_text("", encoding="utf-8")
    seen_timeouts: list = []

    def fake_run(cmd, **kw):
        if "compose" in cmd:
            return _FakeCompleted(0, stdout="gateway\n")
        seen_timeouts.append(kw.get("timeout"))
        return _FakeCompleted(0)

    monkeypatch.setattr(smoke_gate.subprocess, "run", fake_run)
    env = os.environ.copy()
    env["TAKKUB_QA_SMOKE_TIMEOUT_S"] = "45"

    smoke_gate.run_smoke_check(tmp_path, pkg, "npm", env)

    assert seen_timeouts == [45.0]
