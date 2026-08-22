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
    # #341's write-confirmation check (cli.py) forces ok=False on a write-
    # bucketed command ("issue" covers show/list/new/close) whose response has
    # no `msg` and no `quiet` — show's confirmation is the printed content
    # above, not `msg`, so it must set `quiet` or every `issue show` reports
    # false failure despite succeeding.
    assert resp.get("quiet") is True


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


def test_local_store_cwd_passthrough_for_project_ops(tmp_path) -> None:
    """cockpit_bug=False (project-scoped ops) keep using the caller's cwd
    unchanged, whatever it is."""
    from agent_takkub.issues import _local_store_cwd

    assert _local_store_cwd(str(tmp_path), cockpit_bug=False) == str(tmp_path)
    assert _local_store_cwd(None, cockpit_bug=False) is None


def test_local_store_cwd_redirects_cockpit_bug_to_data_home(tmp_path, monkeypatch) -> None:
    """Installed build: REPO_ROOT resolves into a throwaway venv ancestor —
    the local-fallback JSON must redirect to DATA_HOME instead so it survives
    a `pip install --upgrade` (docs/audit/2026-07-05-installed-build-audit-gemini.md,
    finding 3), regardless of what path `_cockpit_repo_cwd()` actually
    resolved to (issue #237 — that's no longer guaranteed to be REPO_ROOT)."""
    from agent_takkub.issues import _local_store_cwd

    fake_repo_root = tmp_path / "venv" / "Lib"
    fake_repo_root.mkdir(parents=True)
    fake_data_home = tmp_path / "agent-takkub-home"
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_data_home)

    assert _local_store_cwd(str(fake_repo_root), cockpit_bug=True) == fake_data_home
    # Even an unrelated detect_cwd (real checkout path from projects.json)
    # still redirects to DATA_HOME — the flag decides, not path equality.
    other = tmp_path / "some-other-checkout"
    assert _local_store_cwd(str(other), cockpit_bug=True) == fake_data_home
    # An unrelated cwd for a project-scoped op is left alone.
    assert _local_store_cwd(str(other), cockpit_bug=False) == str(other)


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

    with patch("agent_takkub.issues.load_projects", return_value={}):
        with patch("agent_takkub.issues._detect_repo", side_effect=RuntimeError("no gh")):
            number, _url = new_issue("installed cockpit bug", "body", cockpit_bug=True)

    assert number == 1
    assert (fake_data_home / ".takkub_issues.json").exists()
    assert not (fake_repo_root / ".takkub_issues.json").exists()


# ── #237: cockpit-bug repo resolution on an installed build ──────────────────


def test_cockpit_repo_cwd_dev_checkout_returns_repo_root() -> None:
    """DATA_HOME == REPO_ROOT (dev checkout) → REPO_ROOT unchanged, matching
    every pre-#237 test's assumption."""
    from agent_takkub.config import REPO_ROOT
    from agent_takkub.issues import _cockpit_repo_cwd

    assert _cockpit_repo_cwd() == REPO_ROOT


def test_cockpit_repo_cwd_installed_build_uses_env_override(tmp_path, monkeypatch) -> None:
    """AGENT_TAKKUB_COCKPIT_REPO takes priority when it points at a real
    git checkout."""
    from agent_takkub.issues import _cockpit_repo_cwd

    fake_repo_root = tmp_path / "venv" / "Lib"
    fake_repo_root.mkdir(parents=True)
    fake_data_home = tmp_path / "agent-takkub-home"
    checkout = tmp_path / "my-checkout"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_data_home)
    monkeypatch.setenv("AGENT_TAKKUB_COCKPIT_REPO", str(checkout))

    assert _cockpit_repo_cwd() == checkout


def test_cockpit_repo_cwd_installed_build_falls_back_to_projects_json(
    tmp_path, monkeypatch
) -> None:
    """No env override → fall back to the user's own projects.json
    "agent-takkub" entry (issue #237, reproduces the exact fix: this repo's
    real checkout path is already correctly recorded there)."""
    from agent_takkub.issues import _cockpit_repo_cwd

    fake_repo_root = tmp_path / "venv" / "Lib"
    fake_repo_root.mkdir(parents=True)
    fake_data_home = tmp_path / "agent-takkub-home"
    checkout = tmp_path / "WebstormProjects" / "agent-takkub"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_data_home)
    monkeypatch.delenv("AGENT_TAKKUB_COCKPIT_REPO", raising=False)

    fake_projects = {"projects": {"agent-takkub": {"paths": {"main": str(checkout)}}}}
    with patch("agent_takkub.issues.load_projects", return_value=fake_projects):
        assert _cockpit_repo_cwd() == checkout


