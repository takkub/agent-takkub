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
from PyQt6.QtWidgets import QLabel

import agent_takkub.boot_flow_window as bfw
from agent_takkub.core.models.version import ComponentVersion


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


def _label_by_text(root, text: str) -> QLabel:
    for lbl in root.findChildren(QLabel):
        if lbl.text() == text:
            return lbl
    raise AssertionError(f"no QLabel with text {text!r} under {root!r}")


class TestLabelsInsideCardsAreBorderless:
    """Round-2 fix (#574): a label nested inside a rounded/bordered `_card()`
    paints a stray box behind its own text once the whole dialog renders
    through a single grab()/render() pass (see `_kv_row`'s docstring) unless
    it explicitly nulls out background+border itself — the dialog-wide
    `QLabel {...}` floor alone isn't enough for these."""

    def test_provider_name_label_is_borderless(self) -> None:
        flow = _FakeFlow(items=_two_providers_one_update())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        label = _label_by_text(w._provider_card, "Claude Code")
        assert "background: transparent" in label.styleSheet()
        assert "border: none" in label.styleSheet()

    def test_premigrate_backup_item_labels_are_borderless(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        key_label = _label_by_text(w._backup_card, "v2/ (ข้อมูลระบบ V2)")
        value_label = _label_by_text(w._backup_card, "15,747 รายการ")
        for label in (key_label, value_label):
            assert "background: transparent" in label.styleSheet()
            assert "border: none" in label.styleSheet()

    def test_kv_key_label_is_borderless_and_wraps_at_170(self) -> None:
        key_label = bfw._kv_key_label("ถ้าต้องกลับเวอร์ชันเดิม", "Sans")
        assert "background: transparent" in key_label.styleSheet()
        assert "border: none" in key_label.styleSheet()
        assert key_label.wordWrap() is True
        assert key_label.minimumWidth() == 170
        assert key_label.maximumWidth() == 170


class TestMigratingFooterWrapAndElide:
    def test_warning_label_wraps_within_a_fixed_width(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        assert w._migrate_warn_label.wordWrap() is True
        assert w._migrate_warn_label.maximumWidth() == 340

    def test_backup_path_elides_from_the_left_and_keeps_full_text_as_tooltip(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        full_text = "สำรองไว้ที่ backups/pre-migrate-2026-09-11-0832-some-very-long-directory-suffix/"
        w._set_footer_right_elided(full_text)
        shown = w._migrate_footer_right.text()
        assert shown != full_text
        assert "…" in shown
        assert shown.endswith("suffix/")
        assert w._migrate_footer_right.toolTip() == full_text

    def test_short_backup_path_is_shown_in_full(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        full_text = "สำรองไว้ที่ backups/short/"
        w._set_footer_right_elided(full_text)
        assert w._migrate_footer_right.text() == full_text


class TestVersionNumbers:
    """#574 fix-loop round: header/footer version text must show the
    RUNNING app's version (`agent_takkub.__version__`) where the mockup
    means "the new version", and fall back to the version marker in
    `version.json` (never the backend outcome's `previous_version` field
    alone, which #574's backend half doesn't guarantee yet) where it means
    "the version being migrated away from"."""

    def test_previous_app_version_reads_the_version_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent_takkub.core.versioning.store as version_store

        monkeypatch.setattr(
            version_store,
            "read_version_doc",
            lambda path=None: [ComponentVersion(id="app", component="app", version="2.0.8")],
        )
        assert bfw._previous_app_version() == "2.0.8"

    def test_previous_app_version_is_none_when_marker_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent_takkub.core.versioning.store as version_store

        monkeypatch.setattr(version_store, "read_version_doc", lambda path=None: [])
        assert bfw._previous_app_version() is None

    def test_previous_app_version_fails_open_on_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agent_takkub.core.versioning.store as version_store

        def _raise(path=None):
            raise OSError("boom")

        monkeypatch.setattr(version_store, "read_version_doc", _raise)
        assert bfw._previous_app_version() is None

    def test_premigrate_subtitle_shows_the_new_running_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bfw, "_app_version", lambda: "2.1.0")
        monkeypatch.setattr(bfw, "_previous_app_version", lambda: "2.0.8")
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        assert "2.1.0" in w._subtitle_label.text()
        assert "2.0.8" not in w._subtitle_label.text()

    def test_done_page_falls_back_to_version_marker_when_outcome_lacks_previous_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bfw, "_previous_app_version", lambda: "2.0.8")
        flow = _FakeFlow(items=[], plan=_plan(), outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        texts = [lbl.text() for lbl in w._done_paths_box.findChildren(QLabel)]
        assert any("2.0.8" in t for t in texts)

    def test_failed_page_falls_back_to_version_marker_for_continue_button(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bfw, "_previous_app_version", lambda: "2.0.8")
        outcome = _outcome(ok=False, failed_phase="verify", rolled_back=True, data_intact=True)
        flow = _FakeFlow(items=[], plan=_plan(), outcome=outcome)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert "2.0.8" in w._failed_continue_btn.text()

    def test_outcome_previous_version_takes_priority_over_the_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bfw, "_previous_app_version", lambda: "2.0.8")
        outcome = _outcome(previous_version="1.9.9")
        flow = _FakeFlow(items=[], plan=_plan(), outcome=outcome)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        texts = [lbl.text() for lbl in w._done_paths_box.findChildren(QLabel)]
        assert any("1.9.9" in t for t in texts)
        assert not any("2.0.8" in t for t in texts)


class TestOptionalBackendFields:
    """#574 backend interface is still landing in parallel — these fields
    (`validated_steps`, `failed_step_index`/`failed_step_total`) aren't
    documented yet, so every real `_outcome()` today omits them; both must
    stay fully optional (`getattr(..., None)`), never required."""

    def test_validated_steps_row_shown_when_present(self) -> None:
        outcome = _outcome(validated_steps=11)
        flow = _FakeFlow(items=[], plan=_plan(), outcome=outcome)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._done_summary_lay.count() == 4
        texts = [lbl.text() for lbl in w._done_summary_box.findChildren(QLabel)]
        assert any("11 ขั้น" in t for t in texts)

    def test_validated_steps_row_absent_by_default(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan(), outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._done_summary_lay.count() == 3

    def test_failed_heading_includes_phase_number_and_step_progress_when_present(self) -> None:
        outcome = _outcome(
            ok=False,
            failed_phase="verify",
            failed_step_index=7,
            failed_step_total=11,
            rolled_back=True,
            data_intact=True,
        )
        flow = _FakeFlow(items=[], plan=_plan(), outcome=outcome)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._failed_heading.text() == "ขั้นที่ 3 ตรวจสอบ (7/11) ไม่ผ่าน"

    def test_failed_heading_falls_back_without_step_progress(self) -> None:
        outcome = _outcome(ok=False, failed_phase="verify", rolled_back=True, data_intact=True)
        flow = _FakeFlow(items=[], plan=_plan(), outcome=outcome)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._failed_heading.text() == "ขั้นตอน ตรวจสอบ ไม่ผ่าน"
