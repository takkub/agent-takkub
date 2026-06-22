"""Tests for path traversal protection in vault AND local runtime writes.

Covers S-1 (HIGH) finding: project name used as path component without
validate_name() in _save_decision_note, end_session, and write_resume_briefs.

Round-2 scope: also verifies LOCAL RUNTIME_DIR writes are guarded (not just vault).
"""

from __future__ import annotations

import pathlib
from datetime import datetime
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "testproj"

TRAVERSAL_PAYLOADS = [
    "../etc",
    "..\\windows",
    "normal/sub",
    "...",
    "../../../sensitive",
    "/absolute",
    "has space",
]


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(
    qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _assert_no_traversal_in_dir(base: pathlib.Path, payload: str) -> None:
    """Assert the payload string doesn't appear in any path under base."""
    if not base.exists():
        return
    for f in base.rglob("*"):
        assert payload not in str(f), f"Traversal payload {payload!r} leaked into path: {f}"


def _assert_sensitive_not_created(cwd: pathlib.Path) -> None:
    """Assert the known traversal target 'sensitive/' wasn't created at cwd."""
    sensitive = cwd / "sensitive"
    assert not sensitive.exists(), (
        f"'sensitive/' dir was created at {sensitive} — path traversal not blocked!"
    )


class TestSaveDecisionNoteRejectsTraversal:
    """_save_decision_note must not write to vault OR local runtime when project name is unsafe."""

    @pytest.mark.parametrize("bad_project", TRAVERSAL_PAYLOADS)
    def test_no_write_for_traversal_payload(
        self,
        bad_project: str,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = tmp_path / "runtime"
        vault = tmp_path / "fake-vault"
        vault.mkdir()
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)

        cwd = pathlib.Path.cwd()
        _assert_sensitive_not_created(cwd)  # baseline

        now = datetime(2026, 1, 1, 12, 0, 0)
        Orchestrator._save_decision_note(
            project=bad_project,
            role="backend",
            note="some meaningful note that would normally be saved",
            now=now,
        )

        # Vault must remain empty.
        all_vault_files = list(vault.rglob("*"))
        assert all_vault_files == [], (
            f"vault write occurred for unsafe project {bad_project!r}: {all_vault_files}"
        )

        # Local runtime must remain empty.
        _assert_no_traversal_in_dir(runtime, bad_project)
        all_runtime = list(runtime.rglob("*")) if runtime.exists() else []
        assert all_runtime == [], (
            f"local runtime write occurred for unsafe project {bad_project!r}: {all_runtime}"
        )

        # 'sensitive/' must not have been created at the repo root.
        _assert_sensitive_not_created(cwd)

    def test_safe_project_name_still_writes(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = tmp_path / "runtime"
        vault = tmp_path / "fake-vault"
        vault.mkdir()
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)

        now = datetime(2026, 1, 1, 12, 0, 0)
        Orchestrator._save_decision_note(
            project="myproject",
            role="backend",
            note="completed the login endpoint with JWT",
            now=now,
        )

        # Vault write happened.
        sessions_dir = vault / "99-Logs" / "sessions" / "myproject"
        assert sessions_dir.is_dir()
        written = list(sessions_dir.glob("*.md"))
        assert len(written) == 1

        # Local runtime write happened.
        local_day = runtime / "sessions" / now.strftime("%Y-%m-%d") / "myproject"
        assert local_day.is_dir()
        local_files = list(local_day.glob("*.md"))
        assert len(local_files) == 1


class TestEndSessionRejectsTraversal:
    """end_session must skip vault AND local write when project_ns is unsafe."""

    @pytest.mark.parametrize("bad_project", TRAVERSAL_PAYLOADS)
    def test_no_write_for_traversal_payload(
        self,
        bad_project: str,
        orch: Orchestrator,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vault = tmp_path / "fake-vault"
        vault.mkdir()
        runtime = tmp_path / "runtime"
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or bad_project),
        )

        cwd = pathlib.Path.cwd()
        _assert_sensitive_not_created(cwd)

        ok, _ = orch.end_session(project=bad_project, note="wrap up")

        # Call must fail (rejected early).
        assert ok is False, f"end_session should have rejected {bad_project!r}"

        # Vault must remain empty.
        all_vault_files = list(vault.rglob("*"))
        assert all_vault_files == [], (
            f"vault write occurred for unsafe project {bad_project!r}: {all_vault_files}"
        )

        # Local runtime must remain empty.
        _assert_no_traversal_in_dir(runtime, bad_project)
        all_runtime = list(runtime.rglob("*")) if runtime.exists() else []
        assert all_runtime == [], (
            f"local runtime write occurred for unsafe project {bad_project!r}: {all_runtime}"
        )

        # 'sensitive/' must not have been created at the repo root.
        _assert_sensitive_not_created(cwd)

    def test_safe_project_vault_written(
        self,
        orch: Orchestrator,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vault = tmp_path / "fake-vault"
        vault.mkdir()
        runtime = tmp_path / "runtime"
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)
        monkeypatch.setattr(
            Orchestrator,
            "_resolve_project",
            staticmethod(lambda project: project or TEST_PROJECT),
        )

        ok, _ = orch.end_session(project=TEST_PROJECT, note="safe project note")
        assert ok is True

        vault_sessions = vault / "99-Logs" / "sessions" / TEST_PROJECT
        assert vault_sessions.is_dir()
        written = list(vault_sessions.glob("*.md"))
        assert len(written) == 1


class TestWriteResumeBriefsRejectsTraversal:
    """write_resume_briefs must skip unsafe project names without writing to vault."""

    @pytest.mark.parametrize("bad_project", TRAVERSAL_PAYLOADS)
    def test_no_vault_write_for_traversal_payload(
        self,
        bad_project: str,
        orch: Orchestrator,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vault = tmp_path / "fake-vault"
        vault.mkdir()
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)

        cwd = pathlib.Path.cwd()
        _assert_sensitive_not_created(cwd)

        # Inject the malicious project name into _panes_by_project.
        orch._panes_by_project[bad_project] = {}

        with patch(
            "agent_takkub.orchestrator.Orchestrator.write_resume_briefs",
            wraps=orch.write_resume_briefs,
        ):
            # Patch build_resume_brief to return content so it would write if not blocked.
            with patch(
                "agent_takkub.chatlog_scanner.build_resume_brief",
                return_value="# Brief content",
            ):
                orch.write_resume_briefs()

        # No file with the traversal payload in its name should exist.
        all_files = list(vault.rglob("*"))
        for f in all_files:
            assert bad_project not in f.name, (
                f"Traversal payload {bad_project!r} appeared in vault path: {f}"
            )

        # 'sensitive/' must not have been created at the repo root.
        _assert_sensitive_not_created(cwd)

        # Clean up so fixture doesn't leak.
        orch._panes_by_project.pop(bad_project, None)

    def test_safe_project_brief_written(
        self,
        orch: Orchestrator,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vault = tmp_path / "fake-vault"
        vault.mkdir()
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: vault)

        orch._panes_by_project["safeproject"] = {}

        with patch(
            "agent_takkub.chatlog_scanner.build_resume_brief",
            return_value="# Resume brief content",
        ):
            orch.write_resume_briefs()

        briefs_dir = vault / "99-Logs" / "briefs"
        written = list(briefs_dir.glob("safeproject-*.md"))
        assert len(written) == 1, "expected one brief file for safe project"

        # Clean up.
        orch._panes_by_project.pop("safeproject", None)
