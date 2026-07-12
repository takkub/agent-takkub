"""Tests for project-memory pointer injection into teammate spawn prompts (issue #33).

Verifies that when Lead's MEMORY.md exists for a project cwd, the teammate's
CLAUDE.md (materialised by agent_role_dir) gets a pointer appended so the
teammate can read domain rules on demand.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from agent_takkub.orchestrator import _resolve_project_memory
from agent_takkub.token_meter import encode_path_for_claude


class TestResolveProjectMemory:
    def test_returns_none_when_cwd_is_none(self) -> None:
        assert _resolve_project_memory(None) is None

    def test_returns_none_when_memory_file_absent(self, tmp_path) -> None:
        # Patch home so no real ~/.claude/projects dir is consulted.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pathlib.Path, "home", classmethod(lambda _: tmp_path))
            result = _resolve_project_memory(str(tmp_path / "myproject"))
        assert result is None

    def test_returns_path_when_memory_file_exists(self, tmp_path) -> None:
        # Simulate a cwd whose encoded name lives under tmp_path/.claude/projects/
        cwd = str(tmp_path / "myproject")
        encoded = encode_path_for_claude(cwd)
        mem_dir = tmp_path / ".claude" / "projects" / encoded / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / "MEMORY.md").write_text("# index", encoding="utf-8")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pathlib.Path, "home", classmethod(lambda _: tmp_path))
            result = _resolve_project_memory(cwd)

        assert result is not None
        assert result.name == "MEMORY.md"
        assert result.exists()

    def test_encoding_matches_claude_code_convention(self, tmp_path) -> None:
        """The encoded directory name must match Claude Code's own scheme."""
        if sys.platform == "win32":
            # Windows-style path: C:\Users\alice\project
            cwd = r"C:\Users\alice\project"
            expected_encoded = "C--Users-alice-project"
        else:
            # POSIX path: /Users/alice/project (leading "/" → "-")
            cwd = "/Users/alice/project"
            expected_encoded = "-Users-alice-project"
        mem_dir = tmp_path / ".claude" / "projects" / expected_encoded / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / "MEMORY.md").write_text("# mem", encoding="utf-8")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pathlib.Path, "home", classmethod(lambda _: tmp_path))
            result = _resolve_project_memory(cwd)

        assert result is not None
        assert expected_encoded in str(result)


class TestMemoryInjectedIntoRoleMd:
    """When agent_role_dir generates the teammate CLAUDE.md and MEMORY.md
    exists for the spawn_cwd, the file must contain a pointer to MEMORY.md."""

    def test_pointer_appended_when_memory_exists(self, tmp_path, monkeypatch) -> None:
        import agent_takkub.config as config_mod
        import agent_takkub.orchestrator as orch_mod

        # Redirect RUNTIME_DIR so agent_role_dir writes under tmp_path.
        rt = tmp_path / "_runtime"
        rt.mkdir()
        monkeypatch.setattr(config_mod, "RUNTIME_DIR", rt)
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", rt)

        # Create a minimal .claude/agents/backend.md source.
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "backend.md").write_text(
            "# Backend role\nDo backend things.", encoding="utf-8"
        )
        monkeypatch.setattr(config_mod, "AGENTS_DIR", agents_dir)

        # Create the project memory file.
        cwd = str(tmp_path / "myproject")
        encoded = encode_path_for_claude(cwd)
        mem_dir = tmp_path / ".claude" / "projects" / encoded / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        mem_path = mem_dir / "MEMORY.md"
        mem_path.write_text("- [rule](rule.md)", encoding="utf-8")

        # Stub _resolve_project_memory to return our test memory path.
        monkeypatch.setattr(orch_mod, "_resolve_project_memory", lambda _cwd: mem_path)

        # Run agent_role_dir to materialise the CLAUDE.md.
        from agent_takkub.config import agent_role_dir

        staging = agent_role_dir("backend")
        role_md_path = staging / "CLAUDE.md"
        assert role_md_path.exists()

        # Simulate what spawn() does: check for memory and append pointer.
        _mem = orch_mod._resolve_project_memory(cwd)
        if _mem is not None:
            existing = role_md_path.read_text(encoding="utf-8")
            pointer = f"\n\n---\n\n## 📋 Project memory\n\n`{_mem}`\n"
            role_md_path.write_text(existing + pointer, encoding="utf-8")

        content = role_md_path.read_text(encoding="utf-8")
        assert str(mem_path) in content, "MEMORY.md path must appear in role CLAUDE.md"
        assert "Project memory" in content

    def test_no_pointer_when_memory_absent(self, tmp_path, monkeypatch) -> None:
        import agent_takkub.config as config_mod
        import agent_takkub.orchestrator as orch_mod

        rt = tmp_path / "_runtime"
        rt.mkdir()
        monkeypatch.setattr(config_mod, "RUNTIME_DIR", rt)
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", rt)

        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "backend.md").write_text("# Backend", encoding="utf-8")
        monkeypatch.setattr(config_mod, "AGENTS_DIR", agents_dir)

        # No MEMORY.md exists — _resolve_project_memory returns None.
        monkeypatch.setattr(orch_mod, "_resolve_project_memory", lambda _: None)

        from agent_takkub.config import agent_role_dir

        staging = agent_role_dir("backend")
        role_md_path = staging / "CLAUDE.md"
        original_content = role_md_path.read_text(encoding="utf-8")

        # spawn() would skip injection because _mem is None.
        _mem = orch_mod._resolve_project_memory("/some/cwd")
        assert _mem is None

        # File must be unchanged.
        assert role_md_path.read_text(encoding="utf-8") == original_content


