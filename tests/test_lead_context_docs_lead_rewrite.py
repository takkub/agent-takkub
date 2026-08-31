"""Tests for lead_context.py's installed-mode `docs/lead/` reference rewrite.

CLAUDE.md cross-references `docs/lead/patterns.md` and
`docs/lead/cli-reference.md` by relative path. That only resolves from a dev
checkout's cwd — an installed build (ASSETS_ROOT != REPO_ROOT, see
config.py's `_resolve_assets_root`) ships those files at
`ASSETS_ROOT/docs/lead/` instead (setup.py's `_stage_assets`), so
`_build_lead_context_text` must rewrite the reference onto that absolute
path. A dev checkout (ASSETS_ROOT == REPO_ROOT) must be byte-identical to
before this rewrite existed.
"""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub import config as config_mod


@pytest.fixture
def dev_mode_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """ASSETS_ROOT == REPO_ROOT (dev checkout) — rewrite must be a no-op."""
    cockpit_md = tmp_path / "CLAUDE.md"
    cockpit_md.write_text(
        "# Cockpit CLAUDE.md\nSee `docs/lead/patterns.md` and `docs/lead/cli-reference.md`.\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    from agent_takkub import lead_context as lc_mod

    monkeypatch.setattr(lc_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lc_mod, "ASSETS_ROOT", tmp_path)
    monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config_mod, "PROJECTS_JSON", tmp_path / "projects.json")
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(lc_mod, "_recent_session_brief", lambda _proj: None)
    try:
        from agent_takkub import provider_state as ps_mod

        monkeypatch.setattr(ps_mod, "all_disabled", lambda: set())
    except Exception:
        pass
    return tmp_path


