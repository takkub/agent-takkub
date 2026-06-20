"""Tests for doctor.py — cockpit environment diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_takkub.doctor import (
    Finding,
    Status,
    check_mcps,
    check_plugins,
    check_projects,
    check_providers,
    check_runtime,
    run_all_checks,
)

# ---------------------------------------------------------------------------
# check_providers — agy resolved via the cockpit's own helper (off-PATH safe)
# ---------------------------------------------------------------------------


class TestCheckProviders:
    def _gemini(self, findings: list[Finding]) -> Finding:
        return next(f for f in findings if f.name == "gemini")

    def test_gemini_found_off_path_reports_installed(self) -> None:
        # agy installed under %LOCALAPPDATA%\agy\bin but NOT on PATH: doctor must
        # resolve it via find_agy_executable (same as the cockpit) → INFO, not
        # a misleading "not installed" SKIP.
        with (
            patch(
                "agent_takkub.gemini_helper.find_agy_executable",
                return_value="C:/x/agy/bin/agy.exe",
            ),
            patch("agent_takkub.doctor.shutil.which", return_value=None),  # off PATH
            patch("agent_takkub.doctor._run", return_value=(0, "1.0.10")),
        ):
            g = self._gemini(check_providers())
        assert g.status == Status.INFO
        assert "1.0.10" in g.detail

    def test_gemini_absent_reports_skip_with_antigravity_hint(self) -> None:
        with (
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value=None),
            patch("agent_takkub.doctor.shutil.which", return_value=None),
        ):
            g = self._gemini(check_providers())
        assert g.status == Status.SKIP
        assert "antigravity.google" in (g.fix_hint or "")


# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------


class TestFindingDataclass:
    def test_create_minimal(self) -> None:
        f = Finding("claude", "binary", Status.OK)
        assert f.category == "claude"
        assert f.name == "binary"
        assert f.status == Status.OK
        assert f.detail == ""
        assert f.fix_hint == ""
        assert f.auto_fix is None

    def test_create_full(self) -> None:
        f = Finding("runtime", "node", Status.FAIL, "not found", "install node", auto_fix=None)
        assert f.detail == "not found"
        assert f.fix_hint == "install node"

    def test_status_values(self) -> None:
        assert Status.OK.value == "ok"
        assert Status.WARN.value == "warn"
        assert Status.FAIL.value == "fail"
        assert Status.SKIP.value == "skip"
        assert Status.INFO.value == "info"

    def test_finding_serializable_to_dict(self) -> None:
        f = Finding("mcps", "playwright", Status.OK, "npx ok", "")
        d = {
            "category": f.category,
            "name": f.name,
            "status": f.status.value,
            "detail": f.detail,
            "fix_hint": f.fix_hint,
        }
        assert d["status"] == "ok"
        assert json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# check_runtime
# ---------------------------------------------------------------------------


class TestCheckRuntime:
    def test_node_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/node" if x == "node" else None)
        with patch("agent_takkub.doctor._run", return_value=(0, "v22.0.0")):
            findings = check_runtime()
        node = next(f for f in findings if f.name == "node")
        assert node.status == Status.OK
        assert "v22.0.0" in node.detail

    def test_node_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda x: None)
        findings = check_runtime()
        node = next(f for f in findings if f.name == "node")
        assert node.status == Status.FAIL
        assert "nodejs.org" in node.fix_hint

    def test_npx_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/node" if x == "node" else None)
        with patch("agent_takkub.doctor._run", return_value=(0, "v22.0.0")):
            findings = check_runtime()
        npx = next(f for f in findings if f.name == "npx")
        assert npx.status == Status.FAIL
        assert "Node" in npx.fix_hint

    def test_python_version_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda x: None)
        findings = check_runtime()
        python = next(f for f in findings if f.name == "python")
        assert python.status == Status.OK

    def test_python_old_version_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda x: None)
        import agent_takkub.doctor as _doc

        monkeypatch.setattr(_doc.sys, "version_info", (3, 10, 0))
        findings = check_runtime()
        python = next(f for f in findings if f.name == "python")
        assert python.status == Status.WARN


# ---------------------------------------------------------------------------
# check_projects
# ---------------------------------------------------------------------------


class TestCheckProjects:
    def _write_projects(self, tmp_path: Path, data: dict) -> None:
        projects_file = tmp_path / "projects.json"
        projects_file.write_text(json.dumps(data), encoding="utf-8")
        # monkeypatching is done in each test — this just builds the file

    def test_all_paths_exist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj_dir = tmp_path / "myapp"
        proj_dir.mkdir()
        data = {
            "active": "myapp",
            "projects": {"myapp": {"paths": {"api": str(proj_dir)}}},
            "open_tabs": ["myapp"],
        }
        projects_file = tmp_path / "projects.json"
        projects_file.write_text(json.dumps(data), encoding="utf-8")

        import agent_takkub.config as _cfg

        monkeypatch.setattr(_cfg, "PROJECTS_JSON", projects_file)

        findings = check_projects()
        fails = [f for f in findings if f.status == Status.FAIL]
        assert not fails

    def test_missing_path_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data = {
            "active": "myapp",
            "projects": {"myapp": {"paths": {"api": str(tmp_path / "nonexistent")}}},
            "open_tabs": [],
        }
        projects_file = tmp_path / "projects.json"
        projects_file.write_text(json.dumps(data), encoding="utf-8")

        import agent_takkub.config as _cfg

        monkeypatch.setattr(_cfg, "PROJECTS_JSON", projects_file)

        findings = check_projects()
        fails = [f for f in findings if f.status == Status.FAIL and f.name == "myapp"]
        assert fails

    def test_orphaned_tab_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data = {
            "active": "myapp",
            "projects": {"myapp": {"paths": {}}},
            "open_tabs": ["ghost-project"],
        }
        projects_file = tmp_path / "projects.json"
        projects_file.write_text(json.dumps(data), encoding="utf-8")

        import agent_takkub.config as _cfg

        monkeypatch.setattr(_cfg, "PROJECTS_JSON", projects_file)

        findings = check_projects()
        warns = [f for f in findings if f.status == Status.WARN and "orphaned" in f.detail]
        assert warns

    def test_active_not_in_projects_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = {
            "active": "missing-proj",
            "projects": {"other": {"paths": {}}},
            "open_tabs": [],
        }
        projects_file = tmp_path / "projects.json"
        projects_file.write_text(json.dumps(data), encoding="utf-8")

        import agent_takkub.config as _cfg

        monkeypatch.setattr(_cfg, "PROJECTS_JSON", projects_file)

        findings = check_projects()
        warns = [f for f in findings if f.status == Status.WARN and "active" in f.name]
        assert warns


# ---------------------------------------------------------------------------
# check_plugins
# ---------------------------------------------------------------------------


class TestCheckPlugins:
    def _make_plugin(
        self,
        cache_root: Path,
        marketplace: str,
        plugin_name: str | None = None,
        version: str = "1.0.0",
    ) -> None:
        """Create cache_root/<marketplace>/<plugin>/<version>/.claude-plugin/plugin.json"""
        plugin_name = plugin_name or marketplace
        plugin_root = cache_root / marketplace / plugin_name / version
        (plugin_root / ".claude-plugin").mkdir(parents=True)
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"version": version}), encoding="utf-8"
        )

    def test_plugin_present_ok(self, tmp_path: Path) -> None:
        self._make_plugin(tmp_path, "superpowers-dev", version="0.4.2")
        with patch("agent_takkub.orchestrator._SAFE_PLUGINS", ("superpowers-dev",)):
            findings = check_plugins(cache_root=tmp_path)

        ok = next(f for f in findings if f.name == "superpowers-dev")
        assert ok.status == Status.OK
        assert "0.4.2" in ok.detail

    def test_plugin_missing_warns(self, tmp_path: Path) -> None:
        with patch("agent_takkub.orchestrator._SAFE_PLUGINS", ("missing-plugin",)):
            findings = check_plugins(cache_root=tmp_path)

        warn = next(f for f in findings if f.name == "missing-plugin")
        assert warn.status == Status.WARN

    def test_plugin_broken_json_fails(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "bad-plugin" / "bad-plugin" / "1.0.0" / ".claude-plugin"
        version_dir.mkdir(parents=True)
        (version_dir / "plugin.json").write_text("not json", encoding="utf-8")

        with patch("agent_takkub.orchestrator._SAFE_PLUGINS", ("bad-plugin",)):
            findings = check_plugins(cache_root=tmp_path)

        fail = next(f for f in findings if f.name == "bad-plugin")
        assert fail.status == Status.FAIL

    def test_ecc_warns_with_session_start_hint(self, tmp_path: Path) -> None:
        self._make_plugin(tmp_path, "ecc", version="2.0.0")
        with patch("agent_takkub.orchestrator._SAFE_PLUGINS", ("ecc",)):
            findings = check_plugins(cache_root=tmp_path)

        ecc = next(f for f in findings if f.name == "ecc")
        assert ecc.status == Status.WARN
        assert "SessionStart" in ecc.detail

    def test_ecc_not_installed_is_skip_not_warn(self, tmp_path: Path) -> None:
        """ECC is intentionally not installed — doctor must not nag to install
        it (SKIP, no fix hint), unlike any other missing _SAFE_PLUGINS entry."""
        with patch("agent_takkub.orchestrator._SAFE_PLUGINS", ("ecc",)):
            findings = check_plugins(cache_root=tmp_path)

        ecc = next(f for f in findings if f.name == "ecc")
        assert ecc.status == Status.SKIP
        assert ecc.fix_hint == ""
        assert "intentional" in ecc.detail

    def test_marketplace_dir_without_plugin_json_fails(self, tmp_path: Path) -> None:
        """A marketplace dir exists but has no .claude-plugin/plugin.json anywhere in its tree
        → should FAIL, not silently accept."""
        (tmp_path / "broken-mp" / "broken-plugin" / "1.0.0").mkdir(parents=True)
        # no plugin.json anywhere
        with patch("agent_takkub.orchestrator._SAFE_PLUGINS", ("broken-mp",)):
            findings = check_plugins(cache_root=tmp_path)
        fail = next(f for f in findings if f.name == "broken-mp")
        assert fail.status == Status.FAIL

    def test_picks_highest_version_dir(self, tmp_path: Path) -> None:
        """When multiple version dirs exist, doctor picks the highest (reverse-sorted)."""
        self._make_plugin(tmp_path, "ecc", "ecc", "1.0.0")
        self._make_plugin(tmp_path, "ecc", "ecc", "2.0.0-rc.1")
        with patch("agent_takkub.orchestrator._SAFE_PLUGINS", ("ecc",)):
            findings = check_plugins(cache_root=tmp_path)
        ecc = next(f for f in findings if f.name == "ecc")
        assert "2.0.0-rc.1" in ecc.detail


# ---------------------------------------------------------------------------
# check_mcps
# ---------------------------------------------------------------------------


class TestCheckMcps:
    def test_missing_file_warns_with_autofix(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "nope.json"
        findings = check_mcps(shared_mcp_file=nonexistent)
        warn = next(f for f in findings if f.name == "shared-mcp.json")
        assert warn.status == Status.WARN
        assert warn.auto_fix is not None

    def test_broken_json_fails(self, tmp_path: Path) -> None:
        bad = tmp_path / "shared-mcp.json"
        bad.write_text("not json", encoding="utf-8")
        findings = check_mcps(shared_mcp_file=bad)
        fail = next(f for f in findings if f.name == "shared-mcp.json")
        assert fail.status == Status.FAIL
        assert fail.auto_fix is None

    def test_valid_file_reports_server_count(self, tmp_path: Path) -> None:
        mcp_file = tmp_path / "shared-mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"type": "stdio", "command": "npx", "args": []},
                        "chrome-devtools": {"type": "stdio", "command": "npx", "args": []},
                    }
                }
            ),
            encoding="utf-8",
        )
        findings = check_mcps(shared_mcp_file=mcp_file)
        summary = next(f for f in findings if f.name == "shared-mcp.json")
        assert summary.status == Status.OK
        assert "2" in summary.detail

    def test_npx_servers_ok(self, tmp_path: Path) -> None:
        mcp_file = tmp_path / "shared-mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {
                            "type": "stdio",
                            "command": "npx",
                            "args": ["-y", "@playwright/mcp"],
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        findings = check_mcps(shared_mcp_file=mcp_file)
        pw = next(f for f in findings if f.name == "playwright")
        assert pw.status == Status.OK

    def test_missing_command_warns(self, tmp_path: Path) -> None:
        mcp_file = tmp_path / "shared-mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "custom-tool": {
                            "type": "stdio",
                            "command": "totally-not-installed-xyz-tool",
                            "args": [],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        findings = check_mcps(shared_mcp_file=mcp_file)
        ct = next(f for f in findings if f.name == "custom-tool")
        assert ct.status == Status.WARN

    def test_auto_fix_calls_ensure_functions(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "nope.json"
        findings = check_mcps(shared_mcp_file=nonexistent)
        warn = next(f for f in findings if f.auto_fix is not None)

        called = []

        def fake_ensure_browser() -> tuple[bool, str]:
            called.append("browser")
            return True, "browser ok"

        def fake_ensure_user() -> tuple[bool, str]:
            called.append("user")
            return True, "user ok"

        with (
            patch("agent_takkub.shared_dev_tools.ensure_browser_mcps", fake_ensure_browser),
            patch("agent_takkub.shared_dev_tools.ensure_user_mcps", fake_ensure_user),
        ):
            ok, _msg = warn.auto_fix()

        assert ok
        assert "browser" in called
        assert "user" in called


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------


class TestRunAllChecks:
    def test_returns_list_of_findings(self) -> None:
        with (
            patch(
                "agent_takkub.doctor.check_claude",
                return_value=[Finding("claude", "binary", Status.OK)],
            ),
            patch(
                "agent_takkub.doctor.check_runtime",
                return_value=[Finding("runtime", "node", Status.OK)],
            ),
            patch("agent_takkub.doctor.check_plugins", return_value=[]),
            patch("agent_takkub.doctor.check_mcps", return_value=[]),
            patch("agent_takkub.doctor.check_projects", return_value=[]),
            patch("agent_takkub.doctor.check_providers", return_value=[]),
            patch("agent_takkub.doctor.check_hooks", return_value=[]),
            patch("agent_takkub.doctor.check_ready_markers", return_value=[]),
        ):
            findings = run_all_checks()

        assert isinstance(findings, list)
        assert all(isinstance(f, Finding) for f in findings)
        assert len(findings) == 2


# ---------------------------------------------------------------------------
# exit code logic via cmd_doctor
# ---------------------------------------------------------------------------


class TestCmdDoctorExitCode:
    def test_ok_when_no_fails(self) -> None:
        findings = [Finding("claude", "binary", Status.OK)]
        with patch("agent_takkub.doctor.run_all_checks", return_value=findings):
            from agent_takkub import cli

            result = cli.main(["doctor"])
        assert result == 0

    def test_exit_1_when_fail(self) -> None:
        findings = [Finding("claude", "binary", Status.FAIL, "not found")]
        with patch("agent_takkub.doctor.run_all_checks", return_value=findings):
            from agent_takkub import cli

            result = cli.main(["doctor"])
        assert result == 1

    def test_json_output_is_valid(self, capsys: pytest.CaptureFixture[str]) -> None:
        findings = [
            Finding("runtime", "node", Status.OK, "v22.0.0"),
            Finding("runtime", "npx", Status.FAIL, "not found", "reinstall node"),
        ]
        with patch("agent_takkub.doctor.run_all_checks", return_value=findings):
            from agent_takkub import cli

            cli.main(["doctor", "--json"])

        captured = capsys.readouterr()
        # find JSON in output (may have trailing ok:/err: line)
        parsed = json.loads(captured.out.split("\n\nok:")[0].split("\nerr:")[0].strip())
        assert isinstance(parsed, list)
        assert len(parsed) == 2
        assert parsed[0]["status"] == "ok"
        assert parsed[1]["status"] == "fail"
