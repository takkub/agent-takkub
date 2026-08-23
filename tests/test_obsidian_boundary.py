"""`obsidian_boundary.is_indexable` — default-deny indexing boundary
(#365 phase 8 improvements 3-5)."""

from __future__ import annotations

import pytest

from agent_takkub.obsidian_boundary import (
    ALLOWLIST_PREFIXES,
    DENYLIST_PREFIXES,
    is_indexable,
)


class TestAllowlist:
    @pytest.mark.parametrize(
        "rel_path",
        [
            "01-Projects/agent-takkub.md",
            "01-Projects/sub/nested.md",
            "02-Areas/bug-patterns.md",
        ],
    )
    def test_curated_paths_are_indexable(self, rel_path):
        assert is_indexable(rel_path) is True

    def test_allowlist_is_exactly_projects_and_areas(self):
        assert set(ALLOWLIST_PREFIXES) == {"01-Projects", "02-Areas"}


class TestDefaultDeny:
    @pytest.mark.parametrize(
        "rel_path",
        [
            "99-Logs/sessions/proj/backend-120000.md",
            ".obsidian/graph.json",
            "runtime/knowledge/proj.md",
            "secrets/token.json",
            "03-Unknown/whatever.md",
            "",
        ],
    )
    def test_denied_by_default(self, rel_path):
        assert is_indexable(rel_path) is False

    def test_denylist_wins_even_if_it_somehow_overlapped_allowlist(self):
        for prefix in DENYLIST_PREFIXES:
            assert prefix not in ALLOWLIST_PREFIXES

    def test_dotfile_anywhere_in_path_denied(self):
        assert is_indexable("01-Projects/.hidden/note.md") is False

    @pytest.mark.parametrize(
        "rel_path",
        [
            "01-Projects/session.transcript",
            "01-Projects/pane.pty",
            "01-Projects/raw.raw",
            "01-Projects/output.log",
        ],
    )
    def test_raw_transcript_suffix_denied_even_inside_allowlist(self, rel_path):
        assert is_indexable(rel_path) is False

    def test_windows_backslash_paths_normalised(self):
        assert is_indexable("01-Projects\\agent-takkub.md") is True
        assert is_indexable("99-Logs\\sessions\\proj\\x.md") is False
