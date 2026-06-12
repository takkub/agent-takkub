"""Tests for `takkub end-session` — Lead session summarizer.

Scenarios:
  1. Happy path: Lead calls end_session → local file written, content correct.
  2. Frontmatter is correct (role=lead, project, date, tags).
  3. Note omitted → default "session ended".
  4. Non-Lead CLI role gate → blocked at cli._enforce_role_gate.
  5. Vault absent → silently skips vault write, local file still written.
  6. Teammate done files present today → listed in body.
  7. Vault present → vault file written.
  8. cli_server end-session from non-lead → rejected.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli import _enforce_role_gate
from agent_takkub.cli_server import CliServer
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "testproj"


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


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
    # Redirect RUNTIME_DIR writes to tmp_path.
    monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", tmp_path / "runtime")
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


class _FakeSock:
    def __init__(self) -> None:
        self._buf = b""

    def write(self, data: bytes) -> None:
        self._buf += data

    def flush(self) -> None:
        pass

    def last_response(self) -> dict:
        line = self._buf.split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8"))

    def reset(self) -> None:
        self._buf = b""


@pytest.fixture
def srv_sock(qapp: QCoreApplication):
    mock_orch = MagicMock()
    mock_orch._lead_token = "test-lead-token"
    mock_orch.end_session.return_value = (
        True,
        "lead session summary written: runtime/sessions/2026-01-01/testproj/lead-120000.md",
    )
    srv = CliServer(mock_orch)
    sock = _FakeSock()
    return srv, sock, mock_orch


# ──────────────────────────────────────────────────────────────
# orchestrator.end_session() unit tests
# ──────────────────────────────────────────────────────────────


class TestEndSession:
    def test_local_file_written(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Happy path: end_session writes a markdown file locally."""
        runtime = tmp_path / "runtime"
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: None)

        ok, msg = orch.end_session(project=TEST_PROJECT, note="wrap up for the day")
        assert ok is True
        assert "lead session summary written" in msg

        # Find the written file.
        session_dir = runtime / "sessions"
        files = list(session_dir.rglob("lead-*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "role: lead" in content
        assert f"project: {TEST_PROJECT}" in content
        assert "wrap up for the day" in content

    def test_frontmatter_correct(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Frontmatter includes role=lead, project, date, tags."""
        runtime = tmp_path / "runtime"
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: None)

        ok, _ = orch.end_session(project=TEST_PROJECT, note="frontmatter test note here")
        assert ok is True

        files = list((runtime / "sessions").rglob("lead-*.md"))
        assert files
        body = files[0].read_text(encoding="utf-8")
        assert "role: lead" in body
        assert f"project: {TEST_PROJECT}" in body
        assert "tags: [session, lead," in body
        assert body.startswith("---")

    def test_default_note_when_empty(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """No note provided → body contains 'session ended'."""
        runtime = tmp_path / "runtime"
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: None)

        ok, _ = orch.end_session(project=TEST_PROJECT, note="")
        assert ok is True

        files = list((runtime / "sessions").rglob("lead-*.md"))
        assert files
        body = files[0].read_text(encoding="utf-8")
        assert "session ended" in body

    def test_vault_absent_graceful(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """No vault configured → silently skips vault write, local file still written."""
        runtime = tmp_path / "runtime"
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: None)

        ok, _ = orch.end_session(project=TEST_PROJECT, note="no vault present here")
        assert ok is True
        files = list((runtime / "sessions").rglob("lead-*.md"))
        assert len(files) == 1

    def test_vault_present_file_written(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Vault configured → file written inside vault/01-Projects/<project>/sessions/."""
        runtime = tmp_path / "runtime"
        fake_vault = tmp_path / "vault"
        (fake_vault / "01-Projects").mkdir(parents=True)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: fake_vault)

        ok, _ = orch.end_session(project=TEST_PROJECT, note="vault write test for session")
        assert ok is True

        vault_files = list(
            (fake_vault / "01-Projects" / TEST_PROJECT / "sessions").rglob("*-lead.md")
        )
        assert len(vault_files) == 1

    def test_done_events_listed_in_body(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Teammate done files present today → paths listed under '## Teammate done events'."""
        runtime = tmp_path / "runtime"
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: None)

        # Pre-create some teammate done files in today's session dir.
        today = datetime.now().strftime("%Y-%m-%d")
        sess_dir = runtime / "sessions" / today / TEST_PROJECT
        sess_dir.mkdir(parents=True)
        (sess_dir / "backend-120000.md").write_text("backend done", encoding="utf-8")
        (sess_dir / "frontend-130000.md").write_text("frontend done", encoding="utf-8")

        ok, _ = orch.end_session(project=TEST_PROJECT, note="checking done events list")
        assert ok is True

        files = list(sess_dir.rglob("lead-*.md"))
        assert files
        body = files[0].read_text(encoding="utf-8")
        assert "## Teammate done events" in body
        assert "backend-120000.md" in body
        assert "frontend-130000.md" in body

    def test_end_session_writes_daily_digest(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """end_session also appends a Finish-Job digest to the vault's
        `05-Daily/<today>.md` note — wiring write_daily_digest into the
        end-session flow so the daily roll-up isn't dead code."""
        runtime = tmp_path / "runtime"
        fake_vault = tmp_path / "vault"
        (fake_vault / "01-Projects").mkdir(parents=True)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: fake_vault)

        ok, _ = orch.end_session(project=TEST_PROJECT, note="daily digest wire test")
        assert ok is True

        today = datetime.now().strftime("%Y-%m-%d")
        daily = fake_vault / "05-Daily" / f"{today}.md"
        assert daily.is_file()
        content = daily.read_text(encoding="utf-8")
        assert TEST_PROJECT in content
        assert "wrapped at" in content

    def test_end_session_ok_when_digest_fails(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """A failure inside write_daily_digest must never fail end_session —
        the local session summary is the contract; the digest is best-effort."""
        runtime = tmp_path / "runtime"
        fake_vault = tmp_path / "vault"
        (fake_vault / "01-Projects").mkdir(parents=True)
        monkeypatch.setattr("agent_takkub.orchestrator.RUNTIME_DIR", runtime)
        monkeypatch.setattr("agent_takkub.orchestrator._resolve_vault_dir", lambda: fake_vault)

        def _boom(_project: str) -> bool:
            raise RuntimeError("digest exploded")

        monkeypatch.setattr(orch, "write_daily_digest", _boom)

        ok, msg = orch.end_session(project=TEST_PROJECT, note="digest blows up but session ok")
        assert ok is True
        assert "lead session summary written" in msg
        files = list((runtime / "sessions").rglob("lead-*.md"))
        assert len(files) == 1


# ──────────────────────────────────────────────────────────────
# CLI role gate test
# ──────────────────────────────────────────────────────────────


class TestEndSessionCliRoleGate:
    def test_teammate_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-lead TAKKUB_ROLE → _enforce_role_gate returns error message."""
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        err = _enforce_role_gate("end-session")
        assert err is not None
        assert "lead" in err.lower()

    def test_lead_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TAKKUB_ROLE=lead → no gate error."""
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        err = _enforce_role_gate("end-session")
        assert err is None

    def test_no_role_env_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No TAKKUB_ROLE set (manual terminal) → no gate error."""
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        err = _enforce_role_gate("end-session")
        assert err is None


# ──────────────────────────────────────────────────────────────
# cli_server server-side gate
# ──────────────────────────────────────────────────────────────


class TestCliServerEndSessionGate:
    def test_non_lead_rejected(self, srv_sock) -> None:
        """end-session from from_role='backend' → rejected."""
        srv, sock, _ = srv_sock
        sock.reset()
        srv._dispatch(sock, {"cmd": "end-session", "from": "backend", "note": "x"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "lead" in resp["msg"].lower()

    def test_lead_dispatches_to_orchestrator(self, srv_sock) -> None:
        """end-session from from_role='lead' with valid token → calls orchestrator.end_session."""
        srv, sock, mock_orch = srv_sock
        sock.reset()
        # end-session is a Lead-only command and requires the lead token.
        srv._dispatch(
            sock,
            {"cmd": "end-session", "from": "lead", "note": "wrap", "auth": "test-lead-token"},
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        mock_orch.end_session.assert_called_once()
