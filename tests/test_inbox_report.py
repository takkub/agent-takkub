"""Targeted tests for #231 (`takkub inbox`) and #228 (origin provenance).

#231: Lead had no CLI command that read the content of a done/FAILED report
still sitting in the outbound pipeline — only `takkub status`'s bare
"queued — not yet delivered" tag. `Orchestrator.inbox_report` /
`takkub inbox` fix that.

#228: a done report queued while one pane instance was alive but delivered
only after a later instance took over the same role name must be flagged,
not silently presented as if it came from the pane currently running under
that name. `inbox_report`'s `origin_confirmed` field surfaces the same
provenance check `_flush_lead_digest` / `_pump_lead_notify` apply at actual
delivery time.
"""

from __future__ import annotations

import collections

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import cli
from agent_takkub.orchestrator import Orchestrator

PROJECT = "inbox-test"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


class TestInboxReportSurfacesEveryQueueTier:
    def test_digest_queue_item_surfaced(self, orch: Orchestrator) -> None:
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[backend done] refactor middleware", None)])
        }

        items = orch.inbox_report(project=PROJECT)

        assert len(items) == 1
        assert items[0]["role"] == "backend"
        assert items[0]["queue"] == "digest"
        assert "refactor middleware" in items[0]["body"]

    def test_live_queue_item_surfaced(self, orch: Orchestrator) -> None:
        orch._lead_notify_queue = {PROJECT: collections.deque([("[qa FAILED] broke", None)])}

        items = orch.inbox_report(project=PROJECT)

        assert len(items) == 1
        assert items[0]["role"] == "qa"
        assert items[0]["queue"] == "live"

    def test_durable_store_item_surfaced(self, orch: Orchestrator) -> None:
        orch._pending_done_notices = {
            PROJECT: [{"role": "frontend", "note": "notify", "body": "[frontend done] ui done"}]
        }

        items = orch.inbox_report(project=PROJECT)

        assert len(items) == 1
        assert items[0]["role"] == "frontend"
        assert items[0]["queue"] == "durable"

    def test_all_three_tiers_combined(self, orch: Orchestrator) -> None:
        orch._lead_digest_queue = {PROJECT: collections.deque([("[a done] x", None)])}
        orch._lead_notify_queue = {PROJECT: collections.deque([("[b done] y", None)])}
        orch._pending_done_notices = {
            PROJECT: [{"role": "c", "note": "notify", "body": "[c done] z"}]
        }

        items = orch.inbox_report(project=PROJECT)

        assert {i["role"] for i in items} == {"a", "b", "c"}
        assert {i["queue"] for i in items} == {"digest", "live", "durable"}

    def test_role_filter(self, orch: Orchestrator) -> None:
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[backend done] x", None), ("[frontend done] y", None)])
        }

        items = orch.inbox_report(project=PROJECT, role="backend")

        assert len(items) == 1
        assert items[0]["role"] == "backend"

    def test_empty_when_nothing_queued(self, orch: Orchestrator) -> None:
        assert orch.inbox_report(project=PROJECT) == []

    def test_scoped_to_project_namespace(self, orch: Orchestrator) -> None:
        orch._lead_digest_queue = {"other-proj": collections.deque([("[backend done] x", None)])}

        assert orch.inbox_report(project=PROJECT) == []


class TestInboxReportOriginProvenance:
    """#228: a queued item whose role slot was respawned since must be
    flagged, using the same _current_pane_identity check _flush_lead_digest
    / _pump_lead_notify apply at actual delivery time."""

    def test_confirmed_when_role_slot_unchanged(self, orch: Orchestrator) -> None:
        orch._pane_tokens = {"tok-current": (PROJECT, "backend")}
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[backend done] x", "tok-current")])
        }

        items = orch.inbox_report(project=PROJECT)

        assert items[0]["origin_confirmed"] is True

    def test_flagged_stale_after_role_slot_respawned(self, orch: Orchestrator) -> None:
        # Item was queued under the pane holding "tok-old"; the role slot now
        # holds a different token — a respawn happened since.
        orch._pane_tokens = {"tok-new": (PROJECT, "backend")}
        orch._lead_digest_queue = {PROJECT: collections.deque([("[backend done] x", "tok-old")])}

        items = orch.inbox_report(project=PROJECT)

        assert items[0]["origin_confirmed"] is False

    def test_flagged_stale_when_role_no_longer_has_a_live_pane(self, orch: Orchestrator) -> None:
        orch._pane_tokens = {}
        orch._lead_digest_queue = {PROJECT: collections.deque([("[backend done] x", "tok-old")])}

        items = orch.inbox_report(project=PROJECT)

        assert items[0]["origin_confirmed"] is False

    def test_none_when_no_origin_was_recorded(self, orch: Orchestrator) -> None:
        """Legacy/system-authored entries carry no pane_token — nothing to
        verify, so this must not be reported as either confirmed or stale."""
        orch._lead_digest_queue = {PROJECT: collections.deque([("[backend done] x", None)])}

        items = orch.inbox_report(project=PROJECT)

        assert items[0]["origin_confirmed"] is None

    def test_ordinary_next_task_respawn_not_flagged_stale(self, orch: Orchestrator) -> None:
        """#315 false positive: the role finished, its report was queued,
        and only THEN was it respawned for its next assignment (mint time
        strictly after queued_ts) — the ordinary steady-state hand-off that
        fired the banner on ~every digest pre-#315. Must not warn."""
        queued_ts = 1_000.0
        orch._pane_tokens = {"tok-new": (PROJECT, "backend")}
        orch._pane_token_minted_at = {"tok-new": queued_ts + 5}
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[backend done] x", "tok-old", queued_ts)])
        }

        items = orch.inbox_report(project=PROJECT)

        assert items[0]["origin_confirmed"] is True

    def test_phantom_report_predating_current_pane_still_flagged_stale(
        self, orch: Orchestrator
    ) -> None:
        """#228 true positive, still caught post-#315: the CURRENT pane was
        already live BEFORE this report claims to have been queued. A live
        done() call always stamps whatever token is current at call time, so
        this ordering can only happen for a replayed/phantom report — must
        still warn."""
        queued_ts = 1_000.0
        orch._pane_tokens = {"tok-new": (PROJECT, "backend")}
        orch._pane_token_minted_at = {"tok-new": queued_ts - 5}
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[backend done] x", "tok-old", queued_ts)])
        }

        items = orch.inbox_report(project=PROJECT)

        assert items[0]["origin_confirmed"] is False