def test_cockpit_repo_cwd_installed_build_returns_none_when_unresolved(
    tmp_path, monkeypatch
) -> None:
    """Nothing resolves (no env override, no matching/valid projects.json
    entry) → None, never a silent reuse of the venv REPO_ROOT path."""
    from agent_takkub.issues import _cockpit_repo_cwd

    fake_repo_root = tmp_path / "venv" / "Lib"
    fake_repo_root.mkdir(parents=True)
    fake_data_home = tmp_path / "agent-takkub-home"
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_data_home)
    monkeypatch.delenv("AGENT_TAKKUB_COCKPIT_REPO", raising=False)

    with patch("agent_takkub.issues.load_projects", return_value={}):
        assert _cockpit_repo_cwd() is None


def test_new_issue_cockpit_bug_with_no_checkout_targets_the_upstream_tracker(
    tmp_path, monkeypatch
) -> None:
    """#297 replaces #237's behaviour here.

    #237 made an unresolvable checkout warn loudly and fall into the local
    store. That was the best available answer while the repo had to be derived
    from a git remote — but it means a plain `npm i -g` install (no checkout,
    no env override, no agent-takkub project registered) files every cockpit
    bug into a JSON file nobody outside that machine ever sees. The tracker is
    a fixed repo, so it is now used directly and the local store is only a
    fallback for when `gh` itself can't run.
    """
    fake_repo_root = tmp_path / "venv" / "Lib"
    fake_repo_root.mkdir(parents=True)
    fake_data_home = tmp_path / "agent-takkub-home"
    fake_data_home.mkdir()
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_data_home)
    monkeypatch.delenv("AGENT_TAKKUB_COCKPIT_REPO", raising=False)
    monkeypatch.delenv("AGENT_TAKKUB_COCKPIT_REPO_SLUG", raising=False)

    with patch("agent_takkub.issues.load_projects", return_value={}):
        with patch("agent_takkub.issues._gh") as mock_gh:
            mock_gh.return_value = "https://github.com/takkub/agent-takkub/issues/42"
            number, url = new_issue("cockpit bug", "body", cockpit_bug=True)

    assert number == 42
    assert url.endswith("/42")
    create_call = next(c for c in mock_gh.call_args_list if c[0][:2] == ("issue", "create"))
    assert "takkub/agent-takkub" in create_call[0]


def test_cockpit_repo_slug_is_env_overridable(monkeypatch) -> None:
    """A fork retargets its own tracker without touching code."""
    from agent_takkub.issues import cockpit_repo_slug

    monkeypatch.delenv("AGENT_TAKKUB_COCKPIT_REPO_SLUG", raising=False)
    assert cockpit_repo_slug() == "takkub/agent-takkub"
    monkeypatch.setenv("AGENT_TAKKUB_COCKPIT_REPO_SLUG", "someone/their-fork")
    assert cockpit_repo_slug() == "someone/their-fork"


def test_mutating_gh_is_refused_in_a_test_process() -> None:
    """The guard that had to exist after this change filed 3 junk issues.

    Read-only calls stay allowed — they cannot pollute a tracker — so only
    the mutating subcommands are blocked.
    """
    from agent_takkub import issues as issues_mod

    assert issues_mod.in_test_or_ci_process() is True
    assert issues_mod._is_mutating_gh(("issue", "create")) is True
    assert issues_mod._is_mutating_gh(("label", "create")) is True
    assert issues_mod._is_mutating_gh(("issue", "list")) is False
    assert issues_mod._is_mutating_gh(("repo", "view")) is False

    with pytest.raises(RuntimeError, match="refusing to run mutating"):
        issues_mod._gh("issue", "create", "--repo", "takkub/agent-takkub", "--title", "x")


def test_real_issue_write_escape_hatch(monkeypatch) -> None:
    """An explicit env flag re-enables real writes for a manual smoke test."""
    from agent_takkub import issues as issues_mod

    monkeypatch.setenv("TAKKUB_ALLOW_REAL_ISSUE_WRITE", "1")
    assert issues_mod.in_test_or_ci_process() is False


def test_cmd_issue_new_flags_local_only_in_msg() -> None:
    """CLI reply must not read as a plain success when it silently landed in
    the local store (issue #237 item 3) — `url` is the source of truth."""
    with patch("agent_takkub.issues.new_issue", return_value=(30, "local://issue/30")):
        args = _args(title="t", body="b")
        resp = cmd_issue_new(args)
    assert resp["ok"] is True
    assert "LOCAL ONLY" in resp["msg"]


