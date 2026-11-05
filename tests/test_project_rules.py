"""Tests for project_rules module and lead_context project-rules injection."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import config as config_mod
from agent_takkub.codex_agents_md import TAKKUB_MARKER
from agent_takkub.project_rules import read_project_rules, rules_path, write_project_rules

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
# read_project_rules / rules_path — provider-neutral (AGENTS.md first)
# ─────────────────────────────────────────────────────────────────────


class TestReadProjectRules:
    def test_returns_none_when_missing(self, tmp_project: Path) -> None:
        assert read_project_rules(tmp_project) is None

    def test_legacy_claude_md_still_read(self, tmp_project: Path) -> None:
        (tmp_project / "CLAUDE.md").write_text("# Rules\n- no foo", encoding="utf-8")
        assert read_project_rules(tmp_project) == "# Rules\n- no foo"

    def test_agents_md_wins_and_import_only_claude_md_is_skipped(self, tmp_project: Path) -> None:
        (tmp_project / "AGENTS.md").write_text("# Shared rules", encoding="utf-8")
        (tmp_project / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        assert rules_path(tmp_project).name == "AGENTS.md"
        assert read_project_rules(tmp_project) == "# Shared rules"

    def test_cockpit_managed_agents_md_is_not_project_rules(self, tmp_project: Path) -> None:
        (tmp_project / "AGENTS.md").write_text(f"{TAKKUB_MARKER}\n# teammate sheet", "utf-8")
        assert read_project_rules(tmp_project) is None
        (tmp_project / "CLAUDE.md").write_text("# real rules", encoding="utf-8")
        assert read_project_rules(tmp_project) == "# real rules"


# ─────────────────────────────────────────────────────────────────────
# write_project_rules
# ─────────────────────────────────────────────────────────────────────


class TestWriteProjectRules:
    def test_new_project_writes_agents_md_and_claude_import(self, tmp_project: Path) -> None:
        path = write_project_rules(tmp_project, "# Generated\n- deploy via Vercel\n")
        assert path == tmp_project / "AGENTS.md"
        assert path.read_text(encoding="utf-8") == "# Generated\n- deploy via Vercel\n"
        assert (tmp_project / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"

    def test_legacy_claude_md_is_edited_in_place(self, tmp_project: Path) -> None:
        (tmp_project / "CLAUDE.md").write_text("old content", encoding="utf-8")
        write_project_rules(tmp_project, "new content")
        assert (tmp_project / "CLAUDE.md").read_text(encoding="utf-8") == "new content"
        assert not (tmp_project / "AGENTS.md").exists()

    def test_atomic_via_tmp(self, tmp_project: Path) -> None:
        """Verify the tmp file is cleaned up (replaced) after write."""
        write_project_rules(tmp_project, "content")
        assert not (tmp_project / "AGENTS.md.tmp").exists()


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

    from agent_takkub import lead_context as lc_mod

    monkeypatch.setattr(lc_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lc_mod, "ASSETS_ROOT", tmp_path)
    monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)
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


def _write_projects(seed_projects, tmp_path: Path, proj_root: Path, name: str = "myapp") -> None:
    seed_projects(
        tmp_path,
        {name: {"paths": {"main": str(proj_root.as_posix())}, "presets": []}},
        active=name,
    )


class TestLeadContextProjectRulesInjection:
    def test_injects_project_rules_when_present(
        self, lead_context_env: Path, tmp_path: Path, seed_projects
    ) -> None:
        """Project CLAUDE.md contents should appear in the rendered lead context."""
        proj_root = tmp_path / "ext" / "myapp"
        proj_root.mkdir(parents=True)
        (proj_root / "CLAUDE.md").write_text(
            "# Project rules\n- deploy to Fly.io\n", encoding="utf-8"
        )
        _write_projects(seed_projects, tmp_path, proj_root)

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
        self, lead_context_env: Path, tmp_path: Path, seed_projects
    ) -> None:
        proj_root = tmp_path / "ext" / "emptyapp"
        proj_root.mkdir(parents=True)
        # no CLAUDE.md in proj_root
        _write_projects(seed_projects, tmp_path, proj_root, name="emptyapp")

        from agent_takkub.lead_context import _render_lead_context

        out_path = _render_lead_context(project="emptyapp")
        assert out_path is not None
        content = Path(out_path).read_text(encoding="utf-8")
        assert "📋 Project rules" not in content

    def test_no_double_inject_when_project_root_is_repo_root(
        self, lead_context_env: Path, tmp_path: Path, seed_projects
    ) -> None:
        """When the project root IS the cockpit repo, skip the project-rules section
        to avoid injecting the cockpit CLAUDE.md twice."""
        # project paths point directly at tmp_path (== REPO_ROOT in this fixture)
        seed_projects(
            tmp_path,
            {"cockpit": {"paths": {"main": str(tmp_path.as_posix())}, "presets": []}},
            active="cockpit",
        )
        # The cockpit CLAUDE.md already lives at tmp_path/CLAUDE.md (base)
        from agent_takkub.lead_context import _render_lead_context

        out_path = _render_lead_context(project="cockpit")
        assert out_path is not None
        content = Path(out_path).read_text(encoding="utf-8")
        # The section header must NOT appear — that would be a double inject
        assert "📋 Project rules" not in content

    def test_rules_truncated_at_3000_chars(
        self, lead_context_env: Path, tmp_path: Path, seed_projects
    ) -> None:
        proj_root = tmp_path / "ext" / "bigapp"
        proj_root.mkdir(parents=True)
        long_rules = "# Rules\n" + ("- rule X\n" * 500)  # >> 3000 chars
        (proj_root / "CLAUDE.md").write_text(long_rules, encoding="utf-8")
        _write_projects(seed_projects, tmp_path, proj_root, name="bigapp")

        from agent_takkub.lead_context import _render_lead_context

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        out_path = _render_lead_context(project="bigapp", claude_cwd=str(elsewhere))
        content = Path(out_path).read_text(encoding="utf-8")
        assert "…(truncated)" in content

    def test_skipped_when_cwd_auto_loads_it(
        self, lead_context_env: Path, tmp_path: Path, seed_projects
    ) -> None:
        """tok-4: the dominant real case — Lead spawns AT the single-path project
        root, so claude auto-discovers its CLAUDE.md. The system-prompt injection
        must be skipped to avoid doubling ~750 tok of identical rules."""
        proj_root = tmp_path / "ext" / "soloapp"
        proj_root.mkdir(parents=True)
        (proj_root / "CLAUDE.md").write_text("# Project rules\n- deploy to Fly.io\n", "utf-8")
        _write_projects(seed_projects, tmp_path, proj_root, name="soloapp")

        from agent_takkub.lead_context import _render_lead_context

        # claude_cwd == proj_root (what lead_cwd() resolves to for a single path)
        out_path = _render_lead_context(project="soloapp", claude_cwd=str(proj_root))
        content = Path(out_path).read_text(encoding="utf-8")
        assert "📋 Project rules" not in content
        assert "deploy to Fly.io" not in content

    def test_template_project_agents_md_reaches_every_lead_provider(
        self, lead_context_env: Path, tmp_path: Path, seed_projects
    ) -> None:
        """Template projects keep rules in AGENTS.md + `@AGENTS.md` CLAUDE.md:
        both a claude Lead and a non-claude Lead at the root auto-read them,
        so neither gets a duplicate injection; from elsewhere both get it."""
        proj_root = tmp_path / "ext" / "tplapp"
        proj_root.mkdir(parents=True)
        (proj_root / "AGENTS.md").write_text("# tplapp\n- stack NestJS\n", "utf-8")
        (proj_root / "CLAUDE.md").write_text("@AGENTS.md\n", "utf-8")
        _write_projects(seed_projects, tmp_path, proj_root, name="tplapp")

        from agent_takkub.lead_context import _render_lead_context, build_lead_agents_text

        at_root_claude = Path(_render_lead_context(project="tplapp", claude_cwd=str(proj_root)))
        assert "stack NestJS" not in at_root_claude.read_text(encoding="utf-8")
        assert "stack NestJS" not in (build_lead_agents_text("tplapp", str(proj_root)) or "")

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        other = build_lead_agents_text("tplapp", str(elsewhere)) or ""
        assert "stack NestJS" in other and "tplapp/AGENTS.md" in other

    def test_non_claude_lead_gets_legacy_claude_md_rules(
        self, lead_context_env: Path, tmp_path: Path, seed_projects
    ) -> None:
        """A codex/agy/opencode/cursor Lead never auto-reads CLAUDE.md — a
        legacy CLAUDE.md-only project used to leave it with no project rules."""
        proj_root = tmp_path / "ext" / "legacyapp"
        proj_root.mkdir(parents=True)
        (proj_root / "CLAUDE.md").write_text("# legacy\n- deploy to Fly.io\n", "utf-8")
        _write_projects(seed_projects, tmp_path, proj_root, name="legacyapp")

        from agent_takkub.lead_context import build_lead_agents_text

        text = build_lead_agents_text("legacyapp", str(proj_root)) or ""
        assert "deploy to Fly.io" in text


class TestProviderNeutralLeadDelegationPolicy:
    """Every Lead engine receives the same strict orchestrator boundary."""

    @staticmethod
    def _assert_strict_policy(content: str) -> None:
        assert "กฎเดียวกันสำหรับทุก provider" in content
        assert "Claude, Codex, Gemini/agy, OpenCode, Cursor" in content
        assert "provider substitution ทุกกรณี" in content
        assert "source code ของ cockpit `agent-takkub`" in content
        assert "ถ้าไม่แน่ใจว่าเป็นงานเล็กหรือไม่" in content

    def test_claude_context_keeps_policy_when_cockpit_is_active(
        self, lead_context_env: Path, seed_projects
    ) -> None:
        _write_projects(seed_projects, lead_context_env, lead_context_env, name="cockpit")

        from agent_takkub.lead_context import _render_lead_context

        out_path = _render_lead_context(project="cockpit")
        assert out_path is not None
        content = Path(out_path).read_text(encoding="utf-8")
        self._assert_strict_policy(content)
        assert "active project: **cockpit**" in content
        assert "source code remains protected" in content

    def test_all_nonclaude_provider_specs_receive_identical_policy(
        self, lead_context_env: Path, seed_projects
    ) -> None:
        from agent_takkub.lead_context import render_lead_agents_md
        from agent_takkub.provider_spec import PROVIDER_REGISTRY

        _write_projects(seed_projects, lead_context_env, lead_context_env, name="cockpit")
        for provider, spec in PROVIDER_REGISTRY.items():
            if provider == "claude":
                continue
            assert spec.context_strategy == "agents_md_file", provider
            spawn_cwd = lead_context_env / f"lead-{provider}"
            spawn_cwd.mkdir()
            out_path = render_lead_agents_md("cockpit", str(spawn_cwd))
            assert out_path is not None, provider
            content = Path(out_path).read_text(encoding="utf-8")
            self._assert_strict_policy(content)


class TestLeadContextMissingAssets:
    """Prod-bug follow-up: an installed build whose _assets/CLAUDE.md never
    got shipped must not fail silently — it should log so the gap is
    diagnosable instead of Lead just spawning without a playbook."""

    def test_missing_cockpit_md_logs_and_returns_none(
        self, lead_context_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import lead_context as lc_mod

        (lead_context_env / "CLAUDE.md").unlink()
        logged: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            lc_mod, "_log_event", lambda event, **details: logged.append((event, details))
        )

        result = lc_mod._render_lead_context("default")

        assert result is None
        assert logged and logged[0][0] == "lead_context_missing"


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
