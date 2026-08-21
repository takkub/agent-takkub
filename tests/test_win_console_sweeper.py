"""Console windows that appear long after a pane spawns (2026-08-21).

`pty_session` swept for stray console windows only in the 3.5 s around the PTY
spawn — enough for the conhost pywinpty surfaces, but codex opens a `pwsh.exe`
shell-tool host every time it runs a shell command, minutes in. Nothing was
watching by then, so a blank PowerShell window popped up over the cockpit
again and again.

The sweep therefore had to become periodic, and a periodic blind sweep is not
acceptable: hiding "any new console window" would hide a terminal the USER
opened. `hide_own_console_windows` only touches windows whose owning process
is a descendant of the cockpit.
"""

from __future__ import annotations

import sys

import pytest

from agent_takkub import _win_console


@pytest.fixture
def fake_windows(monkeypatch: pytest.MonkeyPatch):
    """Replace the three Win32 touchpoints with plain data, so this runs on
    every OS in CI, not just the one that has the bug."""
    state = {"hwnds": set(), "pids": {}, "tree": set(), "hidden": []}

    monkeypatch.setattr(_win_console.sys, "platform", "win32")
    monkeypatch.setattr(_win_console, "snapshot_console_hwnds", lambda: set(state["hwnds"]))
    monkeypatch.setattr(_win_console, "_window_pid", lambda hwnd: state["pids"].get(hwnd))
    monkeypatch.setattr(
        _win_console,
        "window_class",
        lambda hwnd: state.get("classes", {}).get(hwnd, "ConsoleWindowClass"),
    )
    monkeypatch.setattr(
        _win_console, "_is_descendant_of", lambda pid, root, **kw: pid in state["tree"]
    )
    monkeypatch.setattr(
        _win_console, "hide_hwnds", lambda hwnds: state["hidden"].append(set(hwnds)) or len(hwnds)
    )
    return state


class TestConsoleWindowClasses:
    def test_covers_windows_terminal_console_hosting(self) -> None:
        """Matching only `ConsoleWindowClass` made the sweeper blind on
        Windows 11, where the default terminal application is Windows
        Terminal and Windows COM-activates it to host a new console.
        Captured live: a "Terminal" window (CASCADIA_HOSTING_WINDOW_CLASS,
        WindowsTerminal.exe) plus PseudoConsoleWindow (OpenConsole.exe)."""
        assert _win_console.CONSOLE_WINDOW_CLASSES == {
            "ConsoleWindowClass",
            "PseudoConsoleWindow",
            "CASCADIA_HOSTING_WINDOW_CLASS",
        }


class TestOwnershipScoping:
    def test_periodic_sweep_never_hides_a_windows_terminal_window(self, fake_windows) -> None:
        """A COM-activated Windows Terminal is not in our process tree, so on
        this path it is indistinguishable from the terminal the user opened —
        even when the parent walk happens to say yes."""
        fake_windows["hwnds"] = {303}
        fake_windows["pids"] = {303: 5555}
        fake_windows["tree"] = {5555}
        fake_windows["classes"] = {303: "CASCADIA_HOSTING_WINDOW_CLASS"}

        _win_console.hide_own_console_windows(1, set())

        assert fake_windows["hidden"] == []

    def test_periodic_sweep_hides_a_visible_pseudoconsole_host(self, fake_windows) -> None:
        """A healthy terminal keeps its ConPTY host window hidden, so a
        visible one in our own tree is the anomaly this sweeper exists for."""
        fake_windows["hwnds"] = {404}
        fake_windows["pids"] = {404: 5555}
        fake_windows["tree"] = {5555}
        fake_windows["classes"] = {404: "PseudoConsoleWindow"}

        _win_console.hide_own_console_windows(1, set())

        assert fake_windows["hidden"] == [{404}]

    def test_hides_a_console_opened_by_our_own_process_tree(self, fake_windows) -> None:
        fake_windows["hwnds"] = {101}
        fake_windows["pids"] = {101: 5555}
        fake_windows["tree"] = {5555}

        _win_console.hide_own_console_windows(1, set())

        assert fake_windows["hidden"] == [{101}]

    def test_never_hides_a_terminal_the_user_opened(self, fake_windows) -> None:
        """The whole reason the sweep is ownership-scoped instead of blind."""
        fake_windows["hwnds"] = {202}
        fake_windows["pids"] = {202: 9999}  # not in our tree
        fake_windows["tree"] = set()

        _win_console.hide_own_console_windows(1, set())

        assert fake_windows["hidden"] == []

    def test_mixed_sweep_hides_only_ours(self, fake_windows) -> None:
        fake_windows["hwnds"] = {101, 202}
        fake_windows["pids"] = {101: 5555, 202: 9999}
        fake_windows["tree"] = {5555}

        _win_console.hide_own_console_windows(1, set())

        assert fake_windows["hidden"] == [{101}]

    def test_a_window_with_no_resolvable_pid_is_left_alone(self, fake_windows) -> None:
        fake_windows["hwnds"] = {303}
        fake_windows["pids"] = {}

        _win_console.hide_own_console_windows(1, set())

        assert fake_windows["hidden"] == []


