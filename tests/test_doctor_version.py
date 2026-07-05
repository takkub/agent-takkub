"""takkub doctor — check_version (version-behind reporting for CLI-only users).

check_version pulls its git facts from update_helper; we patch those so the test
is deterministic and offline (no real git/network). Verifies the Finding shape
for each state: not-a-repo, up-to-date, behind, behind+deps, offline, dirty.
"""

from __future__ import annotations

import pytest

from agent_takkub import update_helper
from agent_takkub.doctor import Status, check_version


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_repo: bool = True,
    fetched: bool = True,
    status: dict | None = None,
    describe: str = "v0.8.0-3-gabc1234",
    pyproj: bool = False,
) -> None:
    monkeypatch.setattr(update_helper, "is_git_repo", lambda: is_repo)
    monkeypatch.setattr(
        update_helper, "fetch_remote", lambda timeout=8.0: (fetched, "ok" if fetched else "offline")
    )
    monkeypatch.setattr(update_helper, "current_version_describe", lambda: describe)
    monkeypatch.setattr(update_helper, "pyproject_will_change_on_pull", lambda: pyproj)
    monkeypatch.setattr(
        update_helper,
        "local_status",
        lambda: status or {"ok": True, "clean": True, "ahead": 0, "behind": 0, "dirty_files": []},
    )


def _find(findings, name):
    return next((f for f in findings if f.name == name), None)


def test_not_a_git_repo_is_info(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, is_repo=False)
    monkeypatch.setattr("agent_takkub.config.is_installed_package", lambda: False)
    findings = check_version()
    f = _find(findings, "tracking")
    assert f is not None and f.status is Status.INFO
    assert "not a git checkout" in f.detail
    assert "Enable updates" in f.fix_hint


def test_not_a_git_repo_dev_checkout_suggests_update_chip(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, is_repo=False)
    monkeypatch.setattr("agent_takkub.config.is_installed_package", lambda: False)
    f = _find(check_version(), "tracking")
    assert f is not None
    assert "Enable updates" in f.fix_hint
    assert "npm update" not in f.fix_hint


def test_not_a_git_repo_installed_build_suggests_npm_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installed builds don't have the update chip's git-conversion path — the
    hint must point at npm instead (docs/audit/2026-07-05-installed-build-audit-gemini.md,
    finding 7)."""
    _patch(monkeypatch, is_repo=False)
    monkeypatch.setattr("agent_takkub.config.is_installed_package", lambda: True)
    f = _find(check_version(), "tracking")
    assert f is not None
    assert "npm update -g agent-takkub" in f.fix_hint
    assert "Enable updates" not in f.fix_hint


def test_up_to_date_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    f = _find(check_version(), "current")
    assert f is not None and f.status is Status.OK
    assert "up to date" in f.detail
    assert "v0.8.0-3-gabc1234" in f.detail


def test_behind_is_warn_with_pull_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        status={"ok": True, "clean": True, "ahead": 0, "behind": 3, "dirty_files": []},
    )
    f = _find(check_version(), "behind")
    assert f is not None and f.status is Status.WARN
    assert "3 commits behind" in f.detail
    assert "git pull --ff-only origin main" in f.fix_hint


def test_behind_with_dep_change_mentions_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        status={"ok": True, "clean": True, "ahead": 0, "behind": 1, "dirty_files": []},
        pyproj=True,
    )
    f = _find(check_version(), "behind")
    assert f is not None
    assert "1 commit behind" in f.detail
    assert "pip install -e ." in f.fix_hint


def test_offline_notes_last_known_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        fetched=False,
        status={"ok": True, "clean": True, "ahead": 0, "behind": 2, "dirty_files": []},
    )
    f = _find(check_version(), "behind")
    assert f is not None and "offline" in f.detail


def test_dirty_tree_adds_local_edits_info(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        status={
            "ok": True,
            "clean": False,
            "ahead": 0,
            "behind": 0,
            "dirty_files": ["src/a.py", "src/b.py"],
        },
    )
    findings = check_version()
    edits = _find(findings, "local-edits")
    assert edits is not None and edits.status is Status.INFO
    assert "2 tracked files" in edits.detail


def test_version_check_is_in_run_all(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard: check_version must actually be wired into the doctor run.
    import inspect

    from agent_takkub.doctor import run_all_checks

    assert "check_version" in inspect.getsource(run_all_checks)
