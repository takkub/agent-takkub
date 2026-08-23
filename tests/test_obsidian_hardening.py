"""Integration tests for issue #365 phase 8 (Obsidian hardening): canonical
metadata wired into the actual vault writers, persistent dedup on distill,
and `doctor --obsidian`."""

from __future__ import annotations

import pathlib
from datetime import datetime

import pytest

from agent_takkub import config, doctor, obsidian_dedup
from agent_takkub.vault_graph import _parse_frontmatter
from agent_takkub.vault_mirror import (
    _MOC_TEMPLATES,
    _append_decision_entry,
    _ensure_project_page,
    _render_decision_note,
    _scaffold_moc,
    distill_session_facts,
)

_NOW = datetime(2026, 8, 23, 12, 0, 0)


@pytest.fixture(autouse=True)
def _v1_registry(tmp_path, monkeypatch):
    """Every writer under test resolves project_id through
    `config.load_projects()` — give it a real (empty) V1 file so
    resolution falls through to the deterministic slug fallback without
    touching this checkout's real projects.json."""
    pj = tmp_path / "projects.json"
    pj.write_text('{"active": null, "projects": {}}', encoding="utf-8")
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    monkeypatch.delenv("TAKKUB_V2_AUTHORITY", raising=False)


def _make_vault(tmp_path: pathlib.Path) -> pathlib.Path:
    vault = tmp_path / "vault"
    (vault / "01-Projects").mkdir(parents=True)
    return vault


class TestRenderDecisionNoteCanonicalMetadata:
    def test_frontmatter_has_all_canonical_fields(self):
        body = _render_decision_note("demo-project", "backend", "fixed a bug", _NOW)
        for field in ("knowledge_id", "project_id", "source", "kind", "trust", "content_hash"):
            assert f"{field}:" in body
        assert "source: takkub-done" in body
        assert "kind: session" in body
        assert "trust: auto" in body
        assert "project_id: demo-project" in body

    def test_still_parseable_by_vault_graph(self):
        """Canonical metadata must not break the existing frontmatter
        parser vault_graph.py relies on for role/project/date."""
        body = _render_decision_note("demo-project", "backend", "fixed a bug", _NOW)
        fm, rest = _parse_frontmatter(body)
        assert fm["role"] == "backend"
        assert fm["project"] == "demo-project"
        assert fm["knowledge_id"]
        assert "## Note" in rest

    def test_knowledge_id_stable_for_same_inputs(self):
        a = _render_decision_note("demo-project", "backend", "same note text here", _NOW)
        b = _render_decision_note("demo-project", "backend", "same note text here", _NOW)
        fm_a, _ = _parse_frontmatter(a)
        fm_b, _ = _parse_frontmatter(b)
        assert fm_a["knowledge_id"] == fm_b["knowledge_id"]


class TestEnsureProjectPageCanonicalMetadata:
    def test_new_page_has_frontmatter(self, tmp_path):
        vault = _make_vault(tmp_path)
        page = _ensure_project_page(vault, "demo-project", now=_NOW)
        text = page.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "project_id: demo-project" in text
        assert "kind: project" in text
        assert "trust: distilled" in text

    def test_existing_page_untouched(self, tmp_path):
        vault = _make_vault(tmp_path)
        page_path = vault / "01-Projects" / "demo-project.md"
        page_path.write_text("# legacy page, no frontmatter\n", encoding="utf-8")
        page = _ensure_project_page(vault, "demo-project", now=_NOW)
        assert page.read_text(encoding="utf-8") == "# legacy page, no frontmatter\n"


class TestScaffoldMocCanonicalMetadata:
    def test_moc_stub_has_frontmatter(self, tmp_path):
        vault = _make_vault(tmp_path)
        moc_rel, _, content = _MOC_TEMPLATES["bug"]
        _scaffold_moc(vault, moc_rel, content, now=_NOW)
        text = (vault / moc_rel).read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "kind: moc" in text
        assert "Bug Patterns" in text


class TestAppendDecisionEntryReturnsWroteFlag:
    def test_new_entry_returns_true(self, tmp_path):
        page = tmp_path / "project.md"
        page.write_text("# proj\n\n## Decisions & Learnings\n\n", encoding="utf-8")
        assert _append_decision_entry(page, "- new entry", now=_NOW) is True

    def test_duplicate_entry_returns_false(self, tmp_path):
        page = tmp_path / "project.md"
        page.write_text("# proj\n\n## Decisions & Learnings\n\n", encoding="utf-8")
        entry = "- `2026-06-22T12:00:00` **backend** — fix: dup"
        _append_decision_entry(page, entry, now=_NOW)
        assert _append_decision_entry(page, entry, now=_NOW) is False

    def test_updates_frontmatter_updated_at_on_real_write(self, tmp_path):
        page = tmp_path / "project.md"
        page.write_text(
            "---\nproject_id: demo\nupdated_at: 2000-01-01T00:00:00\n---\n\n"
            "## Decisions & Learnings\n\n",
            encoding="utf-8",
        )
        _append_decision_entry(page, "- fresh entry", now=_NOW)
        text = page.read_text(encoding="utf-8")
        assert "updated_at: 2026-08-23T12:00:00" in text
        assert "updated_at: 2000-01-01T00:00:00" not in text


class TestDistillSessionFactsDedupIndex:
    def test_new_durable_fact_recorded_in_dedup_index(self, tmp_path):
        vault = _make_vault(tmp_path)
        distill_session_facts(
            "demo-project", "backend", "root cause: bad config default", vault, now=_NOW
        )
        page = vault / "01-Projects" / "demo-project.md"
        text = page.read_text(encoding="utf-8")
        assert "<!-- kid:" in text

        records = obsidian_dedup.all_records()
        assert any(r["project_id"] == "demo-project" and r["path"] == str(page) for r in records)

    def test_repeat_call_does_not_duplicate_dedup_entry(self, tmp_path):
        vault = _make_vault(tmp_path)
        note = "root cause: identical repeated note text"
        distill_session_facts("demo-project", "backend", note, vault, now=_NOW)
        before = len(obsidian_dedup.all_records())
        distill_session_facts("demo-project", "backend", note, vault, now=_NOW)
        after = len(obsidian_dedup.all_records())
        assert after == before


class TestDoctorCheckObsidian:
    def test_no_vault_configured_is_info(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TAKKUB_VAULT_DIR", str(tmp_path / "no-such-vault"))
        findings = doctor.check_obsidian()
        assert any(f.name == "vault" and f.status == doctor.Status.INFO for f in findings)

    def test_reports_ok_when_all_notes_canonical(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path)
        monkeypatch.setenv("TAKKUB_VAULT_DIR", str(vault))
        _ensure_project_page(vault, "demo-project", now=_NOW)

        findings = doctor.check_obsidian()
        by_name = {f.name: f for f in findings}
        assert by_name["vault"].status == doctor.Status.OK
        assert by_name["canonical-metadata"].status == doctor.Status.OK
        assert "dedup-index" in by_name
        assert "boundary" in by_name

    def test_warns_on_legacy_note_missing_metadata(self, tmp_path, monkeypatch):
        vault = _make_vault(tmp_path)
        monkeypatch.setenv("TAKKUB_VAULT_DIR", str(vault))
        (vault / "01-Projects" / "legacy.md").write_text(
            "# legacy page, no frontmatter\n", encoding="utf-8"
        )

        findings = doctor.check_obsidian()
        by_name = {f.name: f for f in findings}
        assert by_name["canonical-metadata"].status == doctor.Status.WARN
        assert "1/1" in by_name["canonical-metadata"].detail
