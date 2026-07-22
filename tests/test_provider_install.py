"""provider_install.py — shared installer behind doctor --fix, `takkub
provider install`, and the status-bar chip's Install action."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from agent_takkub import provider_install
from agent_takkub.provider_install import install_provider, installable_providers


def _which_missing(name):
    """shutil.which stub: nothing on PATH."""
    return None


class TestInstallableProviders:
    def test_npm_backed_providers_listed(self) -> None:
        names = installable_providers()
        assert "codex" in names
        assert "opencode" in names

    def test_manual_only_and_claude_excluded(self) -> None:
        names = installable_providers()
        assert "gemini" not in names  # GUI installer download — manual only
        assert "claude" not in names  # baseline CLI, never managed here


class TestInstallProvider:
    def test_unknown_provider(self) -> None:
        ok, msg = install_provider("bogus")
        assert not ok
        assert "unknown provider" in msg

    def test_claude_refused(self) -> None:
        ok, _msg = install_provider("claude")
        assert not ok

    def test_already_installed_short_circuits(self) -> None:
        with (
            patch.object(provider_install, "_discover", return_value="C:/bin/opencode.cmd"),
            patch("subprocess.run") as mock_run,
        ):
            ok, msg = install_provider("opencode")
        assert ok
        assert "already installed" in msg
        mock_run.assert_not_called()

    def test_manual_only_provider_returns_instructions(self) -> None:
        with patch.object(provider_install, "_discover", return_value=None):
            ok, msg = install_provider("gemini")
        assert not ok
        assert "antigravity.google" in msg.lower()

    def test_missing_package_manager(self) -> None:
        with (
            patch.object(provider_install, "_discover", return_value=None),
            patch.object(provider_install.shutil, "which", _which_missing),
        ):
            ok, msg = install_provider("opencode")
        assert not ok
        assert "install `npm` first" in msg

    def test_missing_non_npm_package_manager_names_program(self) -> None:
        with (
            patch.object(provider_install, "_discover", return_value=None),
            patch.object(provider_install.shutil, "which", _which_missing),
        ):
            ok, msg = install_provider("kimi")
        assert not ok
        assert "install `uv` first" in msg

    def test_success_requires_post_install_discovery(self) -> None:
        """npm exit 0 but binary still absent → failure, not success."""
        with (
            patch.object(provider_install, "_discover", side_effect=[None, None]),
            patch.object(provider_install.shutil, "which", return_value="C:/npm.cmd"),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")),
        ):
            ok, msg = install_provider("opencode")
        assert not ok
        assert "still not found" in msg

    def test_success_with_verification_and_login_note(self) -> None:
        with (
            patch.object(provider_install, "_discover", side_effect=[None, "C:/bin/opencode.cmd"]),
            patch.object(provider_install.shutil, "which", return_value="C:/npm.cmd"),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")),
        ):
            ok, msg = install_provider("opencode")
        assert ok
        assert "opencode.cmd" in msg
        assert "auth login" in msg  # post_install_note surfaced

    def test_nonzero_exit_reports_tail(self) -> None:
        with (
            patch.object(provider_install, "_discover", return_value=None),
            patch.object(provider_install.shutil, "which", return_value="C:/npm.cmd"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stdout="", stderr="E403 forbidden\n"),
            ),
        ):
            ok, msg = install_provider("opencode")
        assert not ok
        assert "E403" in msg

    def test_timeout_reported(self) -> None:
        with (
            patch.object(provider_install, "_discover", return_value=None),
            patch.object(provider_install.shutil, "which", return_value="C:/npm.cmd"),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="npm", timeout=600),
            ),
        ):
            ok, msg = install_provider("opencode")
        assert not ok
        assert "timed out" in msg


class TestDoctorWiring:
    def test_missing_installable_provider_gets_auto_fix(self) -> None:
        """Doctor attaches opt-in installers only to machine-installable providers.

        Discovery is neutralized per-helper (the spec wrappers lazily re-import
        these names at call time — the documented monkeypatch seam) plus a
        global which() stub, so the verdict is machine-independent."""
        from agent_takkub import doctor as doctor_mod

        with (
            patch("agent_takkub.codex_helper.find_codex_executable", return_value=None),
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value=None),
            patch("shutil.which", _which_missing),
        ):
            findings = doctor_mod.check_providers()

        by_name = {f.name: f for f in findings if f.category == "providers"}
        assert by_name["opencode"].auto_fix is not None
        assert by_name["codex"].auto_fix is not None
        assert by_name["gemini"].auto_fix is None  # manual-only
        assert by_name["cursor"].auto_fix is None  # manual-only
        assert by_name["opencode"].status is doctor_mod.Status.SKIP
        assert "takkub provider install opencode" in by_name["opencode"].fix_hint
        assert "takkub doctor --fix --install-providers" in by_name["opencode"].fix_hint


class TestRunAutoFixes:
    @staticmethod
    def _fix(calls: list[str], name: str):
        def _run() -> tuple[bool, str]:
            calls.append(name)
            return True, f"{name} fixed"

        return _run

    def test_default_skips_provider_fixes_but_runs_other_fixes(self, capsys) -> None:
        from agent_takkub.doctor import Finding, Status, run_auto_fixes

        calls: list[str] = []
        findings = [
            Finding(
                "providers",
                "codex",
                Status.SKIP,
                auto_fix=self._fix(calls, "codex"),
            ),
            Finding("qt", "version", Status.FAIL, auto_fix=self._fix(calls, "qt")),
        ]

        run_auto_fixes(findings)

        assert calls == ["qt"]
        output = capsys.readouterr().out
        assert "[skipped (opt-in)] providers/codex" in output
        assert "takkub doctor --fix --install-providers" in output
        assert "takkub provider install codex" in output
        assert "[fixed] qt/version" in output

    def test_install_providers_true_runs_provider_and_other_fixes(self) -> None:
        from agent_takkub.doctor import Finding, Status, run_auto_fixes

        calls: list[str] = []
        findings = [
            Finding(
                "providers",
                "opencode",
                Status.SKIP,
                auto_fix=self._fix(calls, "opencode"),
            ),
            Finding("qt", "version", Status.FAIL, auto_fix=self._fix(calls, "qt")),
        ]

        run_auto_fixes(findings, install_providers=True)

        assert calls == ["opencode", "qt"]

    def test_manual_providers_without_auto_fix_are_unchanged(self, capsys) -> None:
        from agent_takkub.doctor import Finding, Status, run_auto_fixes

        findings = [
            Finding("providers", "cursor", Status.SKIP, auto_fix=None),
            Finding("providers", "gemini", Status.SKIP, auto_fix=None),
        ]

        run_auto_fixes(findings, install_providers=True)

        assert capsys.readouterr().out == ""
