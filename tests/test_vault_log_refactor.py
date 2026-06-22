"""Phase A+B regression tests: vault-knowledge-refactor storage + distill changes.

Covers:
  - Session logs written to 99-Logs/sessions/<project>/ (not 01-Projects)
  - No false wikilink backlink in log body (project stays in frontmatter only)
  - Prune: retention + keep-last-5 for sessions; 30d for briefs
  - Junk dedup: identical first-line suppressed within a session
  - Obsidian graph filter written to .obsidian/graph.json
  - Phase B: distill durable facts into 01-Projects/<project>.md + MOC scaffolding
"""

from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.vault_mirror import (
    _CURATED_SECTION,
    _DEDUP_HASHES,
    _GRAPH_FILTER,
    _MOC_TEMPLATES,
    _SESSION_KEEP_LAST,
    _SESSION_MAX_AGE_S,
    _append_decision_entry,
    _ensure_project_page,
    _is_dedup_note,
    _is_durable_fact,
    _moc_for_note,
    _render_decision_note,
    _scaffold_moc,
    distill_session_facts,
    prune_vault_logs,
    write_obsidian_graph_filter,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

TEST_PROJECT = "agent-takkub"


def _make_vault(tmp_path: pathlib.Path) -> pathlib.Path:
    vault = tmp_path / "vault"
    (vault / "01-Projects").mkdir(parents=True)
    return vault


def _make_orch(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    from agent_takkub.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    # Minimal attribute setup so _project_panes doesn't crash in end_session.
    orch._registry = MagicMock()
    orch._registry.panes_by_project = {}
    orch._pending_done_notices = {}
    monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
    return orch


# ---------------------------------------------------------------------------
# 1. Writer places files in 99-Logs/sessions/<project>/
# ---------------------------------------------------------------------------


class TestSessionWriterPath:
    def test_save_decision_note_writes_to_99_logs(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)
        # Clear dedup cache so this note is fresh.
        _DEDUP_HASHES.clear()

        Orchestrator._save_decision_note(
            project=TEST_PROJECT,
            role="backend",
            note="added /login endpoint with JWT authentication",
            now=datetime(2026, 6, 22, 10, 0, 0),
        )

        new_sessions = vault / "99-Logs" / "sessions" / TEST_PROJECT
        assert new_sessions.is_dir(), "99-Logs/sessions/<project>/ should be created"
        written = list(new_sessions.glob("*.md"))
        assert len(written) == 1

    def test_save_decision_note_does_not_write_to_old_path(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)
        _DEDUP_HASHES.clear()

        Orchestrator._save_decision_note(
            project=TEST_PROJECT,
            role="backend",
            note="prune old 01-Projects check — long enough note body here",
            now=datetime(2026, 6, 22, 11, 0, 0),
        )

        old_path = vault / "01-Projects" / TEST_PROJECT / "sessions"
        assert not old_path.exists(), "old 01-Projects/sessions/ must not be created"

    def test_end_session_vault_writes_to_99_logs(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)

        orch = Orchestrator.__new__(Orchestrator)

        orch._resolve_project = lambda p: TEST_PROJECT
        orch._project_panes = lambda p: {}
        orch.write_daily_digest = lambda p: None

        ok, _ = orch.end_session(project=TEST_PROJECT, note="end-session vault path test")
        assert ok is True

        new_sessions = vault / "99-Logs" / "sessions" / TEST_PROJECT
        assert new_sessions.is_dir()
        written = list(new_sessions.glob("*-lead.md"))
        assert len(written) == 1

        old_path = vault / "01-Projects" / TEST_PROJECT / "sessions"
        assert not old_path.exists()


# ---------------------------------------------------------------------------
# 2. No false wikilink backlink in log body
# ---------------------------------------------------------------------------


class TestNoFalseBacklink:
    def test_render_decision_note_has_no_wikilink(self) -> None:
        body = _render_decision_note(
            "agent-takkub", "backend", "added /login endpoint with JWT", datetime.now()
        )
        assert "[[01-Projects/" not in body, "log body must not contain wikilink backlink"

    def test_frontmatter_project_still_present(self) -> None:
        body = _render_decision_note(
            "myprojx", "frontend", "redesigned login page layout flow", datetime.now()
        )
        assert "project: myprojx" in body, "frontmatter project: key must remain for Dataview"

    def test_end_session_body_has_no_wikilink(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: None)

        orch = Orchestrator.__new__(Orchestrator)
        orch._resolve_project = lambda p: TEST_PROJECT
        orch._project_panes = lambda p: {}
        orch.write_daily_digest = lambda p: None

        ok, _ = orch.end_session(project=TEST_PROJECT, note="checking no backlink in lead body")
        assert ok is True

        today = datetime.now().strftime("%Y-%m-%d")
        local_dir = tmp_path / "runtime" / "sessions" / today / TEST_PROJECT
        files = list(local_dir.glob("lead-*.md"))
        assert files
        body = files[0].read_text(encoding="utf-8")
        assert "[[01-Projects/" not in body


# ---------------------------------------------------------------------------
# 3. Prune: retention + keep-last-5
# ---------------------------------------------------------------------------


class TestPruneVaultLogs:
    def _write_session_file(self, proj_dir: pathlib.Path, name: str, age_s: float) -> pathlib.Path:
        f = proj_dir / name
        f.write_text("dummy", encoding="utf-8")
        mtime = time.time() - age_s
        import os

        os.utime(f, (mtime, mtime))
        return f

    def test_prune_deletes_expired_sessions(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        proj_dir = vault / "99-Logs" / "sessions" / TEST_PROJECT
        proj_dir.mkdir(parents=True)

        old = self._write_session_file(proj_dir, "old.md", _SESSION_MAX_AGE_S + 1000)
        fresh = self._write_session_file(proj_dir, "fresh.md", 60)

        prune_vault_logs(vault)

        assert not old.exists(), "file older than 14d must be deleted"
        assert fresh.exists(), "fresh file must survive"

    def test_prune_keeps_last_5(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        proj_dir = vault / "99-Logs" / "sessions" / TEST_PROJECT
        proj_dir.mkdir(parents=True)

        files = []
        for i in range(8):
            # all within retention window but beyond last-5 cap
            age = 3600 * (8 - i)  # oldest first
            files.append(self._write_session_file(proj_dir, f"s{i:02d}.md", age))

        prune_vault_logs(vault)

        remaining = sorted(proj_dir.glob("*.md"), key=lambda p: p.name)
        assert len(remaining) == _SESSION_KEEP_LAST, (
            f"expected {_SESSION_KEEP_LAST} files, got {len(remaining)}"
        )
        # The 5 most recent (lowest age) should survive: s03..s07
        surviving_names = {p.name for p in remaining}
        for i in range(3, 8):
            assert f"s{i:02d}.md" in surviving_names, f"s{i:02d}.md should survive"

    def test_prune_deletes_old_briefs(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        briefs_dir = vault / "99-Logs" / "briefs"
        briefs_dir.mkdir(parents=True)
        BRIEF_MAX = 30 * 86400

        old_brief = self._write_session_file(briefs_dir, "old-brief.md", BRIEF_MAX + 1000)
        new_brief = self._write_session_file(briefs_dir, "new-brief.md", 3600)

        prune_vault_logs(vault)

        assert not old_brief.exists()
        assert new_brief.exists()

    def test_prune_no_crash_on_missing_dirs(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        # 99-Logs doesn't exist at all — must not raise
        prune_vault_logs(vault)


# ---------------------------------------------------------------------------
# 4. Junk dedup: identical first-line suppressed
# ---------------------------------------------------------------------------


class TestDedupNote:
    def setup_method(self) -> None:
        _DEDUP_HASHES.clear()

    def test_first_call_not_dedup(self) -> None:
        assert _is_dedup_note("proj", "backend", "added /login endpoint JWT auth") is False

    def test_second_identical_first_line_is_dedup(self) -> None:
        note = "added /login endpoint with JWT auth flow"
        _is_dedup_note("proj", "backend", note)  # prime
        assert _is_dedup_note("proj", "backend", note) is True

    def test_different_project_not_dedup(self) -> None:
        note = "added /login endpoint JWT tests and coverage"
        _is_dedup_note("proj-a", "backend", note)
        assert _is_dedup_note("proj-b", "backend", note) is False

    def test_different_role_not_dedup(self) -> None:
        note = "added /login endpoint frontend form submission"
        _is_dedup_note("proj", "backend", note)
        assert _is_dedup_note("proj", "frontend", note) is False

    def test_different_first_line_not_dedup(self) -> None:
        _is_dedup_note("proj", "backend", "added /login endpoint with auth")
        assert _is_dedup_note("proj", "backend", "added /logout endpoint cleanup") is False

    def test_dedup_suppresses_vault_write(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)
        _DEDUP_HASHES.clear()

        note = "added /login endpoint with JWT and refresh tokens"
        now = datetime(2026, 6, 22, 12, 0, 0)

        Orchestrator._save_decision_note("myproject", "backend", note, now=now)
        Orchestrator._save_decision_note("myproject", "backend", note, now=now)

        written = list((vault / "99-Logs" / "sessions" / "myproject").glob("*.md"))
        assert len(written) == 1, "duplicate note must not produce a second file"


# ---------------------------------------------------------------------------
# 5. Obsidian graph filter
# ---------------------------------------------------------------------------


class TestObsidianGraphFilter:
    def test_creates_graph_json_with_filter(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        result = write_obsidian_graph_filter(vault)
        assert result is True

        graph_path = vault / ".obsidian" / "graph.json"
        assert graph_path.is_file()
        config = json.loads(graph_path.read_text(encoding="utf-8"))
        assert _GRAPH_FILTER in config.get("search", "")

    def test_merges_with_existing_config(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        obsidian = vault / ".obsidian"
        obsidian.mkdir()
        existing = {"search": "-path:archived", "showArrow": True, "scale": 1.5}
        (obsidian / "graph.json").write_text(json.dumps(existing), encoding="utf-8")

        write_obsidian_graph_filter(vault)

        config = json.loads((obsidian / "graph.json").read_text(encoding="utf-8"))
        assert _GRAPH_FILTER in config["search"]
        assert "-path:archived" in config["search"], "existing filter must be preserved"
        assert config["showArrow"] is True, "other keys must be preserved"

    def test_idempotent_second_call(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        write_obsidian_graph_filter(vault)
        write_obsidian_graph_filter(vault)  # second call

        config = json.loads((vault / ".obsidian" / "graph.json").read_text(encoding="utf-8"))
        # Filter must appear exactly once, not duplicated.
        assert config["search"].count(_GRAPH_FILTER) == 1


# ---------------------------------------------------------------------------
# 6. Phase B — Distill layer
# ---------------------------------------------------------------------------

_DURABLE_NOTE = "fix: root cause was race condition in pane spawn event loop"
_ARCH_NOTE = "decision: use PaneRegistry dataclass — better testability than raw dicts"
_NOISE_NOTE = "all tests passed and build completed successfully"
_NOW = datetime(2026, 6, 22, 12, 0, 0)


class TestIsDurableFact:
    def test_bug_fix_note_is_durable(self) -> None:
        assert _is_durable_fact(_DURABLE_NOTE) is True

    def test_decision_note_is_durable(self) -> None:
        assert _is_durable_fact(_ARCH_NOTE) is True

    def test_pattern_note_is_durable(self) -> None:
        assert (
            _is_durable_fact("refactor: extract spawn engine into separate mixin pattern") is True
        )

    def test_noise_note_is_not_durable(self) -> None:
        assert _is_durable_fact(_NOISE_NOTE) is False

    def test_short_status_not_durable(self) -> None:
        assert _is_durable_fact("ok done appended") is False

    def test_thai_bug_signal_is_durable(self) -> None:
        assert _is_durable_fact("แก้ ปัญหา pane ไม่ close เมื่อ session จบ") is True


class TestMocForNote:
    def test_bug_note_routes_to_bug_moc(self) -> None:
        assert _moc_for_note(_DURABLE_NOTE) == "bug"

    def test_arch_note_routes_to_arch_moc(self) -> None:
        assert _moc_for_note(_ARCH_NOTE) == "arch"

    def test_noise_note_returns_none(self) -> None:
        assert _moc_for_note(_NOISE_NOTE) is None

    def test_refactor_routes_to_arch(self) -> None:
        assert _moc_for_note("refactor: extract spawn engine") == "arch"


class TestScaffoldMoc:
    def test_creates_moc_when_absent(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        moc_rel, _, content = _MOC_TEMPLATES["bug"]
        _scaffold_moc(vault, moc_rel, content)
        moc = vault / moc_rel
        assert moc.is_file()
        assert "Bug Patterns" in moc.read_text(encoding="utf-8")

    def test_does_not_overwrite_existing_moc(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        moc_rel, _, content = _MOC_TEMPLATES["arch"]
        moc = vault / moc_rel
        moc.parent.mkdir(parents=True, exist_ok=True)
        moc.write_text("# Custom Content\n", encoding="utf-8")
        _scaffold_moc(vault, moc_rel, content)
        assert moc.read_text(encoding="utf-8") == "# Custom Content\n"


class TestEnsureProjectPage:
    def test_creates_page_when_absent(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        page = _ensure_project_page(vault, "agent-takkub")
        assert page.is_file()
        text = page.read_text(encoding="utf-8")
        assert _CURATED_SECTION in text

    def test_returns_existing_page_unchanged(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        (vault / "01-Projects" / "agent-takkub.md").write_text(
            "# existing content\n", encoding="utf-8"
        )
        page = _ensure_project_page(vault, "agent-takkub")
        assert page.read_text(encoding="utf-8") == "# existing content\n"


class TestAppendDecisionEntry:
    def test_appends_to_existing_section(self, tmp_path: pathlib.Path) -> None:
        page = tmp_path / "project.md"
        page.write_text(
            f"# proj\n\n{_CURATED_SECTION}\n\nintro text here.\n",
            encoding="utf-8",
        )
        entry = "- `2026-06-22T12:00:00` **backend** — fix root cause"
        _append_decision_entry(page, entry)
        text = page.read_text(encoding="utf-8")
        assert entry in text

    def test_creates_section_when_missing(self, tmp_path: pathlib.Path) -> None:
        page = tmp_path / "project.md"
        page.write_text("# proj\n\nsome existing content.\n", encoding="utf-8")
        entry = "- `2026-06-22T12:00:00` **backend** — decision: approach A"
        _append_decision_entry(page, entry)
        text = page.read_text(encoding="utf-8")
        assert _CURATED_SECTION in text
        assert entry in text

    def test_idempotent_second_append(self, tmp_path: pathlib.Path) -> None:
        page = tmp_path / "project.md"
        page.write_text(f"# proj\n\n{_CURATED_SECTION}\n\n", encoding="utf-8")
        entry = "- `2026-06-22T12:00:00` **backend** — fix: duplicate entry test"
        _append_decision_entry(page, entry)
        _append_decision_entry(page, entry)  # second call
        text = page.read_text(encoding="utf-8")
        assert text.count(entry) == 1

    def test_inserts_before_subsequent_section(self, tmp_path: pathlib.Path) -> None:
        page = tmp_path / "project.md"
        page.write_text(
            f"# proj\n\n{_CURATED_SECTION}\n\n- existing entry\n\n## Other Section\n\ncontent\n",
            encoding="utf-8",
        )
        entry = "- `2026-06-22T12:00:00` **backend** — new entry"
        _append_decision_entry(page, entry)
        text = page.read_text(encoding="utf-8")
        decisions_idx = text.index(_CURATED_SECTION)
        other_idx = text.index("## Other Section")
        entry_idx = text.index(entry)
        assert decisions_idx < entry_idx < other_idx


class TestDistillSessionFacts:
    def test_durable_note_written_to_project_page(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        result = distill_session_facts(TEST_PROJECT, "backend", _DURABLE_NOTE, vault, now=_NOW)
        assert result is True
        page = vault / "01-Projects" / f"{TEST_PROJECT}.md"
        assert page.is_file()
        text = page.read_text(encoding="utf-8")
        assert "backend" in text
        assert "root cause" in text

    def test_noise_note_not_written(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        result = distill_session_facts(TEST_PROJECT, "backend", _NOISE_NOTE, vault, now=_NOW)
        assert result is False
        page = vault / "01-Projects" / f"{TEST_PROJECT}.md"
        assert not page.is_file()

    def test_bug_note_scaffolds_moc(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        distill_session_facts(TEST_PROJECT, "backend", _DURABLE_NOTE, vault, now=_NOW)
        moc = vault / "02-Areas" / "bug-patterns.md"
        assert moc.is_file()

    def test_arch_note_scaffolds_arch_moc(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        distill_session_facts(TEST_PROJECT, "backend", _ARCH_NOTE, vault, now=_NOW)
        moc = vault / "02-Areas" / "architecture-decisions.md"
        assert moc.is_file()

    def test_entry_contains_moc_wikilink(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        distill_session_facts(TEST_PROJECT, "backend", _DURABLE_NOTE, vault, now=_NOW)
        page = vault / "01-Projects" / f"{TEST_PROJECT}.md"
        text = page.read_text(encoding="utf-8")
        assert "[[02-Areas/bug-patterns|bug-patterns]]" in text

    def test_idempotent_same_note_twice(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        distill_session_facts(TEST_PROJECT, "backend", _DURABLE_NOTE, vault, now=_NOW)
        distill_session_facts(TEST_PROJECT, "backend", _DURABLE_NOTE, vault, now=_NOW)
        page = vault / "01-Projects" / f"{TEST_PROJECT}.md"
        text = page.read_text(encoding="utf-8")
        # entry line should appear exactly once
        entry_lines = [
            line for line in text.splitlines() if "root cause" in line and line.startswith("- ")
        ]
        assert len(entry_lines) == 1

    def test_entry_has_iso_timestamp_and_role(self, tmp_path: pathlib.Path) -> None:
        vault = _make_vault(tmp_path)
        distill_session_facts(TEST_PROJECT, "qa", _DURABLE_NOTE, vault, now=_NOW)
        page = vault / "01-Projects" / f"{TEST_PROJECT}.md"
        text = page.read_text(encoding="utf-8")
        assert "`2026-06-22T12:00:00`" in text
        assert "**qa**" in text

    def test_error_does_not_raise(self, tmp_path: pathlib.Path) -> None:
        # Pass a non-existent vault (01-Projects won't exist) with an unwritable path
        vault = tmp_path / "no-vault"  # 01-Projects missing → _ensure_project_page can still mkdir
        # Corrupt the vault path to a file to force an OSError
        vault.write_text("not a dir", encoding="utf-8")
        result = distill_session_facts(TEST_PROJECT, "backend", _DURABLE_NOTE, vault, now=_NOW)
        assert result is False  # must not raise

    def test_orchestrator_save_decision_note_triggers_distill(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        _DEDUP_HASHES.clear()
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)

        Orchestrator._save_decision_note(
            project=TEST_PROJECT,
            role="backend",
            note=_DURABLE_NOTE,
            now=datetime(2026, 6, 22, 10, 0, 0),
        )

        page = vault / "01-Projects" / f"{TEST_PROJECT}.md"
        assert page.is_file(), "project page must be created by distill"
        assert "backend" in page.read_text(encoding="utf-8")

    def test_orchestrator_end_session_triggers_distill(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)

        orch = Orchestrator.__new__(Orchestrator)
        orch._resolve_project = lambda p: TEST_PROJECT
        orch._project_panes = lambda p: {}
        orch.write_daily_digest = lambda p: None

        ok, _ = orch.end_session(project=TEST_PROJECT, note=_DURABLE_NOTE)
        assert ok is True

        page = vault / "01-Projects" / f"{TEST_PROJECT}.md"
        assert page.is_file(), "end_session must trigger distill for durable note"
