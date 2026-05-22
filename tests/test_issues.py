"""Unit tests for takkub issue tracker (src/agent_takkub/issues.py)."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from agent_takkub.issues import (
    _parse_file,
    close_issue,
    cmd_issue_close,
    cmd_issue_list,
    cmd_issue_new,
    cmd_issue_show,
    list_issues,
    new_issue,
    next_id,
    show_issue,
)

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def idir(tmp_path: Path) -> Path:
    d = tmp_path / "docs" / "issues"
    d.mkdir(parents=True)
    return d


# ── next_id ───────────────────────────────────────────────────────────────────


def test_next_id_first_of_day(idir: Path) -> None:
    assert next_id(idir, "20260522") == "20260522-001"


def test_next_id_increments(idir: Path) -> None:
    (idir / "20260522-001.md").write_text("---\nid: 20260522-001\n---\n", encoding="utf-8")
    assert next_id(idir, "20260522") == "20260522-002"


def test_next_id_collision_retry(idir: Path) -> None:
    # Simulate 001 and 002 existing; expect 003
    (idir / "20260522-001.md").write_text("---\nid: 20260522-001\n---\n", encoding="utf-8")
    (idir / "20260522-002.md").write_text("---\nid: 20260522-002\n---\n", encoding="utf-8")
    assert next_id(idir, "20260522") == "20260522-003"


def test_next_id_different_days_dont_collide(idir: Path) -> None:
    # A file from a different date should not affect today's NNN
    (idir / "20260521-001.md").write_text("---\nid: 20260521-001\n---\n", encoding="utf-8")
    assert next_id(idir, "20260522") == "20260522-001"


# ── new_issue ─────────────────────────────────────────────────────────────────


def test_new_issue_creates_file(idir: Path) -> None:
    issue_id, path = new_issue("test title", "symptom here", issues_dir=idir)
    assert path.exists()
    assert path.suffix == ".md"
    assert issue_id in path.stem


def test_new_issue_frontmatter_fields(idir: Path) -> None:
    issue_id, path = new_issue(
        "backend pane ไม่ report กลับ Lead",
        "repro steps",
        severity="high",
        noticed_in="pms",
        role="backend",
        tags=["orchestration", "timeout"],
        issues_dir=idir,
    )
    fm, body = _parse_file(path)
    assert fm["id"] == issue_id
    assert fm["title"] == "backend pane ไม่ report กลับ Lead"
    assert fm["status"] == "open"
    assert fm["severity"] == "high"
    assert fm["noticed_in"] == "pms"
    assert fm["role"] == "backend"
    assert fm["tags"] == ["orchestration", "timeout"]
    assert "created_at" in fm
    assert body.strip() == "repro steps"


def test_new_issue_body_preserved(idir: Path) -> None:
    body = "line1\nline2\n\nline3"
    _, path = new_issue("title", body, issues_dir=idir)
    _, parsed_body = _parse_file(path)
    assert "line1" in parsed_body
    assert "line3" in parsed_body


def test_new_issue_handles_missing_dir(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does" / "not" / "exist"
    assert not nonexistent.exists()
    _, path = new_issue("title", "body", issues_dir=nonexistent)
    assert path.exists()


def test_new_issue_default_severity(idir: Path) -> None:
    _, path = new_issue("t", "b", issues_dir=idir)
    fm, _ = _parse_file(path)
    assert fm["severity"] == "med"


def test_new_issue_invalid_severity(idir: Path) -> None:
    with pytest.raises(ValueError, match="severity"):
        new_issue("t", "b", severity="critical", issues_dir=idir)


def test_new_issue_empty_title_raises(idir: Path) -> None:
    with pytest.raises(ValueError, match="title"):
        new_issue("", "body", issues_dir=idir)


def test_new_issue_unicode_title(idir: Path) -> None:
    title = "🐛 สมาชิก API ตอบ 500 เมื่อ role=lead"
    _, path = new_issue(title, "body", issues_dir=idir)
    fm, _ = _parse_file(path)
    assert fm["title"] == title


# ── list_issues ───────────────────────────────────────────────────────────────


def _make(idir: Path, **overrides) -> str:
    """Helper: create an issue with minimal defaults and return its id."""
    kwargs = dict(
        title=overrides.pop("title", "default title"),
        body=overrides.pop("body", "body"),
        severity=overrides.pop("severity", "med"),
        noticed_in=overrides.pop("noticed_in", None),
        role=overrides.pop("role", None),
        tags=overrides.pop("tags", None),
        issues_dir=idir,
    )
    issue_id, _ = new_issue(**kwargs)
    if overrides.get("closed"):
        close_issue(issue_id, issues_dir=idir)
    return issue_id


def test_list_all_default(idir: Path) -> None:
    _make(idir, title="a")
    _make(idir, title="b", closed=True)
    items = list_issues(issues_dir=idir)
    assert len(items) == 2


def test_list_filter_open(idir: Path) -> None:
    open_id = _make(idir, title="open one")
    _make(idir, title="closed one", closed=True)
    items = list_issues(filter_open=True, issues_dir=idir)
    ids = [i["id"] for i in items]
    assert open_id in ids
    assert len(items) == 1


def test_list_filter_closed(idir: Path) -> None:
    _make(idir, title="open one")
    closed_id = _make(idir, title="closed one", closed=True)
    items = list_issues(filter_closed=True, issues_dir=idir)
    ids = [i["id"] for i in items]
    assert closed_id in ids
    assert len(items) == 1


def test_list_filter_noticed_in(idir: Path) -> None:
    _make(idir, noticed_in="pms")
    _make(idir, noticed_in="unirecon")
    items = list_issues(noticed_in="pms", issues_dir=idir)
    assert len(items) == 1
    assert items[0]["noticed_in"] == "pms"


def test_list_filter_role(idir: Path) -> None:
    _make(idir, role="backend")
    _make(idir, role="frontend")
    items = list_issues(role="backend", issues_dir=idir)
    assert len(items) == 1
    assert items[0]["role"] == "backend"


def test_list_filter_severity(idir: Path) -> None:
    _make(idir, severity="high")
    _make(idir, severity="low")
    items = list_issues(severity="high", issues_dir=idir)
    assert len(items) == 1
    assert items[0]["severity"] == "high"


def test_list_empty_dir_returns_empty(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nope"
    items = list_issues(issues_dir=nonexistent)
    assert items == []


# ── close_issue ───────────────────────────────────────────────────────────────


def test_close_sets_status(idir: Path) -> None:
    issue_id, _ = new_issue("t", "b", issues_dir=idir)
    close_issue(issue_id, issues_dir=idir)
    path = idir / f"{issue_id}.md"
    fm, _ = _parse_file(path)
    assert fm["status"] == "closed"


def test_close_sets_closed_at(idir: Path) -> None:
    issue_id, _ = new_issue("t", "b", issues_dir=idir)
    close_issue(issue_id, issues_dir=idir)
    fm, _ = _parse_file(idir / f"{issue_id}.md")
    assert "closed_at" in fm


def test_close_sets_closed_note(idir: Path) -> None:
    issue_id, _ = new_issue("t", "b", issues_dir=idir)
    close_issue(issue_id, note="fixed by restart", issues_dir=idir)
    fm, _ = _parse_file(idir / f"{issue_id}.md")
    assert fm["closed_note"] == "fixed by restart"


def test_close_preserves_body(idir: Path) -> None:
    body = "original body text\nwith multiple lines"
    issue_id, _ = new_issue("t", body, issues_dir=idir)
    close_issue(issue_id, issues_dir=idir)
    _, parsed_body = _parse_file(idir / f"{issue_id}.md")
    assert "original body text" in parsed_body
    assert "with multiple lines" in parsed_body


def test_close_missing_id_raises(idir: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        close_issue("20260522-999", issues_dir=idir)


def test_close_double_close_raises(idir: Path) -> None:
    issue_id, _ = new_issue("t", "b", issues_dir=idir)
    close_issue(issue_id, issues_dir=idir)
    with pytest.raises(ValueError, match="already closed"):
        close_issue(issue_id, issues_dir=idir)


# ── show_issue ────────────────────────────────────────────────────────────────


def test_show_returns_content(idir: Path) -> None:
    issue_id, _ = new_issue("my title", "my body", issues_dir=idir)
    content = show_issue(issue_id, issues_dir=idir)
    assert "my title" in content
    assert "my body" in content


def test_show_missing_raises(idir: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        show_issue("20260522-999", issues_dir=idir)


def test_show_after_close_has_closed_status(idir: Path) -> None:
    issue_id, _ = new_issue("t", "b", issues_dir=idir)
    close_issue(issue_id, note="test fix", issues_dir=idir)
    content = show_issue(issue_id, issues_dir=idir)
    assert "closed" in content
    assert "test fix" in content


# ── malformed frontmatter ─────────────────────────────────────────────────────


def test_parse_no_frontmatter_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.md"
    p.write_text("just body text, no frontmatter", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed frontmatter"):
        _parse_file(p)


def test_parse_unclosed_frontmatter_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.md"
    p.write_text("---\nid: foo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed frontmatter"):
        _parse_file(p)


def test_parse_invalid_yaml_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.md"
    p.write_text("---\n: : :\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed frontmatter"):
        _parse_file(p)


# ── cmd_* handlers (CLI layer) ────────────────────────────────────────────────


def _args(**kwargs):
    """Minimal argparse.Namespace substitute."""
    defaults = {
        "title": "test issue",
        "body": "test body",
        "severity": "med",
        "noticed_in": None,
        "role": None,
        "tag": None,
        "issues_dir": None,
        "note": "",
        "id": None,
        "open": False,
        "closed": False,
    }
    defaults.update(kwargs)
    ns = types.SimpleNamespace(**defaults)
    return ns


def test_cmd_new_creates_issue(idir: Path) -> None:
    args = _args(title="cmd title", body="cmd body", issues_dir=str(idir))
    resp = cmd_issue_new(args)
    assert resp["ok"] is True
    files = list(idir.glob("*.md"))
    assert len(files) == 1


def test_cmd_new_no_body_no_tty_errors(idir: Path, monkeypatch) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    args = _args(title="t", body=None, issues_dir=str(idir))
    resp = cmd_issue_new(args)
    assert resp["ok"] is False
    assert "no --body" in resp["msg"]


def test_cmd_list_output(idir: Path, capsys) -> None:
    new_issue("issue one", "body", issues_dir=idir)
    args = _args(issues_dir=str(idir))
    resp = cmd_issue_list(args)
    assert resp["ok"] is True
    captured = capsys.readouterr()
    assert "issue one" in captured.out


def test_cmd_close_and_show(idir: Path, capsys) -> None:
    issue_id, _ = new_issue("t", "b", issues_dir=idir)
    args_close = _args(id=issue_id, note="fixed", issues_dir=str(idir))
    resp = cmd_issue_close(args_close)
    assert resp["ok"] is True

    args_show = _args(id=issue_id, issues_dir=str(idir))
    resp2 = cmd_issue_show(args_show)
    assert resp2["ok"] is True
    captured = capsys.readouterr()
    assert "closed" in captured.out
    assert "fixed" in captured.out


def test_cmd_close_missing_id(idir: Path) -> None:
    args = _args(id="20260101-999", issues_dir=str(idir))
    resp = cmd_issue_close(args)
    assert resp["ok"] is False
    assert "not found" in resp["msg"]


def test_cmd_show_missing_id(idir: Path) -> None:
    args = _args(id="20260101-999", issues_dir=str(idir))
    resp = cmd_issue_show(args)
    assert resp["ok"] is False
    assert "not found" in resp["msg"]


# ── path traversal prevention ─────────────────────────────────────────────────


def test_close_issue_path_traversal(idir: Path) -> None:
    with pytest.raises(ValueError, match="invalid issue ID"):
        close_issue("../../secret", issues_dir=idir)


def test_show_issue_path_traversal(idir: Path) -> None:
    with pytest.raises(ValueError, match="invalid issue ID"):
        show_issue("../etc/passwd", issues_dir=idir)


def test_show_issue_path_traversal_with_slash(idir: Path) -> None:
    with pytest.raises(ValueError, match="invalid issue ID"):
        show_issue("20260522-001/../../foo", issues_dir=idir)


# ── EDITOR with spaces ────────────────────────────────────────────────────────


def test_cmd_new_editor_with_spaces_shlex(idir: Path, monkeypatch) -> None:
    """EDITOR='code --wait' must be shlex-split, not passed as single token."""
    import io
    import subprocess

    mock_stdin = io.StringIO("")
    mock_stdin.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", mock_stdin)

    captured_cmds: list[list[str]] = []

    def fake_call(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        # write body so the issue is actually created
        from pathlib import Path as _P

        _P(cmd[-1]).write_text("body from editor", encoding="utf-8")
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)
    monkeypatch.setenv("EDITOR", "code --wait")

    args = _args(title="editor test", body=None, issues_dir=str(idir))
    resp = cmd_issue_new(args)
    assert resp["ok"] is True
    assert captured_cmds, "subprocess.call was not invoked"
    assert captured_cmds[0][0] == "code", "first token should be 'code'"
    assert captured_cmds[0][1] == "--wait", "second token should be '--wait'"


def test_cmd_new_editor_nonzero_exit(idir: Path, monkeypatch) -> None:
    """Editor returning nonzero should yield ok=False."""
    import io
    import subprocess

    mock_stdin = io.StringIO("")
    mock_stdin.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", mock_stdin)
    monkeypatch.setattr(subprocess, "call", lambda cmd, **kw: 1)
    monkeypatch.setenv("EDITOR", "vim")

    args = _args(title="t", body=None, issues_dir=str(idir))
    resp = cmd_issue_new(args)
    assert resp["ok"] is False
    assert "editor exited" in resp["msg"]


# ── --issues-dir argparse wiring ──────────────────────────────────────────────


def test_issues_dir_flag_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    """takkub issue list --issues-dir PATH must parse without 'unrecognized arguments'."""
    import sys

    from agent_takkub import cli

    monkeypatch.setattr(sys, "argv", ["takkub", "issue", "list", "--issues-dir", str(tmp_path)])
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code == 0, f"CLI exited {exc.code}"
    captured = capsys.readouterr()
    assert "unrecognized" not in captured.err


# ── malformed file warning ────────────────────────────────────────────────────


def test_list_issues_warns_malformed(idir: Path, capsys) -> None:
    """list_issues skips corrupt files but emits a warning to stderr."""
    _make(idir, title="valid one")
    _make(idir, title="valid two")
    (idir / "20260522-corrupt.md").write_text("no frontmatter here", encoding="utf-8")

    items = list_issues(issues_dir=idir)
    captured = capsys.readouterr()

    assert len(items) == 2, "corrupt file should be skipped, two valid items remain"
    assert "warn:" in captured.err, "expected warning on stderr"
    assert "corrupt" in captured.err
