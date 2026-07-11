"""Tests for docs_verify.py — markdown reference verifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub.docs_verify import (
    PathRef,
    SymbolRef,
    VerifyResult,
    extract_path_refs,
    extract_symbol_refs,
    format_drift_report,
    strip_code_blocks,
    verify_docs,
    verify_path,
    verify_symbol,
)

# ---------------------------------------------------------------------------
# extract_path_refs
# ---------------------------------------------------------------------------


def test_extract_path_refs_catches_path_with_line() -> None:
    md = "See `src/foo.py:42` for details."
    refs = extract_path_refs(md, source="test.md")
    assert len(refs) == 1
    assert refs[0].path == "src/foo.py"
    assert refs[0].line == 42


def test_extract_path_refs_catches_md_without_line() -> None:
    md = "Read `docs/ARCHITECTURE.md` for context."
    refs = extract_path_refs(md, source="README.md")
    assert len(refs) == 1
    assert refs[0].path == "docs/ARCHITECTURE.md"
    assert refs[0].line is None


def test_extract_path_refs_ignores_urls() -> None:
    md = "See `https://example.com/foo.py` for the upstream."
    refs = extract_path_refs(md, source="doc.md")
    assert refs == []


def test_extract_path_refs_ignores_plain_english() -> None:
    md = "He said he is i.e. wrong about `this`."
    refs = extract_path_refs(md, source="doc.md")
    # "this" has no extension → not matched
    assert refs == []


def test_extract_path_refs_source_recorded() -> None:
    md = "Check `src/cli.py:10`."
    refs = extract_path_refs(md, source="myfile.md")
    assert refs[0].source == "myfile.md"
    assert refs[0].source_line == 1


# ---------------------------------------------------------------------------
# extract_symbol_refs
# ---------------------------------------------------------------------------


def test_extract_symbol_refs_catches_class_method() -> None:
    md = "Call `Foo.compute` to start."
    refs = extract_symbol_refs(md, source="test.md")
    names = [r.method for r in refs]
    assert "compute" in names


def test_extract_symbol_refs_catches_function_call() -> None:
    md = "Use `compute_tfidf()` for TF-IDF."
    refs = extract_symbol_refs(md, source="test.md")
    names = [r.method for r in refs]
    assert "compute_tfidf" in names


def test_extract_symbol_refs_ignores_ie_eg() -> None:
    md = "This works `i.e.` always and `e.g.` every time."
    refs = extract_symbol_refs(md, source="test.md")
    assert refs == []


# ---------------------------------------------------------------------------
# verify_path
# ---------------------------------------------------------------------------


def test_verify_path_existing_file_ok(tmp_path: Path) -> None:
    f = tmp_path / "src" / "foo.py"
    f.parent.mkdir(parents=True)
    f.write_text("line1\nline2\nline3\n")
    ref = PathRef(text="`src/foo.py`", path="src/foo.py", line=None, source="doc.md", source_line=1)
    result = verify_path(ref, repo_root=tmp_path)
    assert result.status == "ok"


def test_verify_path_missing_file(tmp_path: Path) -> None:
    ref = PathRef(
        text="`src/missing.py`", path="src/missing.py", line=None, source="doc.md", source_line=1
    )
    result = verify_path(ref, repo_root=tmp_path)
    assert result.status == "missing"


def test_verify_path_line_beyond_eof(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("one\ntwo\n")
    ref = PathRef(text="`src.py:99`", path="src.py", line=99, source="doc.md", source_line=1)
    result = verify_path(ref, repo_root=tmp_path)
    assert result.status == "line_oob"


# ---------------------------------------------------------------------------
# verify_symbol
# ---------------------------------------------------------------------------


def test_verify_symbol_defined_function_ok(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("def compute_tf(tokens):\n    pass\n")
    ref = SymbolRef(
        text="`compute_tf`", class_name=None, method="compute_tf", source="doc.md", source_line=1
    )
    result = verify_symbol(ref, repo_root=tmp_path, search_dirs=(Path("src"),))
    assert result.status == "ok"


def test_verify_symbol_undefined_missing(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("def something_else(): pass\n")
    ref = SymbolRef(
        text="`nonexistent`", class_name=None, method="nonexistent", source="doc.md", source_line=1
    )
    result = verify_symbol(ref, repo_root=tmp_path, search_dirs=(Path("src"),))
    assert result.status == "missing"


# ---------------------------------------------------------------------------
# verify_docs
# ---------------------------------------------------------------------------


def test_verify_docs_empty_markdown(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "empty.md").write_text("# Title\n\nNo references here.\n")
    results = verify_docs(
        docs_dirs=(Path("docs"),),
        extras=(),
        repo_root=tmp_path,
    )
    assert results == []


def test_verify_docs_broken_ref_returns_missing(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("See `src/nonexistent.py:1` for details.\n")
    results = verify_docs(
        docs_dirs=(Path("docs"),),
        extras=(),
        repo_root=tmp_path,
    )
    broken = [r for r in results if r.status != "ok"]
    assert len(broken) == 1
    assert broken[0].status == "missing"


# ---------------------------------------------------------------------------
# format_drift_report
# ---------------------------------------------------------------------------


def test_format_drift_report_empty() -> None:
    report = format_drift_report([])
    assert "No broken refs" in report


def test_format_drift_report_with_broken_refs() -> None:
    results = [
        VerifyResult(
            ref_text="`src/foo.py:10`",
            status="missing",
            message="file not found",
            source="docs/A.md",
            source_line=5,
        ),
        VerifyResult(
            ref_text="`src/bar.py:999`",
            status="line_oob",
            message="line 999 > 3 lines",
            source="docs/B.md",
            source_line=12,
        ),
    ]
    report = format_drift_report(results)
    assert "src/foo.py" in report
    assert "src/bar.py" in report
    assert "missing" in report
    assert "line_oob" in report


# ---------------------------------------------------------------------------
# Integration: real docs/ + CLAUDE.md
# ---------------------------------------------------------------------------


def test_verify_docs_real_repo_no_crash() -> None:
    """Run on real repo — should not raise, returns a list."""
    repo_root = Path(".")
    if not (repo_root / "docs").exists():
        pytest.skip("docs/ not found")
    results = verify_docs(
        docs_dirs=(Path("docs"),),
        extras=(Path("CLAUDE.md"),) if (repo_root / "CLAUDE.md").exists() else (),
        repo_root=repo_root,
    )
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# strip_code_blocks
# ---------------------------------------------------------------------------


def test_strip_code_blocks_removes_fenced_content() -> None:
    md = "before\n```\n`src/foo.py:1`\n```\nafter\n"
    stripped = strip_code_blocks(md)
    assert "src/foo.py" not in stripped
    assert "before" in stripped
    assert "after" in stripped


def test_strip_code_blocks_preserves_line_count() -> None:
    md = "line1\n```\ninner1\ninner2\n```\nline6\n"
    stripped = strip_code_blocks(md)
    assert stripped.count("\n") == md.count("\n")


def test_strip_code_blocks_unmatched_fence_returns_original() -> None:
    md = "line1\n```\nno closing fence\n"
    stripped = strip_code_blocks(md)
    assert stripped == md


def test_strip_code_blocks_multiple_fences() -> None:
    md = "```\n`src/a.py`\n```\nmiddle\n```\n`src/b.py`\n```\nend\n"
    stripped = strip_code_blocks(md)
    assert "src/a.py" not in stripped
    assert "src/b.py" not in stripped
    assert "middle" in stripped
    assert "end" in stripped


# ---------------------------------------------------------------------------
# verify_docs — code block skipping
# ---------------------------------------------------------------------------


def test_verify_docs_code_block_refs_not_reported(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    # Path ref inside a fenced code block — should not be flagged as broken
    (docs_dir / "guide.md").write_text("```\nSee `src/nonexistent_file.py:1` for example.\n```\n")
    results = verify_docs(
        docs_dirs=(Path("docs"),),
        extras=(),
        repo_root=tmp_path,
    )
    broken = [r for r in results if r.status != "ok"]
    assert broken == []


# ---------------------------------------------------------------------------
# verify_docs — exclude_globs
# ---------------------------------------------------------------------------


def test_verify_docs_exclude_globs_skips_matching_file(tmp_path: Path) -> None:
    reviews_dir = tmp_path / "docs" / "reviews"
    reviews_dir.mkdir(parents=True)
    (reviews_dir / "external.md").write_text("See `src/nonexistent_vendored.py:1`.\n")
    results = verify_docs(
        docs_dirs=(Path("docs"),),
        extras=(),
        repo_root=tmp_path,
        exclude_globs=("docs/reviews/*",),
    )
    broken = [r for r in results if r.status != "ok"]
    assert broken == []


def test_verify_docs_default_excludes_reviews_dir(tmp_path: Path) -> None:
    reviews_dir = tmp_path / "docs" / "reviews"
    reviews_dir.mkdir(parents=True)
    (reviews_dir / "audit.md").write_text("See `src/nonexistent_audit.py:1`.\n")
    # Default call (no explicit exclude_globs) — reviews/ auto-excluded
    results = verify_docs(
        docs_dirs=(Path("docs"),),
        extras=(),
        repo_root=tmp_path,
    )
    broken = [r for r in results if r.status != "ok"]
    assert broken == []


def test_verify_docs_default_excludes_audit_dir(tmp_path: Path) -> None:
    audits_dir = tmp_path / "docs" / "audit"
    audits_dir.mkdir(parents=True)
    (audits_dir / "historical.md").write_text("See `src/removed_after_audit.py:1`.\n")
    results = verify_docs(docs_dirs=(Path("docs"),), extras=(), repo_root=tmp_path)
    assert [r for r in results if r.status != "ok"] == []


def test_extract_symbol_refs_skips_python_builtin_isinstance() -> None:
    assert extract_symbol_refs("Uses `isinstance()` for the runtime check.") == []


def test_verify_docs_default_excludes_point_in_time_artifacts(tmp_path: Path) -> None:
    """Design plans/specs, code reviews and QA reports are excluded by default —
    they reference prototype/renamed/test/external symbols that are false drift."""
    for sub, fname in (
        ("docs/code-review", "r.md"),
        ("docs/qa-reports", "q.md"),
        ("docs/superpowers/specs", "s.md"),
        ("docs/superpowers/plans", "p.md"),
    ):
        d = tmp_path / sub
        d.mkdir(parents=True)
        (d / fname).write_text("ref `src/nonexistent_proto.py:1`\n")
    # Loose-file artifacts too.
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "MACOS_PORT_PLAN.md").write_text("ref `src/_pty_posix.py:1`\n")
    results = verify_docs(docs_dirs=(Path("docs"),), extras=(), repo_root=tmp_path)
    assert [r for r in results if r.status != "ok"] == []


def test_verify_docs_excludes_nested_point_in_time_artifacts(tmp_path: Path) -> None:
    """A `dir/*` exclude must also cover files in SUBDIRS of that dir. The real
    case: docs/code-review/2026-05-29-system/codex.md referenced gemini_md.py
    (removed in the agy migration) and blocked the commit because PurePath's `*`
    doesn't cross `/` and Python 3.11 lacks recursive `**`."""
    nested = tmp_path / "docs" / "code-review" / "2026-05-29-system"
    nested.mkdir(parents=True)
    (nested / "codex.md").write_text("ref `src/agent_takkub/gemini_md.py:1`\n")
    results = verify_docs(docs_dirs=(Path("docs"),), extras=(), repo_root=tmp_path)
    assert [r for r in results if r.status != "ok"] == []


