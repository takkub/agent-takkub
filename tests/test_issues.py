"""Unit tests for takkub issue tracker (src/agent_takkub/issues.py) — GitHub backend."""

from __future__ import annotations

import json
import types
from unittest.mock import MagicMock, patch

import pytest

from agent_takkub.issues import (
    _detect_repo,
    _ensure_label,
    _parse_issue_number,
    close_issue,
    cmd_issue_close,
    cmd_issue_list,
    cmd_issue_new,
    cmd_issue_show,
    list_issues,
    new_issue,
    show_issue,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _gh_result(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


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
        "cwd": None,
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


# ── _parse_issue_number ───────────────────────────────────────────────────────


def test_parse_number_plain() -> None:
    assert _parse_issue_number("123") == 123


def test_parse_number_hash_prefix() -> None:
    assert _parse_issue_number("#42") == 42


def test_parse_number_owner_repo_hash() -> None:
    assert _parse_issue_number("owner/repo#99") == 99


def test_parse_number_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid issue ID"):
        _parse_issue_number("20260522-001")


def test_parse_number_zero_raises() -> None:
    with pytest.raises(ValueError, match="invalid issue ID"):
        _parse_issue_number("0")


def test_parse_number_negative_raises() -> None:
    with pytest.raises(ValueError, match="invalid issue ID"):
        _parse_issue_number("-5")


# ── _detect_repo ──────────────────────────────────────────────────────────────


def test_detect_repo_returns_name(tmp_path) -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _gh_result("takkub/agent-takkub")
        repo = _detect_repo(cwd=tmp_path)
    assert repo == "takkub/agent-takkub"


def test_detect_repo_missing_gh_raises() -> None:
    with patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="gh CLI not found"):
            _detect_repo()


def test_detect_repo_no_git_remote_raises() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _gh_result("", returncode=1, stderr="not a git repository")
        with pytest.raises(RuntimeError, match="no GitHub remote"):
            _detect_repo()


# ── new_issue ─────────────────────────────────────────────────────────────────


def test_new_issue_calls_gh_create(tmp_path) -> None:
    with patch("agent_takkub.issues._ensure_labels"):
        with patch("agent_takkub.issues._gh") as mock_gh:
            mock_gh.side_effect = [
                "takkub/agent-takkub",  # _detect_repo
                "https://github.com/takkub/agent-takkub/issues/7\n",  # issue create
            ]
            number, url = new_issue("Bug title", "body text", cwd=tmp_path)

    assert number == 7
    assert "issues/7" in url


def test_new_issue_empty_title_raises() -> None:
    with pytest.raises(ValueError, match="title must not be empty"):
        new_issue("", "body")


def test_new_issue_invalid_severity_raises() -> None:
    with pytest.raises(ValueError, match="severity"):
        new_issue("title", "body", severity="critical")


def test_new_issue_builds_correct_labels() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _gh_result("takkub/agent-takkub")
        with patch("agent_takkub.issues._ensure_labels") as mock_ensure:
            with patch("agent_takkub.issues._gh") as mock_gh:
                mock_gh.side_effect = [
                    "takkub/agent-takkub",  # _detect_repo
                    "https://github.com/takkub/agent-takkub/issues/5\n",  # create
                ]
                new_issue(
                    "title",
                    "body",
                    severity="high",
                    noticed_in="unirecon",
                    role="backend",
                    tags=["cockpit"],
                )
        labels_arg = mock_ensure.call_args[0][0]
    assert "severity:high" in labels_arg
    assert "role:backend" in labels_arg
    assert "noticed-in:unirecon" in labels_arg
    assert "cockpit" in labels_arg


# ── list_issues ───────────────────────────────────────────────────────────────


def _gh_issue_list_json(items) -> str:
    return json.dumps(items)


def _sample_gh_issue(number=1, title="Sample", state="OPEN", labels=None):
    return {
        "number": number,
        "title": title,
        "state": state,
        "labels": [{"name": lb} for lb in (labels or ["severity:med"])],
        "url": f"https://github.com/takkub/agent-takkub/issues/{number}",
        "createdAt": "2026-05-26T12:00:00Z",
        "closedAt": None,
    }


def test_list_issues_open_filter() -> None:
    gh_json = _gh_issue_list_json([_sample_gh_issue(1, "Open bug", "OPEN")])
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", gh_json]
        items = list_issues(filter_open=True)
    assert len(items) == 1
    assert items[0]["status"] == "open"


def test_list_issues_closed_filter() -> None:
    gh_json = _gh_issue_list_json([_sample_gh_issue(2, "Closed bug", "CLOSED")])
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", gh_json]
        list_issues(filter_closed=True)

    # gh list --state closed call must include '--state closed'
    list_call_args = mock_gh.call_args_list[1]
    assert "closed" in list_call_args[0]


def test_list_issues_severity_filter() -> None:
    gh_json = _gh_issue_list_json([_sample_gh_issue(3, "High bug", labels=["severity:high"])])
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", gh_json]
        items = list_issues(severity="high")
    assert items[0]["severity"] == "high"


def test_list_issues_empty_returns_empty() -> None:
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", "[]"]
        items = list_issues()
    assert items == []


def test_list_issues_role_label_passed() -> None:
    gh_json = _gh_issue_list_json([])
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", gh_json]
        list_issues(role="frontend")
    list_call_args = mock_gh.call_args_list[1][0]
    assert "--label" in list_call_args
    assert "role:frontend" in list_call_args


def test_list_issues_noticed_in_label_passed() -> None:
    gh_json = _gh_issue_list_json([])
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", gh_json]
        list_issues(noticed_in="unirecon")
    list_call_args = mock_gh.call_args_list[1][0]
    assert "noticed-in:unirecon" in list_call_args


# ── close_issue ───────────────────────────────────────────────────────────────


def test_close_issue_calls_gh_close() -> None:
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", ""]  # repo + close
        url = close_issue("42")
    assert "issues/42" in url
    close_args = mock_gh.call_args_list[1][0]
    assert "close" in close_args
    assert "42" in close_args


def test_close_issue_with_note_adds_comment() -> None:
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", ""]
        close_issue("10", note="fixed in commit abc")
    close_call = mock_gh.call_args_list[1][0]
    assert "--comment" in close_call
    assert "fixed in commit abc" in close_call


def test_close_issue_invalid_id_raises() -> None:
    with pytest.raises(ValueError, match="invalid issue ID"):
        close_issue("bad-id")


# ── show_issue ────────────────────────────────────────────────────────────────


def test_show_issue_calls_gh_view() -> None:
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", "issue body here"]
        content = show_issue("5")
    assert content == "issue body here"
    view_call = mock_gh.call_args_list[1][0]
    assert "view" in view_call
    assert "5" in view_call


def test_show_issue_invalid_id_raises() -> None:
    with pytest.raises(ValueError, match="invalid issue ID"):
        show_issue("20260522-001")


# ── label auto-create ─────────────────────────────────────────────────────────


def test_ensure_label_ignores_already_exists() -> None:
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = RuntimeError("already exists")
        # Should not raise
        _ensure_label("severity:high", "#d73a4a", "owner/repo")


def test_ensure_label_raises_other_errors() -> None:
    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = RuntimeError("network error")
        with pytest.raises(RuntimeError, match="network error"):
            _ensure_label("severity:high", "#d73a4a", "owner/repo")


# ── missing gh CLI (falls back to local issues) ───────────────────────────────


def test_missing_gh_cli_falls_back_to_local(tmp_path) -> None:
    local_json = tmp_path / ".takkub_issues.json"
    with patch("shutil.which", return_value=None):
        number, url = new_issue("local title", "local body", cwd=tmp_path, cockpit_bug=False)
    assert number == 1
    assert url == "local://issue/1"
    assert local_json.exists()


# ── #12: gh timeout + visible local-fallback warning ──────────────────────────


def test_gh_passes_timeout_to_subprocess() -> None:
    from agent_takkub.issues import _gh

    with patch("shutil.which", return_value="/usr/bin/gh"):
        with patch("subprocess.run", return_value=_gh_result(stdout="ok")) as mock_run:
            _gh("issue", "list", timeout=42)
    assert mock_run.call_args.kwargs["timeout"] == 42


def test_gh_default_timeout_is_bounded() -> None:
    from agent_takkub.issues import _gh

    with patch("shutil.which", return_value="/usr/bin/gh"):
        with patch("subprocess.run", return_value=_gh_result(stdout="ok")) as mock_run:
            _gh("issue", "view", "1")
    # Never unbounded — a stalled gh must not block forever (issue #12).
    assert mock_run.call_args.kwargs["timeout"] > 0


def test_gh_timeout_raises_runtimeerror() -> None:
    import subprocess as _sp

    from agent_takkub.issues import _gh

    with patch("shutil.which", return_value="/usr/bin/gh"):
        with patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="gh", timeout=30)):
            with pytest.raises(RuntimeError, match="timed out"):
                _gh("issue", "list")


