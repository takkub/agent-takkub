"""Tests for the 2026-08-13 remote-mirror-blank fix: `takkub doctor --live`
diagnostics for "phone shows nothing back from Lead". Covers the two layers,
same split as test_spawn_queue_health.py's #141 tests: the cli_server.py TCP
endpoint (`remote-mirror-status`) that reads live pane state, and the
`doctor.check_remote_mirror_live` pure interpretation of that response.
"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import CliServer
from agent_takkub.doctor import Status, check_remote_mirror_live


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


class _FakeSock:
    def __init__(self) -> None:
        self.written = b""

    def write(self, b) -> None:
        self.written += bytes(b)

    def flush(self) -> None:
        pass


def _replies(sock: _FakeSock) -> list[dict]:
    return [json.loads(line) for line in sock.written.decode().splitlines() if line.strip()]


class _FakePaneModel:
    def __init__(self, provider_name: str | None) -> None:
        self.provider_name = provider_name


class _FakePane:
    def __init__(self, provider_name: str | None) -> None:
        self.model = _FakePaneModel(provider_name)


class _FakePaneState:
    def __init__(self, session_uuid: str | None) -> None:
        self.session_uuid = session_uuid


class _FakeOrch:
    def __init__(
        self,
        panes_by_project: dict | None = None,
        pane_state: dict | None = None,
        project_ns: str = "proj-a",
    ) -> None:
        self._lead_token = "tok"
        self._panes_by_project = panes_by_project or {}
        self._pane_state = pane_state or {}
        self._resolve_project_ns = project_ns

    def _resolve_project(self, project: str | None) -> str:
        return project or self._resolve_project_ns


# ---------------------------------------------------------------------------
# cli_server.py — "remote-mirror-status" TCP command
# ---------------------------------------------------------------------------


class TestRemoteMirrorStatusCommand:
    def test_no_lead_pane_open(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch(panes_by_project={"proj-a": {}})
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(sock, {"cmd": "remote-mirror-status", "from_project": "proj-a"})

        r = _replies(sock)[0]
        assert r["ok"] is True
        assert r["project"] == "proj-a"
        assert r["lead_pane_open"] is False
        assert r["session_uuid"] is None

    def test_reports_live_pane_provider_over_config_fallback(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch(
            panes_by_project={"proj-a": {"lead": _FakePane("opencode")}},
            pane_state={"proj-a::lead": _FakePaneState(None)},
        )
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(sock, {"cmd": "remote-mirror-status", "from_project": "proj-a"})

        r = _replies(sock)[0]
        assert r["lead_pane_open"] is True
        assert r["provider"] == "opencode"
        assert r["supports_remote_history"] is False
        assert r["session_uuid"] is None
        assert r["transcript_exists"] is None

    def test_reports_session_uuid_for_claude_lead(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch(
            panes_by_project={"proj-a": {"lead": _FakePane("claude")}},
            pane_state={"proj-a::lead": _FakePaneState("abc-123")},
        )
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(sock, {"cmd": "remote-mirror-status", "from_project": "proj-a"})

        r = _replies(sock)[0]
        assert r["provider"] == "claude"
        assert r["supports_remote_history"] is True
        assert r["session_uuid"] == "abc-123"
        # No matching jsonl exists anywhere on this test machine's config dir
        # for this made-up uuid — the important thing is the field resolves
        # to a real bool (file-existence checked), not None (N/A).
        assert r["transcript_exists"] is False

    def test_namespace_mismatch_reports_lead_pane_closed(self, qapp: QCoreApplication) -> None:
        """A pane recorded under a different project namespace than the one
        being queried must read as 'no Lead pane here', not silently return
        someone else's state."""
        orch = _FakeOrch(
            panes_by_project={"other-proj": {"lead": _FakePane("claude")}},
            pane_state={"other-proj::lead": _FakePaneState("abc-123")},
            project_ns="proj-a",
        )
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(sock, {"cmd": "remote-mirror-status", "from_project": "proj-a"})

        r = _replies(sock)[0]
        assert r["lead_pane_open"] is False
        assert r["session_uuid"] is None


# ---------------------------------------------------------------------------
# doctor.check_remote_mirror_live — interpretation layer
# ---------------------------------------------------------------------------