@pytest.fixture
def installed_mode_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """ASSETS_ROOT != REPO_ROOT (installed build) with a staged docs/lead/
    file actually on disk, mirroring what setup.py's `_stage_assets` ships."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    assets_root = tmp_path / "site-packages" / "agent_takkub" / "_assets"
    assets_root.mkdir(parents=True)

    (assets_root / "CLAUDE.md").write_text(
        "# Cockpit CLAUDE.md\nSee `docs/lead/patterns.md` and `docs/lead/cli-reference.md`.\n",
        encoding="utf-8",
    )
    docs_lead = assets_root / "docs" / "lead"
    docs_lead.mkdir(parents=True)
    (docs_lead / "patterns.md").write_text("# patterns doc", encoding="utf-8")
    (docs_lead / "cli-reference.md").write_text("# cli reference doc", encoding="utf-8")

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    data_home = tmp_path / "data-home"
    data_home.mkdir()

    from agent_takkub import lead_context as lc_mod

    monkeypatch.setattr(lc_mod, "REPO_ROOT", repo_root)
    monkeypatch.setattr(lc_mod, "ASSETS_ROOT", assets_root)
    monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config_mod, "PROJECTS_JSON", data_home / "projects.json")
    monkeypatch.setattr(config_mod, "REPO_ROOT", repo_root)
    monkeypatch.setattr(config_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(lc_mod, "_recent_session_brief", lambda _proj: None)
    try:
        from agent_takkub import provider_state as ps_mod

        monkeypatch.setattr(ps_mod, "all_disabled", lambda: set())
    except Exception:
        pass
    return assets_root


class TestRewriteHelperIdempotent:
    """#447: the rewrite must not re-match its own output on repeated runs,
    and must self-heal text already corrupted by the old non-idempotent
    str.replace()."""

    def test_repeated_calls_converge_after_first(self) -> None:
        from agent_takkub.lead_context import _rewrite_docs_lead_refs

        docs_lead_dir = (
            "C:/Users/monch/.agent-takkub/venv/Lib/site-packages/agent_takkub/_assets/docs/lead"
        )
        text = "See `docs/lead/patterns.md` and `docs/lead/cli-reference.md`.\n"

        once = _rewrite_docs_lead_refs(text, docs_lead_dir)
        twice = _rewrite_docs_lead_refs(once, docs_lead_dir)
        thrice = _rewrite_docs_lead_refs(twice, docs_lead_dir)

        assert once == twice == thrice
        assert once.count(docs_lead_dir) == 2
        assert f"{docs_lead_dir}/patterns.md" in once
        assert f"{docs_lead_dir}/cli-reference.md" in once

    def test_repeated_calls_converge_posix_path(self) -> None:
        from agent_takkub.lead_context import _rewrite_docs_lead_refs

        docs_lead_dir = "/Users/someone/.agent-takkub/venv/lib/agent_takkub/_assets/docs/lead"
        text = "See `docs/lead/patterns.md`.\n"

        once = _rewrite_docs_lead_refs(text, docs_lead_dir)
        twice = _rewrite_docs_lead_refs(once, docs_lead_dir)

        assert once == twice
        assert once.count(docs_lead_dir) == 1

    def test_normalizes_already_corrupted_nested_prefix(self) -> None:
        """A file already on disk from a prior buggy install — the prefix
        nested 3x before a single trailing docs/lead/ — must collapse to one
        copy, not grow a 4th."""
        from agent_takkub.lead_context import _rewrite_docs_lead_refs

        docs_lead_dir = "C:/site-packages/agent_takkub/_assets/docs/lead"
        prefix = "C:/site-packages/agent_takkub/_assets/"
        corrupted = f"See `{prefix * 3}docs/lead/role-and-workflow.md`.\n"

        fixed = _rewrite_docs_lead_refs(corrupted, docs_lead_dir)

        assert fixed == f"See `{docs_lead_dir}/role-and-workflow.md`.\n"
        assert fixed.count(prefix) == 1
        # And re-running it again must be a true no-op (real idempotency).
        assert _rewrite_docs_lead_refs(fixed, docs_lead_dir) == fixed

    def test_docs_lead_inside_unrelated_absolute_path_untouched(self) -> None:
        """A `docs/lead/` substring that is part of some other absolute path
        (not our own staged-docs prefix) must be left alone — it's already
        preceded by a path character, so the guarded regex must not treat it
        as a relative reference to prepend onto."""
        from agent_takkub.lead_context import _rewrite_docs_lead_refs

        docs_lead_dir = "C:/site-packages/agent_takkub/_assets/docs/lead"
        text = "unrelated: /opt/other/docs/lead/notes.md stays put\n"

        fixed = _rewrite_docs_lead_refs(text, docs_lead_dir)

        assert fixed == text


class TestDevModeUnchanged:
    def test_docs_lead_reference_left_relative(self, dev_mode_env: pathlib.Path) -> None:
        from agent_takkub.lead_context import _build_lead_context_text

        text = _build_lead_context_text()
        assert text is not None
        assert "`docs/lead/patterns.md`" in text
        assert "`docs/lead/cli-reference.md`" in text


class TestInstalledModeRewrite:
    def test_docs_lead_reference_rewritten_to_absolute_staged_path(
        self, installed_mode_env: pathlib.Path
    ) -> None:
        from agent_takkub.lead_context import _build_lead_context_text

        text = _build_lead_context_text()
        assert text is not None
        assert "`docs/lead/patterns.md`" not in text
        assert "`docs/lead/cli-reference.md`" not in text

        docs_lead_dir = installed_mode_env / "docs" / "lead"
        rewritten_patterns = f"{docs_lead_dir.as_posix()}/patterns.md"
        rewritten_cli_ref = f"{docs_lead_dir.as_posix()}/cli-reference.md"
        assert rewritten_patterns in text
        assert rewritten_cli_ref in text

        # The rewritten reference actually resolves to a real, readable file
        # — proves this isn't just string surgery pointing at nothing.
        assert pathlib.Path(rewritten_patterns).is_file()
        assert pathlib.Path(rewritten_cli_ref).is_file()

    def test_three_spawns_produce_identical_text_and_staged_files(
        self, installed_mode_env: pathlib.Path
    ) -> None:
        """#447: simulate 3 Lead spawns in a row (each re-reads/rewrites the
        staged docs from disk, as a real upgrade-then-spawn cycle does) — the
        3rd spawn's rendered text and the staged files on disk must be
        identical to the 1st, not carry a growing nested prefix."""
        from agent_takkub.lead_context import _build_lead_context_text

        first = _build_lead_context_text()
        second = _build_lead_context_text()
        third = _build_lead_context_text()

        assert first == second == third

        docs_lead = installed_mode_env / "docs" / "lead"
        patterns_text = (docs_lead / "patterns.md").read_text(encoding="utf-8")
        assert patterns_text == "# patterns doc"

    def test_rendered_context_written_to_runtime_points_at_real_files(
        self, installed_mode_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import lead_context as lc_mod

        out_path = lc_mod._render_lead_context()
        assert out_path is not None
        text = pathlib.Path(out_path).read_text(encoding="utf-8")
        docs_lead_dir = installed_mode_env / "docs" / "lead"
        assert f"{docs_lead_dir.as_posix()}/patterns.md" in text