def test_new_issue_transient_gh_failure_warns_and_falls_back(tmp_path, capsys) -> None:
    # Repo detected but `gh issue create` fails (network/auth) → dangerous
    # silent divergence; must fall back to local AND warn on stderr.
    with patch("agent_takkub.issues._ensure_labels"):
        with patch("agent_takkub.issues._gh") as mock_gh:
            mock_gh.side_effect = ["takkub/agent-takkub", RuntimeError("503 server error")]
            _, url = new_issue("transient title", "body", cwd=tmp_path, cockpit_bug=False)
    assert url == "local://issue/1"
    assert "gh unavailable" in capsys.readouterr().err


def test_new_issue_no_remote_falls_back_quietly(tmp_path, capsys) -> None:
    # A genuine no-GitHub-remote project is legit local mode — no scary warning.
    with patch("agent_takkub.issues._detect_repo", side_effect=RuntimeError("no remote")):
        _, url = new_issue("local title", "body", cwd=tmp_path, cockpit_bug=False)
    assert url == "local://issue/1"
    assert "gh unavailable" not in capsys.readouterr().err


# ── cmd_* handlers (CLI layer) ────────────────────────────────────────────────


def test_cmd_new_creates_issue() -> None:
    with patch(
        "agent_takkub.issues.new_issue", return_value=(7, "https://github.com/owner/repo/issues/7")
    ):
        args = _args(title="cmd title", body="cmd body")
        resp = cmd_issue_new(args)
    assert resp["ok"] is True
    assert "7" in resp["msg"]


