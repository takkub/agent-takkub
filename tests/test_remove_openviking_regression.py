"""Regression guardrails for the OpenViking removal
(docs/plans/remove-openviking-2026-08-24/08_TESTS.md) — the backend-owned
half: package/module gone, no `ov` CLI command, no network/process code
path can exist because the module that held it is deleted, and a leftover
v1.5.0 env var/runtime directory never breaks startup. UI-side regressions
(no OpenViking Settings page) are frontend's own test surface.
"""

from __future__ import annotations

import importlib

import pytest


def test_openviking_package_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_takkub.openviking")


def test_openviking_adapter_module_is_gone():
    """No `GET http://127.0.0.1:1933/health` code path can exist — the
    module that made that request no longer exists to import."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_takkub.core.context_sources.openviking_adapter")


def test_openviking_source_and_indexing_modules_are_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_takkub.core.context_sources.openviking_source")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_takkub.core.context_sources.indexing")


def test_core_boot_modules_import_without_openviking(monkeypatch):
    """The backend/core import surface a plain `takkub` boot touches
    (`cli`, `doctor`, `core.brain.facade`, `core.context_sources`) loads
    cleanly with no `agent_takkub.openviking*` module ever entering
    `sys.modules` — nothing on this path imports it, lazily or otherwise."""
    import sys

    for name in list(sys.modules):
        if name == "agent_takkub.openviking" or name.startswith("agent_takkub.openviking."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    importlib.import_module("agent_takkub.cli")
    importlib.import_module("agent_takkub.doctor")
    importlib.import_module("agent_takkub.core.brain.facade")
    importlib.import_module("agent_takkub.core.context_sources")

    leaked = [
        name
        for name in sys.modules
        if name == "agent_takkub.openviking" or name.startswith("agent_takkub.openviking.")
    ]
    assert leaked == []


def test_cli_ov_command_is_rejected(capsys):
    from agent_takkub import cli

    with pytest.raises(SystemExit):
        cli.main(["ov", "index"])
    assert "invalid choice" in capsys.readouterr().err


def test_cli_cleanup_openviking_command_exists():
    from agent_takkub import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["cleanup", "--help"])
    assert exc.value.code == 0


def test_secret_manager_has_no_openviking_backend():
    from agent_takkub.core.secrets.manager import default_backends

    assert "openviking" not in default_backends()


def test_secret_manager_ignores_legacy_openviking_env(monkeypatch):
    """A v1.5.0 user's `TAKKUB_OPENVIKING_API_KEY`/`OPENVIKING_API_KEY` env
    vars must be silently ignored, never crash `SecretManager` construction
    or any other provider's lookup."""
    monkeypatch.setenv("TAKKUB_OPENVIKING_API_KEY", "leftover-key")
    monkeypatch.setenv("OPENVIKING_API_KEY", "leftover-key")
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")

    from agent_takkub.core.secrets.manager import SecretManager

    SecretManager()  # must not raise


def test_doctor_run_all_checks_survives_legacy_env_and_leftover_dir(monkeypatch, tmp_path):
    """A leftover `~/.agent-takkub/services/openviking/` directory (v1.5.0)
    plus the old enable env var must not break `takkub doctor`."""
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_URL", "http://127.0.0.1:1933")
    leftover = tmp_path / "services" / "openviking"
    leftover.mkdir(parents=True)
    (leftover / "state.json").write_text("{}")

    from agent_takkub import doctor

    findings = doctor.run_all_checks()
    assert isinstance(findings, list)
    assert findings, "run_all_checks() must not return empty on legacy env + leftover dir"
    for f in findings:
        haystack = f"{f.name} {f.category} {f.detail}".lower()
        assert "openviking" not in haystack, f"stale openviking finding: {f!r}"
