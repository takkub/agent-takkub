"""Tests for `_render_hot_md`, the helper that composes the body of
`<vault>/hot.md` — the cockpit's live-state snapshot the user opens
in Obsidian to orient themselves.

The renderer takes plain values (no Qt / Pane refs) so the contract
is testable in isolation. Pin the section headings, the empty-state
behaviour, and the cap on "Recent" so a noisy run doesn't push the
file past one screen in Obsidian.
"""

from __future__ import annotations

import datetime as dt

from agent_takkub.orchestrator import _HOT_MD_INTERVAL_MS, _render_hot_md


class TestRenderHotMd:
    def test_title_and_timestamp_are_first(self) -> None:
        now = dt.datetime(2026, 5, 17, 11, 0, 0)
        body = _render_hot_md({}, None, [], now)
        first = body.splitlines()[0]
        assert first == "# Hot — cockpit live state"
        assert "2026-05-17T11:00:00" in body

    def test_active_project_named_when_set(self) -> None:
        body = _render_hot_md({}, "agent-takkub", [], dt.datetime.now())
        assert "**Active project:** `agent-takkub`" in body

    def test_active_project_shows_none_marker_when_unset(self) -> None:
        body = _render_hot_md({}, None, [], dt.datetime.now())
        assert "projects.json `active` unset" in body

    def test_no_projects_shows_empty_marker(self) -> None:
        body = _render_hot_md({}, None, [], dt.datetime.now())
        assert "_No projects open in cockpit._" in body

    def test_projects_render_sorted_with_roles(self) -> None:
        # Two projects, multiple roles each — both projects must
        # appear, sorted, and inside each project the roles are
        # sorted too. Deterministic output makes file diffs cheap to
        # read and the file safe to commit to a vault repo.
        body = _render_hot_md(
            {
                "pms": {"backend": "working", "lead": "idle"},
                "agent-takkub": {"reviewer": "done"},
            },
            "agent-takkub",
            [],
            dt.datetime.now(),
        )
        # project order: agent-takkub before pms
        a_idx = body.index("`agent-takkub`")
        p_idx = body.index("`pms`")
        assert a_idx < p_idx
        # roles inside pms: backend before lead
        b_idx = body.index("**backend**")
        l_idx = body.index("**lead**")
        assert b_idx < l_idx
        # states show up
        assert "working" in body
        assert "idle" in body

    def test_empty_project_shows_no_panes_marker(self) -> None:
        # A project that's been registered (e.g. tab opened) but has
        # no panes yet should explicitly say so rather than rendering
        # a blank section the user has to interpret.
        body = _render_hot_md({"agent-takkub": {}}, None, [], dt.datetime.now())
        assert "_(no panes)_" in body

    def test_recent_done_empty_shows_marker(self) -> None:
        body = _render_hot_md({}, None, [], dt.datetime.now())
        assert "_(no done events this session)_" in body

    def test_recent_done_caps_at_ten(self) -> None:
        # The "Recent" section is for orientation, not auditing — the
        # full trail lives in `01-Projects/<p>/sessions/`. A long
        # session shouldn't push hot.md past one screen.
        recent = [
            ("p", f"r{i}", f"2026-05-17T1100{i:02d}-r{i}.md") for i in range(25)
        ]
        body = _render_hot_md({}, None, recent, dt.datetime.now())
        # All entries 0..9 present; 10..24 absent
        for i in range(10):
            assert f"**r{i}**" in body
        for i in range(10, 25):
            assert f"**r{i}**" not in body

    def test_thai_in_pane_state_survives(self) -> None:
        # Pane `state.note` often holds Thai task descriptions
        # (truncated to ~60 chars in `pane.set_state`). Confirm the
        # renderer doesn't re-encode them.
        body = _render_hot_md(
            {"agent-takkub": {"backend": "working (เขียน endpoint)"}},
            None,
            [],
            dt.datetime.now(),
        )
        assert "เขียน endpoint" in body

    def test_footer_references_actual_interval(self) -> None:
        # The footer hints how often the file refreshes. If
        # `_HOT_MD_INTERVAL_MS` changes, the message must follow so a
        # user looking at a stale hot.md knows the expected cadence.
        body = _render_hot_md({}, None, [], dt.datetime.now())
        assert f"every {_HOT_MD_INTERVAL_MS // 1000}s" in body

    def test_overall_structure_has_three_sections(self) -> None:
        body = _render_hot_md(
            {"agent-takkub": {"lead": "idle"}},
            "agent-takkub",
            [("agent-takkub", "qa", "2026-05-17T100000-qa.md")],
            dt.datetime.now(),
        )
        # Three H2 sections: Panes, Recent, plus the title is H1.
        # (Hook noise section is omitted when there are no counts.)
        assert body.count("\n## ") == 2
        assert "## Panes" in body
        assert "## Recent `takkub done`" in body

    def test_hook_noise_section_omitted_when_empty(self) -> None:
        # A quiet day (no hooks fired) should not push an empty
        # "Hook noise" header to the file — keeps the digest scannable.
        body = _render_hot_md({}, None, [], dt.datetime.now(), hook_counts={})
        assert "Hook noise" not in body

    def test_hook_noise_section_rendered_when_counts_present(self) -> None:
        body = _render_hot_md(
            {},
            None,
            [],
            dt.datetime.now(),
            hook_counts={"ecc-gateguard": 47, "ecc-cost-monitor": 62},
        )
        assert "## Hook noise today" in body
        assert "ecc-gateguard" in body
        assert "47" in body
        assert "ecc-cost-monitor" in body
        assert "62" in body

    def test_friction_section_omitted_when_empty(self) -> None:
        body = _render_hot_md({}, None, [], dt.datetime.now(), friction={})
        assert "Friction" not in body

    def test_friction_section_omitted_when_all_zero(self) -> None:
        # Empty-dict and all-zero counts should both suppress the
        # section — otherwise a quiet day still gets an empty header.
        body = _render_hot_md(
            {},
            None,
            [],
            dt.datetime.now(),
            friction={"corrections": 0, "tool_retries": 0},
        )
        assert "Friction" not in body

    def test_friction_section_rendered_with_counts(self) -> None:
        body = _render_hot_md(
            {},
            None,
            [],
            dt.datetime.now(),
            friction={"corrections": 5, "tool_retries": 2},
        )
        assert "## Friction today" in body
        assert "user corrections" in body
        assert "tool retry storms" in body
        assert "5" in body
        assert "2" in body

    def test_friction_skips_zero_counts(self) -> None:
        # If corrections > 0 but tool_retries == 0, only the
        # corrections bullet should render (cleaner output).
        body = _render_hot_md(
            {},
            None,
            [],
            dt.datetime.now(),
            friction={"corrections": 3, "tool_retries": 0},
        )
        assert "user corrections" in body
        assert "tool retry storms" not in body

    def test_hook_noise_orders_loudest_first(self) -> None:
        # The user opens hot.md to see the worst offender first — sort
        # by count descending so it's the topmost line in the section.
        body = _render_hot_md(
            {},
            None,
            [],
            dt.datetime.now(),
            hook_counts={"low": 3, "high": 99, "mid": 30},
        )
        # Find the hook bullets (use " — " separator unique to hook
        # entries vs. the session bullets which use " · ").
        hook_lines = [
            l for l in body.splitlines() if l.startswith("- **") and " — " in l
        ]
        assert hook_lines[0].startswith("- **high**")
        assert hook_lines[1].startswith("- **mid**")
        assert hook_lines[2].startswith("- **low**")
