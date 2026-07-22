"""Unit tests for main_window.MainWindow status-bar static helpers.

Covers:
  #16 — doctor button: _on_doctor_clicked wiring path (dialog logic via
        static method tests; full Qt dialog tested via headless smoke).

No Qt application is started here — we only invoke static/class-level
methods that do not depend on Qt widgets.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from agent_takkub.main_window import MainWindow

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


# ---------------------------------------------------------------------------
# #102 -- closing the last tab must not leave a stale `active` project
# ---------------------------------------------------------------------------


class TestOnTabSwitchedNoTabsLeft:
    def test_negative_index_clears_active_project(self) -> None:
        """QTabWidget emits currentChanged(-1) once the last tab is removed.
        That path used to `return` immediately, skipping set_active_project
        entirely and leaving projects.json's `active` pointing at a project
        with no open tab."""
        fake_self = Mock()
        with patch("agent_takkub.main_window.clear_active_project") as mock_clear:
            MainWindow._on_tab_switched(fake_self, -1)
        mock_clear.assert_called_once_with()
        # Nothing past the early-return branch should be touched.
        fake_self.tabs.widget.assert_not_called()
