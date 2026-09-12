"""M5#23: `_launch_session` — the common ConPTY launch tail extracted from the
shell / gemini / codex spawn branches.

These pin the provider-specific drift that the extraction had to preserve:
  - codex_exit=True  → stamps codex_spawn_ts + wires _on_codex_exit
  - codex_exit=False → wires the stale-guarded _on_session_exit
  - auto_trust=True  → calls _auto_trust after attach (gemini/codex; NOT shell)
  - success / failure messages carry the provider label
A mocked PtySession stands in for the native ConPTY so no real process spawns.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "launchtest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or TEST_PROJECT))
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    o = Orchestrator()
    o.shutdown_timers()
    # Neutralise collaborators the tail touches so we observe wiring, not effects.
    o._auto_trust = MagicMock()
    o._on_codex_exit = MagicMock()
    o._on_session_exit = MagicMock()
    o._drain_spawn_queue = MagicMock()
    monkeypatch.setattr(o, "_final_gate_clear", lambda: True)
    return o


def _launch(orch, pane, *, label, codex_exit=False, auto_trust=False, auto_trust_wait_ms=None):
    """Run _launch_session with PtySession + transcript mocked. Returns
    (ok, msg, mock_session, connected_handler)."""
    connected: list = []
    mock_session = MagicMock()
    mock_session.processExited.connect.side_effect = lambda h: connected.append(h)

    kwargs = {}
    if auto_trust_wait_ms is not None:
        kwargs["auto_trust_wait_ms"] = auto_trust_wait_ms

    with (
        patch("agent_takkub.orchestrator.PtySession", return_value=mock_session),
        patch("agent_takkub.orchestrator._build_transcript_path", return_value="/tmp/t.log"),
    ):
        ok, msg = orch._launch_session(
            pane=pane,
            role_name=label,
            project_ns=TEST_PROJECT,
            spawn_cwd="/work/dir",
            argv=[label, "--flag"],
            env={"X": "1"},
            pane_tok="tok-" + label,
            label=label,
            cwd=None,
            project=TEST_PROJECT,
            _from_auto_respawn=False,
            _shard_total=0,
            codex_exit=codex_exit,
            auto_trust=auto_trust,
            **kwargs,
        )
    return ok, msg, mock_session, (connected[0] if connected else None)


def _pane():
    p = MagicMock()
    p.attach_session = MagicMock()
    return p


class TestLaunchSessionCommonTail:
    def test_shell_spawns_and_returns_label_message(self, orch):
        pane = _pane()
        ok, msg, sess, _ = _launch(orch, pane, label="shell")
        assert ok is True
        assert msg == "shell spawned in /work/dir"
        # native spawn invoked with our argv/env/cwd
        sess.spawn.assert_called_once()
        kw = sess.spawn.call_args.kwargs
        assert kw["argv"] == ["shell", "--flag"]
        assert kw["cwd"] == "/work/dir"
        pane.attach_session.assert_called_once()

    def test_shell_does_not_auto_trust(self, orch):
        _launch(orch, _pane(), label="shell")
        orch._auto_trust.assert_not_called()

    def test_gemini_auto_trusts(self, orch):
        _launch(orch, _pane(), label="gemini", auto_trust=True)
        orch._auto_trust.assert_called_once()

    def test_gemini_auto_trust_uses_default_window_when_unspecified(self, orch):
        """#186: a caller that doesn't opt into a longer window (the claude
        inline branch, which never overrides) still gets the historical 30s
        default via `_launch_session`'s own default kwarg."""
        _launch(orch, _pane(), label="gemini", auto_trust=True)
        assert orch._auto_trust.call_args.kwargs["max_ms"] == 30_000

    def test_auto_trust_wait_ms_forwarded_as_max_ms(self, orch):
        """#186: a cold-boot provider (agy — ready_wait_ms=90_000) can render
        its trust modal well after a hardcoded 30s watcher would already
        have given up (a real worktree fan-out incident). The spec-driven
        spawn branch threads its own ready_wait_ms through here instead of
        relying on the claude-tuned 30s default."""
        _launch(orch, _pane(), label="gemini", auto_trust=True, auto_trust_wait_ms=90_000)
        orch._auto_trust.assert_called_once()
        assert orch._auto_trust.call_args.kwargs["max_ms"] == 90_000

    def test_non_codex_wires_session_exit_not_codex(self, orch):
        orch._panes_by_project[TEST_PROJECT] = {}
        pane = _pane()
        _ok, _msg, sess, handler = _launch(orch, pane, label="gemini", auto_trust=True)
        # make the stale-guard see this pane+session as current, then fire
        orch._panes_by_project[TEST_PROJECT] = {"gemini": pane}
        pane.session = sess
        handler(0)
        orch._on_session_exit.assert_called_once()
        orch._on_codex_exit.assert_not_called()

    def test_codex_stamps_spawn_ts_and_wires_codex_exit(self, orch):
        pane = _pane()
        _ok, _msg, _sess, handler = _launch(
            orch, pane, label="codex", codex_exit=True, auto_trust=True
        )
        # codex early-crash bookkeeping recorded
        ps = orch._ps(f"{TEST_PROJECT}::codex")
        assert ps.codex_spawn_ts is not None and ps.codex_spawn_ts > 0
        # the wired handler routes to _on_codex_exit, not _on_session_exit
        handler(0)
        orch._on_codex_exit.assert_called_once()
        orch._on_session_exit.assert_not_called()
        orch._auto_trust.assert_called_once()

    def test_spawn_failure_revokes_token_and_reports(self, orch):
        orch._pane_tokens["tok-codex"] = (TEST_PROJECT, "codex")
        pane = _pane()
        connected: list = []
        mock_session = MagicMock()
        mock_session.spawn.side_effect = RuntimeError("conpty boom")
        with (
            patch("agent_takkub.orchestrator.PtySession", return_value=mock_session),
            patch("agent_takkub.orchestrator._build_transcript_path", return_value="/tmp/t.log"),
        ):
            ok, msg = orch._launch_session(
                pane=pane,
                role_name="codex",
                project_ns=TEST_PROJECT,
                spawn_cwd="/work/dir",
                argv=["codex"],
                env={},
                pane_tok="tok-codex",
                label="codex",
                cwd=None,
                project=TEST_PROJECT,
                _from_auto_respawn=False,
                _shard_total=0,
                codex_exit=True,
            )
        assert ok is False
        assert "failed to spawn codex" in msg
        assert "tok-codex" not in orch._pane_tokens  # revoked on failure
        assert connected == []

    def test_spawn_in_progress_reset_in_finally(self, orch):
        _launch(orch, _pane(), label="shell")
        assert orch._spawn_in_progress is False
        orch._drain_spawn_queue.assert_called()

    def test_resets_stale_last_spawn_resumed_flag(self, orch):
        """FU1 (2026-07-10 cross-platform followup): shell/gemini/codex never
        set last_spawn_resumed (that's a claude-`--resume`-only concept), so a
        role slot that previously spawned via the claude branch (provider
        substitution) and set it True must NOT carry that stale True into a
        later codex/gemini spawn — _auto_respawn reads it to decide whether to
        replay the cached task, and a stale True would wrongly suppress replay
        after a crash on this branch."""
        ps = orch._ps(f"{TEST_PROJECT}::gemini")
        ps.last_spawn_resumed = True
        _launch(orch, _pane(), label="gemini", auto_trust=True)
        assert orch._ps(f"{TEST_PROJECT}::gemini").last_spawn_resumed is False