class TestCheckRemoteMirrorLive:
    """Pure interpreter of an already-fetched response dict — doctor.py must
    never import cli/orchestrator/remote itself (leaf-modules-pure +
    remote-bolt-on-isolation import-linter contracts)."""

    def test_cockpit_not_running_is_skip(self) -> None:
        findings = check_remote_mirror_live(None)
        assert len(findings) == 1
        assert findings[0].status is Status.SKIP
        assert findings[0].category == "remote-mirror"

    def test_request_failure_is_warn(self) -> None:
        findings = check_remote_mirror_live({"ok": False, "msg": "connection refused"})
        assert findings[0].status is Status.WARN

    def test_no_lead_pane_is_warn_and_stops(self) -> None:
        findings = check_remote_mirror_live(
            {"ok": True, "project": "proj-a", "lead_pane_open": False}
        )
        assert len(findings) == 1
        assert findings[0].status is Status.WARN
        assert findings[0].name == "lead-pane"

    def test_supported_provider_with_matching_transcript_is_all_ok(self) -> None:
        findings = check_remote_mirror_live(
            {
                "ok": True,
                "project": "proj-a",
                "provider": "claude",
                "lead_pane_open": True,
                "supports_remote_history": True,
                "session_uuid": "abc-123",
                "transcript_exists": True,
            }
        )
        by_name = {f.name: f for f in findings}
        assert by_name["lead-pane"].status is Status.OK
        assert by_name["history-scanner"].status is Status.OK
        assert by_name["session-uuid"].status is Status.OK
        assert by_name["transcript-file"].status is Status.OK

    def test_unsupported_provider_is_fail_not_silent(self) -> None:
        """The concrete diagnosed bug: OpenCode Lead, message delivered fine,
        but no scanner exists — this must surface as an explicit FAIL with a
        reason, not a clean/quiet report."""
        findings = check_remote_mirror_live(
            {
                "ok": True,
                "project": "proj-a",
                "provider": "opencode",
                "lead_pane_open": True,
                "supports_remote_history": False,
                "session_uuid": None,
                "transcript_exists": None,
            }
        )
        by_name = {f.name: f for f in findings}
        assert by_name["history-scanner"].status is Status.FAIL
        assert "opencode" in by_name["history-scanner"].detail
        assert "#103" in by_name["history-scanner"].fix_hint
        # No uuid on a provider without a scanner: not-yet-set, softened to
        # SKIP rather than piling on a second FAIL for the same root cause.
        assert by_name["session-uuid"].status is Status.SKIP
        # Non-applicable provider: no transcript-file finding emitted at all.
        assert "transcript-file" not in by_name

    def test_uuid_present_but_transcript_missing_is_fail(self) -> None:
        """Session-uuid drift for a SUPPORTED provider (claude/codex/gemini)
        — the file-based mirror path is broken even though the provider is
        capable, distinct from the opencode "not capable at all" case."""
        findings = check_remote_mirror_live(
            {
                "ok": True,
                "project": "proj-a",
                "provider": "claude",
                "lead_pane_open": True,
                "supports_remote_history": True,
                "session_uuid": "abc-123",
                "transcript_exists": False,
            }
        )
        by_name = {f.name: f for f in findings}
        assert by_name["history-scanner"].status is Status.OK
        assert by_name["session-uuid"].status is Status.OK
        assert by_name["transcript-file"].status is Status.FAIL
        assert "drift" in by_name["transcript-file"].fix_hint

    def test_missing_uuid_on_supported_provider_is_warn(self) -> None:
        findings = check_remote_mirror_live(
            {
                "ok": True,
                "project": "proj-a",
                "provider": "claude",
                "lead_pane_open": True,
                "supports_remote_history": True,
                "session_uuid": None,
                "transcript_exists": None,
            }
        )
        by_name = {f.name: f for f in findings}
        assert by_name["session-uuid"].status is Status.WARN
        assert "transcript-file" not in by_name

    def test_not_in_run_all_checks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`takkub doctor` (no --live) must stay orchestrator-socket-free."""
        import inspect

        import agent_takkub.doctor as doctor_mod
        from agent_takkub.doctor import run_all_checks

        for name, _fn in inspect.getmembers(doctor_mod, inspect.isfunction):
            if name.startswith("check_") and name not in (
                "check_spawn_queue_live",
                "check_remote_mirror_live",
            ):
                monkeypatch.setattr(doctor_mod, name, lambda: [])

        names = {(f.category, f.name) for f in run_all_checks()}
        assert ("remote-mirror", "lead-pane") not in names
