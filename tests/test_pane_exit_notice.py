"""Targeted tests for #397 — a pane whose provider process exits
unexpectedly (crash / OOM / user `/exit`) without a preceding `takkub done`
must tell Lead immediately, not only once the auto-respawn budget is fully
exhausted.

Before this fix the ONLY proactive notice for an unexpected exit was
`_warn_lead_respawn_capped`, fired solely once `AUTO_RESPAWN_MAX` attempts
were exhausted — a crash that auto-respawned successfully (the common case)
reached Lead through no path at all. See `spawn_engine._on_session_exit` /
`lead_inbox._warn_lead_pane_exited` / `_write_pane_exit_snapshot`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, _exit_key

TEST_PROJECT = "pane-exit-test"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _lead_pane() -> MagicMock:
    lead_pane = MagicMock()
    lead_pane.session = MagicMock()
    lead_pane.session.is_alive = True
    return lead_pane


class TestUnexpectedExitNotifiesLeadImmediately:
    def test_first_crash_notifies_lead_before_any_cap_is_hit(self, orch: Orchestrator) -> None:
        """A pane's very first unexpected exit (well under AUTO_RESPAWN_MAX)
        must reach Lead right away — before this fix it was silently
        absorbed into the auto-respawn retry loop with no notice at all."""
        lead_pane = _lead_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead_pane

        pane = MagicMock()
        pane.state = "exited"
        pane.session = None
        orch._panes_by_project[TEST_PROJECT]["backend"] = pane

        with patch("agent_takkub.orchestrator.QTimer"):
            orch._on_session_exit("backend", "/proj", TEST_PROJECT, exit_code=1)

        lead_pane.session.write.assert_called_once()
        written = lead_pane.session.write.call_args.args[0]
        assert "[system]" in written
        assert "backend" in written
        assert "exited" in written
        assert "1" in written  # the exit code

    def test_logs_pane_exited_without_done_event_with_exit_code(self, orch: Orchestrator) -> None:
        lead_pane = _lead_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead_pane

        pane = MagicMock()
        pane.state = "exited"
        pane.session = None
        orch._panes_by_project[TEST_PROJECT]["qa"] = pane

        with (
            patch("agent_takkub.orchestrator.QTimer"),
            patch("agent_takkub.lead_inbox._log_event") as log_spy,
        ):
            orch._on_session_exit("qa", "/proj", TEST_PROJECT, exit_code=139)

        events = [
            c for c in log_spy.call_args_list if c.args and c.args[0] == "pane_exited_without_done"
        ]
        assert len(events) == 1
        assert events[0].kwargs["role"] == "qa"
        assert events[0].kwargs["exit_code"] == 139

    def test_expected_exit_after_done_does_not_notify(self, orch: Orchestrator) -> None:
        """A pane that already called `done()`/`close()` lands on "empty",
        not "exited" — decide_exit_state's whole point — so this must stay
        silent; only a genuinely unexpected exit is newsworthy."""
        lead_pane = _lead_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead_pane

        pane = MagicMock()
        pane.state = "empty"
        pane.session = None
        orch._panes_by_project[TEST_PROJECT]["backend"] = pane

        with patch("agent_takkub.orchestrator.QTimer"):
            orch._on_session_exit("backend", "/proj", TEST_PROJECT, exit_code=0)

        lead_pane.session.write.assert_not_called()

    def test_no_lead_pane_is_a_silent_no_op(self, orch: Orchestrator) -> None:
        """No-op (never raises) when Lead is absent — same reasoning as the
        existing respawn-capped warning."""
        pane = MagicMock()
        pane.state = "exited"
        pane.session = None
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane

        with patch("agent_takkub.orchestrator.QTimer"):
            orch._on_session_exit("backend", "/proj", TEST_PROJECT, exit_code=1)  # must not raise

    def test_second_crash_still_notifies_before_the_cap_fires_on_the_third(
        self, orch: Orchestrator
    ) -> None:
        """AUTO_RESPAWN_MAX is 2 — the FIRST two crashes must each get their
        own exited-notice even though respawn is still under budget; only
        the third additionally gets the separate respawn-capped notice."""
        key = _exit_key(TEST_PROJECT, "devops")
        orch._ps(key).auto_respawn_attempts = 1  # one prior crash already counted

        lead_pane = _lead_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead_pane

        pane = MagicMock()
        pane.state = "exited"
        pane.session = None
        orch._panes_by_project[TEST_PROJECT]["devops"] = pane

        with patch("agent_takkub.orchestrator.QTimer"):
            orch._on_session_exit("devops", "/proj", TEST_PROJECT, exit_code=1)

        # attempts(1) < AUTO_RESPAWN_MAX(2) — under budget, no cap notice yet,
        # only the exited-notice.
        lead_pane.session.write.assert_called_once()
        written = lead_pane.session.write.call_args.args[0]
        assert "[system]" in written
        assert "respawn-capped" not in written


class TestPaneExitSnapshot:
    def test_last_output_written_to_session_dir_on_unexpected_exit(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)

        pane = MagicMock()
        pane.state = "exited"
        pane.session = None
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane

        dying_session = MagicMock()
        dying_session.display_lines.return_value = ["line one", "line two", "final line"]

        with patch("agent_takkub.orchestrator.QTimer"):
            orch._on_session_exit(
                "backend", "/proj", TEST_PROJECT, session=dying_session, exit_code=1
            )

        from datetime import datetime

        day = tmp_path / "sessions" / datetime.now().strftime("%Y-%m-%d") / TEST_PROJECT
        snapshot = day / "backend-last-output.txt"
        assert snapshot.exists()
        text = snapshot.read_text(encoding="utf-8")
        assert "final line" in text

    def test_missing_session_does_not_write_or_raise(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The generic (non-codex) processExited connection may still call
        without a session in older call shapes — must degrade quietly."""
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)

        pane = MagicMock()
        pane.state = "exited"
        pane.session = None
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane

        with patch("agent_takkub.orchestrator.QTimer"):
            orch._on_session_exit("backend", "/proj", TEST_PROJECT, session=None, exit_code=1)

        assert not (tmp_path / "sessions").exists()

    def test_snapshot_write_failure_never_breaks_exit_handling(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A best-effort diagnostic must never take the whole exit-handling
        path down with it (matches _write_codex_crash_dump's own contract)."""
        lead_pane = _lead_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead_pane

        pane = MagicMock()
        pane.state = "exited"
        pane.session = None
        orch._panes_by_project[TEST_PROJECT]["backend"] = pane

        broken_session = MagicMock()
        broken_session.display_lines.side_effect = RuntimeError("pty already torn down")

        with patch("agent_takkub.orchestrator.QTimer"):
            orch._on_session_exit(
                "backend", "/proj", TEST_PROJECT, session=broken_session, exit_code=1
            )

        # The exited-notice must still have gone out despite the snapshot failing.
        lead_pane.session.write.assert_called_once()