def test_cmd_new_no_body_no_tty_errors(monkeypatch) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    args = _args(title="t", body=None)
    resp = cmd_issue_new(args)
    assert resp["ok"] is False
    assert "no --body" in resp["msg"]


def test_cmd_new_gh_error_returns_error() -> None:
    with patch("agent_takkub.issues.new_issue", side_effect=RuntimeError("no remote")):
        args = _args(title="t", body="b")
        resp = cmd_issue_new(args)
    assert resp["ok"] is False
    assert "no remote" in resp["msg"]


def test_cmd_new_issues_dir_deprecated_warns(capsys) -> None:
    with patch(
        "agent_takkub.issues.new_issue", return_value=(1, "https://github.com/o/r/issues/1")
    ):
        args = _args(title="t", body="b", issues_dir="/old/path")
        cmd_issue_new(args)
    captured = capsys.readouterr()
    assert "deprecated" in captured.err


def test_cmd_list_output(capsys) -> None:
    items = [
        {
            "number": 3,
            "title": "some bug",
            "status": "open",
            "severity": "high",
            "role": "backend",
            "noticed_in": "",
            "tags": [],
            "url": "",
        }
    ]
    with patch("agent_takkub.issues.list_issues", return_value=items):
        args = _args()
        resp = cmd_issue_list(args)
    assert resp["ok"] is True
    captured = capsys.readouterr()
    assert "some bug" in captured.out


def test_cmd_list_empty(capsys) -> None:
    with patch("agent_takkub.issues.list_issues", return_value=[]):
        resp = cmd_issue_list(_args())
    assert resp["ok"] is True
    captured = capsys.readouterr()
    assert "no issues" in captured.out


