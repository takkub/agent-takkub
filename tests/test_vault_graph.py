"""Tests for vault_graph — decision-note log analysis."""

from __future__ import annotations

import pathlib
import re
from datetime import date, datetime

import pytest

from agent_takkub.vault_graph import (
    GraphReport,
    SessionEntry,
    _analyse_blockers,
    _analyse_role_trend,
    _build_decision_chains,
    _extract_note_text,
    _parse_frontmatter,
    _parse_session_file,
    _render_report,
    analyse,
    load_sessions,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_session_md(
    role: str,
    project: str,
    iso: str,
    note: str,
) -> str:
    """Produce a session markdown matching _render_decision_note output."""
    return (
        f"---\n"
        f"role: {role}\n"
        f"project: {project}\n"
        f"date: {iso}\n"
        f"tags: [session, {role}, {project}]\n"
        f"---\n\n"
        f"# {role} done · {iso}\n\n"
        f"**Project:** [[01-Projects/{project}|{project}]]\n"
        f"**Role:** {role}\n\n"
        f"## Note\n\n{note.strip()}\n"
    )


def _sessions_dir(vault: pathlib.Path, project: str) -> pathlib.Path:
    d = vault / "01-Projects" / project / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_session(
    vault: pathlib.Path,
    project: str,
    role: str,
    iso: str,  # "YYYY-MM-DDTHH:MM:SS"
    note: str,
) -> pathlib.Path:
    ts_file = iso.replace(":", "").replace("-", "", 2)  # "2026-06-09T143022"
    filename = f"{ts_file}-{role}.md"
    content = _make_session_md(role, project, iso, note)
    path = _sessions_dir(vault, project) / filename
    path.write_text(content, encoding="utf-8")
    return path


# ── _parse_frontmatter ────────────────────────────────────────────────────────


class TestParseFrontmatter:
    def test_extracts_yaml_fields(self) -> None:
        text = "---\nrole: backend\nproject: foo\n---\n\nbody"
        fm, body = _parse_frontmatter(text)
        assert fm["role"] == "backend"
        assert fm["project"] == "foo"
        assert "body" in body

    def test_returns_empty_dict_on_missing_frontmatter(self) -> None:
        fm, body = _parse_frontmatter("# plain markdown\n\nsome text")
        assert fm == {}
        assert "plain markdown" in body

    def test_returns_empty_dict_on_yaml_error(self) -> None:
        fm, _body = _parse_frontmatter("---\n: bad: yaml: {{{\n---\nbody")
        assert isinstance(fm, dict)


# ── _extract_note_text ────────────────────────────────────────────────────────


class TestExtractNoteText:
    def test_returns_text_under_note_section(self) -> None:
        body = "## Note\n\nAdded /auth/login endpoint with JWT\n"
        assert _extract_note_text(body) == "Added /auth/login endpoint with JWT"

    def test_stops_at_next_section(self) -> None:
        body = "## Note\n\nFirst line\nSecond line\n\n## Transcript\n\nignore"
        result = _extract_note_text(body)
        assert "First line" in result
        assert "Transcript" not in result

    def test_returns_empty_when_no_note_section(self) -> None:
        assert _extract_note_text("# heading\n\nsome text") == ""

    def test_strips_surrounding_whitespace(self) -> None:
        body = "## Note\n\n  trimmed note  \n"
        assert _extract_note_text(body) == "trimmed note"


# ── _parse_session_file ───────────────────────────────────────────────────────


class TestParseSessionFile:
    def test_parses_well_formed_file(self, tmp_path: pathlib.Path) -> None:
        content = _make_session_md(
            "backend",
            "agent-takkub",
            "2026-06-09T14:30:22",
            "เพิ่ม POST /auth/login endpoint พร้อม JWT HS256 expiry 24h",
        )
        p = tmp_path / "2026-06-09T143022-backend.md"
        p.write_text(content, encoding="utf-8")

        entry = _parse_session_file(p)
        assert entry is not None
        assert entry.role == "backend"
        assert entry.project == "agent-takkub"
        assert entry.timestamp == datetime(2026, 6, 9, 14, 30, 22)
        assert "POST /auth/login" in entry.note

    def test_falls_back_to_filename_when_no_frontmatter(self, tmp_path: pathlib.Path) -> None:
        content = "## Note\n\nsome long enough note text here for parsing\n"
        p = tmp_path / "2026-06-09T150000-qa.md"
        p.write_text(content, encoding="utf-8")

        entry = _parse_session_file(p)
        assert entry is not None
        assert entry.role == "qa"
        assert entry.timestamp == datetime(2026, 6, 9, 15, 0, 0)

    def test_returns_none_for_unreadable_file(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / "2026-06-09T143022-backend.md"
        # Don't create the file
        assert _parse_session_file(p) is None

    def test_returns_none_for_unparseable_stem(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / "bad_filename.md"
        p.write_text("no frontmatter\n", encoding="utf-8")
        assert _parse_session_file(p) is None


# ── SessionEntry ──────────────────────────────────────────────────────────────


class TestSessionEntry:
    def test_is_blocker_detects_english_keywords(self) -> None:
        e = SessionEntry("qa", "proj", datetime.now(), "login flow blocked by CORS error", "f.md")
        assert e.is_blocker is True

    def test_is_blocker_detects_thai_keywords(self) -> None:
        e = SessionEntry("qa", "proj", datetime.now(), "ติดปัญหา CORS บน /auth/login", "f.md")
        assert e.is_blocker is True

    def test_is_blocker_false_for_normal_note(self) -> None:
        e = SessionEntry(
            "backend", "proj", datetime.now(), "เพิ่ม /auth/login endpoint เสร็จแล้ว", "f.md"
        )
        assert e.is_blocker is False

    def test_note_preview_truncates_long_notes(self) -> None:
        long_note = "A" * 100
        e = SessionEntry("backend", "proj", datetime.now(), long_note, "f.md")
        assert len(e.note_preview) <= 80
        assert e.note_preview.endswith("...")

    def test_note_preview_keeps_short_notes_intact(self) -> None:
        short = "short note"
        e = SessionEntry("backend", "proj", datetime.now(), short, "f.md")
        assert e.note_preview == short


# ── load_sessions ─────────────────────────────────────────────────────────────


class TestLoadSessions:
    def test_returns_empty_when_sessions_dir_missing(self, tmp_path: pathlib.Path) -> None:
        vault = tmp_path / "vault"
        (vault / "01-Projects").mkdir(parents=True)
        assert load_sessions("agent-takkub", vault) == []

    def test_loads_and_sorts_sessions_by_timestamp(self, tmp_path: pathlib.Path) -> None:
        vault = tmp_path / "vault"
        _write_session(
            vault, "proj", "backend", "2026-06-09T14:30:00", "เพิ่ม POST /auth/login endpoint สำเร็จ"
        )
        _write_session(
            vault, "proj", "qa", "2026-06-09T15:00:00", "smoke test /auth/login ผ่านหมด happy path"
        )
        _write_session(
            vault, "proj", "frontend", "2026-06-09T16:00:00", "login form wired to API token stored"
        )

        entries = load_sessions("proj", vault)
        assert len(entries) == 3
        assert entries[0].role == "backend"
        assert entries[1].role == "qa"
        assert entries[2].role == "frontend"

    def test_skips_unparseable_files(self, tmp_path: pathlib.Path) -> None:
        vault = tmp_path / "vault"
        sdir = _sessions_dir(vault, "proj")
        (sdir / "bad_file.md").write_text("garbage\n", encoding="utf-8")
        _write_session(
            vault, "proj", "backend", "2026-06-09T14:30:00", "added /auth/login endpoint with JWT"
        )

        entries = load_sessions("proj", vault)
        assert len(entries) == 1
        assert entries[0].role == "backend"


# ── analysis functions ────────────────────────────────────────────────────────


class TestAnalyseRoleTrend:
    def _make_entries(self) -> list[SessionEntry]:
        return [
            SessionEntry(
                "backend",
                "p",
                datetime(2026, 6, 9, 14, 0),
                "added /login endpoint and tests",
                "a.md",
            ),
            SessionEntry(
                "backend",
                "p",
                datetime(2026, 6, 9, 15, 0),
                "refactored auth middleware cleanly",
                "b.md",
            ),
            SessionEntry(
                "qa",
                "p",
                datetime(2026, 6, 9, 16, 0),
                "smoke test /login endpoint passed ok",
                "c.md",
            ),
        ]

    def test_counts_per_role(self) -> None:
        rows = _analyse_role_trend(self._make_entries())
        counts = {r["role"]: r["count"] for r in rows}
        assert counts["backend"] == 2
        assert counts["qa"] == 1

    def test_sorts_by_count_desc(self) -> None:
        rows = _analyse_role_trend(self._make_entries())
        assert rows[0]["role"] == "backend"

    def test_avg_note_len_is_int(self) -> None:
        rows = _analyse_role_trend(self._make_entries())
        for row in rows:
            assert isinstance(row["avg_note_len"], int)

    def test_last_active_is_date_string(self) -> None:
        rows = _analyse_role_trend(self._make_entries())
        for row in rows:
            assert re.match(r"\d{4}-\d{2}-\d{2}", row["last_active"])


class TestAnalyseBlockers:
    def test_identifies_blocker_entries(self) -> None:
        entries = [
            SessionEntry(
                "qa", "p", datetime.now(), "login flow blocked by missing CORS header", "a.md"
            ),
            SessionEntry(
                "qa", "p", datetime.now(), "passed: all smoke tests passed ok green", "b.md"
            ),
            SessionEntry(
                "backend", "p", datetime.now(), "ติด dependency version conflict ต้องแก้", "c.md"
            ),
        ]
        rows = _analyse_blockers(entries)
        roles = {r["role"] for r in rows}
        assert "qa" in roles
        assert "backend" in roles

    def test_returns_empty_when_no_blockers(self) -> None:
        entries = [
            SessionEntry("backend", "p", datetime.now(), "endpoint added and tests pass", "a.md"),
        ]
        assert _analyse_blockers(entries) == []

    def test_includes_examples(self) -> None:
        entries = [
            SessionEntry(
                "qa", "p", datetime.now(), "login blocked: CORS error on preflight", "a.md"
            ),
        ]
        rows = _analyse_blockers(entries)
        assert len(rows[0]["examples"]) >= 1


class TestBuildDecisionChains:
    def test_groups_by_date(self) -> None:
        entries = [
            SessionEntry("backend", "p", datetime(2026, 6, 8, 10, 0), "note a", "a.md"),
            SessionEntry("qa", "p", datetime(2026, 6, 9, 11, 0), "note b", "b.md"),
            SessionEntry("frontend", "p", datetime(2026, 6, 9, 14, 0), "note c", "c.md"),
        ]
        chains = _build_decision_chains(entries)
        dates = [day for day, _ in chains]
        assert "2026-06-08" in dates
        assert "2026-06-09" in dates

    def test_most_recent_date_first(self) -> None:
        entries = [
            SessionEntry("backend", "p", datetime(2026, 6, 8, 10, 0), "note a", "a.md"),
            SessionEntry("qa", "p", datetime(2026, 6, 9, 11, 0), "note b", "b.md"),
        ]
        chains = _build_decision_chains(entries)
        assert chains[0][0] == "2026-06-09"


# ── _render_report ────────────────────────────────────────────────────────────


class TestRenderReport:
    def _make_report(self, n_backend: int = 2, n_qa: int = 1, blocker: bool = False) -> GraphReport:
        entries = []
        for i in range(n_backend):
            entries.append(
                SessionEntry(
                    "backend",
                    "proj",
                    datetime(2026, 6, 9, 10 + i, 0),
                    f"backend task {i}: added /endpoint/{i} with unit tests",
                    f"b{i}.md",
                )
            )
        for i in range(n_qa):
            note = (
                "qa blocked: login flow fails on Edge browser completely"
                if blocker
                else f"qa task {i}: all smoke tests passed green ok"
            )
            entries.append(
                SessionEntry("qa", "proj", datetime(2026, 6, 9, 14 + i, 0), note, f"q{i}.md")
            )
        return GraphReport(
            project="proj", generated_at=datetime(2026, 6, 9, 20, 0), entries=entries
        )

    def test_includes_project_name(self) -> None:
        md = _render_report(self._make_report())
        assert "proj" in md

    def test_includes_total_sessions(self) -> None:
        md = _render_report(self._make_report(n_backend=2, n_qa=1))
        assert "3" in md

    def test_includes_role_trend_table(self) -> None:
        md = _render_report(self._make_report())
        assert "## Role Trend" in md
        assert "backend" in md

    def test_includes_decision_chain(self) -> None:
        md = _render_report(self._make_report())
        assert "## Decision Chain" in md
        assert "2026-06-09" in md

    def test_blocker_appears_in_report(self) -> None:
        md = _render_report(self._make_report(blocker=True))
        assert "⚠ blocker" in md

    def test_no_blocker_section_shows_none_found(self) -> None:
        md = _render_report(self._make_report(blocker=False))
        assert "ไม่พบ blocker events" in md

    def test_empty_entries_shows_no_logs_message(self) -> None:
        report = GraphReport(project="proj", generated_at=datetime.now(), entries=[])
        md = _render_report(report)
        assert "ไม่พบ session logs" in md

    def test_date_filter_restricts_entries(self) -> None:
        entries = [
            SessionEntry(
                "backend", "proj", datetime(2026, 6, 8, 10, 0), "old entry task done", "a.md"
            ),
            SessionEntry(
                "qa", "proj", datetime(2026, 6, 9, 11, 0), "new smoke test all passed ok", "b.md"
            ),
        ]
        report = GraphReport(
            project="proj",
            generated_at=datetime(2026, 6, 9, 20, 0),
            entries=entries,
            date_filter=date(2026, 6, 9),
        )
        md = _render_report(report)
        assert "qa" in md
        # backend entry from 2026-06-08 should not appear in decision chain
        assert "2026-06-08" not in md


# ── analyse() integration ─────────────────────────────────────────────────────


class TestAnalyse:
    def test_writes_report_file(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = tmp_path / "vault"
        _write_session(
            vault, "myproj", "backend", "2026-06-09T14:30:00", "เพิ่ม POST /auth/login endpoint สำเร็จ"
        )
        _write_session(
            vault, "myproj", "qa", "2026-06-09T15:00:00", "smoke test /auth/login ผ่านหมด happy path"
        )

        # Redirect output dir to tmp_path so test doesn't write into the real repo
        import agent_takkub.vault_graph as vg

        monkeypatch.setattr(vg, "REPO_ROOT", tmp_path / "repo")

        result = analyse("myproj", vault=vault)
        assert result is not None
        assert result.exists()
        content = result.read_text(encoding="utf-8")
        assert "myproj" in content
        assert "backend" in content

    def test_returns_none_when_vault_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "agent_takkub.vault_mirror._DEFAULT_VAULT", pathlib.Path("/nonexistent")
        )
        monkeypatch.delenv("TAKKUB_VAULT_DIR", raising=False)
        result = analyse("myproj")
        assert result is None

    def test_returns_path_with_empty_sessions(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = tmp_path / "vault"
        (vault / "01-Projects").mkdir(parents=True)

        import agent_takkub.vault_graph as vg

        monkeypatch.setattr(vg, "REPO_ROOT", tmp_path / "repo")

        # No sessions dir for project — should write an "empty" report, not crash
        result = analyse("ghost-project", vault=vault)
        assert result is not None
        content = result.read_text(encoding="utf-8")
        assert "ghost-project" in content

    def test_date_filter_written_to_filename(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = tmp_path / "vault"
        (vault / "01-Projects").mkdir(parents=True)

        import agent_takkub.vault_graph as vg

        monkeypatch.setattr(vg, "REPO_ROOT", tmp_path / "repo")

        result = analyse("proj", date_str="2026-06-09", vault=vault)
        assert result is not None
        assert "2026-06-09" in result.name

    def test_invalid_date_returns_none(self, tmp_path: pathlib.Path) -> None:
        vault = tmp_path / "vault"
        (vault / "01-Projects").mkdir(parents=True)
        result = analyse("proj", date_str="not-a-date", vault=vault)
        assert result is None
