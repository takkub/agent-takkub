"""Tests for per-(role × project) learned memory (role_memory.py).

Each teammate role gets its own ``runtime/role-memory/<project>/<role>.md`` that
it reads on spawn and appends learnings to. Seeding must be idempotent (never
clobber accumulated learnings), per-role templated, and per-project isolated.
"""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub import role_memory as rm
from agent_takkub.role_memory import ensure_role_memory, role_memory_path


@pytest.fixture
def isolated_role_memory(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Redirect ROLE_MEMORY_DIR to a tmp path so tests don't write under runtime/."""
    target = tmp_path / "role-memory"
    monkeypatch.setattr(rm, "ROLE_MEMORY_DIR", target)
    return target


class TestEnsureRoleMemory:
    def test_seeds_file_when_missing(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("proj_a", "frontend")
        assert path is not None
        assert path == isolated_role_memory / "proj_a" / "frontend.md"
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "frontend" in text  # header names the role
        assert "proj_a" in text  # …and the project
        assert "## Conventions" in text  # base sections present

    def test_qa_template_has_login_and_flows(self, isolated_role_memory: pathlib.Path) -> None:
        text = ensure_role_memory("proj_a", "qa").read_text(encoding="utf-8")
        assert "Test login" in text
        assert "Known flows" in text

    def test_role_specific_sections(self, isolated_role_memory: pathlib.Path) -> None:
        assert "Components" in ensure_role_memory("p", "frontend").read_text(encoding="utf-8")
        assert "Endpoints" in ensure_role_memory("p", "backend").read_text(encoding="utf-8")

    def test_unknown_role_gets_base_template_only(self, isolated_role_memory: pathlib.Path) -> None:
        text = ensure_role_memory("proj_a", "data-eng").read_text(encoding="utf-8")
        assert "## Conventions" in text
        assert "Gotchas" in text
        assert "Test login" not in text  # no qa extras
        assert "Endpoints" not in text  # no backend extras

    def test_idempotent_preserves_appended_learnings(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        path = ensure_role_memory("proj_a", "qa")
        learned = path.read_text(encoding="utf-8") + "\n- learned: login is admin / test123\n"
        path.write_text(learned, encoding="utf-8")
        # Re-ensure (next spawn) must NOT clobber the role's accumulated learning.
        again = ensure_role_memory("proj_a", "qa")
        assert again == path
        assert "admin / test123" in path.read_text(encoding="utf-8")

    def test_per_project_and_per_role_isolation(self, isolated_role_memory: pathlib.Path) -> None:
        qa_a = ensure_role_memory("proj_a", "qa")
        qa_b = ensure_role_memory("proj_b", "qa")
        fe_a = ensure_role_memory("proj_a", "frontend")
        assert qa_a != qa_b != fe_a
        assert qa_a.parent.name == "proj_a"
        assert qa_b.parent.name == "proj_b"
        assert fe_a.name == "frontend.md"

    def test_sanitizes_unsafe_names(self, isolated_role_memory: pathlib.Path) -> None:
        path = role_memory_path("my proj/weird:name", "qa")
        assert path.parent.name == "my_proj_weird_name"  # no / or : in the segment
        assert path.name == "qa.md"

    def test_no_parent_dir_traversal(self, isolated_role_memory: pathlib.Path) -> None:
        # Dots are dropped so a `..` project/role can't escape ROLE_MEMORY_DIR.
        for evil in ("..", "../../etc", "a..b"):
            p = role_memory_path(evil, "qa")
            assert ".." not in p.parts
            assert isolated_role_memory in p.parents
        assert role_memory_path("p", "..").name == "__.md"


class TestCuration:
    """ensure_role_memory curates the accumulated file on read (#43): dedup
    repeated bullets + size-cap, preserving the header + seeded skeleton."""

    def test_dedup_collapses_repeated_bullet(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "qa")
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n- pitfall: ZZZWIDGET needs a retry\n- Pitfall:  zzzwidget needs a retry  \n",
            encoding="utf-8",
        )
        ensure_role_memory("p", "qa")  # curate on next spawn
        lines = [
            ln for ln in path.read_text(encoding="utf-8").splitlines() if "zzzwidget" in ln.lower()
        ]
        assert len(lines) == 1, lines
        # newest (last) occurrence is the one kept, verbatim.
        assert lines[0] == "- Pitfall:  zzzwidget needs a retry  "

    def test_dedup_keeps_distinct_bullets(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "qa")
        path.write_text(
            path.read_text(encoding="utf-8") + "\n- alpha note one\n- beta note two\n",
            encoding="utf-8",
        )
        ensure_role_memory("p", "qa")
        text = path.read_text(encoding="utf-8")
        assert "alpha note one" in text and "beta note two" in text

    def test_size_cap_trims_oldest_keeps_newest(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rm, "_MEM_MAX_ENTRIES", 5)
        path = ensure_role_memory("p", "backend")
        notes = "".join(f"\n- note number {i:03d}\n" for i in range(20))
        path.write_text(path.read_text(encoding="utf-8") + notes, encoding="utf-8")
        ensure_role_memory("p", "backend")
        text = path.read_text(encoding="utf-8")
        n_bullets = sum(1 for ln in text.splitlines() if rm._BULLET_RE.match(ln))
        assert n_bullets <= 5
        assert "number 019" in text  # newest survives
        assert "number 000" not in text  # oldest trimmed

    def test_preserves_header_and_seeded_headings(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "qa")
        # A duplicate forces a curation rewrite.
        path.write_text(path.read_text(encoding="utf-8") + "\n- dupe\n- dupe\n", encoding="utf-8")
        ensure_role_memory("p", "qa")
        text = path.read_text(encoding="utf-8")
        assert "# qa — learned notes" in text  # header survives
        assert "## Conventions / patterns" in text  # seeded skeleton survives
        assert "## Gotchas / pitfalls" in text

    def test_sub_headings_stay_in_body(self) -> None:
        _header, sections = rm._split_doc("# h\n\n## A\n- one\n### sub\n- two\n## B\n- three\n")
        assert len(sections) == 2  # only ## splits; ### is body
        assert any("### sub" in ln for ln in sections[0][1])

    def test_curation_is_idempotent(self, isolated_role_memory: pathlib.Path) -> None:
        text = ensure_role_memory("p", "qa").read_text(encoding="utf-8")
        new, changed = rm._curate_text(text)
        assert changed is False and new == text

    def test_literal_braces_preserved(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "devops")
        brace = "- health: {{.State.Health.Status}} must be healthy"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n" + brace + "\n- dupe\n- dupe\n",
            encoding="utf-8",
        )
        ensure_role_memory("p", "devops")  # must not raise on literal braces
        assert brace in path.read_text(encoding="utf-8")

    def test_best_effort_on_unreadable(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = ensure_role_memory("p", "qa")

        def boom(*_a, **_k):
            raise OSError("read failed")

        monkeypatch.setattr(pathlib.Path, "read_text", boom)
        # A read failure during curation must be swallowed — ensure still returns.
        assert ensure_role_memory("p", "qa") == path