class TestSweepCost:
    def test_each_window_is_ruled_on_once(self, fake_windows) -> None:
        """This runs on a timer for the life of the process — re-resolving the
        same HWND's ancestry every two seconds forever is the thing to avoid."""
        fake_windows["hwnds"] = {101}
        fake_windows["pids"] = {101: 5555}
        fake_windows["tree"] = {5555}
        seen: set[int] = set()

        _win_console.hide_own_console_windows(1, seen)
        _win_console.hide_own_console_windows(1, seen)
        _win_console.hide_own_console_windows(1, seen)

        assert fake_windows["hidden"] == [{101}], "only the first sweep should act"
        assert seen == {101}

    def test_a_window_appearing_later_is_still_caught(self, fake_windows) -> None:
        """The actual failure mode: the pwsh window shows up minutes after
        spawn, long after the seen-set was first populated."""
        fake_windows["hwnds"] = {101}
        fake_windows["pids"] = {101: 5555}
        fake_windows["tree"] = {5555}
        seen: set[int] = set()
        _win_console.hide_own_console_windows(1, seen)

        fake_windows["hwnds"] = {101, 404}
        fake_windows["pids"][404] = 6666
        fake_windows["tree"].add(6666)
        _win_console.hide_own_console_windows(1, seen)

        assert fake_windows["hidden"] == [{101}, {404}]


class TestNonWindows:
    def test_noop_off_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_win_console.sys, "platform", "darwin")
        called: list = []
        monkeypatch.setattr(_win_console, "snapshot_console_hwnds", lambda: called.append(1))

        assert _win_console.hide_own_console_windows(1, set()) == set()
        assert not called


class TestSweeperWiring:
    @staticmethod
    def _fake_app(platform_name: str):
        return type("App", (), {"platformName": staticmethod(lambda: platform_name)})()

    def _install(self, monkeypatch, platform_name="windows", plat="win32"):
        """Stand in for the Qt side so these run on any OS."""
        from agent_takkub import pty_session

        made: list = []

        class _FakeTimer:
            def __init__(self, parent=None) -> None:
                made.append(parent)
                self.timeout = type("S", (), {"connect": lambda _s, _f: None})()

            def setInterval(self, ms) -> None: ...
            def start(self) -> None: ...

        app = self._fake_app(platform_name)
        monkeypatch.setattr(pty_session, "_console_sweeper", None)
        monkeypatch.setattr(pty_session.sys, "platform", plat)
        monkeypatch.setattr(pty_session, "QTimer", _FakeTimer)
        monkeypatch.setattr(pty_session.QCoreApplication, "instance", staticmethod(lambda: app))
        return pty_session, made, app

    def test_sweeper_is_a_single_process_wide_timer(self, monkeypatch) -> None:
        """A timer per pane would multiply an EnumWindows sweep by the pane
        count for no benefit."""
        pty_session, made, _app = self._install(monkeypatch)

        pty_session._ensure_console_sweeper()
        pty_session._ensure_console_sweeper()
        pty_session._ensure_console_sweeper()

        assert len(made) == 1

    def test_timer_is_owned_by_the_application(self, monkeypatch) -> None:
        """An ownerless QObject outlives QApplication teardown and takes the
        process down with it — the abort that killed CI's Windows job."""
        pty_session, made, app = self._install(monkeypatch)

        pty_session._ensure_console_sweeper()

        assert made == [app]

    def test_never_starts_under_the_offscreen_platform(self, monkeypatch) -> None:
        """Test/headless runs have no OS windows to hide, and a process-wide
        timer started inside one test keeps firing through every later one."""
        pty_session, made, _app = self._install(monkeypatch, platform_name="offscreen")

        pty_session._ensure_console_sweeper()

        assert made == []
        assert pty_session._console_sweeper is None

    def test_never_starts_without_an_application(self, monkeypatch) -> None:
        pty_session, made, _app = self._install(monkeypatch)
        monkeypatch.setattr(pty_session.QCoreApplication, "instance", staticmethod(lambda: None))

        pty_session._ensure_console_sweeper()

        assert made == []

    def test_never_starts_off_windows(self, monkeypatch) -> None:
        pty_session, made, _app = self._install(monkeypatch, plat="linux")

        pty_session._ensure_console_sweeper()

        assert made == []
        assert pty_session._console_sweeper is None


@pytest.mark.skipif(sys.platform != "win32", reason="parent-walk needs a real process table")
def test_descendant_walk_recognises_our_own_process() -> None:
    """Sanity check against the real psutil tree, so the fake above can't
    drift from how ancestry actually resolves."""
    import os

    import psutil

    parent = psutil.Process(os.getpid()).parent()
    if parent is None:
        pytest.skip("no parent process to test against")
    assert _win_console._is_descendant_of(os.getpid(), parent.pid)
    assert not _win_console._is_descendant_of(os.getpid(), os.getpid())
