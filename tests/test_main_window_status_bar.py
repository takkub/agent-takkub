"""Unit tests for main_window.MainWindow status-bar static helpers.

Covers:
  #16 — doctor button: _on_doctor_clicked wiring path (dialog logic via
        static method tests; full Qt dialog tested via headless smoke).
  #17 — 3-state provider chips: _provider_chip_style, _provider_chip_state,
        _provider_chip_tooltip.

No Qt application is started here — we only invoke static/class-level
methods that do not depend on Qt widgets.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from agent_takkub.main_window import MainWindow

# ---------------------------------------------------------------------------
# #17 — provider chip style (3 states)
# ---------------------------------------------------------------------------


class TestProviderChipStyle:
    def test_disabled_has_strikethrough(self) -> None:
        style = MainWindow._provider_chip_style("codex", disabled=True)
        assert "line-through" in style

    def test_disabled_is_gray(self) -> None:
        style = MainWindow._provider_chip_style("codex", disabled=True)
        assert "#71717a" in style

    def test_not_installed_is_amber(self) -> None:
        style = MainWindow._provider_chip_style("codex", disabled=False, not_installed=True)
        assert "#d97706" in style
        assert "line-through" not in style

    def test_available_codex_is_teal(self) -> None:
        style = MainWindow._provider_chip_style("codex", disabled=False, not_installed=False)
        assert "#10a37f" in style

    def test_available_gemini_is_blue(self) -> None:
        style = MainWindow._provider_chip_style("gemini", disabled=False, not_installed=False)
        assert "#4285f4" in style

    def test_disabled_overrides_not_installed(self) -> None:
        # disabled=True wins — show gray strikethrough regardless
        style = MainWindow._provider_chip_style("codex", disabled=True, not_installed=True)
        assert "line-through" in style
        assert "#d97706" not in style

    def test_default_not_installed_is_false(self) -> None:
        # Calling with old 2-arg signature still works (backward compat)
        style = MainWindow._provider_chip_style("codex", False)
        assert "#10a37f" in style


class TestProviderChipTooltip:
    def test_disabled(self) -> None:
        tip = MainWindow._provider_chip_tooltip("codex", "disabled")
        assert "enable" in tip.lower()
        assert "Codex" in tip

    def test_not_installed_mentions_substitute_and_diversity(self) -> None:
        tip = MainWindow._provider_chip_tooltip("gemini", "not_installed")
        assert "substitute" in tip.lower()
        assert "diversity" in tip.lower()

    def test_available(self) -> None:
        tip = MainWindow._provider_chip_tooltip("codex", "available")
        assert "disable" in tip.lower()

    def test_gemini_available_names_gemini(self) -> None:
        tip = MainWindow._provider_chip_tooltip("gemini", "available")
        assert "Gemini" in tip


class TestProviderChipState:
    def test_disabled_returns_disabled(self, tmp_path: Path) -> None:
        dp = tmp_path / "disabled-providers.json"
        dp.write_text(json.dumps({"codex": True}))
        with patch("agent_takkub.provider_state._PATH", dp):
            state = MainWindow._provider_chip_state("codex")
        assert state == "disabled"

    def test_enabled_and_installed_returns_available(self, tmp_path: Path) -> None:
        dp = tmp_path / "disabled-providers.json"
        dp.write_text("{}")
        with (
            patch("agent_takkub.provider_state._PATH", dp),
            patch("agent_takkub.codex_helper.find_codex_executable", return_value="/usr/bin/codex"),
        ):
            state = MainWindow._provider_chip_state("codex")
        assert state == "available"

    def test_enabled_but_not_installed_returns_not_installed(self, tmp_path: Path) -> None:
        dp = tmp_path / "disabled-providers.json"
        dp.write_text("{}")
        with (
            patch("agent_takkub.provider_state._PATH", dp),
            patch("agent_takkub.codex_helper.find_codex_executable", return_value=None),
        ):
            state = MainWindow._provider_chip_state("codex")
        assert state == "not_installed"

    def test_gemini_disabled(self, tmp_path: Path) -> None:
        dp = tmp_path / "disabled-providers.json"
        dp.write_text(json.dumps({"gemini": True}))
        with patch("agent_takkub.provider_state._PATH", dp):
            state = MainWindow._provider_chip_state("gemini")
        assert state == "disabled"

    def test_gemini_not_installed(self, tmp_path: Path) -> None:
        dp = tmp_path / "disabled-providers.json"
        dp.write_text("{}")
        with (
            patch("agent_takkub.provider_state._PATH", dp),
            patch("agent_takkub.gemini_helper.find_agy_executable", return_value=None),
        ):
            state = MainWindow._provider_chip_state("gemini")
        assert state == "not_installed"

    def test_codex_helper_exception_falls_back_to_shutil(self, tmp_path: Path) -> None:
        dp = tmp_path / "disabled-providers.json"
        dp.write_text("{}")
        with (
            patch("agent_takkub.provider_state._PATH", dp),
            patch(
                "agent_takkub.codex_helper.find_codex_executable",
                side_effect=ImportError("no module"),
            ),
            patch("shutil.which", return_value=None),
        ):
            state = MainWindow._provider_chip_state("codex")
        assert state == "not_installed"


# ---------------------------------------------------------------------------
# #16 — doctor integration: run_all_checks + format_report round-trip
# ---------------------------------------------------------------------------


class TestDoctorIntegration:
    """Exercises the doctor module that _on_doctor_clicked calls.
    No Qt; verifies the data pipeline the dialog displays."""

    def test_run_all_checks_returns_list(self) -> None:
        from agent_takkub import doctor

        findings = doctor.run_all_checks()
        assert isinstance(findings, list)
        assert len(findings) > 0

    def test_format_report_contains_summary(self) -> None:
        from agent_takkub import doctor

        findings = doctor.run_all_checks()
        report = doctor.format_report(findings)
        assert "Summary:" in report

    def test_format_report_contains_category_headers(self) -> None:
        from agent_takkub import doctor

        findings = doctor.run_all_checks()
        report = doctor.format_report(findings)
        # At least one category header in bracketed form
        assert "[" in report and "]" in report

    def test_auto_fix_findings_have_callable(self) -> None:
        from agent_takkub import doctor

        findings = doctor.run_all_checks()
        for f in findings:
            if f.auto_fix is not None:
                assert callable(f.auto_fix)

    def test_run_auto_fixes_does_not_raise_on_no_fixes(self) -> None:
        from agent_takkub import doctor
        from agent_takkub.doctor import Finding, Status

        findings = [Finding("test", "item", Status.OK, "all good")]
        # Should be a no-op, not raise
        doctor.run_auto_fixes(findings)
