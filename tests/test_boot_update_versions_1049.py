"""Boot update: version parsing, the "มีอัพเดต" decision, and the opencode
package rename (#1049 #1050 #1051)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agent_takkub import provider_update as pu
from agent_takkub.boot_flow_window import _has_update
from agent_takkub.claude_update import parse_version
from agent_takkub.provider_spec import PROVIDER_REGISTRY


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("opencode v2.3.2", "2.3.2"),
        ("v2.3.2", "2.3.2"),
        ("2.1.156 (Claude Code)", "2.1.156"),
        ("2.1.156\n", "2.1.156"),
        ("codex-cli 0.169.0", "0.169.0"),
        ("1.19.5", "1.19.5"),
        ("no version here", None),
        ("", None),
    ],
)
def test_parse_version_keeps_major_after_v_prefix(text, expected):
    assert parse_version(text) == expected


def test_lower_latest_is_not_an_update():
    item = SimpleNamespace(name="opencode", current="2.3.2", latest="1.19.6")
    assert _has_update(item) is False


def test_newer_latest_is_an_update():
    assert _has_update(SimpleNamespace(current="2.3.2", latest="2.4.0")) is True
    assert _has_update(SimpleNamespace(current="2.3.2", latest="2.3.2")) is False
    assert _has_update(SimpleNamespace(current="2.9.0", latest="2.10.0")) is True


def test_ui_follows_the_status_check_provider_updates_computed():
    up_to_date = SimpleNamespace(current="3.2", latest="1.19.6", status="up_to_date")
    available = SimpleNamespace(current="1.0.0", latest="1.0.1", status="update_available")
    assert _has_update(up_to_date) is False
    assert _has_update(available) is True


def test_opencode_installs_the_current_package():
    spec = PROVIDER_REGISTRY["opencode"]
    assert spec.install_command[-1] == "@opencode/cli"
    assert "opencode-ai" in spec.legacy_npm_packages


def _proc(rc=0, out=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


def test_installed_legacy_package_is_removed_before_the_install(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, timeout):
        calls.append(argv[1:])
        if argv[1] == "ls":
            return _proc(0, "+-- opencode-ai@1.19.5\n")
        return _proc(0)

    monkeypatch.setattr(pu, "_run", fake_run)
    spec = SimpleNamespace(legacy_npm_packages=("opencode-ai",))
    assert pu._remove_legacy_npm_packages("opencode", spec, "npm") is None
    assert calls[-1] == ["uninstall", "-g", "opencode-ai"]


def test_absent_legacy_package_is_left_alone(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, timeout):
        calls.append(argv[1:])
        return _proc(1, "`-- (empty)\n")

    monkeypatch.setattr(pu, "_run", fake_run)
    spec = SimpleNamespace(legacy_npm_packages=("opencode-ai",))
    assert pu._remove_legacy_npm_packages("opencode", spec, "npm") is None
    assert all(c[0] == "ls" for c in calls)


def test_failed_legacy_removal_stops_the_update(monkeypatch):
    def fake_run(argv, timeout):
        return _proc(0, "opencode-ai@1.19.5") if argv[1] == "ls" else _proc(1)

    monkeypatch.setattr(pu, "_run", fake_run)
    spec = SimpleNamespace(legacy_npm_packages=("opencode-ai",))
    outcome = pu._remove_legacy_npm_packages("opencode", spec, "npm")
    assert outcome is not None and outcome.status == pu.STATUS_FAILED


def test_spec_without_the_field_is_fine(monkeypatch):
    monkeypatch.setattr(pu, "_run", lambda *a, **k: pytest.fail("no npm call expected"))
    assert pu._remove_legacy_npm_packages("x", SimpleNamespace(), "npm") is None