class TestExitGuardSurvivesOwnHandlerRace:
    """#540: a pane that dies right after spawn (banner, then exit) never
    triggered auto-respawn or the unexpected-exit Lead notice.

    Root cause: AgentPane/HeadlessPane's OWN `processExited` handler is
    connected first (inside `attach_session`, called by `_launch_session`
    before it wires its own handler below) and always nulls `pane.session`
    via `detach_session()` on its way out — for a genuinely stale exit
    (session already replaced) exactly as much as for an ordinary one. The
    old guard compared `pane.session is <the exited session object>`, which
    is therefore False in both cases, so the second-connected handler
    (`_on_session_exit` / `_on_codex_exit`) never ran for a plain, first-ever
    exit — only a `_session_generation` comparison tells the two apart.

    These build a REAL `HeadlessPane` (not a MagicMock) so the pane's own
    `_on_exit` genuinely runs and genuinely nulls `session` before the
    orchestrator's wrapped handler fires, exactly mirroring Qt's
    connect-order signal dispatch.
    """

    def _real_pane(self):
        from agent_takkub.headless_pane import HeadlessPane
        from agent_takkub.roles import by_name

        return HeadlessPane(by_name("backend"))

    def test_non_codex_exit_fires_despite_own_handler_nulling_session(self, orch):
        """The pane's own exit handler runs first and clears `pane.session`
        — the orchestrator's handler must still fire (generic/non-codex
        path), not read that as a stale signal."""
        pane = self._real_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        _ok, _msg, mock_session, _first_handler = _launch(
            orch, pane, label="backend", codex_exit=False
        )
        # Real `HeadlessPane.attach_session` connects its own `_on_exit`
        # before `_launch_session` wires its own handler below — two
        # handlers total, connected in that order. `_launch` only surfaces
        # the first (`connected[0]`); recover the full list from the mock's
        # recorded connect calls to fire both, in connection order.
        handlers = [c.args[0] for c in mock_session.processExited.connect.call_args_list]
        assert len(handlers) == 2

        for handler in handlers:
            handler(1)  # exit code 1, dispatched in connection order

        assert pane.session is None  # own handler really did detach
        assert pane.state == "exited"
        orch._on_session_exit.assert_called_once()
        orch._on_codex_exit.assert_not_called()

    def test_codex_exit_fires_despite_own_handler_nulling_session(self, orch):
        """Same race, codex branch (`_on_codex_exit`, gated separately)."""
        pane = self._real_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["codex"] = pane
        _ok, _msg, mock_session, _last = _launch(orch, pane, label="codex", codex_exit=True)
        handlers = [c.args[0] for c in mock_session.processExited.connect.call_args_list]
        assert len(handlers) == 2

        for handler in handlers:
            handler(1)

        assert pane.session is None
        orch._on_codex_exit.assert_called_once()
        orch._on_session_exit.assert_not_called()

    def test_late_exit_from_a_replaced_session_is_still_dropped(self, orch):
        """The generation check must still reject a genuinely stale signal —
        a late `processExited` from an OLD session after a NEW one already
        attached — so the fix doesn't turn the guard into a no-op."""
        pane = self._real_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        _ok, _msg, mock_session, _last = _launch(orch, pane, label="backend", codex_exit=False)
        handlers = [c.args[0] for c in mock_session.processExited.connect.call_args_list]
        assert len(handlers) == 2
        own_handler, orch_handler = handlers

        # A replacement session attaches (bumps the pane's generation) before
        # the OLD session's queued exit signal is finally dispatched.
        pane.attach_session(MagicMock(), cwd="/work/dir", provider_name="claude")

        own_handler(1)  # old handler is generation-guarded internally too
        orch_handler(1)

        orch._on_session_exit.assert_not_called()


