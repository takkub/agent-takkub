"""Tests for `_recent_session_brief` — context inheritance across Lead sessions.

The new Lead spawn-time prompt embeds a brief of recent end-session summaries
and today's teammate done events for the active project, so a fresh session
isn't a blank slate. These tests cover:

  1. No history → returns None (brief is omitted, not an empty section).
  2. Recent `lead-*.md` → brief includes the latest summary's content.
  3. Today's teammate `<role>-*.md` files → listed under done-events.
  4. Output is size-capped so it can't bloat Lead's context budget.
  5. Brief is project-scoped — files under other project dirs are ignored.
  6. `_render_lead_context` appends the brief section when history exists.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta

import pytest

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import _recent_session_brief, _render_lead_context

TEST_PROJECT = "testproj"


@pytest.fixture
def runtime_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Point RUNTIME_DIR at tmp_path so brief reads don't see real session logs.

    `_recent_session_brief` and `_render_lead_context` were extracted from
    orchestrator.py to lead_context.py; both modules need the patched
    RUNTIME_DIR because functions read it from their own module namespace.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    from agent_takkub import lead_context as lc_mod

    monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)
    return runtime


def _write_lead_summary(
    runtime: pathlib.Path, project: str, day: str, time_str: str, note: str
) -> pathlib.Path:
    """Helper: write a fake `lead-HHMMSS.md` matching end_session()'s format."""
    target_dir = runtime / "sessions" / day / project
    target_dir.mkdir(parents=True, exist_ok=True)
    body = (
        f"---\n"
        f"role: lead\n"
        f"project: {project}\n"
        f"date: {day}T{time_str[:2]}:{time_str[2:4]}:{time_str[4:]}\n"
        f"tags: [session, lead, {project}]\n"
        f"---\n\n"
        f"# lead session end\n\n"
        f"## Note\n\n{note}\n"
    )
    f = target_dir / f"lead-{time_str}.md"
    f.write_text(body, encoding="utf-8")
    return f


def _write_done(
    runtime: pathlib.Path, project: str, day: str, role: str, time_str: str
) -> pathlib.Path:
    """Helper: write a teammate done-event file in today's session dir."""
    target_dir = runtime / "sessions" / day / project
    target_dir.mkdir(parents=True, exist_ok=True)
    f = target_dir / f"{role}-{time_str}.md"
    f.write_text(f"# {role} done\n\nwork content\n", encoding="utf-8")
    return f


def _write_done_note(
    runtime: pathlib.Path, project: str, day: str, role: str, time_str: str, note: str
) -> pathlib.Path:
    """Helper: write a teammate done file with a real `## Note` block,
    matching `_render_decision_note`'s on-disk layout."""
    target_dir = runtime / "sessions" / day / project
    target_dir.mkdir(parents=True, exist_ok=True)
    body = (
        f"---\n"
        f"role: {role}\n"
        f"project: {project}\n"
        f"date: {day}T{time_str[:2]}:{time_str[2:4]}:{time_str[4:]}\n"
        f"tags: [session, {role}, {project}]\n"
        f"---\n\n"
        f"# {role} done\n\n"
        f"**Project:** [[01-Projects/{project}|{project}]]\n"
        f"**Role:** {role}\n\n"
        f"## Note\n\n{note}\n"
    )
    f = target_dir / f"{role}-{time_str}.md"
    f.write_text(body, encoding="utf-8")
    return f