def test_cmd_issue_new_no_local_tag_on_real_github_issue() -> None:
    with patch(
        "agent_takkub.issues.new_issue",
        return_value=(7, "https://github.com/takkub/agent-takkub/issues/7"),
    ):
        args = _args(title="t", body="b")
        resp = cmd_issue_new(args)
    assert "LOCAL ONLY" not in resp["msg"]


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


# ── #142: list/close/show must read the same store `new` writes to ───────────


def test_list_issues_default_cockpit_bug_routes_to_repo_root(tmp_path) -> None:
    """`list_issues()` with no flag must detect the repo at REPO_ROOT, exactly
    like `new_issue()` — regardless of a caller-supplied cwd pointing at some
    other pane's directory."""
    from agent_takkub.config import REPO_ROOT

    detected_cwds: list = []

    def fake_gh(*args, cwd=None, **kwargs):
        detected_cwds.append(str(cwd) if cwd is not None else None)
        if args[:2] == ("repo", "view"):
            return "takkub/agent-takkub"
        return "[]"

    with patch("agent_takkub.issues._gh", side_effect=fake_gh):
        list_issues(cwd=str(tmp_path))

    assert detected_cwds[0] == str(REPO_ROOT)


def test_list_issues_no_cockpit_bug_uses_cwd(tmp_path) -> None:
    """`--no-cockpit-bug` opts back into cwd-based repo detection."""
    detected_cwds: list = []

    def fake_gh(*args, cwd=None, **kwargs):
        detected_cwds.append(str(cwd) if cwd is not None else None)
        if args[:2] == ("repo", "view"):
            return "owner/repo"
        return "[]"

    with patch("agent_takkub.issues._gh", side_effect=fake_gh):
        list_issues(cwd=str(tmp_path), cockpit_bug=False)

    assert detected_cwds[0] == str(tmp_path)


def test_close_issue_default_cockpit_bug_routes_to_repo_root() -> None:
    from agent_takkub.config import REPO_ROOT

    detected_cwds: list = []

    def fake_gh(*args, cwd=None, **kwargs):
        detected_cwds.append(str(cwd) if cwd is not None else None)
        if args[:2] == ("repo", "view"):
            return "takkub/agent-takkub"
        return ""

    with patch("agent_takkub.issues._gh", side_effect=fake_gh):
        close_issue("5", cwd="/some/other/pane/dir")

    assert detected_cwds[0] == str(REPO_ROOT)


def test_show_issue_default_cockpit_bug_routes_to_repo_root() -> None:
    from agent_takkub.config import REPO_ROOT

    detected_cwds: list = []

    def fake_gh(*args, cwd=None, **kwargs):
        detected_cwds.append(str(cwd) if cwd is not None else None)
        if args[:2] == ("repo", "view"):
            return "takkub/agent-takkub"
        return "issue content"

    with patch("agent_takkub.issues._gh", side_effect=fake_gh):
        show_issue("5", cwd="/some/other/pane/dir")

    assert detected_cwds[0] == str(REPO_ROOT)


def test_new_then_list_symmetry_local_fallback(tmp_path, monkeypatch) -> None:
    """End-to-end regression for #142: an issue filed with the default
    cockpit_bug=True (gh unavailable → local store) must be found by
    list_issues() with the default flags too, no matter what cwd the
    *listing* pane happens to be in — this is the exact bug: `new` and
    `list` reading two different local-store files."""
    fake_repo_root = tmp_path / "cockpit-checkout"
    fake_repo_root.mkdir()
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_repo_root)

    other_pane_cwd = tmp_path / "some-project-pane"
    other_pane_cwd.mkdir()

    with patch("agent_takkub.issues._detect_repo", side_effect=RuntimeError("no gh")):
        number, _url = new_issue("cockpit bug found in pane", "body", cwd=str(other_pane_cwd))

    with patch("agent_takkub.issues._detect_repo", side_effect=RuntimeError("no gh")):
        # Listing from a totally different pane cwd — must still see it.
        found = list_issues(cwd=str(other_pane_cwd / "nested" / "dir"))

    assert any(iss["number"] == number for iss in found)


def test_cmd_list_shows_cockpit_scope(capsys) -> None:
    with patch("agent_takkub.issues.list_issues", return_value=[]):
        cmd_issue_list(_args())
    captured = capsys.readouterr()
    assert "scope: cockpit" in captured.out


