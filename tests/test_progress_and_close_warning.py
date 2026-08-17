"""Targeted tests for #234:

1. `Orchestrator.progress()` / `takkub progress` — a status-update channel
   that, unlike `done()`, never schedules the pane's teardown.
2. `Orchestrator._warn_if_live_children()` — a best-effort warning fired
   from `close()` right before `terminate()` kills whatever subprocess
   tree is still running under the pane's shell.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import cli
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import LEAD, Orchestrator, PaneState


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_alive_session(pid: int | None = 4242) -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    s._pid = pid
    return s


def _make_pane(session=None, state: str = "working") -> MagicMock:
    p = MagicMock()
    p.session = session
    p.state = state
    p.set_state = MagicMock()
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

    with patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None):
        o = Orchestrator.__new__(Orchestrator)
        from PyQt6.QtCore import QObject

        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
        o._pending_done_notices = {}
    return o


PROJECT = "progress-test"


def _register(orch: Orchestrator, role: str, session=None, state: str = "working"):
    pane = _make_pane(session, state=state)
    orch._panes_by_project.setdefault(PROJECT, {})[role] = pane
    return pane


class TestProgressDoesNotTeardown:
    def test_unknown_role_rejected(self, orch: Orchestrator) -> None:
        ok, msg = orch.progress("backend", note="still going", project=PROJECT)
        assert ok is False
        assert "unknown role" in msg

    def test_lead_cannot_call_progress_on_itself(self, orch: Orchestrator) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        ok, msg = orch.progress(LEAD.name, note="x", project=PROJECT)
        assert ok is False
        assert "lead cannot" in msg.lower()

    def test_empty_note_rejected(self, orch: Orchestrator) -> None:
        _register(orch, "devops", _make_alive_session())
        ok, _msg = orch.progress("devops", note="   ", project=PROJECT)
        assert ok is False

    def test_success_notifies_lead_with_progress_tag(self, orch: Orchestrator) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        _register(orch, "devops", _make_alive_session())

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            ok, _msg = orch.progress(
                "devops", note="docker build still running, ~35s in", project=PROJECT
            )

        assert ok is True
        lead = orch._panes_by_project[PROJECT][LEAD.name]
        written = "".join(
            c.args[0].decode() if isinstance(c.args[0], bytes) else str(c.args[0])
            for c in lead.session.write.call_args_list
        )
        assert "[devops progress]" in written
        assert "docker build still running" in written

    def test_never_schedules_a_close_timer(self, orch: Orchestrator) -> None:
        """The whole point of #234: unlike done(), progress() must not arm
        the 2.5s auto-close QTimer that kills the pane's subprocess tree."""
        _register(orch, LEAD.name, _make_alive_session())
        _register(orch, "devops", _make_alive_session())

        timers: list[tuple[int, object]] = []
        with patch(
            "agent_takkub.orchestrator.QTimer.singleShot",
            side_effect=lambda ms, cb: timers.append((ms, cb)),
        ):
            orch.progress("devops", note="still building", project=PROJECT)

        assert not any(ms == 2_500 for ms, _ in timers), (
            "progress() must never schedule the done()-style auto-close timer"
        )

    def test_pane_state_untouched(self, orch: Orchestrator) -> None:
        """done() pops _pane_state/_idle_state; progress() must leave them
        alone — the task is still in flight."""
        _register(orch, LEAD.name, _make_alive_session())
        _register(orch, "devops", _make_alive_session())
        key = f"{PROJECT}::devops"
        orch._pane_state[key] = PaneState()
        orch._idle_state[key] = object()

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch.progress("devops", note="still building", project=PROJECT)

        assert key in orch._pane_state
        assert key in orch._idle_state

    def test_pane_still_alive_after_progress_call(self, orch: Orchestrator) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        devops_session = _make_alive_session()
        _register(orch, "devops", devops_session)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch.progress("devops", note="still building", project=PROJECT)

        devops_session.terminate.assert_not_called()


class TestProgressCli:
    def test_teammate_can_call_progress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict] = []
        monkeypatch.setattr(cli, "_request", lambda p: sent.append(p) or {"ok": True})
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        rc = cli.main(["progress", "still building"])
        assert rc == 0
        assert sent[-1]["cmd"] == "progress"
        assert sent[-1]["note"] == "still building"

    def test_lead_cannot_call_progress_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        rc = cli.main(["progress", "x"])
        assert rc == 1


