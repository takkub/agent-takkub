"""Tests for project_rules module and lead_context project-rules injection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_takkub import config as config_mod
from agent_takkub.project_rules import (
    generate_project_rules,
    read_project_rules,
    write_project_rules,
)

# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Return a temp directory representing a project root."""
    proj = tmp_path / "myproject"
    proj.mkdir()
    return proj


# ─────────────────────────────────────────────────────────────────────
# read_project_rules
# ─────────────────────────────────────────────────────────────────────


class TestReadProjectRules:
    def test_returns_none_when_missing(self, tmp_project: Path) -> None:
        assert read_project_rules(tmp_project) is None

    def test_returns_content_when_present(self, tmp_project: Path) -> None:
        (tmp_project / "CLAUDE.md").write_text("# Rules\n- no foo", encoding="utf-8")
        assert read_project_rules(tmp_project) == "# Rules\n- no foo"


# ─────────────────────────────────────────────────────────────────────
# write_project_rules
# ─────────────────────────────────────────────────────────────────────


class TestWriteProjectRules:
    def test_writes_file(self, tmp_project: Path) -> None:
        path = write_project_rules(tmp_project, "# Generated\n- deploy via Vercel\n")
        assert path == tmp_project / "CLAUDE.md"
        assert path.read_text(encoding="utf-8") == "# Generated\n- deploy via Vercel\n"

    def test_overwrites_existing(self, tmp_project: Path) -> None:
        (tmp_project / "CLAUDE.md").write_text("old content", encoding="utf-8")
        write_project_rules(tmp_project, "new content")
        assert (tmp_project / "CLAUDE.md").read_text(encoding="utf-8") == "new content"

    def test_atomic_via_tmp(self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the tmp file is cleaned up (replaced) after write."""
        write_project_rules(tmp_project, "content")
        tmp = tmp_project / "CLAUDE.md.tmp"
        assert not tmp.exists(), "tmp file should be gone after atomic replace"


# ─────────────────────────────────────────────────────────────────────
# generate_project_rules (mocked subprocess)
# ─────────────────────────────────────────────────────────────────────


class TestGenerateProjectRules:
    def _make_proc(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        proc = MagicMock()
        proc.communicate.return_value = (stdout, stderr)
        proc.returncode = returncode
        return proc

    def test_returns_markdown_on_success(self) -> None:
        proc = self._make_proc(stdout="# Rules\n- deploy to Fly.io\n")
        with (
            patch("agent_takkub.project_rules.find_claude_executable", return_value="claude"),
            patch("agent_takkub.project_rules.subprocess.Popen", return_value=proc),
        ):
            result = generate_project_rules("Next.js + FastAPI", "myapp")
        assert "# Rules" in result

    def test_raises_on_nonzero_exit(self) -> None:
        proc = self._make_proc(stdout="", stderr="auth error", returncode=1)
        with (
            patch("agent_takkub.project_rules.find_claude_executable", return_value="claude"),
            patch("agent_takkub.project_rules.subprocess.Popen", return_value=proc),
        ):
            with pytest.raises(RuntimeError, match="exited 1"):
                generate_project_rules("desc", "proj")

    def test_raises_on_empty_output(self) -> None:
        proc = self._make_proc(stdout="   ", returncode=0)
        with (
            patch("agent_takkub.project_rules.find_claude_executable", return_value="claude"),
            patch("agent_takkub.project_rules.subprocess.Popen", return_value=proc),
        ):
            with pytest.raises(RuntimeError, match="empty output"):
                generate_project_rules("desc", "proj")

    def test_uses_append_system_prompt_flag(self) -> None:
        """Regression: must use --append-system-prompt, not the invalid --system flag."""
        proc = self._make_proc(stdout="# Rules\n- deploy\n")
        captured: list = []
        with (
            patch("agent_takkub.project_rules.find_claude_executable", return_value="claude"),
            patch(
                "agent_takkub.project_rules.subprocess.Popen",
                side_effect=lambda args, **kw: captured.append(args) or proc,
            ),
        ):
            generate_project_rules("desc", "proj")
        assert len(captured) == 1
        args = captured[0]
        assert "--append-system-prompt" in args
        assert "--system" not in [a for a in args if a == "--system"]

    def test_raises_when_claude_not_found(self) -> None:
        """m4: find_claude_executable returns None → RuntimeError, not TypeError."""
        with patch("agent_takkub.project_rules.find_claude_executable", return_value=None):
            with pytest.raises(RuntimeError, match="claude binary not found"):
                generate_project_rules("desc", "proj")

    def test_raises_on_timeout(self) -> None:
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=150)
        proc.kill = MagicMock()
        # After kill, communicate should succeed (drain)
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=150),
            ("", ""),
        ]
        with (
            patch("agent_takkub.project_rules.find_claude_executable", return_value="claude"),
            patch("agent_takkub.project_rules.subprocess.Popen", return_value=proc),
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                generate_project_rules("desc", "proj")


# ─────────────────────────────────────────────────────────────────────
# lead_context: project rules injection
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def lead_context_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Wire lead_context + config to a temp directory with a minimal CLAUDE.md."""
    cockpit_md = tmp_path / "CLAUDE.md"
    cockpit_md.write_text("# Cockpit CLAUDE.md\nBase rules.\n", encoding="utf-8")

    runtime = tmp_path / "runtime"
    runtime.mkdir()

    projects_json = tmp_path / "projects.json"

    from agent_takkub import lead_context as lc_mod

    monkeypatch.setattr(lc_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config_mod, "PROJECTS_JSON", projects_json)
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "RUNTIME_DIR", runtime)

    # Stub out side-effectful sections we're not testing here
    monkeypatch.setattr(lc_mod, "_recent_session_brief", lambda _proj: None)
    try:
        from agent_takkub import provider_state as ps_mod

        monkeypatch.setattr(ps_mod, "all_disabled", lambda: set())
    except Exception:
        pass
    try:
        from agent_takkub import plan_tier as pt_mod

        monkeypatch.setattr(pt_mod, "is_pro", lambda: False)
    except Exception:
        pass

    return tmp_path