def test_cmd_list_shows_project_scope_on_no_cockpit_bug(capsys) -> None:
    with patch("agent_takkub.issues.list_issues", return_value=[]):
        cmd_issue_list(_args(cockpit_bug=False, cwd="/my/project"))
    captured = capsys.readouterr()
    assert "scope: active project" in captured.out
    assert "/my/project" in captured.out


def test_cli_issue_list_no_cockpit_bug_opt_out(monkeypatch) -> None:
    import sys

    from agent_takkub import cli

    captured: dict = {}

    def fake_list_issues(**kw):
        captured.update(kw)
        return []

    with patch("agent_takkub.issues.list_issues", side_effect=fake_list_issues):
        monkeypatch.setattr(sys, "argv", ["takkub", "issue", "list", "--no-cockpit-bug"])
        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code == 0, f"CLI exited {exc.code}"
    assert captured.get("cockpit_bug") is False


def test_cli_issue_close_defaults_to_cockpit_bug(monkeypatch) -> None:
    import sys

    from agent_takkub import cli

    captured: dict = {}

    def fake_close_issue(issue_id, **kw):
        captured.update(kw)
        return "https://github.com/takkub/agent-takkub/issues/1"

    with patch("agent_takkub.issues.close_issue", side_effect=fake_close_issue):
        monkeypatch.setattr(sys, "argv", ["takkub", "issue", "close", "1"])
        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code == 0, f"CLI exited {exc.code}"
    assert captured.get("cockpit_bug") is True


# ── #174: gh-recovered list must not silently drop the local backlog ────────


def test_list_issues_merges_unreconciled_local_backlog(tmp_path, monkeypatch, capsys) -> None:
    """gh was down when `new_issue` wrote to the local fallback store; gh has
    since recovered. `list_issues()` must still surface those records instead
    of reporting '(no issues)' just because the GitHub query came back empty."""
    fake_repo_root = tmp_path / "cockpit-checkout"
    fake_repo_root.mkdir()
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_repo_root)

    local_path = fake_repo_root / ".takkub_issues.json"
    local_path.write_text(
        json.dumps(
            [
                {
                    "number": 1,
                    "title": "stranded local issue",
                    "status": "open",
                    "severity": "med",
                    "role": "",
                    "noticed_in": "",
                    "tags": [],
                    "url": "local://issue/1",
                    "created_at": "2026-08-04T08:37:31Z",
                    "closed_at": "",
                }
            ]
        ),
        encoding="utf-8",
    )

    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", "[]"]  # gh now works, but empty
        items = list_issues()

    assert any(i["number"] == 1 and i["title"] == "stranded local issue" for i in items)
    assert "unreconciled local issue" in capsys.readouterr().err


def test_list_issues_no_backlog_no_warning(tmp_path, monkeypatch, capsys) -> None:
    """No local fallback file at all → no spurious warning, normal empty gh result."""
    fake_repo_root = tmp_path / "cockpit-checkout"
    fake_repo_root.mkdir()
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_repo_root)

    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", "[]"]
        items = list_issues()

    assert items == []
    assert "unreconciled" not in capsys.readouterr().err


def test_list_issues_backlog_respects_filters(tmp_path, monkeypatch) -> None:
    """Local backlog merge must honour the same filters as the gh-path query —
    a closed local issue must not leak into an --open listing."""
    fake_repo_root = tmp_path / "cockpit-checkout"
    fake_repo_root.mkdir()
    monkeypatch.setattr("agent_takkub.issues.REPO_ROOT", fake_repo_root)
    monkeypatch.setattr("agent_takkub.issues.DATA_HOME", fake_repo_root)

    local_path = fake_repo_root / ".takkub_issues.json"
    local_path.write_text(
        json.dumps(
            [
                {"number": 1, "title": "closed one", "status": "closed", "severity": "med"},
                {"number": 2, "title": "open one", "status": "open", "severity": "med"},
            ]
        ),
        encoding="utf-8",
    )

    with patch("agent_takkub.issues._gh") as mock_gh:
        mock_gh.side_effect = ["takkub/agent-takkub", "[]"]
        items = list_issues(filter_open=True)

    numbers = [i["number"] for i in items]
    assert 2 in numbers
    assert 1 not in numbers


def test_cli_issue_show_defaults_to_cockpit_bug(monkeypatch) -> None:
    import sys

    from agent_takkub import cli

    captured: dict = {}

    def fake_show_issue(issue_id, **kw):
        captured.update(kw)
        return "content"

    with patch("agent_takkub.issues.show_issue", side_effect=fake_show_issue):
        monkeypatch.setattr(sys, "argv", ["takkub", "issue", "show", "1"])
        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code == 0, f"CLI exited {exc.code}"
    assert captured.get("cockpit_bug") is True