class TestLaunchSessionFlushesQueuedNoPaneMessages:
    """#558: a bare `takkub spawn` for a non-claude role (codex/gemini/shell —
    everything routed through `_launch_session`, per its own docstring) used
    to skip the queued `takkub send` flush entirely: only the separate
    claude-branch success tail (inline in `spawn()`) scheduled
    `_flush_queued_no_pane_messages`. A message queued while e.g. `codex`
    had no pane open stayed stuck "will be delivered as soon as it spawns"
    forever once spawned via bare `spawn` (only `assign` dispatches its own
    task text directly, sidestepping the gap). `_launch_session`'s own
    success path must schedule the same flush `spawn()`'s claude branch
    does.
    """

    def test_schedules_and_flushes_a_message_queued_before_spawn(self, orch, monkeypatch):
        fired: list = []
        monkeypatch.setattr(
            "agent_takkub.spawn_engine.QTimer.singleShot",
            lambda _ms, fn: (fired.append(fn), fn())[1],
        )
        ok, _msg = orch.send("codex", "safety note", from_role="lead", project=TEST_PROJECT)
        assert ok is True

        pane = _pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["codex"] = pane
        ok, _msg, sess, _handler = _launch(orch, pane, label="codex", codex_exit=True)
        pane.session = sess  # _flush_queued_no_pane_messages reads pane.session

        assert ok is True
        assert fired, "queued-message flush timer was never scheduled"

        from agent_takkub import role_messages

        assert role_messages.queued_no_pane_for_role(
            orch_mod.RUNTIME_DIR, TEST_PROJECT, "codex"
        ) == (
            [],
            [],
        )
        all_records = role_messages.read(orch_mod.RUNTIME_DIR, TEST_PROJECT, role="codex")
        assert any(r["state"] == "sent" for r in all_records)

    def test_does_not_schedule_a_flush_when_nothing_was_queued(self, orch, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            "agent_takkub.spawn_engine.QTimer.singleShot",
            lambda _ms, fn: calls.append(fn),
        )
        _launch(orch, _pane(), label="shell")
        assert calls == []
