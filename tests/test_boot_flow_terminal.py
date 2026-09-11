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
