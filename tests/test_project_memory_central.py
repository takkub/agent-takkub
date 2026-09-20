"""#687: provider-neutral central project memory.

Lead memory used to live only under claude-config (claude-only) — switching
the Lead to another provider silently lost every learned lesson. These tests
cover the central store + two-way newest-wins sync + resolver preference.
"""

from __future__ import annotations

import os
import pathlib
from types import SimpleNamespace

import pytest

from agent_takkub import project_memory


@pytest.fixture
def env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    runtime = tmp_path / "runtime"
    cfg = tmp_path / "claude-config"
    monkeypatch.setattr(project_memory, "RUNTIME_DIR", runtime)
    monkeypatch.setattr("agent_takkub.user_profile.config_dir_for", lambda project=None: cfg)
    monkeypatch.setattr(project_memory, "lead_cwd", lambda project=None: None)
    native = cfg / "projects" / "takkub-project-demo" / "memory"
    return SimpleNamespace(runtime=runtime, cfg=cfg, native=native)


class TestSync:
    def test_first_sync_migrates_native_to_central(self, env: SimpleNamespace) -> None:
        env.native.mkdir(parents=True)
        (env.native / "MEMORY.md").write_text("- [rule](rule.md)\n", encoding="utf-8")
        (env.native / "rule.md").write_text("never do X\n", encoding="utf-8")

        copied = project_memory.sync("demo")

        central = project_memory.central_dir("demo")
        assert copied == 2
        assert (central / "MEMORY.md").read_text(encoding="utf-8") == "- [rule](rule.md)\n"
        assert (central / "rule.md").exists()
        # #504 invariant: migration never deletes the source.
        assert (env.native / "MEMORY.md").exists()

    def test_back_sync_central_to_native(self, env: SimpleNamespace) -> None:
        env.native.mkdir(parents=True)
        central = project_memory.central_dir("demo")
        central.mkdir(parents=True)
        (central / "from-codex.md").write_text("lesson learned by codex Lead\n", encoding="utf-8")

        project_memory.sync("demo")

        assert (env.native / "from-codex.md").exists()

    def test_newer_file_wins(self, env: SimpleNamespace) -> None:
        env.native.mkdir(parents=True)
        central = project_memory.central_dir("demo")
        central.mkdir(parents=True)
        (central / "MEMORY.md").write_text("old\n", encoding="utf-8")
        (env.native / "MEMORY.md").write_text("new\n", encoding="utf-8")
        old_ts = (central / "MEMORY.md").stat().st_mtime - 60
        os.utime(central / "MEMORY.md", (old_ts, old_ts))

        project_memory.sync("demo")

        assert (central / "MEMORY.md").read_text(encoding="utf-8") == "new\n"

    def test_sync_is_idempotent(self, env: SimpleNamespace) -> None:
        env.native.mkdir(parents=True)
        (env.native / "MEMORY.md").write_text("x\n", encoding="utf-8")
        assert project_memory.sync("demo") == 1
        assert project_memory.sync("demo") == 0

    def test_role_suffixed_teammate_dirs_not_synced(self, env: SimpleNamespace) -> None:
        # #516 F1 split teammate memory off Lead's on purpose — the central
        # store must not pool it back in.
        tm = env.cfg / "projects" / "takkub-project-demo-frontend" / "memory"
        tm.mkdir(parents=True)
        (tm / "teammate.md").write_text("frontend private note\n", encoding="utf-8")

        project_memory.sync("demo")

        assert not (project_memory.central_dir("demo") / "teammate.md").exists()

    def test_no_native_dir_is_quiet(self, env: SimpleNamespace) -> None:
        assert project_memory.sync("demo") == 0


class TestResolver:
    def test_resolve_prefers_central_when_project_given(self, env: SimpleNamespace) -> None:
        from agent_takkub.orchestrator_text import _resolve_project_memory

        env.native.mkdir(parents=True)
        (env.native / "MEMORY.md").write_text("- rule\n", encoding="utf-8")

        mem = _resolve_project_memory(None, project_ns="demo")

        assert mem == project_memory.central_memory_md("demo")
        assert mem is not None and mem.exists()

    def test_resolve_without_project_keeps_legacy_behavior(
        self, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        from agent_takkub.orchestrator_text import _resolve_project_memory

        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path / "nohome")
        assert _resolve_project_memory("C:/some/cwd") is None


class TestEntryCount:
    def test_counts_entries_excluding_index(self, env: SimpleNamespace) -> None:
        central = project_memory.central_dir("demo")
        central.mkdir(parents=True)
        (central / "MEMORY.md").write_text("index\n", encoding="utf-8")
        (central / "a.md").write_text("a\n", encoding="utf-8")
        (central / "b.md").write_text("b\n", encoding="utf-8")
        assert project_memory.memory_entry_count("demo") == 2

    def test_missing_dir_is_zero(self, env: SimpleNamespace) -> None:
        assert project_memory.memory_entry_count("demo") == 0
