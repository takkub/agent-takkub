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
