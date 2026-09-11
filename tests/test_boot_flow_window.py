"""Tests for boot_flow_window.py (#574) — the 5-page boot wizard.

No real `boot_flow` module is imported here (it's being written in parallel
by backend#2 and may not exist yet on this checkout) — every test passes a
`_FakeFlow` instance directly to `BootFlowWindow(flow=...)`, exercising the
same duck-typed interface the real module will satisfy. `QThread.start` is
monkeypatched to run `run()` synchronously in-thread (this repo's standard
pattern, see `test_boot_update_window.py`) so every test is deterministic
and instant — no real background thread, no timing races.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QThread

import agent_takkub.boot_flow_window as bfw


@pytest.fixture(autouse=True)
def _run_threads_synchronously(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QThread, "start", lambda self: self.run())


@dataclass
class _Item:
    name: str
    label: str
    current: str
    latest: str | None
    selected: bool = True
    status: str = ""


@dataclass
class _FakeFlow:
    items: list[_Item] = field(default_factory=list)
    remembered: dict | None = None
    plan: object | None = None
    outcome: object | None = None
    remembered_writes: list[dict] = field(default_factory=list)
    run_updates_calls: list[list[_Item]] = field(default_factory=list)
    run_migration_calls: int = 0

    def check_provider_updates(self, timeout_s: float) -> list[_Item]:
        return list(self.items)

    def remembered_provider_choice(self) -> dict | None:
        return self.remembered

    def remember_provider_choice(self, choice: dict) -> None:
        self.remembered_writes.append(choice)

    def run_provider_updates(self, items: list[_Item], progress_cb) -> list[_Item]:
        self.run_updates_calls.append(list(items))
        return list(items)

    def plan_migration(self):
        return self.plan

    def run_migration(self, progress_cb):
        self.run_migration_calls += 1
        return self.outcome


def _two_providers_one_update() -> list[_Item]:
    return [
        _Item(name="claude", label="Claude Code", current="2.1.267", latest="2.1.270"),
        _Item(name="gemini", label="Gemini (agy)", current="1.2.0", latest=None, selected=False),
    ]


def _plan(**overrides) -> SimpleNamespace:
    base = dict(
        backup_items=[("v2/ (ข้อมูลระบบ V2)", 15747), ("โปรเจค", 29)],
        estimated_bytes=1.2e9,
        free_bytes=123e9,
        backup_dir="backups/pre-migrate-test/",
        promote_items=["a", "b"],
        archive_items=list(range(27)),
        junk_items=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _outcome(**overrides) -> SimpleNamespace:
    base = dict(
        ok=True,
        duration_s=192,
        promoted=["accounts", "projects"],
        archived=list(range(27)),
        junk_deleted=3,
        projects_count=29,
        backup_dir="backups/pre-migrate-test/",
        archive_dir="backups/v1-archive-test/",
        failed_phase=None,
        failed_step=None,
        error=None,
        rolled_back=False,
        data_intact=True,
        log_paths=["runtime/boot.log"],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBootUpdateEnabled:
    def test_default_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_BOOT_UPDATE", raising=False)
        assert bfw.boot_update_enabled() is True

    def test_explicit_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_UPDATE", "0")
        assert bfw.boot_update_enabled() is False


class TestPageAToMigrationCheck:
    def test_shows_page_a_when_updates_available(self) -> None:
        flow = _FakeFlow(items=_two_providers_one_update())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        assert w._stack.currentIndex() == bfw.PAGE_MAIN
        assert w._main_update_btn.text() == "อัพเดตที่เลือก (1)"

    def test_skips_page_a_when_nothing_to_update(self) -> None:
        items = [_Item(name="gemini", label="Gemini", current="1.2.0", latest=None, selected=False)]
        flow = _FakeFlow(items=items, plan=None)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        # No migration needed either -> wizard closes itself (proceed=True).
        assert w._proceed is True

    def test_skips_page_a_when_boot_update_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_UPDATE", "0")
        flow = _FakeFlow(items=_two_providers_one_update(), plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        assert w._stack.currentIndex() == bfw.PAGE_PREMIGRATE
        assert flow.run_updates_calls == []

    def test_remembered_skip_goes_straight_to_migration_check(self) -> None:
        flow = _FakeFlow(
            items=_two_providers_one_update(),
            remembered={"mode": "skip"},
            plan=_plan(),
        )
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        assert flow.run_updates_calls == []
        assert w._stack.currentIndex() == bfw.PAGE_PREMIGRATE

    def test_remembered_update_all_runs_updates_then_migration_check(self) -> None:
        flow = _FakeFlow(
            items=_two_providers_one_update(),
            remembered={"mode": "update_all"},
            plan=None,
        )
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        assert len(flow.run_updates_calls) == 1
        assert [i.name for i in flow.run_updates_calls[0]] == ["claude"]
        assert w._proceed is True  # plan None -> no migration needed, wizard closes

    def test_update_button_click_runs_selected_and_advances(self) -> None:
        flow = _FakeFlow(items=_two_providers_one_update(), plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._main_update_btn.click()
        assert len(flow.run_updates_calls) == 1
        assert w._stack.currentIndex() == bfw.PAGE_PREMIGRATE

    def test_skip_button_click_advances_without_running_updates(self) -> None:
        flow = _FakeFlow(items=_two_providers_one_update(), plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._main_skip_btn.click()
        assert flow.run_updates_calls == []
        assert w._stack.currentIndex() == bfw.PAGE_PREMIGRATE

    def test_remember_checkbox_persists_choice(self) -> None:
        flow = _FakeFlow(items=_two_providers_one_update(), plan=None)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._remember_check.set_checked(True)
        w._main_skip_btn.click()
        assert flow.remembered_writes == [{"mode": "skip", "selected": []}]


class TestPreMigratePage:
    def test_populates_from_plan(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        assert w._stack.currentIndex() == bfw.PAGE_PREMIGRATE
        assert w._backup_card_lay.count() == 2

    def test_cancel_button_closes_without_proceeding(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_cancel_btn.click()
        assert w._proceed is False

    def test_start_button_moves_to_migrating_page(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan(), outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        # Migration ran synchronously (QThread patched) and already finished.
        assert flow.run_migration_calls == 1
        assert w._stack.currentIndex() == bfw.PAGE_DONE


class TestMigratingPageCloseGuard:
    def test_close_ignored_while_migrating(self) -> None:
        # An outcome that never resolves the fake `_migrate_throttle` timer
        # matters less than the guard itself; use a flow whose run_migration
        # blocks synchronously (still on PAGE_MIGRATING at the moment
        # start_migration kicks off, before the QThread patch's synchronous
        # run() returns) is awkward to freeze mid-flight with the sync
        # patch, so this test checks the guard directly against the page
        # index instead of a real in-flight worker.
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w._stack.setCurrentIndex(bfw.PAGE_MIGRATING)
        from PyQt6.QtGui import QCloseEvent

        event = QCloseEvent()
        w.closeEvent(event)
        assert event.isAccepted() is False


class TestDonePage:
    def test_success_outcome_shows_done_page(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan(), outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._stack.currentIndex() == bfw.PAGE_DONE
        assert w._done_summary_lay.count() == 3

    def test_open_program_button_proceeds(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan(), outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        w._done_open_btn.click()
        assert w._proceed is True


class TestFailedPage:
    def test_failed_outcome_shows_failed_page(self) -> None:
        outcome = _outcome(
            ok=False,
            failed_phase="verify",
            failed_step="projects/registry.json อ่านไม่ได้",
            rolled_back=True,
            data_intact=True,
        )
        flow = _FakeFlow(items=[], plan=_plan(), outcome=outcome)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._stack.currentIndex() == bfw.PAGE_FAILED
        assert "ตรวจสอบ" in w._failed_heading.text()

    def test_continue_with_old_version_proceeds(self) -> None:
        outcome = _outcome(ok=False, failed_phase="verify", rolled_back=True, data_intact=True)
        flow = _FakeFlow(items=[], plan=_plan(), outcome=outcome)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        w._failed_continue_btn.click()
        assert w._proceed is True

    def test_worker_exception_shows_failed_page(self) -> None:
        class _RaisingFlow(_FakeFlow):
            def run_migration(self, progress_cb):
                raise RuntimeError("boom")

        flow = _RaisingFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._stack.currentIndex() == bfw.PAGE_FAILED


class TestFlowFinishedSignal:
    def test_emits_once_on_accept(self) -> None:
        flow = _FakeFlow(items=[], plan=None)
        w = bfw.BootFlowWindow(flow=flow)
        received: list[bool] = []
        w.flowFinished.connect(received.append)
        w.start()
        assert received == [True]