class TestCliServerProgressDispatch:
    @pytest.fixture
    def srv_sock(self, qapp):
        from agent_takkub.cli_server import CliServer

        mock_orch = MagicMock()
        mock_orch._lead_token = "leadtok"
        mock_orch._pane_tokens = {"panetok": (PROJECT, "devops")}
        mock_orch.progress.return_value = (True, "devops progress reported")
        srv = CliServer(mock_orch)

        class _FakeSock:
            def __init__(self) -> None:
                self._buf = b""

            def write(self, data: bytes) -> None:
                self._buf += data

            def flush(self) -> None:
                pass

        return srv, _FakeSock(), mock_orch

    def test_requires_a_valid_pane_token(self, srv_sock) -> None:
        srv, sock, mock_orch = srv_sock
        req = {"cmd": "progress", "from": "devops", "note": "x"}
        srv._dispatch(sock, req)
        mock_orch.progress.assert_not_called()

    def test_valid_token_derives_role_and_calls_orch(self, srv_sock) -> None:
        srv, sock, mock_orch = srv_sock
        req = {"cmd": "progress", "from": "devops", "auth": "panetok", "note": "still building"}
        srv._dispatch(sock, req)
        mock_orch.progress.assert_called_once()
        assert mock_orch.progress.call_args.args[0] == "devops"

    def test_lead_role_rejected_before_reaching_orch(self, srv_sock) -> None:
        srv, sock, mock_orch = srv_sock
        req = {"cmd": "progress", "from": "lead", "note": "x"}
        srv._dispatch(sock, req)
        mock_orch.progress.assert_not_called()


