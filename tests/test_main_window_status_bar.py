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
from agent_takkub.status_header import StatusHeaderMixin

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


# ---------------------------------------------------------------------------
# ⚠ Usage-overage chip (#161) — status_header._refresh_overage_chip
# ---------------------------------------------------------------------------


class TestOverageChip:
    """No Qt widget tree needed: _refresh_overage_chip only touches
    `self._chip_overage` (a plain Mock stands in fine — show()/hide()/
    setVisible() are just recorded calls) and reads through
    `self._limit_store`, never constructing real widgets."""

    def _fake_self(self, *, limit_store=None):
        fake_self = Mock()
        fake_self._chip_overage = Mock()
        fake_self._limit_store = limit_store
        return fake_self

    def test_hides_when_no_limit_store_yet(self) -> None:
        """The 3s boot window before _init_limit_store runs — must degrade
        to hidden, never raise."""
        fake_self = self._fake_self(limit_store=None)
        StatusHeaderMixin._refresh_overage_chip(fake_self)
        fake_self._chip_overage.hide.assert_called_once()

    def test_hides_when_no_active_project(self) -> None:
        fake_self = self._fake_self(limit_store=Mock())
        with patch("agent_takkub.config.active_project", return_value=(None, None)):
            StatusHeaderMixin._refresh_overage_chip(fake_self)
        fake_self._chip_overage.hide.assert_called_once()

    def test_visible_when_active_project_is_in_overage(self) -> None:
        from datetime import UTC, datetime

        from agent_takkub.limit_status import LimitWindow, UsageData

        data = UsageData(
            plan="Max",
            windows=[
                LimitWindow(
                    name="five_hour",
                    utilization=100.0,
                    resets_at=datetime(2026, 6, 9, 10, 0, 0, tzinfo=UTC),
                )
            ],
            extra_usage_enabled=False,
        )
        store = Mock()
        store.get.return_value = data
        fake_self = self._fake_self(limit_store=store)
        with (
            patch("agent_takkub.config.active_project", return_value=("demo", None)),
            patch("agent_takkub.user_profile.config_dir_for", return_value="/fake/cd"),
        ):
            StatusHeaderMixin._refresh_overage_chip(fake_self)
        fake_self._chip_overage.setVisible.assert_called_once_with(True)

    def test_hidden_when_active_project_is_not_in_overage(self) -> None:
        store = Mock()
        store.get.return_value = None
        fake_self = self._fake_self(limit_store=store)
        with (
            patch("agent_takkub.config.active_project", return_value=("demo", None)),
            patch("agent_takkub.user_profile.config_dir_for", return_value="/fake/cd"),
        ):
            StatusHeaderMixin._refresh_overage_chip(fake_self)
        fake_self._chip_overage.setVisible.assert_called_once_with(False)

    def test_no_op_when_chip_was_never_built(self) -> None:
        """Same `__dict__`-membership guard as _refresh_remote_chip/
        _refresh_graft_chip — a MainWindow.__new__() test stub whose Qt C++
        side never ran must not crash here."""
        fake_self = Mock(spec=[])  # empty spec → no attributes at all
        StatusHeaderMixin._refresh_overage_chip(fake_self)  # must not raise
