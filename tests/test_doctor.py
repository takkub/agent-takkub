"""Tests for doctor.py — cockpit environment diagnostics."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_takkub.doctor import (
    Finding,
    Status,
    check_arch,
    check_claude,
    check_installed_integrity,
    check_mcps,
    check_plugins,
    check_projects,
    check_providers,
    check_qt,
    check_runtime,
    run_all_checks,
)

# ---------------------------------------------------------------------------
# check_arch — Apple Silicon Rosetta / native-arm64 shell hygiene
# ---------------------------------------------------------------------------


class TestCheckArch:
    def _sysctl(self, arm64: str, translated: str):
        """side_effect for doctor._run keyed on the sysctl key being queried."""

        def _side(argv: list[str]) -> tuple[int, str]:
            key = argv[-1]
            if key == "hw.optional.arm64":
                return 0, arm64
            if key == "sysctl.proc_translated":
                return 0, translated
            return 1, ""

        return _side

    def _f(self, findings: list[Finding], name: str) -> Finding:
        return next(f for f in findings if f.name == name)

    def test_non_darwin_returns_empty(self) -> None:
        # Windows/Linux: the whole topic is moot — must not clutter the report.
        with patch("agent_takkub.doctor.sys.platform", "win32"):
            assert check_arch() == []

    def test_intel_mac_reports_na(self) -> None:
        # Genuine Intel Mac: no arm64 slice → single benign OK, no Rosetta nag.
        with (
            patch("agent_takkub.doctor.sys.platform", "darwin"),
            patch("agent_takkub.doctor._run", side_effect=self._sysctl(arm64="0", translated="")),
        ):
            findings = check_arch()
        assert len(findings) == 1
        assert findings[0].name == "cpu"
        assert findings[0].status is Status.OK

    def test_apple_silicon_native_shell_all_ok(self) -> None:
        with (
            patch("agent_takkub.doctor.sys.platform", "darwin"),
            patch("agent_takkub.doctor._run", side_effect=self._sysctl(arm64="1", translated="0")),
            patch("agent_takkub.doctor._rosetta_installed", return_value=True),
            patch("agent_takkub.doctor._zshrc_has_guard", return_value=True),
        ):
            findings = check_arch()
        assert self._f(findings, "shell").status is Status.OK
        assert self._f(findings, "rosetta").status is Status.OK
        assert self._f(findings, "zshrc-guard").status is Status.OK

    def test_apple_silicon_translated_shell_warns(self) -> None:
        # The core trap: shell under Rosetta → x86_64 wheels. Must WARN.
        with (
            patch("agent_takkub.doctor.sys.platform", "darwin"),
            patch("agent_takkub.doctor._run", side_effect=self._sysctl(arm64="1", translated="1")),
            patch("agent_takkub.doctor._rosetta_installed", return_value=True),
            patch("agent_takkub.doctor._zshrc_has_guard", return_value=True),
        ):
            findings = check_arch()
        assert self._f(findings, "shell").status is Status.WARN

    def test_missing_rosetta_and_guard_offer_auto_fix(self) -> None:
        with (
            patch("agent_takkub.doctor.sys.platform", "darwin"),
            patch("agent_takkub.doctor._run", side_effect=self._sysctl(arm64="1", translated="0")),
            patch("agent_takkub.doctor._rosetta_installed", return_value=False),
            patch("agent_takkub.doctor._zshrc_has_guard", return_value=False),
        ):
            findings = check_arch()
        rosetta = self._f(findings, "rosetta")
        guard = self._f(findings, "zshrc-guard")
        assert rosetta.status is Status.WARN and rosetta.auto_fix is not None
        assert guard.status is Status.WARN and guard.auto_fix is not None

    def test_zshrc_guard_auto_fix_appends_block(self, tmp_path) -> None:
        # The auto_fix must append the marked, Rosetta-safe guard idempotently.
        from agent_takkub.doctor import _ARM64_GUARD_MARKER

        fake_home = tmp_path
        zshrc = fake_home / ".zshrc"
        zshrc.write_text("export PATH=/usr/bin\n", encoding="utf-8")
        with (
            patch("agent_takkub.doctor.sys.platform", "darwin"),
            patch("agent_takkub.doctor._run", side_effect=self._sysctl(arm64="1", translated="1")),
            patch("agent_takkub.doctor._rosetta_installed", return_value=True),
            patch("agent_takkub.doctor.Path.home", return_value=fake_home),
        ):
            findings = check_arch()
            guard = self._f(findings, "zshrc-guard")
            assert guard.auto_fix is not None
            ok, _ = guard.auto_fix()
            assert ok
            body = zshrc.read_text(encoding="utf-8")
            assert _ARM64_GUARD_MARKER in body
            assert body.startswith("export PATH=/usr/bin")  # original preserved
            # idempotent: a second run must not duplicate the block
            ok2, _ = guard.auto_fix()
            assert ok2
            assert zshrc.read_text(encoding="utf-8").count(_ARM64_GUARD_MARKER) == 1


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
# check_qt — Qt 6.8 LTS pin + crash-guard gate
# ---------------------------------------------------------------------------


class TestCheckQt:
    def _f(self, findings: list[Finding], name: str) -> Finding:
        return next(f for f in findings if f.name == name)

    def test_pinned_68_is_ok_no_autofix(self) -> None:
        with patch("PyQt6.QtCore.QT_VERSION_STR", "6.8.2"):
            v = self._f(check_qt(), "version")
        assert v.status == Status.OK
        assert v.auto_fix is None
        assert "6.8.2" in v.detail

    def test_611_flagged_fail_with_autofix_and_regression_note(self) -> None:
        with patch("PyQt6.QtCore.QT_VERSION_STR", "6.11.0"):
            v = self._f(check_qt(), "version")
        assert v.status == Status.FAIL
        assert v.auto_fix is not None  # --fix can reinstall the 6.8 pin
        assert "regression" in v.detail

    def test_non_68_below_is_fail_but_marked_untested_not_regression(self) -> None:
        with patch("PyQt6.QtCore.QT_VERSION_STR", "6.7.0"):
            v = self._f(check_qt(), "version")
        assert v.status == Status.FAIL
        assert "untested" in v.detail
        assert "regression" not in v.detail

    def test_crash_guard_detected_in_shipped_source(self) -> None:
        # The real app.py defines _install_exception_guard — static source read.
        g = self._f(check_qt(), "crash-guard")
        assert g.status == Status.OK


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

    def test_python_below_baseline_min_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Python 3.10 is below the system-core baseline minimum (3.11) → FAIL
        # (the interpreter may not even import the cockpit). This is the
        # baseline-driven behaviour: below-minimum is unsupported, not a nudge.
        monkeypatch.setattr("shutil.which", lambda x: None)
        import agent_takkub.doctor as _doc

        monkeypatch.setattr(_doc.sys, "version_info", (3, 10, 0))
        findings = check_runtime()
        python = next(f for f in findings if f.name == "python")
        assert python.status == Status.FAIL
        assert "3.11" in python.fix_hint


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
        with patch("agent_takkub.config._SAFE_PLUGINS", ("superpowers-dev",)):
            findings = check_plugins(cache_root=tmp_path)

        ok = next(f for f in findings if f.name == "superpowers-dev")
        assert ok.status == Status.OK
        assert "0.4.2" in ok.detail

    def test_plugin_missing_warns(self, tmp_path: Path) -> None:
        with patch("agent_takkub.config._SAFE_PLUGINS", ("missing-plugin",)):
            findings = check_plugins(cache_root=tmp_path)

        warn = next(f for f in findings if f.name == "missing-plugin")
        assert warn.status == Status.WARN

    def test_plugin_broken_json_fails(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "bad-plugin" / "bad-plugin" / "1.0.0" / ".claude-plugin"
        version_dir.mkdir(parents=True)
        (version_dir / "plugin.json").write_text("not json", encoding="utf-8")

        with patch("agent_takkub.config._SAFE_PLUGINS", ("bad-plugin",)):
            findings = check_plugins(cache_root=tmp_path)

        fail = next(f for f in findings if f.name == "bad-plugin")
        assert fail.status == Status.FAIL

    def test_marketplace_dir_without_plugin_json_fails(self, tmp_path: Path) -> None:
        """A marketplace dir exists but has no .claude-plugin/plugin.json anywhere in its tree
        → should FAIL, not silently accept."""
        (tmp_path / "broken-mp" / "broken-plugin" / "1.0.0").mkdir(parents=True)
        # no plugin.json anywhere
        with patch("agent_takkub.config._SAFE_PLUGINS", ("broken-mp",)):
            findings = check_plugins(cache_root=tmp_path)
        fail = next(f for f in findings if f.name == "broken-mp")
        assert fail.status == Status.FAIL

    def test_picks_highest_version_dir(self, tmp_path: Path) -> None:
        """When multiple version dirs exist, doctor picks the highest (reverse-sorted)."""
        self._make_plugin(tmp_path, "ecc", "ecc", "1.0.0")
        self._make_plugin(tmp_path, "ecc", "ecc", "2.0.0-rc.1")
        with patch("agent_takkub.config._SAFE_PLUGINS", ("ecc",)):
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
# check_claude — prod-instance default Claude profile login state
# (isolation plan, finding C5)
# ---------------------------------------------------------------------------


class TestCheckClaudeProdProfile:
    def test_dev_checkout_has_no_prod_profile_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", config_mod.REPO_ROOT)

        findings = check_claude()

        assert not any(f.name == "prod_profile_authenticated" for f in findings)

    def test_installed_instance_ok_when_credentials_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import agent_takkub.config as config_mod
        import agent_takkub.user_profile as up_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "data-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")
        prod_dir = tmp_path / "claude-config"
        prod_dir.mkdir()
        (prod_dir / ".credentials.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(up_mod, "_DEFAULT_CONFIG_DIR", prod_dir)

        findings = check_claude()

        f = next(x for x in findings if x.name == "prod_profile_authenticated")
        assert f.status == Status.OK

    def test_installed_instance_warns_when_not_logged_in(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import agent_takkub.config as config_mod
        import agent_takkub.user_profile as up_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "data-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")
        prod_dir = tmp_path / "claude-config"  # does not exist yet
        monkeypatch.setattr(up_mod, "_DEFAULT_CONFIG_DIR", prod_dir)

        findings = check_claude()

        f = next(x for x in findings if x.name == "prod_profile_authenticated")
        assert f.status == Status.WARN
        assert "claude login" in f.fix_hint
        assert str(prod_dir) in f.fix_hint


# ---------------------------------------------------------------------------
# check_installed_integrity — Phase D installed-mode gate
# ---------------------------------------------------------------------------


class TestCheckInstalledIntegrity:
    def test_dev_checkout_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", config_mod.REPO_ROOT)

        assert check_installed_integrity() == []

    def _fake_installed_layout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import agent_takkub.config as config_mod

        data_home = tmp_path / "data-home"
        assets_root = tmp_path / "assets"
        agents_dir = assets_root / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (assets_root / "CLAUDE.md").write_text("# lead", encoding="utf-8")
        (agents_dir / "backend.md").write_text("# backend", encoding="utf-8")
        cli_bin_dir = tmp_path / "scripts"
        cli_bin_dir.mkdir()
        script_name = "takkub.exe" if sys.platform == "win32" else "takkub"
        (cli_bin_dir / script_name).write_text("", encoding="utf-8")

        monkeypatch.setattr(config_mod, "DATA_HOME", data_home)
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")
        monkeypatch.setattr(config_mod, "ASSETS_ROOT", assets_root)
        monkeypatch.setattr(config_mod, "AGENTS_DIR", agents_dir)
        monkeypatch.setattr(config_mod, "CLI_BIN_DIR", cli_bin_dir)
        monkeypatch.setattr(config_mod, "RUNTIME_DIR", data_home / "runtime")
        return data_home

    def test_all_ok_when_wheel_shipped_correctly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._fake_installed_layout(monkeypatch, tmp_path)

        findings = check_installed_integrity()

        assert findings
        assert all(f.status == Status.OK for f in findings)
        names = {f.name for f in findings}
        assert names == {
            "assets-claude-md",
            "assets-role-files",
            "cli-bin",
            "runtime-writable",
        }

    def test_fails_when_claude_md_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._fake_installed_layout(monkeypatch, tmp_path)
        import agent_takkub.config as config_mod

        (config_mod.ASSETS_ROOT / "CLAUDE.md").unlink()

        findings = check_installed_integrity()

        f = next(x for x in findings if x.name == "assets-claude-md")
        assert f.status == Status.FAIL

    def test_fails_when_role_files_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._fake_installed_layout(monkeypatch, tmp_path)
        import agent_takkub.config as config_mod

        for f in config_mod.AGENTS_DIR.glob("*.md"):
            f.unlink()

        findings = check_installed_integrity()

        f = next(x for x in findings if x.name == "assets-role-files")
        assert f.status == Status.FAIL

    def test_fails_when_cli_bin_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._fake_installed_layout(monkeypatch, tmp_path)
        import agent_takkub.config as config_mod

        script_name = "takkub.exe" if sys.platform == "win32" else "takkub"
        (config_mod.CLI_BIN_DIR / script_name).unlink()

        findings = check_installed_integrity()

        f = next(x for x in findings if x.name == "cli-bin")
        assert f.status == Status.FAIL

    def test_fails_when_runtime_dir_not_writable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        data_home = self._fake_installed_layout(monkeypatch, tmp_path)
        import agent_takkub.config as config_mod

        # Simulate an unwritable DATA_HOME by pointing RUNTIME_DIR at a path
        # whose parent is a file, not a directory — mkdir(parents=True) then
        # raises NotADirectoryError (an OSError subclass).
        blocker = data_home.parent / "blocker-file"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(config_mod, "RUNTIME_DIR", blocker / "runtime")

        findings = check_installed_integrity()

        f = next(x for x in findings if x.name == "runtime-writable")
        assert f.status == Status.FAIL


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
            patch("agent_takkub.doctor.check_installed_integrity", return_value=[]),
            patch("agent_takkub.doctor.check_env_path", return_value=[]),
            patch("agent_takkub.doctor.check_arch", return_value=[]),
            patch("agent_takkub.doctor.check_qt", return_value=[]),
            patch("agent_takkub.doctor.check_plugins", return_value=[]),
            patch("agent_takkub.doctor.check_mcps", return_value=[]),
            patch("agent_takkub.doctor.check_projects", return_value=[]),
            patch("agent_takkub.doctor.check_providers", return_value=[]),
            patch("agent_takkub.doctor.check_hooks", return_value=[]),
            patch("agent_takkub.doctor.check_hook_wiring", return_value=[]),
            patch("agent_takkub.doctor.check_ready_markers", return_value=[]),
            patch("agent_takkub.doctor.check_version", return_value=[]),
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


class TestCheckClaudeAuthenticated:
    """[claude] authenticated — must read `.credentials.json` (leading dot,
    not `credentials.json`) and probe the macOS Keychain before warning
    (M1, docs/reviews/2026-07-10-xplatform-CONSOLIDATED.md)."""

    def _auth(self, findings: list[Finding]) -> Finding:
        return next(f for f in findings if f.name == "authenticated")

    def _import_limit_status_before_platform_spoof(self):
        # limit_status.py imports urllib.request at module level, which itself
        # lazily imports the macOS-only `_scproxy` the FIRST time it sees
        # sys.platform == "darwin" — importing it now (real platform) caches
        # the module so a later darwin spoof doesn't retrigger that import.
        import agent_takkub.limit_status  # noqa: F401

    def test_darwin_keychain_present_is_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._import_limit_status_before_platform_spoof()
        monkeypatch.setattr("agent_takkub.doctor.sys.platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(
            "agent_takkub.limit_status._read_keychain_credentials", lambda: "raw-blob"
        )
        f = self._auth(check_claude())
        assert f.status == Status.OK
        assert "Keychain" in f.detail

    def test_darwin_keychain_absent_no_file_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._import_limit_status_before_platform_spoof()
        monkeypatch.setattr("agent_takkub.doctor.sys.platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr("agent_takkub.limit_status._read_keychain_credentials", lambda: None)
        f = self._auth(check_claude())
        assert f.status == Status.WARN
        assert "claude login" in f.fix_hint

    def test_darwin_keychain_absent_but_file_present_is_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._import_limit_status_before_platform_spoof()
        monkeypatch.setattr("agent_takkub.doctor.sys.platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr("agent_takkub.limit_status._read_keychain_credentials", lambda: None)
        creds_dir = tmp_path / ".claude"
        creds_dir.mkdir()
        (creds_dir / ".credentials.json").write_text("{}", encoding="utf-8")
        f = self._auth(check_claude())
        assert f.status == Status.OK
        assert ".credentials.json" in f.detail

    def test_posix_dotfile_present_is_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agent_takkub.doctor.sys.platform", "linux")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        creds_dir = tmp_path / ".claude"
        creds_dir.mkdir()
        (creds_dir / ".credentials.json").write_text("{}", encoding="utf-8")
        f = self._auth(check_claude())
        assert f.status == Status.OK
        assert ".credentials.json" in f.detail

    def test_posix_dotfile_missing_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agent_takkub.doctor.sys.platform", "linux")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        f = self._auth(check_claude())
        assert f.status == Status.WARN
        assert ".credentials.json" in f.detail

    def test_windows_dotfile_present_is_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agent_takkub.doctor.sys.platform", "win32")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        creds_dir = tmp_path / ".claude"
        creds_dir.mkdir()
        (creds_dir / ".credentials.json").write_text("{}", encoding="utf-8")
        f = self._auth(check_claude())
        assert f.status == Status.OK

    def test_windows_dotfile_missing_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agent_takkub.doctor.sys.platform", "win32")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        f = self._auth(check_claude())
        assert f.status == Status.SKIP


class TestEnvPathCheck:
    """[env] npm-global-bin — the PATH-health check born from the 2026-07-04
    field incident (npm bin dir dropped off PATH → claude/takkub vanished)."""

    def test_dir_on_path_normalized_membership(self) -> None:
        # Windows: case + trailing-slash insensitive. POSIX: trailing-slash
        # insensitive, case-sensitive (normcase is identity there).
        from agent_takkub.doctor import _dir_on_path

        if sys.platform == "win32":
            path_value = os.pathsep.join([r"C:\Windows", "c:\\users\\x\\appdata\\roaming\\NPM\\"])
            assert _dir_on_path(r"C:\Users\X\AppData\Roaming\npm", path_value)
            assert not _dir_on_path(r"C:\Users\X\other", path_value)
        else:
            path_value = os.pathsep.join(["/usr/bin", "/Users/x/.npm-global/bin/"])
            assert _dir_on_path("/Users/x/.npm-global/bin", path_value)
            assert not _dir_on_path("/Users/x/other", path_value)

    def test_skips_when_npm_missing(self) -> None:
        from agent_takkub import doctor

        with patch("agent_takkub.doctor._npm_global_bin_dir", return_value=None):
            findings = doctor.check_env_path()
        assert len(findings) == 1
        assert findings[0].status == Status.SKIP

    def test_warn_with_auto_fix_when_dir_missing_from_path(self) -> None:
        from agent_takkub import doctor

        fake_bin = r"C:\fake\npm-bin" if sys.platform == "win32" else "/fake/npm-bin"
        with patch("agent_takkub.doctor._npm_global_bin_dir", return_value=fake_bin):
            if sys.platform == "win32":
                with (
                    patch("agent_takkub.doctor._read_win_user_path", return_value=(r"C:\other", 1)),
                    patch.dict(os.environ, {"PATH": r"C:\other"}),
                ):
                    findings = doctor.check_env_path()
            else:
                with patch.dict(os.environ, {"PATH": "/usr/bin"}):
                    findings = doctor.check_env_path()
        assert findings[0].status == Status.WARN
        assert findings[0].auto_fix is not None

    def test_posix_rc_fix_is_idempotent(self, tmp_path, monkeypatch) -> None:
        from agent_takkub import doctor

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        ok, _msg = doctor._append_posix_rc_path("/fake/bin")
        assert ok
        zshrc = tmp_path / ".zshrc"
        first = zshrc.read_text(encoding="utf-8")
        assert "/fake/bin" in first
        ok, msg = doctor._append_posix_rc_path("/fake/bin")
        assert ok and "already" in msg
        assert zshrc.read_text(encoding="utf-8") == first
