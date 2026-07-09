"""Repro + regression test for issue #113 ("Resume cancelled" on fresh boot).

Root cause: `inject_slash_command_when_ready` had no lock across calls — the
`/remote-control` auto-bridge (fires on every Lead spawn) and the ↻ Resume
button (fires `/resume`) both poll the SAME Lead pane independently. When both
observe `ready=True` around the same tick, both `_deliver()` and write
payload+Enter into the pane interleaved — the Enter meant for one command
lands mid-render of the other's picker. This drives the REAL `QTimer.singleShot`
chain (captured + fired manually, same technique as `test_remote_bridge_repro.py`)
with both commands' ready checks resolving on the very same tick, proving the
writes serialise instead of interleaving.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import lead_inbox as lead_inbox_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.roles import LEAD

TEST_PROJECT = "slashinject"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _make_pane() -> MagicMock:
    pane = MagicMock()
    pane.role.name = LEAD.name
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt = MagicMock(return_value=True)
    pane.session.write = MagicMock()
    return pane


def _drive_timer_chain(capture_calls, *, max_iterations: int) -> None:
    i = 0
    while capture_calls and i < max_iterations:
        _delay, fn = capture_calls.pop(0)
        fn()
        i += 1


class TestConcurrentCallsSerialize:
    """Two independent inject_slash_command_when_ready calls for the same
    (project, role) — the exact #113 race between the /remote-control
    auto-bridge and the ↻ Resume button — must serialise, never interleave
    their payload+Enter writes into the pane."""

    def test_second_call_queues_instead_of_racing(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = pane

        calls: list[tuple[int, object]] = []
        monkeypatch.setattr(
            lead_inbox_mod.QTimer, "singleShot", lambda ms, fn: calls.append((ms, fn))
        )
        delivered = []

        # Bridge call — starts polling first.
        orch.inject_slash_command_when_ready(
            LEAD.name,
            "/remote-control",
            project=TEST_PROJECT,
            on_delivered=lambda: delivered.append("/remote-control"),
        )
        # Resume-button call — fires while the bridge poll is still in
        # flight (nothing has been driven off `calls` yet), reproducing the
        # exact race: both would observe ready=True on the same tick without
        # the lock.
        orch.inject_slash_command_when_ready(
            LEAD.name,
            "/resume",
            project=TEST_PROJECT,
            on_delivered=lambda: delivered.append("/resume"),
        )

        # Only the first call's poll should be scheduled yet — the second is
        # queued, not polling concurrently.
        assert len(calls) == 1

        _drive_timer_chain(calls, max_iterations=20)

        # Both eventually delivered, strictly in arrival order — no drop.
        assert delivered == ["/remote-control", "/resume"]

        writes = [c.args[0] for c in pane.session.write.call_args_list]
        # The full write sequence: payload1, its Enter, payload2, its Enter —
        # never payload2 (or its Enter) sandwiched between payload1 and its
        # own Enter, which is what produced the observed "Resume cancelled".
        assert writes == ["/remote-control", b"\r", "/resume", b"\r"]

    def test_queued_call_delivers_even_if_first_drops(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A queued call must still get its own turn even when the call
        ahead of it in line drops (session dies) instead of delivering —
        the lock must release on every exit path, not only success."""
        pane = _make_pane()
        pane.session.is_at_ready_prompt = MagicMock(return_value=False)
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = pane

        calls: list[tuple[int, object]] = []
        monkeypatch.setattr(
            lead_inbox_mod.QTimer, "singleShot", lambda ms, fn: calls.append((ms, fn))
        )
        outcomes: list[tuple[str, str]] = []

        orch.inject_slash_command_when_ready(
            LEAD.name,
            "/remote-control",
            max_wait_ms=1_000,
            project=TEST_PROJECT,
            on_dropped=lambda reason: outcomes.append(("/remote-control", reason)),
        )
        orch.inject_slash_command_when_ready(
            LEAD.name,
            "/resume",
            project=TEST_PROJECT,
            on_delivered=lambda: outcomes.append(("/resume", "delivered")),
        )

        assert len(calls) == 1  # second call still queued

        # First call times out (never ready within its 1s window).
        _drive_timer_chain(calls, max_iterations=5)
        assert outcomes[0][0] == "/remote-control"
        assert outcomes[0][1].startswith("timeout_")

        # Second call now becomes ready and must have started its own poll
        # once the first released the lock — deliver it.
        pane.session.is_at_ready_prompt = MagicMock(return_value=True)
        _drive_timer_chain(calls, max_iterations=10)

        assert ("/resume", "delivered") in outcomes
        writes = [c.args[0] for c in pane.session.write.call_args_list]
        assert writes == ["/resume", b"\r"]


class TestAutoEnterSubmit:
    """Every command routed through `inject_slash_command_when_ready` is
    submitted with a trailing auto-Enter. (The ↻ Resume button used to opt
    out of this for `/resume`, but it now drives the native `--resume <uuid>`
    respawn and never touches this path — so there is no opt-out flag left.)"""

    def test_slash_command_gets_trailing_enter(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: a slash command is written then submitted with
        b"\\r" (#4/#107/#110/#112 must not regress)."""
        pane = _make_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = pane

        calls: list[tuple[int, object]] = []
        monkeypatch.setattr(
            lead_inbox_mod.QTimer, "singleShot", lambda ms, fn: calls.append((ms, fn))
        )
        delivered = []

        orch.inject_slash_command_when_ready(
            LEAD.name,
            "/remote-control",
            project=TEST_PROJECT,
            on_delivered=lambda: delivered.append("/remote-control"),
        )
        _drive_timer_chain(calls, max_iterations=10)

        assert delivered == ["/remote-control"]
        writes = [c.args[0] for c in pane.session.write.call_args_list]
        assert writes == ["/remote-control", b"\r"]