def test_verify_docs_still_checks_live_guides(tmp_path: Path) -> None:
    """The exclusions must NOT swallow agent-takkub's own live docs — a normal
    guide with a broken ref must still be flagged (the gate keeps its value)."""
    guides = tmp_path / "docs" / "guides"
    guides.mkdir(parents=True)
    (guides / "2026-06-09-cockpit-usage.md").write_text("see `src/nonexistent_live.py:1`\n")
    results = verify_docs(docs_dirs=(Path("docs"),), extras=(), repo_root=tmp_path)
    broken = [r for r in results if r.status != "ok"]
    assert len(broken) == 1
    assert "nonexistent_live" in broken[0].message


def test_verify_docs_no_default_excludes_includes_reviews(tmp_path: Path) -> None:
    reviews_dir = tmp_path / "docs" / "reviews"
    reviews_dir.mkdir(parents=True)
    (reviews_dir / "audit.md").write_text("See `src/nonexistent_reincluded.py:1`.\n")
    # Passing exclude_globs=() disables default excludes
    results = verify_docs(
        docs_dirs=(Path("docs"),),
        extras=(),
        repo_root=tmp_path,
        exclude_globs=(),
        use_default_excludes=False,
    )
    broken = [r for r in results if r.status != "ok"]
    assert len(broken) == 1
    assert "nonexistent_reincluded" in broken[0].message