class TestCliInboxCommand:
    def test_sends_inbox_cmd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict] = []
        monkeypatch.setattr(cli, "_request", lambda payload: sent.append(payload) or {"ok": True})
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        cli.main(["inbox"])

        assert sent[-1]["cmd"] == "inbox"
        assert "role" not in sent[-1]

    def test_role_flag_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict] = []
        monkeypatch.setattr(cli, "_request", lambda payload: sent.append(payload) or {"ok": True})
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        cli.main(["inbox", "--role", "backend#1"])

        assert sent[-1]["role"] == "backend#1"

    def test_teammate_role_blocked_by_cli_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        rc = cli.main(["inbox"])
        assert rc == 1

    def test_lead_role_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_request", lambda payload: {"ok": True, "items": []})
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        rc = cli.main(["inbox"])
        assert rc == 0

    def test_prints_pending_report_body(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: {
                "ok": True,
                "msg": "1 pending item(s)",
                "items": [
                    {
                        "role": "backend#1",
                        "queue": "durable",
                        "body": "[backend#1 done] tests 52 green",
                        "origin_confirmed": True,
                    }
                ],
            },
        )
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        rc = cli.main(["inbox"])
        out = capsys.readouterr().out

        assert "backend#1" in out
        assert "tests 52 green" in out
        assert rc == 0

    def test_prints_stale_origin_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: {
                "ok": True,
                "msg": "1 pending item(s)",
                "items": [
                    {
                        "role": "frontend#1",
                        "queue": "digest",
                        "body": "[frontend#1 done] withdrawal form wired",
                        "origin_confirmed": False,
                    }
                ],
            },
        )
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        rc = cli.main(["inbox"])
        out = capsys.readouterr().out

        assert "unconfirmed origin" in out
        assert rc == 0

    def test_nothing_pending_prints_friendly_message(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(cli, "_request", lambda payload: {"ok": True, "items": []})
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        cli.main(["inbox"])
        out = capsys.readouterr().out

        assert "nothing pending" in out


class TestCliServerInboxDispatch:
    @pytest.fixture
    def srv_sock(self, qapp):
        from unittest.mock import MagicMock

        from agent_takkub.cli_server import CliServer

        mock_orch = MagicMock()
        mock_orch._lead_token = "tok"
        mock_orch.inbox_report.return_value = [
            {"role": "backend", "queue": "durable", "body": "x", "origin_confirmed": True}
        ]
        srv = CliServer(mock_orch)

        class _FakeSock:
            def __init__(self) -> None:
                self._buf = b""

            def write(self, data: bytes) -> None:
                self._buf += data

            def flush(self) -> None:
                pass

        return srv, _FakeSock(), mock_orch

    def test_lead_token_required(self, srv_sock) -> None:
        import json

        srv, sock, mock_orch = srv_sock
        req = {"cmd": "inbox", "from": "backend"}
        srv._dispatch(sock, req)
        resp = json.loads(sock._buf.split(b"\n", 1)[0].decode("utf-8"))
        assert resp["ok"] is False
        mock_orch.inbox_report.assert_not_called()

    def test_lead_token_reaches_orch(self, srv_sock) -> None:
        import json

        srv, sock, mock_orch = srv_sock
        req = {"cmd": "inbox", "from": "lead", "auth": "tok", "role": "backend"}
        srv._dispatch(sock, req)
        resp = json.loads(sock._buf.split(b"\n", 1)[0].decode("utf-8"))
        assert resp["ok"] is True
        assert resp["items"][0]["role"] == "backend"
        mock_orch.inbox_report.assert_called_once()
        assert mock_orch.inbox_report.call_args.kwargs.get("role") == "backend"
