"""Tests for `takkub provision` (cmd_provision) — detect-first, idempotent.

Provision installs the recommended plugin set + browser MCPs, but ONLY the
gaps: a machine that already has everything is a clean no-op (no `claude plugin
install` calls). All externals are monkeypatched so no network / real claude CLI
is touched.
"""

from __future__ import annotations

import argparse

from agent_takkub import cli, plugin_installer


def _patch_common(monkeypatch, *, on_disk, install_result):
    installed: list[str] = []

    def _install(p, **_k):
        installed.append(p.key)
        return install_result

    monkeypatch.setattr(plugin_installer, "installed_on_disk", lambda: set(on_disk))
    monkeypatch.setattr(plugin_installer, "ensure_marketplaces", lambda missing: {})
    monkeypatch.setattr(plugin_installer, "install_plugin", _install)
    monkeypatch.setattr(
        "agent_takkub.shared_dev_tools.ensure_browser_mcps", lambda: (True, "browser MCPs ready")
    )
    return installed


def test_provision_noop_when_all_present(monkeypatch):
    all_keys = {p.key for p in plugin_installer.RECOMMENDED}
    installed = _patch_common(monkeypatch, on_disk=all_keys, install_result=(True, "x"))
    res = cli.cmd_provision(argparse.Namespace())
    assert res["ok"] is True
    assert res["plugins_installed"] == []
    assert installed == []  # nothing was installed — true no-op


def test_provision_installs_only_missing(monkeypatch):
    # superpowers already present; the rest missing → install just the rest.
    installed = _patch_common(
        monkeypatch, on_disk={"superpowers"}, install_result=(True, "installed")
    )
    res = cli.cmd_provision(argparse.Namespace())
    assert res["ok"] is True
    expected = {p.key for p in plugin_installer.RECOMMENDED if p.key != "superpowers"}
    assert set(installed) == expected
    assert "superpowers" not in installed


def test_provision_reports_plugin_failure(monkeypatch):
    _patch_common(monkeypatch, on_disk=set(), install_result=(False, "boom"))
    res = cli.cmd_provision(argparse.Namespace())
    assert res["ok"] is False  # at least one plugin failed → overall not ok
