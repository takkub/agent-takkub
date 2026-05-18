"""Tests for `_render_daily_digest`, the helper that composes one
Finish-Job section for `<vault>/05-Daily/<date>.md`.

The renderer takes plain values so the contract is testable without
spinning up Qt or touching the filesystem. Pinning the heading
format matters because multiple Finish Jobs in one day all append
to the same daily note — drift in the H2 marker would break visual
scanning.
"""

from __future__ import annotations

from datetime import datetime

from agent_takkub.orchestrator import _render_daily_digest


class TestRenderDailyDigest:
    def test_empty_sessions_shows_marker(self) -> None:
        body = _render_daily_digest("agent-takkub", datetime(2026, 5, 17, 14, 30, 5), [])
        assert "## `agent-takkub`" in body
        assert "14:30:05" in body
        assert "No `takkub done` events recorded today" in body

    def test_one_session_renders_line(self) -> None:
        body = _render_daily_digest(
            "agent-takkub",
            datetime(2026, 5, 17, 14, 30, 5),
            [("103026", "backend", "fixed 401 bug")],
        )
        assert "**Sessions completed today: 1**" in body
        assert "`103026` **backend** — fixed 401 bug" in body

    def test_multiple_sessions_listed_in_given_order(self) -> None:
        # Caller passes sessions most-recent-first; renderer preserves
        # the order it gets. No re-sorting so the caller controls
        # which entry appears at the top of the section.
        body = _render_daily_digest(
            "agent-takkub",
            datetime(2026, 5, 17, 14, 30, 5),
            [
                ("120000", "reviewer", "TS compile passes"),
                ("103026", "backend", "fixed 401 bug"),
            ],
        )
        lines = body.splitlines()
        # Find the two bullet lines and confirm reviewer appears before backend
        reviewer_idx = next(i for i, line in enumerate(lines) if "reviewer" in line)
        backend_idx = next(i for i, line in enumerate(lines) if "backend" in line)
        assert reviewer_idx < backend_idx

    def test_multiline_note_collapses_to_first_line(self) -> None:
        # Real `takkub done` notes are sometimes multi-line. The daily
        # digest is for scanning, not reading the full note, so the
        # renderer must keep each entry to one line.
        body = _render_daily_digest(
            "p",
            datetime.now(),
            [("100000", "qa", "smoke ok\nfollowups: A, B, C\nlinks: X")],
        )
        assert "smoke ok" in body
        assert "followups" not in body
        assert "links:" not in body

    def test_empty_note_falls_back_to_role_only(self) -> None:
        body = _render_daily_digest("p", datetime.now(), [("100000", "qa", "")])
        # Bullet exists but no em-dash + text
        assert "`100000` **qa**" in body
        assert "**qa** —" not in body

    def test_section_is_one_h2(self) -> None:
        # Multiple Finish Job invocations append to the same file —
        # each digest must produce exactly one H2 (no surprise H1/H3
        # nesting that would mess up Obsidian's outline view).
        body = _render_daily_digest("p", datetime.now(), [("100000", "qa", "ok")])
        # No H1 anywhere
        h1_count = sum(1 for line in body.splitlines() if line.startswith("# "))
        assert h1_count == 0
        # Exactly one H2
        h2_count = sum(1 for line in body.splitlines() if line.startswith("## "))
        assert h2_count == 1

    def test_thai_unicode_survives(self) -> None:
        body = _render_daily_digest("p", datetime.now(), [("100000", "backend", "แก้ bug auth")])
        assert "แก้ bug auth" in body

    def test_decisions_section_omitted_when_empty(self) -> None:
        body = _render_daily_digest("p", datetime.now(), [], decisions=[])
        assert "Decisions today" not in body

    def test_decisions_section_omitted_when_none(self) -> None:
        body = _render_daily_digest("p", datetime.now(), [], decisions=None)
        assert "Decisions today" not in body

    def test_decisions_section_lists_each_with_timestamp(self) -> None:
        decisions = [
            {
                "timestamp": "2026-05-17T11:30:05Z",
                "heading": "Bracketed paste fix",
                "project": "p",
            },
            {
                "timestamp": "2026-05-17T10:00:00Z",
                "heading": "ECC mute decision",
                "project": "p",
            },
        ]
        body = _render_daily_digest("p", datetime.now(), [], decisions=decisions)
        assert "**Decisions today: 2**" in body
        assert "Bracketed paste fix" in body
        assert "ECC mute decision" in body
        # Timestamp shown without trailing Z / seconds-precision
        assert "2026-05-17 11:30" in body

    def test_decisions_skip_empty_headings(self) -> None:
        # Defensive: a decision dict without a heading shouldn't
        # render an empty bullet.
        body = _render_daily_digest(
            "p",
            datetime.now(),
            [],
            decisions=[{"timestamp": "x", "heading": "", "project": "p"}],
        )
        # Section header still appears (count > 0) but no bullet line.
        assert "Decisions today" in body
        bullets = [line for line in body.splitlines() if line.startswith("- ") and "`" in line]
        assert bullets == []
