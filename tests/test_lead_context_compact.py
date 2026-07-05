"""Tests for post-compact brief injection into _render_lead_context.

Verifies that:
  1. _render_lead_context accepts post_compact_brief and appends it.
  2. None brief is silently omitted (no extra section written).
  3. Non-None brief appears after the BLOCKED_DIRS section in the output.
"""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub import orchestrator as orch_mod
from agent_takkub.lead_context import _render_lead_context


@pytest.fixture
def runtime_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    from agent_takkub import lead_context as lc_mod

    monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)
    return runtime


@pytest.fixture
def cockpit_md(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Provide a minimal CLAUDE.md so _render_lead_context doesn't return None."""
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Lead Instructions\n\nsome rules\n", encoding="utf-8")
    import agent_takkub.orchestrator as orch_m
    from agent_takkub import lead_context as lc_mod

    monkeypatch.setattr(lc_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lc_mod, "ASSETS_ROOT", tmp_path)
    monkeypatch.setattr(orch_m, "REPO_ROOT", tmp_path)
    return md


class TestPostCompactBriefInjection:
    def test_none_brief_omitted(self, runtime_tmp: pathlib.Path, cockpit_md: pathlib.Path) -> None:
        result_path = _render_lead_context("default", post_compact_brief=None)
        assert result_path is not None
        text = pathlib.Path(result_path).read_text(encoding="utf-8")
        assert "Post-compact" not in text

    def test_brief_appended_when_provided(
        self, runtime_tmp: pathlib.Path, cockpit_md: pathlib.Path
    ) -> None:
        brief = "\n---\n\n## 🔄 Post-compact status (auto-injected)\n\nsome content\n"
        result_path = _render_lead_context("default", post_compact_brief=brief)
        assert result_path is not None
        text = pathlib.Path(result_path).read_text(encoding="utf-8")
        assert "Post-compact status" in text
        assert "some content" in text

    def test_brief_appears_after_blocked_dirs(
        self, runtime_tmp: pathlib.Path, cockpit_md: pathlib.Path
    ) -> None:
        brief = "\n---\n\n## 🔄 Post-compact status (auto-injected)\n\nsome content\n"
        result_path = _render_lead_context("default", post_compact_brief=brief)
        text = pathlib.Path(result_path).read_text(encoding="utf-8")
        blocked_idx = text.find("BLOCKED_DIRS")
        compact_idx = text.find("Post-compact")
        assert blocked_idx != -1
        assert compact_idx != -1
        assert compact_idx > blocked_idx

    def test_session_brief_and_compact_brief_both_present(
        self, runtime_tmp: pathlib.Path, cockpit_md: pathlib.Path
    ) -> None:
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        sess_dir = runtime_tmp / "sessions" / today / "default"
        sess_dir.mkdir(parents=True)
        (sess_dir / "lead-120000.md").write_text(
            "---\nrole: lead\nproject: default\ndate: 2026-05-26\ntags: []\n---\n\n# lead end\n\n## Note\n\nprev session note\n",
            encoding="utf-8",
        )

        brief = "\n---\n\n## 🔄 Post-compact status (auto-injected)\n\nsome content\n"
        result_path = _render_lead_context("default", post_compact_brief=brief)
        text = pathlib.Path(result_path).read_text(encoding="utf-8")
        assert "prev session note" in text
        assert "Post-compact status" in text


class TestParallelModeWorktreeRule:
    """The PARALLEL exec-mode block must teach the Lead to isolate same-repo
    fan-out instances with --isolation worktree (#81 Phase 1.5) — and SOLO
    spawns must not pay for the block at all."""

    def test_parallel_block_includes_worktree_rule(
        self, runtime_tmp: pathlib.Path, cockpit_md: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import exec_mode

        monkeypatch.setattr(exec_mode, "current", lambda: exec_mode.PARALLEL)
        result_path = _render_lead_context("default")
        text = pathlib.Path(result_path).read_text(encoding="utf-8")
        assert "Execution mode: PARALLEL" in text
        assert "--isolation worktree" in text
        assert "merge proposal" in text

    def test_solo_mode_has_no_parallel_block(
        self, runtime_tmp: pathlib.Path, cockpit_md: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import exec_mode

        monkeypatch.setattr(exec_mode, "current", lambda: exec_mode.SOLO)
        result_path = _render_lead_context("default")
        text = pathlib.Path(result_path).read_text(encoding="utf-8")
        assert "Execution mode: PARALLEL" not in text
        assert "--isolation worktree" not in text