def test_cmd_close_success(capsys) -> None:
    with patch("agent_takkub.issues.close_issue", return_value="https://github.com/o/r/issues/5"):
        args = _args(id="5", note="fixed")
        resp = cmd_issue_close(args)
    assert resp["ok"] is True
    captured = capsys.readouterr()
    assert "issues/5" in captured.out


def test_cmd_close_invalid_id_returns_error() -> None:
    args = _args(id="bad-format")
    resp = cmd_issue_close(args)
    assert resp["ok"] is False
    assert "invalid issue ID" in resp["msg"]


def test_cmd_show_success(capsys) -> None:
    with patch("agent_takkub.issues.show_issue", return_value="Issue content here"):
        args = _args(id="10")
        resp = cmd_issue_show(args)
    assert resp["ok"] is True
    captured = capsys.readouterr()
    assert "Issue content here" in captured.out


def test_cmd_show_invalid_id_returns_error() -> None:
    args = _args(id="not-a-number")
    resp = cmd_issue_show(args)
    assert resp["ok"] is False
    assert "invalid issue ID" in resp["msg"]


# ── auto-detect repo from cwd ─────────────────────────────────────────────────


def test_new_issue_no_cockpit_bug_passes_cwd_to_detect_repo(tmp_path) -> None:
    """With cockpit_bug=False (explicit opt-out) routing follows cwd again."""
    detected_cwds: list = []

    def fake_detect_repo(cwd=None):
        detected_cwds.append(cwd)
        return "owner/repo"

    with patch("agent_takkub.issues._detect_repo", side_effect=fake_detect_repo):
        with patch("agent_takkub.issues._ensure_labels"):
            with patch(
                "agent_takkub.issues._gh", return_value="https://github.com/owner/repo/issues/1"
            ):
                new_issue("t", "b", cwd=str(tmp_path), cockpit_bug=False)

    assert str(tmp_path) in str(detected_cwds[0])


def test_new_issue_cockpit_bug_overrides_cwd_to_repo_root() -> None:
    """`cockpit_bug=True` must route gh issue create to REPO_ROOT's remote
    instead of the caller's cwd. Regression guard: bug-check broadcasts
    fired from a pms-api pane must NOT file against the pms-api repo —
    cockpit/orchestrator/CLI bugs always go to agent-takkub.
    """
    from agent_takkub.config import REPO_ROOT

    detected_cwds: list = []

    def fake_detect_repo(cwd=None):
        detected_cwds.append(str(cwd) if cwd is not None else None)
        return "takkub/agent-takkub"

    with patch("agent_takkub.issues._detect_repo", side_effect=fake_detect_repo):
        with patch("agent_takkub.issues._ensure_labels"):
            with patch(
                "agent_takkub.issues._gh",
                return_value="https://github.com/takkub/agent-takkub/issues/42",
            ):
                new_issue(
                    "cockpit bug",
                    "body",
                    cwd="/unrelated/pms-api/path",
                    cockpit_bug=True,
                )

    assert detected_cwds == [str(REPO_ROOT)]


def test_new_issue_default_routes_to_agent_takkub_repo(tmp_path) -> None:
    """cockpit_bug now defaults to True — issues land on the agent-takkub repo
    (REPO_ROOT) regardless of cwd, so a forgotten flag can't leak a cockpit
    bug onto another project's repo. This is the fix for issues filed against
    other projects when they should only be agent-takkub bugs."""
    from agent_takkub.config import REPO_ROOT

    detected_cwds: list = []

    def fake_detect_repo(cwd=None):
        detected_cwds.append(str(cwd) if cwd is not None else None)
        return "takkub/agent-takkub"

    with patch("agent_takkub.issues._detect_repo", side_effect=fake_detect_repo):
        with patch("agent_takkub.issues._ensure_labels"):
            with patch(
                "agent_takkub.issues._gh",
                return_value="https://github.com/takkub/agent-takkub/issues/1",
            ):
                # cwd points at another project, but default routing ignores it
                new_issue("cockpit bug", "body", cwd=str(tmp_path))

    assert detected_cwds == [str(REPO_ROOT)]


# ── installed-build local-fallback redirect (issues.py, DATA_HOME vs REPO_ROOT) ──