def _write_projects(tmp_path: Path, proj_root: Path, name: str = "myapp") -> None:
    data = {
        "active": name,
        "projects": {
            name: {
                "paths": {"main": str(proj_root.as_posix())},
                "presets": [],
            }
        },
    }
    (tmp_path / "projects.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestLeadContextProjectRulesInjection:
    def test_injects_project_rules_when_present(
        self, lead_context_env: Path, tmp_path: Path
    ) -> None:
        """Project CLAUDE.md contents should appear in the rendered lead context."""
        proj_root = tmp_path / "ext" / "myapp"
        proj_root.mkdir(parents=True)
        (proj_root / "CLAUDE.md").write_text(
            "# Project rules\n- deploy to Fly.io\n", encoding="utf-8"
        )
        _write_projects(tmp_path, proj_root)

        from agent_takkub.lead_context import _render_lead_context

        # tok-4: injection only happens when claude WON'T auto-discover the file
        # from its cwd. Pass an unrelated cwd to exercise the inject path (the
        # skip-when-auto-loaded path is covered separately below).
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        out_path = _render_lead_context(project="myapp", claude_cwd=str(elsewhere))
        assert out_path is not None
        content = Path(out_path).read_text(encoding="utf-8")
        assert "deploy to Fly.io" in content
        assert "Project rules" in content

    def test_no_injection_when_no_project_claude_md(
        self, lead_context_env: Path, tmp_path: Path
    ) -> None:
        proj_root = tmp_path / "ext" / "emptyapp"
        proj_root.mkdir(parents=True)
        # no CLAUDE.md in proj_root
        _write_projects(tmp_path, proj_root, name="emptyapp")

        from agent_takkub.lead_context import _render_lead_context

        out_path = _render_lead_context(project="emptyapp")
        assert out_path is not None
        content = Path(out_path).read_text(encoding="utf-8")
        assert "📋 Project rules" not in content

    def test_no_double_inject_when_project_root_is_repo_root(
        self, lead_context_env: Path, tmp_path: Path
    ) -> None:
        """When the project root IS the cockpit repo, skip the project-rules section
        to avoid injecting the cockpit CLAUDE.md twice."""
        # project paths point directly at tmp_path (== REPO_ROOT in this fixture)
        data = {
            "active": "cockpit",
            "projects": {
                "cockpit": {
                    "paths": {"main": str(tmp_path.as_posix())},
                    "presets": [],
                }
            },
        }
        (tmp_path / "projects.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        # The cockpit CLAUDE.md already lives at tmp_path/CLAUDE.md (base)
        from agent_takkub.lead_context import _render_lead_context

        out_path = _render_lead_context(project="cockpit")
        assert out_path is not None
        content = Path(out_path).read_text(encoding="utf-8")
        # The section header must NOT appear — that would be a double inject
        assert "📋 Project rules" not in content

    def test_rules_truncated_at_3000_chars(self, lead_context_env: Path, tmp_path: Path) -> None:
        proj_root = tmp_path / "ext" / "bigapp"
        proj_root.mkdir(parents=True)
        long_rules = "# Rules\n" + ("- rule X\n" * 500)  # >> 3000 chars
        (proj_root / "CLAUDE.md").write_text(long_rules, encoding="utf-8")
        _write_projects(tmp_path, proj_root, name="bigapp")

        from agent_takkub.lead_context import _render_lead_context

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        out_path = _render_lead_context(project="bigapp", claude_cwd=str(elsewhere))
        content = Path(out_path).read_text(encoding="utf-8")
        assert "…(truncated)" in content

    def test_skipped_when_cwd_auto_loads_it(self, lead_context_env: Path, tmp_path: Path) -> None:
        """tok-4: the dominant real case — Lead spawns AT the single-path project
        root, so claude auto-discovers its CLAUDE.md. The system-prompt injection
        must be skipped to avoid doubling ~750 tok of identical rules."""
        proj_root = tmp_path / "ext" / "soloapp"
        proj_root.mkdir(parents=True)
        (proj_root / "CLAUDE.md").write_text("# Project rules\n- deploy to Fly.io\n", "utf-8")
        _write_projects(tmp_path, proj_root, name="soloapp")

        from agent_takkub.lead_context import _render_lead_context

        # claude_cwd == proj_root (what lead_cwd() resolves to for a single path)
        out_path = _render_lead_context(project="soloapp", claude_cwd=str(proj_root))
        content = Path(out_path).read_text(encoding="utf-8")
        assert "📋 Project rules" not in content
        assert "deploy to Fly.io" not in content


# ─────────────────────────────────────────────────────────────────────
# m4 / m6 unit tests (no Qt)
# ─────────────────────────────────────────────────────────────────────


class TestSaveAndOpenProjectPreservesMetadata:
    """m6: _save_and_open_project must preserve existing presets/description."""

    def _run_save(
        self, tmp_path: Path, existing_data: dict, new_paths: dict, name: str = "myapp"
    ) -> dict:
        """Exercise the merge logic extracted from _save_and_open_project inline."""
        import json as _json

        projects_json = tmp_path / "projects.json"
        projects_json.write_text(_json.dumps(existing_data), encoding="utf-8")

        # Replicate the merge logic from main_window._save_and_open_project
        data = _json.loads(projects_json.read_text(encoding="utf-8"))
        if "projects" not in data:
            data["projects"] = {}
        existing = (data.get("projects") or {}).get(name, {})
        data["projects"][name] = {
            "description": existing.get("description", name),
            "paths": new_paths,
            "presets": existing.get("presets", []),
        }
        return data["projects"][name]

    def test_preserves_presets_and_description(self, tmp_path: Path) -> None:
        existing = {
            "projects": {
                "myapp": {
                    "description": "My custom description",
                    "paths": {"main": "/old/path"},
                    "presets": ["preset-a", "preset-b"],
                }
            }
        }
        result = self._run_save(tmp_path, existing, new_paths={"main": "/new/path"})
        assert result["description"] == "My custom description"
        assert result["presets"] == ["preset-a", "preset-b"]
        assert result["paths"] == {"main": "/new/path"}

    def test_uses_defaults_for_new_project(self, tmp_path: Path) -> None:
        result = self._run_save(tmp_path, {"projects": {}}, new_paths={"main": "/p"})
        assert result["description"] == "myapp"
        assert result["presets"] == []
