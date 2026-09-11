"""`boot_flow_terminal.py` (#574) — text-formatting for the 5-screen flow,
and `run_cli`'s `--json` mode (used by `takkub migrate run`)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agent_takkub import boot_flow, boot_flow_terminal, config


def test_format_progress_line_shows_bar_percent_phase_and_count():
    event = boot_flow.ProgressEvent(
        phase=2,
        phase_label="คัดลอกขึ้นโครงใหม่",
        phases_total=5,
        done=3,
        total=9,
        unit="รายการ",
        percent_overall=34.0,
        eta_s=120.0,
        log_line="providers/…",
        backup_dir=Path("/tmp/backups/pre-migrate-x"),
    )
    line = boot_flow_terminal.format_progress_line(event)
    assert "34%" in line
    assert "2/5" in line
    assert "3/9" in line
    assert "เหลือ ~2 นาที" in line


def test_format_progress_line_shows_within_entry_file_count():
    """#574 round11 item 3: `files_done`/`files_total` (progress WITHIN
    one large directory entry) must render as "(n/m ไฟล์)" appended to
    the step count — surfaced during a real ~200k-file rehearsal that
    otherwise sat silent on one entry for 17+ minutes."""
    event = boot_flow.ProgressEvent(
        phase=1,
        phase_label="สำรองข้อมูล",
        phases_total=5,
        done=1,
        total=3,
        unit="รายการ",
        percent_overall=10.0,
        eta_s=None,
        log_line="pre-migrate-backup: claude-config (1200/5000)",
        backup_dir=None,
        current_path="claude-config/some/file.json",
        files_done=1200,
        files_total=5000,
    )
    line = boot_flow_terminal.format_progress_line(event)
    assert "1200/5000 ไฟล์" in line


def test_format_progress_line_defensive_when_files_fields_missing():
    """A hand-built or older `ProgressEvent` with no `files_done`/
    `files_total` at all must still render — the fields were added after
    this dataclass first shipped."""

    class _OldEvent:
        phase = 1
        phase_label = "สำรองข้อมูล"
        phases_total = 5
        done = 1
        total = 3
        percent_overall = 10.0
        eta_s = None

    line = boot_flow_terminal.format_progress_line(_OldEvent())
    assert "ไฟล์" not in line
    assert "1/3" in line


def test_format_provider_menu_marks_selected_items():
    items = [
        boot_flow.ProviderUpdateItem(
            "claude",
            "Claude Code",
            "2.1.267",
            "2.1.270",
            True,
            boot_flow.PROVIDER_STATUS_UPDATE_AVAILABLE,
        ),
        boot_flow.ProviderUpdateItem(
            "gemini", "Gemini (agy)", "1.2.0", None, False, boot_flow.PROVIDER_STATUS_UP_TO_DATE
        ),
    ]
    text = boot_flow_terminal.format_provider_menu(items)
    assert "[x]" in text
    assert "[ ]" in text
    assert "Claude Code" in text
    assert "มีอัพเดต" in text
    assert "ล่าสุดแล้ว" in text


def test_format_outcome_success_lists_counts():
    outcome = boot_flow.MigrationOutcome(
        ok=True,
        duration_s=192.0,
        promoted=["config", "models"],
        archived=["custom-roles.json"],
        junk_deleted=1,
        projects_count=29,
        backup_dir=Path("/tmp/backups/pre-migrate-x"),
        archive_dir=Path("/tmp/backups/v1-archive-1"),
        failed_phase=None,
        failed_step=None,
        error=None,
        rolled_back=False,
        data_intact=True,
        log_paths=[],
    )
    text = boot_flow_terminal.format_outcome(outcome)
    assert "เสร็จแล้ว" in text
    assert "2 รายการ" in text
    assert "29 โปรเจค" in text


def test_format_outcome_failure_shows_failed_step_and_error():
    outcome = boot_flow.MigrationOutcome(
        ok=False,
        duration_s=12.0,
        promoted=[],
        archived=[],
        junk_deleted=0,
        projects_count=0,
        backup_dir=Path("/tmp/backups/pre-migrate-x"),
        archive_dir=None,
        failed_phase=3,
        failed_step="project",
        error="registry.json อ่านไม่ได้",
        rolled_back=True,
        data_intact=True,
        log_paths=[Path("/tmp/boot.log")],
    )
    text = boot_flow_terminal.format_outcome(outcome)
    assert "ไม่สำเร็จ" in text
    assert "project" in text
    assert "registry.json" in text


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_home = tmp_path / "data_home"
    settings_home = tmp_path / "settings_home"
    data_home.mkdir()
    settings_home.mkdir()
    monkeypatch.setattr(config, "DATA_HOME", data_home)
    monkeypatch.setattr(config, "SETTINGS_HOME", settings_home)
    monkeypatch.setattr(config, "RUNTIME_DIR", data_home / "runtime")
    yield


def test_run_cli_json_mode_emits_provider_and_outcome_lines(monkeypatch):
    import agent_takkub.core.storage.layout as layout_mod

    monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v2")
    monkeypatch.setattr(boot_flow, "check_provider_updates", lambda **k: [])
    from agent_takkub import auto_migrate_boot

    monkeypatch.setattr(
        auto_migrate_boot,
        "run_boot_stage",
        lambda **k: auto_migrate_boot.BootMigrationResult("skipped", "nothing pending", []),
    )

    out = io.StringIO()
    code = boot_flow_terminal.run_cli(["--json"], out=out)

    assert code == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    types = [line["type"] for line in lines]
    assert "outcome" in types


# ---------------------------------------------------------------------------
# #574 round11 item 9 — acceptance review round 7's `boot_flow_terminal.py`
# findings (R7-M1/M3/M4/L1).
# ---------------------------------------------------------------------------


def _one_update_available_item(**overrides):
    defaults = dict(
        name="claude",
        label="Claude Code",
        current="2.1.267",
        latest="2.1.270",
        selected=True,
        status=boot_flow.PROVIDER_STATUS_UPDATE_AVAILABLE,
    )
    defaults.update(overrides)
    return boot_flow.ProviderUpdateItem(**defaults)


def _no_op_run_boot_stage(monkeypatch):
    import agent_takkub.core.storage.layout as layout_mod
    from agent_takkub import auto_migrate_boot

    monkeypatch.setattr(layout_mod, "layout_state", lambda *a, **k: "v2")
    monkeypatch.setattr(
        auto_migrate_boot,
        "run_boot_stage",
        lambda **k: auto_migrate_boot.BootMigrationResult("skipped", "nothing pending", []),
    )


def test_json_mode_never_updates_providers_it_did_not_ask_about(monkeypatch):
    """#574 round11 R7-M3: `--providers ask` (the default) used to run
    `run_provider_updates` for whatever `check_provider_updates()`
    pre-selected, without ever actually asking — a `--json` (necessarily
    non-interactive) caller must resolve to "none", never silently update
    anything."""
    _no_op_run_boot_stage(monkeypatch)
    monkeypatch.setattr(
        boot_flow, "check_provider_updates", lambda **k: [_one_update_available_item()]
    )
    updated = []
    monkeypatch.setattr(
        boot_flow,
        "run_provider_updates",
        lambda items, **k: updated.append(items) or items,
    )

    out = io.StringIO()
    code = boot_flow_terminal.run_cli(["--json"], out=out)

    assert code == 0
    assert updated == []  # never called — nothing was ever actually selected
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    provider_lines = [line for line in lines if line["type"] == "provider"]
    assert provider_lines and all(not line["selected"] for line in provider_lines)
    assert any(line["type"] == "provider_prompt_skipped" for line in lines)


def test_remember_persists_even_when_providers_is_none(monkeypatch):
    """#574 round11 R7-M4: `--providers none --remember` used to never
    persist anything (gated behind `if any(it.selected for it in items)`)
    — must now record mode "skip", not "selected"."""
    _no_op_run_boot_stage(monkeypatch)
    monkeypatch.setattr(
        boot_flow, "check_provider_updates", lambda **k: [_one_update_available_item()]
    )
    saved = {}
    monkeypatch.setattr(boot_flow, "remember_provider_choice", lambda choice: saved.update(choice))

    out = io.StringIO()
    code = boot_flow_terminal.run_cli(["--providers", "none", "--remember", "--json"], out=out)

    assert code == 0
    assert saved.get("mode") == "skip"


def test_ask_mode_reads_back_a_remembered_skip_choice_without_prompting(monkeypatch):
    """#574 round11 R7-M4: `run_cli` used to never call
    `remembered_provider_choice()` at all — a previously remembered
    "skip" must be honored on the default `ask` path, never re-prompted
    (and never silently updated, the R7-M3 failure mode)."""
    _no_op_run_boot_stage(monkeypatch)
    monkeypatch.setattr(
        boot_flow, "check_provider_updates", lambda **k: [_one_update_available_item()]
    )
    monkeypatch.setattr(
        boot_flow, "remembered_provider_choice", lambda: {"mode": "skip", "selected": []}
    )
    updated = []
    monkeypatch.setattr(
        boot_flow,
        "run_provider_updates",
        lambda items, **k: updated.append(items) or items,
    )

    def _fail_if_prompted(*a, **k):
        raise AssertionError("must not prompt when a remembered choice already resolves this")

    monkeypatch.setattr("builtins.input", _fail_if_prompted)

    out = io.StringIO()
    code = boot_flow_terminal.run_cli(["--json"], out=out)

    assert code == 0
    assert updated == []


def test_no_backup_flag_is_gone(monkeypatch):
    """#574 round11 R7-M2: `--no-backup` warned but never actually skipped
    anything — removed outright (the task's own stated alternative to
    wiring a real skip) rather than left as a flag that silently does
    nothing."""
    _no_op_run_boot_stage(monkeypatch)
    monkeypatch.setattr(boot_flow, "check_provider_updates", lambda **k: [])

    out = io.StringIO()
    with pytest.raises(SystemExit):
        boot_flow_terminal.run_cli(["--no-backup"], out=out)


def test_json_mode_output_is_every_line_valid_json(monkeypatch):
    """#574 round11 R7-M1: every line `--json` mode ever writes must parse
    as JSON — the old `--no-backup` warning broke this with a bare Thai
    `print(...)`. A standing guard against any future plain `print(...,
    file=out)` creeping back into the `--json` path."""
    _no_op_run_boot_stage(monkeypatch)
    monkeypatch.setattr(
        boot_flow, "check_provider_updates", lambda **k: [_one_update_available_item()]
    )
    monkeypatch.setattr(boot_flow, "run_provider_updates", lambda items, **k: items)

    out = io.StringIO()
    code = boot_flow_terminal.run_cli(["--providers", "all", "--json"], out=out)

    assert code == 0
    for line in out.getvalue().splitlines():
        if line.strip():
            json.loads(line)  # raises if any line isn't valid JSON


def test_json_implies_yes_logs_auto_confirmed(monkeypatch):
    """#574 round11 R7-L1: `--json` silently implying `--yes` (no human to
    answer the confirm prompt) must be logged as an explicit event, not
    just silently assumed."""
    _no_op_run_boot_stage(monkeypatch)
    monkeypatch.setattr(boot_flow, "check_provider_updates", lambda **k: [])

    out = io.StringIO()
    code = boot_flow_terminal.run_cli(["--json"], out=out)

    assert code == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert any(line["type"] == "auto_confirmed" for line in lines)

    # An explicit --yes needs no such note — nothing was auto-anything.
    out2 = io.StringIO()
    boot_flow_terminal.run_cli(["--json", "--yes"], out=out2)
    lines2 = [json.loads(line) for line in out2.getvalue().splitlines() if line.strip()]
    assert not any(line["type"] == "auto_confirmed" for line in lines2)
