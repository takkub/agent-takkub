"""Tests for boot_update_window.py — per-provider state machine + the
MainWindow-open gate (#313 Part 2).

No real provider update ever runs here: `QThreadPool.globalInstance().start`
is monkeypatched to run the worker's `run()` synchronously in the test
thread instead of a real background thread (deterministic, offscreen,
matches this repo's `_NpmUpdateThread` test pattern) and
`provider_update.update_provider` itself is monkeypatched per test — no
subprocess, no network, no real spawn.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication, QThreadPool

import agent_takkub.boot_update_window as bw
import agent_takkub.provider_update as pu
from agent_takkub.provider_spec import PROVIDER_REGISTRY


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture(autouse=True)
def _run_workers_synchronously(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every QThreadPool.start(worker) call in these tests runs worker.run()
    immediately in-thread instead of scheduling it on a real pool thread —
    deterministic and instant, no timing races to wait out."""
    monkeypatch.setattr(QThreadPool, "start", lambda self, worker: worker.run())


def _all_disabled_except(monkeypatch: pytest.MonkeyPatch, keep: set[str]) -> None:
    """Make every provider except `keep` read as not-installed, so a test
    only exercises the providers it cares about."""
    monkeypatch.setattr(
        pu,
        "_discover",
        lambda spec: f"/bin/{spec.name}" if spec.name in keep else None,
    )
    import agent_takkub.provider_state as provider_state

    monkeypatch.setattr(provider_state, "is_disabled", lambda name: False)


class TestBootUpdateEnabled:
    def test_default_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_BOOT_UPDATE", raising=False)
        assert bw.boot_update_enabled() is True

    def test_explicit_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_UPDATE", "0")
        assert bw.boot_update_enabled() is False

    def test_other_values_stay_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_UPDATE", "1")
        assert bw.boot_update_enabled() is True


class TestTimeoutEnvOverride:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_BOOT_UPDATE_TIMEOUT_S", raising=False)
        assert bw._boot_update_timeout_s() == bw._DEFAULT_TIMEOUT_S

    def test_valid_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_UPDATE_TIMEOUT_S", "30")
        assert bw._boot_update_timeout_s() == 30.0

    def test_garbage_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_UPDATE_TIMEOUT_S", "not-a-number")
        assert bw._boot_update_timeout_s() == bw._DEFAULT_TIMEOUT_S

    def test_non_positive_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_UPDATE_TIMEOUT_S", "-5")
        assert bw._boot_update_timeout_s() == bw._DEFAULT_TIMEOUT_S