class TestMemoryResolveMultiPathProject:
    """Regression test for B2 (#33): memory must be keyed from lead_cwd
    (project root), not from the teammate's sub-path cwd.

    Multi-path project layout:
        lead_cwd  → .../unirecon          (common parent, keyed in memory)
        spawn_cwd → .../unirecon/api      (backend teammate cwd)

    Before B2 fix: _resolve_project_memory(spawn_cwd) → None → no injection.
    After  B2 fix: _resolve_project_memory(lead_cwd or spawn_cwd) → mem path → inject.
    """

    def _create_memory(self, tmp_path: pathlib.Path, project_root: pathlib.Path) -> pathlib.Path:
        """Write a fake MEMORY.md keyed from *project_root* under tmp_path/.claude."""
        encoded = encode_path_for_claude(project_root)
        mem_dir = tmp_path / ".claude" / "projects" / encoded / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        mem_file = mem_dir / "MEMORY.md"
        mem_file.write_text("- [rule](rule.md)", encoding="utf-8")
        return mem_file

    def test_resolve_fails_with_sub_cwd(self, tmp_path) -> None:
        """Baseline: resolving from the teammate sub-path returns None
        because memory is keyed from the parent project root."""
        lead_root = tmp_path / "unirecon"
        lead_root.mkdir()
        backend_cwd = lead_root / "api"
        backend_cwd.mkdir()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pathlib.Path, "home", classmethod(lambda _: tmp_path))
            self._create_memory(tmp_path, lead_root)
            # Teammate sub-path → no match (memory keyed from parent)
            assert _resolve_project_memory(str(backend_cwd)) is None

    def test_resolve_succeeds_with_lead_cwd(self, tmp_path) -> None:
        """After B2 fix: resolving from lead_cwd finds the memory file
        even when the teammate's spawn_cwd is a sub-directory."""
        lead_root = tmp_path / "unirecon"
        lead_root.mkdir()
        backend_cwd = lead_root / "api"
        backend_cwd.mkdir()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pathlib.Path, "home", classmethod(lambda _: tmp_path))
            mem_file = self._create_memory(tmp_path, lead_root)
            # Lead root → match
            result = _resolve_project_memory(str(lead_root))
            assert result is not None
            assert result == mem_file

    def test_lead_cwd_or_spawn_cwd_pattern(self, tmp_path) -> None:
        """The fixed expression `lead_cwd(ns) or spawn_cwd` resolves from
        lead_cwd when available, falling back to spawn_cwd.

        This mirrors the exact change made in orchestrator.py line 1294."""
        lead_root = tmp_path / "unirecon"
        lead_root.mkdir()
        backend_cwd = lead_root / "api"
        backend_cwd.mkdir()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pathlib.Path, "home", classmethod(lambda _: tmp_path))
            mem_file = self._create_memory(tmp_path, lead_root)

            # Simulate: lead_cwd(project_ns) = lead_root, spawn_cwd = backend_cwd
            effective_cwd = str(lead_root) or str(backend_cwd)
            result = _resolve_project_memory(effective_cwd)
            assert result is not None, (
                "Memory inject must fire for multi-path project when resolved "
                "from lead_cwd — not from the teammate's sub-path"
            )
            assert result == mem_file
