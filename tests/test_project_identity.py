"""`project_identity.resolve_project_id` — the single project_id resolver
for Obsidian hardening (#365 phase 8, master plan §4 fix 3)."""

from __future__ import annotations

import json

import pytest

from agent_takkub import config, project_identity


def _write_v1_projects(tmp_path, monkeypatch, projects: dict) -> None:
    pj = tmp_path / "projects.json"
    pj.write_text(json.dumps({"active": None, "projects": projects}), encoding="utf-8")
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    # v2 authority off so load_projects() falls back to the V1 file above.
    monkeypatch.delenv("TAKKUB_V2_AUTHORITY", raising=False)


class TestResolveFromV1Fallback:
    def test_exact_match_returns_registered_key(self, tmp_path, monkeypatch):
        _write_v1_projects(tmp_path, monkeypatch, {"agent-takkub": {"paths": {}}})
        assert project_identity.resolve_project_id("agent-takkub") == "agent-takkub"

    def test_case_insensitive_match_returns_registered_key(self, tmp_path, monkeypatch):
        _write_v1_projects(tmp_path, monkeypatch, {"Agent-Takkub": {"paths": {}}})
        assert project_identity.resolve_project_id("agent-takkub") == "Agent-Takkub"

    def test_unregistered_project_falls_back_to_normalised_slug(self, tmp_path, monkeypatch):
        _write_v1_projects(tmp_path, monkeypatch, {"other-project": {"paths": {}}})
        assert project_identity.resolve_project_id("brand-new") == "brand-new"

    def test_missing_projects_json_falls_back_to_normalised_slug(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PROJECTS_JSON", tmp_path / "nope.json")
        monkeypatch.delenv("TAKKUB_V2_AUTHORITY", raising=False)
        assert project_identity.resolve_project_id("solo-project") == "solo-project"

    def test_invalid_name_raises(self, tmp_path, monkeypatch):
        _write_v1_projects(tmp_path, monkeypatch, {})
        with pytest.raises(ValueError):
            project_identity.resolve_project_id("../etc")


class TestResolveFromV2Registry:
    def test_reads_through_load_projects_single_reader(self, monkeypatch):
        """`resolve_project_id` must not re-derive identity on its own — it
        delegates to `config.load_projects()`, the one registry reader
        already wired to prefer the V2 mirror under `TAKKUB_V2_AUTHORITY`
        (#362 piece 1). Monkeypatching that one function stands in for a
        migrated, V2-authoritative machine without re-testing v2_authority
        itself."""
        monkeypatch.setattr(
            config,
            "load_projects",
            lambda: {"active": None, "projects": {"v2-project": {"paths": {}}}},
        )
        assert project_identity.resolve_project_id("v2-project") == "v2-project"
        assert project_identity.resolve_project_id("V2-Project") == "v2-project"
