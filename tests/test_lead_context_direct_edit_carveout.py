"""#474: the Lead direct-edit carve-out (small cockpit typo/policy/config/docs
edit) must not be contradicted by the self-check and the absolute-ban list
that appear later in the same rendered suffix."""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture
def dev_mode_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    cockpit_md = tmp_path / "CLAUDE.md"
    cockpit_md.write_text("# Cockpit CLAUDE.md\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    from agent_takkub import config as config_mod
    from agent_takkub import lead_context as lc_mod

    monkeypatch.setattr(lc_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lc_mod, "ASSETS_ROOT", tmp_path)
    monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config_mod, "PROJECTS_JSON", tmp_path / "projects.json")
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(lc_mod, "_recent_session_brief", lambda _proj: None)
    try:
        from agent_takkub import provider_state as ps_mod

        monkeypatch.setattr(ps_mod, "all_disabled", lambda: set())
    except Exception:
        pass
    return tmp_path


def test_carveout_present_and_self_check_references_it(dev_mode_env: pathlib.Path) -> None:
    from agent_takkub.lead_context import _build_lead_context_text

    text = _build_lead_context_text()
    assert text is not None

    # The carve-out conditions are still declared.
    assert "Lead ทำเองได้เฉพาะงานเล็กเมื่อเข้าเงื่อนไขครบทุกข้อ" in text
    assert "ไม่เกิน 1 ไฟล์ และไม่เกิน 30 บรรทัด" in text

    # The mandatory self-check no longer reads as an unconditional ban —
    # it must point back at the carve-out instead of contradicting it.
    self_check_idx = text.index("Self-check บังคับ")
    self_check_line = text[self_check_idx : text.index("\n", self_check_idx)]
    assert "ข้อยกเว้น" in self_check_line

    # Source-code / tests remain banned with no exception, unchanged.
    assert "ห้ามทำเองแม้แค่บรรทัดเดียว" in text
    assert "ทุกไฟล์ที่เป็น source code หรือ tests" in text


def test_carveout_covers_user_project_md_txt_under_blocked_dirs(dev_mode_env: pathlib.Path) -> None:
    """#474 round 2: the carve-out must explicitly reach *.md/*.txt files
    under BLOCKED_DIRS in the *user's* project, not just cockpit docs, while
    still banning source/tests/schema/config that qa-gate must cover."""
    from agent_takkub.lead_context import _build_lead_context_text

    text = _build_lead_context_text()
    assert text is not None

    assert "#474" in text
    assert "*.md" in text and "*.txt" in text
    assert "BLOCKED_DIRS" in text

    # Criteria: <=1 file, ~40 lines, verified this turn, no suitable pane open.
    assert "≤ 1 ไฟล์" in text or "≤1 ไฟล์" in text
    assert "40 บรรทัด" in text
    assert "verify" in text
    assert "ไม่มี pane role ที่เหมาะเปิดอยู่" in text

    # Still bans source/tests/schema/config that runtime/qa-gate must cover.
    assert "ทุกไฟล์ที่เป็น source code หรือ tests" in text
    assert "qa-gate ต้องคุม" in text