class TestWarnIfLiveChildren:
    def test_no_pid_is_a_noop(self, orch: Orchestrator) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "devops", _make_alive_session(pid=None))
        lead = orch._panes_by_project[PROJECT][LEAD.name]
        lead.session.write.assert_not_called()

    def test_no_children_is_a_noop(self, orch: Orchestrator, monkeypatch) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        fake_proc = MagicMock()
        fake_proc.children.return_value = []
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "devops", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        lead.session.write.assert_not_called()

    def test_live_children_warn_lead(self, orch: Orchestrator, monkeypatch) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        child = MagicMock()
        child.name.return_value = "docker"
        fake_proc = MagicMock()
        fake_proc.children.return_value = [child]
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "devops", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        written = "".join(
            c.args[0].decode() if isinstance(c.args[0], bytes) else str(c.args[0])
            for c in lead.session.write.call_args_list
        )
        assert "devops closing" in written
        assert "1 subprocess" in written
        assert "docker" in written

    def test_psutil_failure_is_swallowed(self, orch: Orchestrator, monkeypatch) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(
            "psutil.Process", lambda pid: (_ for _ in ()).throw(RuntimeError("no such process"))
        )

        # Must not raise.
        orch._warn_if_live_children(PROJECT, "devops", _make_alive_session())

    def test_scaffolding_only_children_stay_silent(self, orch: Orchestrator, monkeypatch) -> None:
        """#272: a pane closing with only its own CLI-launcher scaffolding
        alive (npm .cmd shim's cmd.exe/conhost.exe/node.exe on Windows) must
        not warn — that was every single close before this filter existed.

        cmd.exe/conhost.exe are the Windows-only ConPTY console-host pair
        (`GENERIC_SCAFFOLDING_PROCESS_NAMES_WIN32`), so this pins them by
        forcing `sys.platform` rather than relying on which OS the suite
        happens to run on — otherwise this test silently no-ops on
        macOS/Linux CI runners instead of proving the Windows baseline."""
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "claude",
        )
        children = []
        for name in ("cmd.exe", "conhost.exe", "node.exe"):
            c = MagicMock()
            c.name.return_value = name
            children.append(c)
        fake_proc = MagicMock()
        fake_proc.children.return_value = children
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "devops", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        lead.session.write.assert_not_called()

    def test_scaffolding_only_children_stay_silent_posix(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """POSIX side of the same #272 guarantee: no ConPTY console-host
        pair exists there, so only the provider's own confirmed scaffolding
        (`scaffolding_process_names`, unsuffixed on POSIX — e.g. claude's
        bundled `node` runtime) is expected to be filtered."""
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "claude",
        )
        child = MagicMock()
        child.name.return_value = "node"
        fake_proc = MagicMock()
        fake_proc.children.return_value = [child]
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "devops", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        lead.session.write.assert_not_called()

    def test_codex_scaffolding_stays_silent(self, orch: Orchestrator, monkeypatch) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "codex",
        )
        children = []
        for name in ("cmd.exe", "conhost.exe", "node.exe", "codex-code-mode-host.exe"):
            c = MagicMock()
            c.name.return_value = name
            children.append(c)
        fake_proc = MagicMock()
        fake_proc.children.return_value = children
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "codex", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        lead.session.write.assert_not_called()

    @pytest.mark.parametrize("shell_name", ["pwsh.exe", "powershell.exe"])
    def test_codex_powershell_shell_host_stays_silent(
        self, orch: Orchestrator, monkeypatch, shell_name: str
    ) -> None:
        """#286: codex's Windows shell-tool host outlives each command, so it
        stands under the pane at close time on EVERY done — prod events.log
        for 2026-08-17 shows all 10 codex-pane closes firing with exactly
        `["pwsh.exe"]`, one extra Lead message after every single report.
        `powershell.exe` is parametrised in because spawn_engine falls back to
        it when PowerShell 7 is absent from PATH."""
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "codex",
        )
        child = MagicMock()
        child.name.return_value = shell_name
        fake_proc = MagicMock()
        fake_proc.children.return_value = [child]
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "frontend", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        lead.session.write.assert_not_called()

    @pytest.mark.parametrize("platform", ["win32", "linux"])
    @pytest.mark.parametrize("provider", ["claude", "codex", "gemini"])
    def test_own_takkub_cli_call_stays_silent(
        self, orch: Orchestrator, monkeypatch, platform: str, provider: str
    ) -> None:
        """#286: the pane's own in-flight `takkub done` is what SCHEDULED the
        close this warning fires from, so it is guaranteed present on every
        done-close regardless of provider or OS. Warning that it is about to
        be killed is self-reference, never a report of lost work."""
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: provider,
        )
        child = MagicMock()
        child.name.return_value = "takkub.exe" if platform == "win32" else "takkub"
        fake_proc = MagicMock()
        fake_proc.children.return_value = [child]
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "devops", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        lead.session.write.assert_not_called()

    def test_real_work_beside_scaffolding_still_warns(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """The filter must stay a subtraction, not a mute button: a codex pane
        closing on a live `docker` build still gets reported even though its
        own pwsh/takkub scaffolding is standing right beside it (#286 widened
        the filter — this pins that #234's actual purpose survived it)."""
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "codex",
        )
        children = []
        for name in ("pwsh.exe", "takkub.exe", "docker.exe"):
            c = MagicMock()
            c.name.return_value = name
            children.append(c)
        fake_proc = MagicMock()
        fake_proc.children.return_value = children
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "frontend", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        written = "".join(
            c.args[0].decode() if isinstance(c.args[0], bytes) else str(c.args[0])
            for c in lead.session.write.call_args_list
        )
        assert "frontend closing" in written
        assert "1 subprocess" in written
        assert "docker.exe" in written
        assert "pwsh" not in written

    def test_codex_scaffolding_stays_silent_posix(self, orch: Orchestrator, monkeypatch) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "codex",
        )
        children = []
        for name in ("node", "codex-code-mode-host"):
            c = MagicMock()
            c.name.return_value = name
            children.append(c)
        fake_proc = MagicMock()
        fake_proc.children.return_value = children
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "codex", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        lead.session.write.assert_not_called()

    def test_kimi_python_scaffolding_stays_silent(self, orch: Orchestrator, monkeypatch) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "kimi",
        )
        child = MagicMock()
        child.name.return_value = "python.exe"
        fake_proc = MagicMock()
        fake_proc.children.return_value = [child]
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "kimi", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        lead.session.write.assert_not_called()

    def test_real_work_still_warns_past_scaffolding(self, orch: Orchestrator, monkeypatch) -> None:
        """#234 must not regress: real work (docker/pytest/build tooling)
        surviving the scaffolding filter still warns, and the count/detail
        reflect only the real work — not the scaffolding noise."""
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "claude",
        )
        children = []
        for name in ("cmd.exe", "conhost.exe", "node.exe", "docker"):
            c = MagicMock()
            c.name.return_value = name
            children.append(c)
        fake_proc = MagicMock()
        fake_proc.children.return_value = children
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "devops", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        written = "".join(
            c.args[0].decode() if isinstance(c.args[0], bytes) else str(c.args[0])
            for c in lead.session.write.call_args_list
        )
        assert "1 subprocess" in written
        assert "docker" in written
        assert "node.exe" not in written
        assert "cmd.exe" not in written

    def test_real_work_still_warns_past_scaffolding_posix(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """POSIX side of the same #234/#272 guarantee: no cmd.exe/conhost.exe
        pair to filter there, but the provider's own unsuffixed scaffolding
        (`node`) must still be subtracted, leaving only real work (`docker`)
        in the count/detail."""
        _register(orch, LEAD.name, _make_alive_session())
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "claude",
        )
        children = []
        for name in ("node", "docker"):
            c = MagicMock()
            c.name.return_value = name
            children.append(c)
        fake_proc = MagicMock()
        fake_proc.children.return_value = children
        monkeypatch.setattr("psutil.Process", lambda pid: fake_proc)

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch._warn_if_live_children(PROJECT, "devops", _make_alive_session())

        lead = orch._panes_by_project[PROJECT][LEAD.name]
        written = "".join(
            c.args[0].decode() if isinstance(c.args[0], bytes) else str(c.args[0])
            for c in lead.session.write.call_args_list
        )
        assert "1 subprocess" in written
        assert "docker" in written
        assert "node" not in written

    def test_close_calls_warn_before_terminate(self, orch: Orchestrator, monkeypatch) -> None:
        _register(orch, LEAD.name, _make_alive_session())
        session = _make_alive_session()
        _register(orch, "devops", session)

        calls: list[str] = []
        monkeypatch.setattr(orch, "_warn_if_live_children", lambda *a, **k: calls.append("warned"))
        session.terminate = MagicMock(side_effect=lambda *a, **k: calls.append("terminated"))

        with patch("agent_takkub.orchestrator.QTimer.singleShot"):
            orch.close("devops", project=PROJECT)

        assert calls == ["warned", "terminated"], "must warn BEFORE terminate() kills the tree"
