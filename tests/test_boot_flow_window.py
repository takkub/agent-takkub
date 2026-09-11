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

import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt, QThread
from PyQt6.QtWidgets import QApplication, QLabel

import agent_takkub.boot_flow_window as bfw
from agent_takkub import cockpit_theme as theme
from agent_takkub import config
from agent_takkub.boot_flow import MigrationOutcome, MigrationPlanSummary, ProgressEvent
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

    def test_kv_key_label_is_borderless_and_wraps_at_150(self) -> None:
        # #574 fix-loop round 3 (V4): mockup key column is 150px, not 170 —
        # the old 170 pushed every info-box value 20px right of spec.
        key_label = bfw._kv_key_label("ถ้าต้องกลับเวอร์ชันเดิม", "Sans")
        assert "background: transparent" in key_label.styleSheet()
        assert "border: none" in key_label.styleSheet()
        assert key_label.wordWrap() is True
        assert key_label.minimumWidth() == 150
        assert key_label.maximumWidth() == 150


class TestMigratingFooterWrap:
    """#574 fix-loop round 3 (V11): the mockup has no ellipsis rule for the
    backup-path footer note — it wraps instead, so the full path (including
    the `สำรองไว้ที่` prefix) stays visible rather than losing its head to
    a fixed-width left-elide."""

    def test_warning_label_wraps_within_a_fixed_width(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        assert w._migrate_warn_label.wordWrap() is True
        assert w._migrate_warn_label.maximumWidth() == 340

    def test_backup_path_is_shown_in_full_not_elided(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        full_text = "สำรองไว้ที่ backups/pre-migrate-2026-09-11-0832-some-very-long-directory-suffix/"
        w._set_footer_right_elided(full_text)
        assert w._migrate_footer_right.text() == full_text
        assert "…" not in w._migrate_footer_right.text()

    def test_footer_right_label_wraps_rather_than_clips(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        assert w._migrate_footer_right.wordWrap() is True

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

    def test_validated_steps_shown_in_heading_not_a_fourth_row(self) -> None:
        # #574 fix-loop round 3 (V12): mockup heading is
        # "ตรวจสอบครบ 11 ขั้น — ..." — the count folds into the heading
        # itself, it does NOT add a 4th summary row.
        outcome = _outcome(validated_steps=11)
        flow = _FakeFlow(items=[], plan=_plan(), outcome=outcome)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._done_summary_lay.count() == 3
        assert w._done_heading.text() == "ตรวจสอบครบ 11 ขั้น — ไม่มีข้อมูลหาย"

    def test_validated_steps_row_absent_by_default(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan(), outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._done_summary_lay.count() == 3
        assert w._done_heading.text() == "ตรวจสอบครบทุกขั้น — ไม่มีข้อมูลหาย"

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


class TestPixelSizedFonts:
    """#574 fix-loop round 3 (V1): `_font()` must use CSS pixel sizes, not
    Qt's point-size constructor overload — at 96 DPI the old `QFont(family,
    size)` rendered ~33% larger than every mockup value it was handed."""

    def test_font_pixel_size_matches_the_requested_value(self) -> None:
        f = bfw._font("Sans", 16, 700)
        assert f.pixelSize() == 16
        assert f.pointSize() == -1  # pixel-size fonts report no point size

    def test_phase_dot_number_uses_pixel_size(self) -> None:
        dot = bfw._PhaseDot()
        dot.set_state("active", "2")
        # paintEvent builds its own QFont from self.font(); exercise the
        # same construction path without requiring an actual paint.
        font = dot.font()
        font.setPixelSize(11)
        assert font.pixelSize() == 11


class TestAggregateTrackAlwaysVisible:
    """#574 fix-loop round 3 (V2/V3): the 4px aggregate track shows on every
    page, empty or filled — round 2 hid it entirely whenever `percent` was
    `None` (pages A/B) and never re-showed it on the real B->C transition."""

    def test_visible_on_page_a(self) -> None:
        flow = _FakeFlow(items=_two_providers_one_update())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        assert w._stack.currentIndex() == bfw.PAGE_MAIN
        assert w._agg_bar.isVisibleTo(w) is True

    def test_visible_on_page_b(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        assert w._stack.currentIndex() == bfw.PAGE_PREMIGRATE
        assert w._agg_bar.isVisibleTo(w) is True


class TestMigratingHeaderSubtitle:
    """#574 fix-loop round 3 (V3): the migrating page's own subtitle
    ("...— ขั้นตอน N จาก 5") must replace page B's leftover subtitle and
    advance with the active phase — round 2 only ever changed the stack
    index, never the header."""

    def test_initial_subtitle_shows_step_one_of_five(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No-op the worker so the (synchronously-completing) fake flow can't
        # race this assertion by finishing before we read the subtitle.
        monkeypatch.setattr(bfw._MigrationWorker, "start", lambda self: None)
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w._plan = _plan()
        try:
            w._start_migration()
            assert "ขั้นตอน 1 จาก 5" in w._subtitle_label.text()
            assert w._agg_bar.isVisibleTo(w) is True
        finally:
            w._migrate_throttle.stop()

    def test_subtitle_advances_with_the_active_phase(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bfw._MigrationWorker, "start", lambda self: None)
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w._plan = _plan()
        try:
            w._start_migration()
            w._on_progress(SimpleNamespace(phase="promote", percent_overall=34))
            w._apply_pending_event()
            assert "ขั้นตอน 2 จาก 5" in w._subtitle_label.text()
            assert w._agg_bar.value() == 34
        finally:
            w._migrate_throttle.stop()


class TestKeyColumnMatchesMockup:
    """#574 fix-loop round 3 (V4): key column is 150px, not 170 — the old
    width pushed every info-box value 20px right of the mockup's x=196."""

    def test_kv_row_key_label_is_150_wide(self) -> None:
        row = bfw._kv_row("key", "value", "Sans")
        key_label = row.findChildren(QLabel)[0]
        assert key_label.minimumWidth() == 150


class TestBackupRowUnits:
    """#574 fix-loop round 3 (V6): each backup category has its own unit
    word (ไฟล์/โปรเจค/รายการ) — round 2 hardcoded "รายการ" for every row."""

    def test_unit_comes_from_the_third_tuple_element(self) -> None:
        plan = _plan(
            backup_items=[
                ("v2/ (ข้อมูลระบบ V2)", 15747, "ไฟล์"),
                ("โปรเจค", 29, "โปรเจค"),
                ("ตั้งค่า (json ชั้นบน)", 15, "ไฟล์"),
                ("runtime/core (ประวัติ, cursor)", 4, "รายการ"),
            ]
        )
        flow = _FakeFlow(items=[], plan=plan)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        texts = {lbl.text() for lbl in w._backup_card.findChildren(QLabel)}
        assert {"15,747 ไฟล์", "29 โปรเจค", "15 ไฟล์", "4 รายการ"} <= texts

    def test_two_tuple_entries_fall_back_to_the_generic_unit(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())  # default fixture: 2-tuples
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        texts = {lbl.text() for lbl in w._backup_card.findChildren(QLabel)}
        assert "15,747 รายการ" in texts


class TestPremigrateNoteRichText:
    """#574 fix-loop round 3 (V7): "คัดลอกก่อนเสมอ" must render bold and
    `TEXT_PRIMARY` inside the otherwise-muted explanatory note."""

    def test_note_bolds_the_copy_first_phrase(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        note = next(
            lbl for lbl in w._page_premigrate.findChildren(QLabel) if "คัดลอกก่อนเสมอ" in lbl.text()
        )
        assert "<b" in note.text()
        assert theme.TEXT_PRIMARY in note.text()
        assert "background: transparent" in note.styleSheet()
        assert "border: none" in note.styleSheet()


class TestFuturePhaseDotNumbers:
    """#574 fix-loop round 3 (V8): not-yet-reached phase dots show their
    position number (3/4/5) — `_PhaseDot.set_state` cleared it by default
    and callers never passed one for the "todo" state."""

    def test_todo_dots_show_their_position_number(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan(), outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._phase_rows["verify"]._number == "3"
        assert w._phase_rows["archive"]._number == "4"
        assert w._phase_rows["done"]._number == "5"


class TestVerifyStepsCount:
    """#574 fix-loop round 3 (V9): the verify row's count is the true
    validation-step count (`plan.verify_steps`), a different number from
    `len(plan.promote_items)` (9 promoted categories vs. 11 validation
    steps in the mockup) — round 2 conflated the two."""

    def test_verify_count_uses_the_verify_steps_field(self) -> None:
        plan = _plan(promote_items=list(range(9)), verify_steps=11)
        flow = _FakeFlow(items=[], plan=plan, outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._phase_count_labels["verify"].text() == "11 ขั้น"

    def test_verify_count_falls_back_to_promote_item_count(self) -> None:
        plan = _plan(promote_items=list(range(9)))
        flow = _FakeFlow(items=[], plan=plan, outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        assert w._phase_count_labels["verify"].text() == "9 ขั้น"


class TestActiveCounterPathAndColor:
    """#574 fix-loop round 3 (V9): the active row's counter appends a
    shortened current-item path and turns `TEXT_PRIMARY` — round 2 only
    ever rendered "done / total unit" in a permanently-`TEXT_FAINT` label."""

    def test_active_row_shows_current_path_in_primary_color(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bfw._MigrationWorker, "start", lambda self: None)
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w._plan = _plan()
        try:
            w._start_migration()
            w._on_progress(
                SimpleNamespace(
                    phase="promote",
                    done=3,
                    total=9,
                    unit="รายการ",
                    percent_overall=34,
                    current_path="providers/codex/default",
                )
            )
            w._apply_pending_event()
            label = w._phase_count_labels["promote"]
            assert label.text() == "3 / 9 รายการ · providers/…"
            assert theme.TEXT_PRIMARY in label.styleSheet()
        finally:
            w._migrate_throttle.stop()

    def test_todo_row_counter_stays_faint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bfw._MigrationWorker, "start", lambda self: None)
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w._plan = _plan()
        try:
            w._start_migration()
            w._on_progress(SimpleNamespace(phase="backup", percent_overall=10))
            w._apply_pending_event()
            assert theme.TEXT_FAINT in w._phase_count_labels["verify"].styleSheet()
        finally:
            w._migrate_throttle.stop()


class TestShortenCurrentPath:
    def test_truncates_to_first_segment(self) -> None:
        assert bfw._shorten_current_path("providers/codex/default") == "providers/…"

    def test_leaves_a_single_segment_untouched(self) -> None:
        assert bfw._shorten_current_path("providers") == "providers"


class TestParseLogLine:
    def test_splits_four_double_space_separated_parts(self) -> None:
        ts, op, path, detail = bfw._parse_log_line(
            "08:31:12  promote  providers/codex/default  คัดลอก 1,204 ไฟล์ · ตรวจ sha256 ตรง"
        )
        assert ts == "08:31:12"
        assert op == "promote"
        assert path == "providers/codex/default"
        assert detail == "คัดลอก 1,204 ไฟล์ · ตรวจ sha256 ตรง"

    def test_degrades_gracefully_on_a_short_line(self) -> None:
        ts, op, path, detail = bfw._parse_log_line("just one part")
        assert ts == "just one part"
        assert (op, path, detail) == ("", "", "")


class TestMigratingLogColors:
    """#574 fix-loop round 3 (V10): the log line's timestamp/operation/path/
    detail each get their own mockup color — round 2 rendered the whole
    composed string as one uniformly `TEXT_MUTED` label."""

    def test_log_line_colors_each_segment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bfw._MigrationWorker, "start", lambda self: None)
        flow = _FakeFlow(items=[], plan=_plan())
        w = bfw.BootFlowWindow(flow=flow)
        w._plan = _plan()
        try:
            w._start_migration()
            w._on_progress(
                SimpleNamespace(
                    phase="promote",
                    log_line="08:31:12  promote  providers/codex/default  คัดลอก 1,204 ไฟล์",
                )
            )
            w._apply_pending_event()
            html_text = w._log_box.text()
            assert theme.TEXT_FAINT in html_text
            assert theme.TEXT_MUTED in html_text
            assert theme.TEXT_PRIMARY in html_text
            assert "08:31:12" in html_text
            assert "providers/codex/default" in html_text
        finally:
            w._migrate_throttle.stop()


class TestDoneHeadingAndRestoreCommand:
    """#574 fix-loop round 3 (V12/V13): the count folds into the heading
    text itself (not a 4th summary row), and the restore command keeps its
    mono font inline within an otherwise-muted sentence."""

    def test_restore_command_keeps_mono_font_inline(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan(), outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        texts = [lbl.text() for lbl in w._done_paths_box.findChildren(QLabel)]
        restore_text = next(t for t in texts if "restore-v1" in t)
        assert w._mono in restore_text
        assert "font-family" in restore_text


class TestDoneFooterHintRichText:
    """#574 fix-loop round 3 (V14): "Settings → Storage" is `TEXT_MUTED`
    inside an otherwise `TEXT_FAINT` hint — round 2 rendered the whole
    sentence in one uniform faint tone."""

    def test_settings_path_span_is_muted(self) -> None:
        flow = _FakeFlow(items=[], plan=_plan(), outcome=_outcome())
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()
        hint = next(lbl for lbl in w._page_done.findChildren(QLabel) if "Settings" in lbl.text())
        assert theme.TEXT_MUTED in hint.text()
        assert "background: transparent" in hint.styleSheet()


class TestEscapeGuardDuringRealMigration:
    """#574 fix-loop round 3, B1: `reject()` (QDialog's Escape-key path) must
    be guarded the same as `closeEvent` (the window-chrome close path) — a
    real QThread is required to prove it, since every other test in this
    file relies on the module's synchronous `QThread.start` patch, which
    can't represent "worker still running" at all."""

    def test_escape_is_swallowed_while_a_real_worker_is_migrating(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        from PyQt6.QtTest import QTest

        # Removes the autouse fixture's synchronous shadow so QThread.start
        # resolves back to the real (C++) implementation for this test only.
        monkeypatch.delattr(QThread, "start", raising=False)
        app = QApplication.instance()
        assert app is not None

        release = threading.Event()
        flow = _FakeFlow(items=[], plan=_plan())
        flow.run_migration = lambda cb: (release.wait(5), _outcome())[1]
        w = bfw.BootFlowWindow(flow=flow)
        try:
            w.show()
            w._on_plan_ready(_plan())
            app.processEvents()
            w._on_premigrate_start_clicked()

            deadline = time.monotonic() + 3
            while not any(t.isRunning() for t in w._workers) and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.001)
            assert any(t.isRunning() for t in w._workers), "worker never started"

            finished: list[bool] = []
            w.flowFinished.connect(finished.append)
            QTest.keyClick(w, Qt.Key.Key_Escape)
            app.processEvents()

            assert w.isVisible() is True
            assert finished == []
            assert any(t.isRunning() for t in w._workers)
        finally:
            release.set()
            deadline = time.monotonic() + 3
            while any(t.isRunning() for t in w._workers) and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.001)
            for worker in w._workers:
                worker.wait(3000)
            app.processEvents()  # flush the queued resultReady -> stops _migrate_throttle
            w.close()
            app.processEvents()


class TestRealBackendDataclasses:
    """#574 fix-loop round 4 — built from the *actual* `boot_flow.py`
    dataclasses, not the `SimpleNamespace`/plain-tuple stand-ins the rest
    of this file uses. A round-2 real-contract audit found crashes and
    behavior regressions those stand-ins could never catch: `Path` fields
    (`backup_dir`/`archive_dir`/`log_paths`) where a bare `str` was
    assumed, and `int` phase/`failed_phase` values where a string phase
    key ("promote"/"verify") was assumed."""

    @staticmethod
    def _real_plan(tmp_path) -> MigrationPlanSummary:
        return MigrationPlanSummary(
            backup_items=[
                ("v2/ (ข้อมูลระบบ V2)", 15747, 1_200_000_000, "ไฟล์"),
                ("โปรเจค", 29, 29_000_000, "โปรเจค"),
            ],
            estimated_bytes=1.2e9,
            free_bytes=123e9,
            backup_dir=tmp_path / "backups" / "pre-migrate-test",
            promote_items=["a", "b"],
            archive_items=list(range(27)),
            junk_items=[],
        )

    @staticmethod
    def _real_outcome(tmp_path, **overrides) -> MigrationOutcome:
        base = dict(
            ok=True,
            duration_s=192.0,
            promoted=["accounts", "projects"],
            archived=list(range(27)),
            junk_deleted=3,
            projects_count=29,
            backup_dir=tmp_path / "backups" / "pre-migrate-test",
            archive_dir=tmp_path / "backups" / "v1-archive-test",
            failed_phase=None,
            failed_step=None,
            error=None,
            rolled_back=False,
            data_intact=True,
            log_paths=[tmp_path / "runtime" / "boot.log"],
        )
        base.update(overrides)
        return MigrationOutcome(**base)

    def test_premigrate_page_renders_with_path_backup_dir_and_4tuple_rows(self, tmp_path) -> None:
        flow = _FakeFlow(items=[], plan=self._real_plan(tmp_path))
        w = bfw.BootFlowWindow(flow=flow)
        w.start()  # must not raise — round-2 audit: aborted the whole process here
        assert w._stack.currentIndex() == bfw.PAGE_PREMIGRATE
        texts = {lbl.text() for lbl in w._backup_card.findChildren(QLabel)}
        # 4-tuple (label, count, bytes, unit): unit must come from index 3,
        # not the byte count at index 2 (round-4 V6 bug).
        assert any(t.startswith("15,747 ไฟล์") for t in texts)
        info_texts = [lbl.text() for lbl in w._backup_info_box.findChildren(QLabel)]
        assert any("pre-migrate-test" in t for t in info_texts)

    def test_done_page_renders_with_path_backup_and_archive_dirs(self, tmp_path) -> None:
        flow = _FakeFlow(
            items=[], plan=self._real_plan(tmp_path), outcome=self._real_outcome(tmp_path)
        )
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()  # must not raise
        assert w._stack.currentIndex() == bfw.PAGE_DONE
        texts = [lbl.text() for lbl in w._done_paths_box.findChildren(QLabel)]
        assert any("pre-migrate-test" in t for t in texts)
        assert any("v1-archive-test" in t for t in texts)

    def test_failed_page_renders_with_integer_phase_and_path_log_list(self, tmp_path) -> None:
        outcome = self._real_outcome(
            tmp_path,
            ok=False,
            failed_phase=3,
            failed_step="validate:projects/registry.json",
            failed_step_index=7,
            failed_step_total=11,
            rolled_back=True,
            error="checksum mismatch",
        )
        flow = _FakeFlow(items=[], plan=self._real_plan(tmp_path), outcome=outcome)
        w = bfw.BootFlowWindow(flow=flow)
        w.start()
        w._premigrate_start_btn.click()  # must not raise on list[Path] log_paths join
        assert w._stack.currentIndex() == bfw.PAGE_FAILED
        assert w._failed_heading.text() == "ขั้นที่ 3 ตรวจสอบ (7/11) ไม่ผ่าน"
        info_texts = [lbl.text() for lbl in w._failed_info_box.findChildren(QLabel)]
        assert any("boot.log" in t for t in info_texts)

    def test_repeated_integer_phase_event_stays_on_the_same_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(bfw._MigrationWorker, "start", lambda self: None)
        flow = _FakeFlow(items=[], plan=self._real_plan(tmp_path))
        w = bfw.BootFlowWindow(flow=flow)
        w._plan = self._real_plan(tmp_path)
        try:
            w._start_migration()
            first = ProgressEvent(
                phase=2,
                phase_label="คัดลอกขึ้นโครงใหม่",
                phases_total=5,
                done=1,
                total=9,
                unit="รายการ",
                percent_overall=20.0,
                eta_s=None,
                log_line="promote-v2-root: providers/claude/default",
                backup_dir=None,
            )
            w._on_progress(first)
            w._apply_pending_event()
            assert w._phase_rows["promote"]._kind == "active"
            second = ProgressEvent(
                phase=2,
                phase_label="คัดลอกขึ้นโครงใหม่",
                phases_total=5,
                done=4,
                total=9,
                unit="รายการ",
                percent_overall=40.0,
                eta_s=None,
                log_line="promote-v2-root: providers/codex/default",
                backup_dir=None,
            )
            w._on_progress(second)
            w._apply_pending_event()
            # Round-4 bug: a 2nd event for the SAME int phase used to fall
            # through to "next never-seen slot" and silently advance to verify.
            assert w._phase_rows["promote"]._kind == "active"
            assert w._phase_rows["verify"]._kind == "todo"
            assert "ขั้นตอน 2 จาก 5" in w._subtitle_label.text()
        finally:
            w._migrate_throttle.stop()

    def test_real_step_colon_log_line_colors_operation_and_path_separately(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(bfw._MigrationWorker, "start", lambda self: None)
        flow = _FakeFlow(items=[], plan=self._real_plan(tmp_path))
        w = bfw.BootFlowWindow(flow=flow)
        w._plan = self._real_plan(tmp_path)
        try:
            w._start_migration()
            event = ProgressEvent(
                phase=2,
                phase_label="คัดลอกขึ้นโครงใหม่",
                phases_total=5,
                done=1,
                total=9,
                unit="รายการ",
                percent_overall=20.0,
                eta_s=None,
                log_line="promote-v2-root: providers/codex/default",
                backup_dir=None,
            )
            w._on_progress(event)
            w._apply_pending_event()
            html_text = w._log_box.text()
            assert "promote-v2-root" in html_text
            assert "providers/codex/default" in html_text
            assert theme.TEXT_MUTED in html_text
            assert theme.TEXT_PRIMARY in html_text
        finally:
            w._migrate_throttle.stop()


class TestDoneGuardOnMigratingPage:
    """#574 fix-loop round 4, B1 residual: the round-2 audit's direct
    `w.done(0)` probe still dismissed the dialog and emitted
    `flowFinished(True)` while a real worker was migrating — `reject()`/
    `accept()`/`closeEvent` were already guarded, `done()` itself was not."""

    def test_direct_done_call_is_swallowed_while_migrating(self) -> None:
        flow = _FakeFlow(items=[], plan=None)
        w = bfw.BootFlowWindow(flow=flow)
        w._stack.setCurrentIndex(bfw.PAGE_MIGRATING)
        finished: list[bool] = []
        w.flowFinished.connect(finished.append)
        w.done(0)
        assert finished == []

    def test_direct_done_call_still_works_outside_migrating_page(self) -> None:
        flow = _FakeFlow(items=[], plan=None)
        w = bfw.BootFlowWindow(flow=flow)
        finished: list[bool] = []
        w.flowFinished.connect(finished.append)
        w.done(1)
        assert finished == [True]


class TestPathStrHelper:
    def test_relative_to_data_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setattr(config, "DATA_HOME", tmp_path)
        p = tmp_path / "backups" / "pre-migrate-test"
        assert bfw._path_str(p) == str(Path("backups") / "pre-migrate-test")

    def test_outside_data_home_falls_back_to_absolute(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(config, "DATA_HOME", tmp_path / "elsewhere")
        p = tmp_path / "backups"
        assert bfw._path_str(p) == str(p)

    def test_none_and_empty_string(self) -> None:
        assert bfw._path_str(None) == ""
        assert bfw._path_str("") == ""

    def test_plain_string_passthrough(self) -> None:
        assert bfw._path_str("backups/pre-migrate-test/") == "backups/pre-migrate-test/"


class TestUnpackBackupRow:
    def test_four_tuple_uses_index_3_as_unit(self) -> None:
        label, count, size_bytes, unit = bfw._unpack_backup_row(
            ("v2/", 15747, 1_200_000_000, "ไฟล์")
        )
        assert (label, count, size_bytes, unit) == ("v2/", 15747, 1_200_000_000, "ไฟล์")

    def test_three_tuple_has_no_size(self) -> None:
        assert bfw._unpack_backup_row(("v2/", 15747, "ไฟล์")) == ("v2/", 15747, None, "ไฟล์")

    def test_two_tuple_falls_back_to_generic_unit(self) -> None:
        assert bfw._unpack_backup_row(("v2/", 15747)) == ("v2/", 15747, None, "รายการ")


class TestPhaseLabelAndNumber:
    def test_integer_phase_resolves_label_and_number(self) -> None:
        assert bfw._phase_label_and_number(3) == ("ตรวจสอบ", 3)

    def test_string_phase_key_still_resolves(self) -> None:
        assert bfw._phase_label_and_number("verify") == ("ตรวจสอบ", 3)

    def test_out_of_range_integer_is_unknown(self) -> None:
        assert bfw._phase_label_and_number(99) == ("ไม่ทราบขั้นตอน", None)

    def test_none_is_unknown(self) -> None:
        assert bfw._phase_label_and_number(None) == ("ไม่ทราบขั้นตอน", None)
