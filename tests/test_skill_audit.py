"""Tests for skill_audit.py — TF-IDF role boundary audit."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from agent_takkub.skill_audit import (
    audit_new_role_text,
    audit_skills,
    compute_idf,
    compute_tf,
    compute_tfidf,
    cosine_similarity,
    format_report,
    load_all_role_docs,
    load_role_docs,
    tokenize,
)

# ---------------------------------------------------------------------------
# load_role_docs
# ---------------------------------------------------------------------------


def test_load_role_docs_reads_md_files(tmp_path: Path) -> None:
    (tmp_path / "frontend.md").write_text("React TypeScript component")
    (tmp_path / "backend.md").write_text("REST API database")
    result = load_role_docs(tmp_path)
    assert set(result.keys()) == {"frontend", "backend"}
    assert "React" in result["frontend"]
    assert "REST" in result["backend"]


def test_load_role_docs_skips_non_md(tmp_path: Path) -> None:
    (tmp_path / "frontend.md").write_text("React")
    (tmp_path / "notes.txt").write_text("text file")
    (tmp_path / "config.json").write_text("{}")
    result = load_role_docs(tmp_path)
    assert set(result.keys()) == {"frontend"}


def test_load_role_docs_empty_dir(tmp_path: Path) -> None:
    result = load_role_docs(tmp_path)
    assert result == {}


def test_load_role_docs_falls_back_to_agents_dir_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nonexistent cwd-relative dir (e.g. `takkub audit-skills` run from an
    installed build, outside any repo checkout) must fall back to
    config.AGENTS_DIR instead of silently returning nothing."""
    fake_agents_dir = tmp_path / "_assets" / ".claude" / "agents"
    fake_agents_dir.mkdir(parents=True)
    (fake_agents_dir / "backend.md").write_text("REST API database")
    monkeypatch.setattr("agent_takkub.config.AGENTS_DIR", fake_agents_dir)

    missing_dir = tmp_path / "does-not-exist" / ".claude" / "agents"
    result = load_role_docs(missing_dir)

    assert set(result.keys()) == {"backend"}