class TestBootUpdateWindowStateMachine:
    def test_shows_one_row_per_registered_provider(self, qapp) -> None:
        win = bw.BootUpdateWindow()
        assert set(win._rows) == set(PROVIDER_REGISTRY)

    def test_not_installed_and_disabled_never_dispatch_a_worker(
        self, qapp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _all_disabled_except(monkeypatch, keep=set())
        dispatched: list[str] = []
        monkeypatch.setattr(
            pu,
            "update_provider",
            lambda name: dispatched.append(name) or pu.UpdateOutcome(name, "x"),
        )
        win = bw.BootUpdateWindow()
        finished_calls = []
        win.finished.connect(lambda: finished_calls.append(1))
        win.start()
        assert not dispatched
        assert finished_calls == [1]
        for row in win._rows.values():
            assert row._status_label.text() != _STATUS(bw, "pending")

    def test_eligible_provider_dispatches_and_reports_terminal_status(
        self, qapp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _all_disabled_except(monkeypatch, keep={"claude"})
        monkeypatch.setattr(
            pu, "update_provider", lambda name: pu.UpdateOutcome(name, pu.STATUS_UPDATED, "v9.9.9")
        )
        win = bw.BootUpdateWindow()
        finished_calls = []
        win.finished.connect(lambda: finished_calls.append(1))
        win.start()
        assert finished_calls == [1]
        assert "v9.9.9" in win._rows["claude"]._status_label.text()

    def test_finished_waits_for_all_eligible_providers(
        self, qapp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the QThreadPool patched to run synchronously the two workers
        actually complete inline during start(); this asserts the pending-set
        bookkeeping itself (not just the end state) by driving
        `_on_provider_finished` directly for one before the other completes."""
        win = bw.BootUpdateWindow()
        win._pending = {"claude", "codex"}
        finished_calls = []
        win.finished.connect(lambda: finished_calls.append(1))
        win._on_provider_finished("claude", pu.STATUS_UPDATED, "")
        assert finished_calls == []  # codex still pending
        win._on_provider_finished("codex", pu.STATUS_UP_TO_DATE, "")
        assert finished_calls == [1]

    def test_timeout_fails_remaining_pending_rows_and_finishes(self, qapp) -> None:
        win = bw.BootUpdateWindow()
        win._pending = {"claude", "codex"}
        finished_calls = []
        win.finished.connect(lambda: finished_calls.append(1))
        win._on_timeout()
        assert finished_calls == [1]
        assert not win._pending
        # Status is now a painted icon, not a text glyph (critic review
        # 2026-08-20, finding #1) — assert the icon's kind, not label text.
        assert win._rows["claude"]._icon._kind == bw._StatusIcon._KIND_ERROR
        assert win._rows["codex"]._icon._kind == bw._StatusIcon._KIND_ERROR

    def test_finished_emitted_at_most_once(self, qapp) -> None:
        win = bw.BootUpdateWindow()
        finished_calls = []
        win.finished.connect(lambda: finished_calls.append(1))
        win._finish()
        win._finish()
        win._on_timeout()
        assert finished_calls == [1]

    def test_every_row_growing_a_wrapped_model_note_does_not_overlap(self, qapp) -> None:
        """Stress case the critic reproduced (review 2026-08-20, finding
        #2): every provider getting a status detail + a 2-line model-catalog
        bump note in the same boot used to overlap the row below it under
        the old row-count `setFixedSize` formula. Height must now be
        layout-driven and grow to fit every row without any collision."""
        win = bw.BootUpdateWindow()
        long_note = (
            "model: some-provider-a-genuinely-long-model-family-name-v3.7-flash-medium ↑ updated"
        )
        for name, row in win._rows.items():
            row.set_status(pu.STATUS_UPDATED, f"v1.0.0 → v9.9.{len(name)}", long_note)
        win._resize_to_content()

        rows = list(win._rows.values())
        for i in range(len(rows) - 1):
            this_bottom = rows[i].geometry().y() + rows[i].height()
            next_top = rows[i + 1].geometry().y()
            assert this_bottom <= next_top, (
                f"{rows[i].name} row (bottom={this_bottom}) overlaps "
                f"{rows[i + 1].name} row (top={next_top})"
            )
        # The window itself must actually be tall enough to contain the last
        # row, not just avoid inter-row overlap within a still-too-short window.
        last = rows[-1]
        assert last.geometry().y() + last.height() <= win.height()

    def test_no_eligible_providers_finishes_immediately(
        self, qapp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _all_disabled_except(monkeypatch, keep=set())
        win = bw.BootUpdateWindow()
        finished_calls = []
        win.finished.connect(lambda: finished_calls.append(1))
        win.start()
        assert finished_calls == [1]
        assert "0" in win._footer_label.text() or "ไม่มี" in win._footer_label.text()


class TestRunBootUpdateGate:
    def test_constructs_main_window_after_finished_without_nested_exec(
        self, qapp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stand in for the real splash with a fake that fires `finished`
        synchronously the moment `start()` is called — proves the gate
        function waits for that signal (via QEventLoop, not app.exec()) and
        then constructs the window, without ever needing a real event loop
        tick or a real QApplication.exec()."""
        from PyQt6.QtCore import QObject, pyqtSignal

        class _FakeSplash(QObject):
            finished = pyqtSignal()

            def show(self) -> None:
                pass

            def start(self) -> None:
                self.finished.emit()

            def close(self) -> None:
                pass

            def close_animated(self) -> None:
                pass

        monkeypatch.setattr(bw, "BootUpdateWindow", _FakeSplash)
        sentinel = object()
        result = bw.run_boot_update_gate(lambda: sentinel)
        assert result is sentinel


def _STATUS(mod, key: str) -> str:
    return mod._STATUS_LABELS[key]