def test_local_store_cwd_passthrough_when_dev_checkout(tmp_path, monkeypatch) -> None:
    """DATA_HOME == REPO_ROOT in a dev checkout, so redirecting is a no-op —
    any cwd that isn't REPO_ROOT stays untouched."""
    from agent_takkub.issues import _local_store_cwd

    assert _local_store_cwd(str(tmp_path)) == str(tmp_path)
    assert _local_store_cwd(None) is None


def test_local_store_cwd_redirects_repo_root_to_data_home(tmp_path, monkeypatch) -> None:
    """Installed build: REPO_ROOT resolves into a throwaway venv ancestor —
    the local-fallback JSON must redirect to DATA_HOME instead so it survives
    a `pip install --upgrade` (docs/audit/2026-07-05-installed-build-audit-gemini.md,
    finding 3)."""
    from agent_takkub.issues import _local_store_cwd

    fake_repo_root = tmp_path / "venv" / "Lib"
    fake_repo_root.mkdir(parents=True)
    fake_data_home = tmp_path / "agent-takkub-home"
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_data_home)

    assert _local_store_cwd(str(fake_repo_root)) == fake_data_home
    # An unrelated cwd (active project pane) is left alone.
    other = tmp_path / "some-other-project"
    assert _local_store_cwd(str(other)) == str(other)


def test_new_issue_cockpit_bug_local_fallback_writes_to_data_home(tmp_path, monkeypatch) -> None:
    """End-to-end: cockpit_bug=True + gh unavailable + installed build → the
    local .takkub_issues.json lands under DATA_HOME, not the venv ancestor
    REPO_ROOT resolves to."""
    fake_repo_root = tmp_path / "venv" / "Lib"
    fake_repo_root.mkdir(parents=True)
    fake_data_home = tmp_path / "agent-takkub-home"
    fake_data_home.mkdir()
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_data_home)

    with patch("agent_takkub.issues._detect_repo", side_effect=RuntimeError("no gh")):
        number, _url = new_issue("installed cockpit bug", "body", cockpit_bug=True)

    assert number == 1
    assert (fake_data_home / ".takkub_issues.json").exists()
    assert not (fake_repo_root / ".takkub_issues.json").exists()


# ── --issues-dir CLI backward compat ─────────────────────────────────────────


def test_cli_issue_new_defaults_to_cockpit_bug(monkeypatch) -> None:
    """`takkub issue new` with no flag → cockpit_bug=True (agent-takkub repo)."""
    import sys

    from agent_takkub import cli

    captured: dict = {}

    def fake_new_issue(title, body, **kw):
        captured.update(kw)
        return (1, "https://github.com/takkub/agent-takkub/issues/1")

    with patch("agent_takkub.issues.new_issue", side_effect=fake_new_issue):
        monkeypatch.setattr(sys, "argv", ["takkub", "issue", "new", "t", "--body", "b"])
        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code == 0, f"CLI exited {exc.code}"
    assert captured.get("cockpit_bug") is True


def test_cli_issue_new_no_cockpit_bug_opt_out(monkeypatch) -> None:
    """`--no-cockpit-bug` opts back into cwd-based (active project) routing."""
    import sys

    from agent_takkub import cli

    captured: dict = {}

    def fake_new_issue(title, body, **kw):
        captured.update(kw)
        return (1, "https://github.com/owner/repo/issues/1")

    with patch("agent_takkub.issues.new_issue", side_effect=fake_new_issue):
        monkeypatch.setattr(
            sys, "argv", ["takkub", "issue", "new", "t", "--body", "b", "--no-cockpit-bug"]
        )
        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code == 0, f"CLI exited {exc.code}"
    assert captured.get("cockpit_bug") is False


def test_issues_dir_flag_cli_deprecated(tmp_path, monkeypatch, capsys) -> None:
    """--issues-dir must still parse without error, just emit a deprecation warning."""
    import sys

    from agent_takkub import cli

    with patch("agent_takkub.issues.list_issues", return_value=[]):
        monkeypatch.setattr(sys, "argv", ["takkub", "issue", "list", "--issues-dir", str(tmp_path)])
        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code == 0, f"CLI exited {exc.code}"
    captured = capsys.readouterr()
    assert "unrecognized" not in captured.err