def test_load_role_docs_missing_agents_dir_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both the passed dir AND the AGENTS_DIR fallback are missing → empty,
    not a crash."""
    monkeypatch.setattr("agent_takkub.config.AGENTS_DIR", tmp_path / "also-missing")
    result = load_role_docs(tmp_path / "does-not-exist")
    assert result == {}


# ---------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------


def test_tokenize_drops_stopwords_and_short_tokens() -> None:
    tokens = tokenize("The frontend developer builds the UI and handles the form")
    assert "the" not in tokens
    assert "and" not in tokens
    assert "ui" not in tokens  # < 3 chars dropped
    assert "frontend" in tokens
    assert "developer" in tokens
    assert "builds" in tokens
    assert "form" in tokens


def test_tokenize_lowercase() -> None:
    tokens = tokenize("React TypeScript Component")
    assert "react" in tokens
    assert "typescript" in tokens
    assert "component" in tokens


# ---------------------------------------------------------------------------
# compute_tfidf
# ---------------------------------------------------------------------------


def test_compute_tfidf_returns_dict_per_doc() -> None:
    docs = {
        "frontend": "React TypeScript component button",
        "backend": "database REST endpoint migration",
    }
    result = compute_tfidf(docs)
    assert set(result.keys()) == {"frontend", "backend"}
    assert isinstance(result["frontend"], dict)
    assert isinstance(result["backend"], dict)


def test_compute_tf_normalized() -> None:
    tokens = ["react", "react", "typescript"]
    tf = compute_tf(tokens)
    assert abs(tf["react"] - 2 / 3) < 1e-9
    assert abs(tf["typescript"] - 1 / 3) < 1e-9


def test_compute_idf_log_formula() -> None:
    docs_tokens = {
        "a": ["react", "typescript"],
        "b": ["react", "python"],
        "c": ["python", "django"],
    }
    idf = compute_idf(docs_tokens)
    # "react" appears in 2/3 docs: log(3/2)
    assert abs(idf["react"] - math.log(3 / 2)) < 1e-9
    # "django" appears in 1/3 docs: log(3/1)
    assert abs(idf["django"] - math.log(3 / 1)) < 1e-9


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


def test_cosine_similarity_identical_vectors() -> None:
    vec = {"a": 1.0, "b": 2.0, "c": 3.0}
    assert abs(cosine_similarity(vec, vec) - 1.0) < 1e-9


def test_cosine_similarity_disjoint_vectors() -> None:
    a = {"x": 1.0, "y": 2.0}
    b = {"z": 1.0, "w": 2.0}
    assert cosine_similarity(a, b) == 0.0


def test_cosine_similarity_empty_vectors() -> None:
    assert cosine_similarity({}, {}) == 0.0
    assert cosine_similarity({"a": 1.0}, {}) == 0.0


# ---------------------------------------------------------------------------
# audit_skills
# ---------------------------------------------------------------------------


def _make_role_dir(tmp_path: Path, roles: dict[str, str]) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    for name, content in roles.items():
        (d / f"{name}.md").write_text(content)
    return d


def test_audit_skills_returns_pairs_above_threshold(tmp_path: Path) -> None:
    roles = {
        "frontend": "React TypeScript component button form render layout",
        "frontend2": "React TypeScript component button form render layout",
        "backend": "database REST endpoint migration schema query",
    }
    d = _make_role_dir(tmp_path, roles)
    pairs = audit_skills(d, threshold=0.9)
    names = [(a, b) for a, b, _ in pairs]
    assert ("frontend", "frontend2") in names or ("frontend2", "frontend") in names


def test_audit_skills_returns_empty_when_no_pairs_above_threshold(tmp_path: Path) -> None:
    roles = {
        "frontend": "React TypeScript component button form render layout user",
        "backend": "database REST endpoint migration schema query index table",
    }
    d = _make_role_dir(tmp_path, roles)
    pairs = audit_skills(d, threshold=0.99)
    assert pairs == []


def test_audit_skills_excludes_self_pairs(tmp_path: Path) -> None:
    roles = {
        "frontend": "React TypeScript component",
        "backend": "database REST endpoint",
    }
    d = _make_role_dir(tmp_path, roles)
    pairs = audit_skills(d, threshold=0.0)
    for a, b, _ in pairs:
        assert a != b


def test_audit_skills_role_a_lt_role_b(tmp_path: Path) -> None:
    roles = {
        "zebra": "React TypeScript component button form render layout",
        "alpha": "React TypeScript component button form render layout",
    }
    d = _make_role_dir(tmp_path, roles)
    pairs = audit_skills(d, threshold=0.0)
    for a, b, _ in pairs:
        assert a < b


def test_audit_skills_sorted_desc(tmp_path: Path) -> None:
    roles = {
        "a": "react typescript component button form render",
        "b": "react typescript component button form render",
        "c": "database rest endpoint migration schema query",
        "d": "react typescript endpoint migration",
    }
    d = _make_role_dir(tmp_path, roles)
    pairs = audit_skills(d, threshold=0.0)
    scores = [s for _, _, s in pairs]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


def test_format_report_produces_markdown_table() -> None:
    pairs = [("backend", "frontend", 0.75), ("devops", "qa", 0.62)]
    report = format_report(pairs, threshold=0.6)
    assert "# Skill boundary audit" in report
    assert "backend" in report
    assert "frontend" in report
    assert "0.75" in report


def test_format_report_empty_pairs() -> None:
    report = format_report([], threshold=0.6)
    assert "No role overlaps above threshold" in report


# ---------------------------------------------------------------------------
# Integration: real .claude/agents/ dir
# ---------------------------------------------------------------------------


def test_audit_skills_real_agents_dir_no_crash() -> None:
    agents_dir = Path(".claude/agents")
    if not agents_dir.exists():
        pytest.skip(".claude/agents not found")
    result = audit_skills(agents_dir, threshold=0.5)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# A6: load_all_role_docs (merged built-in + custom corpus) / audit_new_role_text
# ---------------------------------------------------------------------------


class TestLoadAllRoleDocs:
    def test_merges_builtin_and_custom_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import config

        builtin_dir = tmp_path / "builtin"
        custom_dir = tmp_path / "custom"
        builtin_dir.mkdir()
        custom_dir.mkdir()
        (builtin_dir / "backend.md").write_text("REST API database", encoding="utf-8")
        (custom_dir / "data-eng.md").write_text("ETL pipelines warehouse", encoding="utf-8")
        monkeypatch.setattr(config, "AGENTS_DIR", builtin_dir)
        monkeypatch.setattr(config, "CUSTOM_AGENTS_DIR", custom_dir)

        docs = load_all_role_docs()
        assert set(docs.keys()) == {"backend", "data-eng"}

    def test_custom_dir_missing_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import config

        builtin_dir = tmp_path / "builtin"
        builtin_dir.mkdir()
        (builtin_dir / "backend.md").write_text("REST API", encoding="utf-8")
        monkeypatch.setattr(config, "AGENTS_DIR", builtin_dir)
        monkeypatch.setattr(config, "CUSTOM_AGENTS_DIR", tmp_path / "does-not-exist")

        docs = load_all_role_docs()
        assert set(docs.keys()) == {"backend"}


class TestAuditNewRoleText:
    def test_flags_high_overlap_with_existing_role(self) -> None:
        # A third, unrelated doc keeps IDF from zeroing out every shared term
        # (a term present in every doc has idf=log(N/df)=0 when df==N).
        existing = {
            "backend": "REST API endpoint database schema migration handler",
            "designer": "figma color palette typography spacing layout",
        }
        overlaps = audit_new_role_text(
            "shadow-backend",
            "REST API endpoint database schema migration handler",
            threshold=0.5,
            existing=existing,
        )
        assert overlaps
        assert overlaps[0][0] == "backend"
        assert overlaps[0][1] >= 0.5

    def test_no_overlap_for_distinct_text(self) -> None:
        existing = {"backend": "REST API endpoint database schema migration handler"}
        overlaps = audit_new_role_text(
            "designer",
            "figma color palette typography spacing layout",
            threshold=0.6,
            existing=existing,
        )
        assert overlaps == []

    def test_candidate_never_compared_against_itself(self) -> None:
        existing = {"qa": "playwright browser test smoke e2e"}
        overlaps = audit_new_role_text(
            "qa2", "playwright browser test smoke e2e", threshold=0.9, existing=existing
        )
        assert all(name != "qa2" for name, _sim in overlaps)