class TestRecentSessionBrief:
    def test_no_history_returns_none(self, runtime_tmp: pathlib.Path) -> None:
        """No session dir at all → brief returns None (caller skips section)."""
        assert _recent_session_brief(TEST_PROJECT) is None

    def test_empty_project_dir_returns_none(self, runtime_tmp: pathlib.Path) -> None:
        """Project dir exists but has no md files → None."""
        today = datetime.now().strftime("%Y-%m-%d")
        (runtime_tmp / "sessions" / today / TEST_PROJECT).mkdir(parents=True)
        assert _recent_session_brief(TEST_PROJECT) is None

    def test_includes_latest_lead_summary(self, runtime_tmp: pathlib.Path) -> None:
        """Most-recent `lead-*.md` content appears in the brief."""
        today = datetime.now().strftime("%Y-%m-%d")
        _write_lead_summary(runtime_tmp, TEST_PROJECT, today, "120000", "rebuild login flow done")
        brief = _recent_session_brief(TEST_PROJECT)
        assert brief is not None
        assert "rebuild login flow done" in brief

    def test_picks_newest_lead_across_dates(self, runtime_tmp: pathlib.Path) -> None:
        """When summaries exist on multiple dates, the brief uses the newest."""
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        _write_lead_summary(runtime_tmp, TEST_PROJECT, yesterday, "100000", "OLD summary content")
        _write_lead_summary(runtime_tmp, TEST_PROJECT, today, "150000", "NEW summary content")
        brief = _recent_session_brief(TEST_PROJECT)
        assert brief is not None
        assert "NEW summary content" in brief
        # Older summary should not dominate — brief is "most recent first" focused.
        assert (
            brief.index("NEW summary content") < brief.index("OLD summary content")
            if ("OLD summary content" in brief)
            else True
        )

    def test_lists_todays_done_events(self, runtime_tmp: pathlib.Path) -> None:
        """Today's substantive teammate done events surface with both their
        `<role>-<time>` stem and their note body."""
        today = datetime.now().strftime("%Y-%m-%d")
        _write_done_note(
            runtime_tmp, TEST_PROJECT, today, "backend", "090000", "added rate-limit middleware"
        )
        _write_done_note(
            runtime_tmp, TEST_PROJECT, today, "frontend", "100000", "wired login form to /auth"
        )
        brief = _recent_session_brief(TEST_PROJECT)
        assert brief is not None
        assert "backend-090000" in brief
        assert "frontend-100000" in brief
        assert "rate-limit middleware" in brief
        assert "wired login form" in brief

    def test_includes_teammate_note_content(self, runtime_tmp: pathlib.Path) -> None:
        """A teammate done note's *body* (not just its filename) is injected —
        this is the whole point: a fresh Lead recalls *what* was done."""
        today = datetime.now().strftime("%Y-%m-%d")
        _write_done_note(
            runtime_tmp,
            TEST_PROJECT,
            today,
            "backend",
            "090000",
            "implemented JWT refresh-token rotation with 7-day sliding window",
        )
        brief = _recent_session_brief(TEST_PROJECT)
        assert brief is not None
        assert "JWT refresh-token rotation" in brief

    def test_teammate_notes_carry_across_days(self, runtime_tmp: pathlib.Path) -> None:
        """Substantive teammate notes from previous days still surface when
        there are no events today — knowledge must not vanish at midnight."""
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        _write_done_note(
            runtime_tmp,
            TEST_PROJECT,
            yesterday,
            "frontend",
            "143000",
            "migrated dashboard to server components, cut TTI by 400ms",
        )
        brief = _recent_session_brief(TEST_PROJECT)
        assert brief is not None
        assert "server components" in brief

    def test_skips_junk_teammate_notes(self, runtime_tmp: pathlib.Path) -> None:
        """Thin acknowledgement notes ('ok'/'wip') don't pollute the brief —
        with no substantive history the brief is omitted entirely."""
        today = datetime.now().strftime("%Y-%m-%d")
        _write_done_note(runtime_tmp, TEST_PROJECT, today, "backend", "090000", "ok")
        _write_done_note(runtime_tmp, TEST_PROJECT, today, "qa", "100000", "wip")
        assert _recent_session_brief(TEST_PROJECT) is None

    def test_newest_teammate_notes_first(self, runtime_tmp: pathlib.Path) -> None:
        """Most-recent teammate note appears before older ones in the brief."""
        today = datetime.now().strftime("%Y-%m-%d")
        _write_done_note(
            runtime_tmp, TEST_PROJECT, today, "backend", "090000", "OLDER substantive note here"
        )
        _write_done_note(
            runtime_tmp, TEST_PROJECT, today, "frontend", "150000", "NEWER substantive note here"
        )
        brief = _recent_session_brief(TEST_PROJECT)
        assert brief is not None
        assert brief.index("NEWER substantive note here") < brief.index(
            "OLDER substantive note here"
        )

    def test_ignores_other_projects(self, runtime_tmp: pathlib.Path) -> None:
        """Done events under a *different* project don't leak into the brief."""
        today = datetime.now().strftime("%Y-%m-%d")
        _write_done(runtime_tmp, "other_proj", today, "backend", "090000")
        _write_lead_summary(runtime_tmp, "other_proj", today, "120000", "OTHER PROJECT note")
        assert _recent_session_brief(TEST_PROJECT) is None

    def test_output_size_capped(self, runtime_tmp: pathlib.Path) -> None:
        """Even with a huge note, brief stays under the size budget (~4KB)."""
        today = datetime.now().strftime("%Y-%m-%d")
        huge_note = "x" * 50_000
        _write_lead_summary(runtime_tmp, TEST_PROJECT, today, "120000", huge_note)
        brief = _recent_session_brief(TEST_PROJECT)
        assert brief is not None
        assert len(brief) < 4096, f"brief grew to {len(brief)} chars (budget: 4096)"


class TestLeadContextAppendsBrief:
    def test_section_appended_when_history(
        self, runtime_tmp: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_render_lead_context` appends a 'Recent session' section when history exists."""
        today = datetime.now().strftime("%Y-%m-%d")
        _write_lead_summary(
            runtime_tmp, TEST_PROJECT, today, "140000", "wrap-up note from prior session"
        )

        # Provide minimal projects.json shape so _render_lead_context resolves paths.
        # `_render_lead_context` reads load_projects from the lead_context
        # module namespace (where it was extracted to). Patching orch_mod
        # would no-op now — the orchestrator re-export was removed when
        # ruff cleaned up unused imports during the extraction.
        from agent_takkub import lead_context as lc_mod

        monkeypatch.setattr(
            lc_mod,
            "load_projects",
            lambda: {"projects": {TEST_PROJECT: {"paths": {"web": "/tmp/web"}}}},
        )
        out_path = _render_lead_context(TEST_PROJECT)
        assert out_path is not None
        content = pathlib.Path(out_path).read_text(encoding="utf-8")
        assert "Recent session" in content
        assert "wrap-up note from prior session" in content

    def test_section_omitted_when_no_history(
        self, runtime_tmp: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No history → no 'Recent session' header (avoids empty stub)."""
        # `_render_lead_context` reads load_projects from the lead_context
        # module namespace (where it was extracted to). Patching orch_mod
        # would no-op now — the orchestrator re-export was removed when
        # ruff cleaned up unused imports during the extraction.
        from agent_takkub import lead_context as lc_mod

        monkeypatch.setattr(
            lc_mod,
            "load_projects",
            lambda: {"projects": {TEST_PROJECT: {"paths": {"web": "/tmp/web"}}}},
        )
        out_path = _render_lead_context(TEST_PROJECT)
        assert out_path is not None
        content = pathlib.Path(out_path).read_text(encoding="utf-8")
        assert "Recent session" not in content
