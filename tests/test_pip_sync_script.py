"""Self-update dependency-sync script builder (update_helper.build_pip_sync_script).

When a self-update pull changes pyproject.toml, the cockpit must re-run
`pip install -e .` before booting the new version or it runs against stale
deps. These tests pin the detached-script builder that does it — mirroring the
Claude-CLI updater pattern (wait for cockpit exit → install → relaunch, even on
failure so the user is never bricked).
"""

from __future__ import annotations

from agent_takkub.update_helper import build_pip_sync_script


class TestBuildPipSyncScript:
    def test_windows_script_shape(self) -> None:
        s = build_pip_sync_script(
            python_exe="C:/p/python.exe",
            repo_root="C:/repo",
            log_path="C:/repo/runtime/pip_sync.log",
            is_windows=True,
            cockpit_pid=4242,
        )
        assert "Get-Process -Id 4242" in s  # waits for cockpit to exit
        assert 'pip install -e "C:/repo"' in s  # installs the freshly-pulled deps
        assert "$LASTEXITCODE" in s
        assert "C:/repo/runtime/pip_sync.log.failed" in s  # failure sentinel
        assert "agent_takkub" in s and "C:/p/python.exe" in s  # relaunch

    def test_posix_script_shape(self) -> None:
        s = build_pip_sync_script(
            python_exe="/usr/bin/python3",
            repo_root="/home/u/repo",
            log_path="/home/u/repo/runtime/pip_sync.log",
            is_windows=False,
            cockpit_pid=4242,
        )
        assert "kill -0" in s and "4242" in s
        assert 'pip install -e "/home/u/repo"' in s
        assert "pip_sync.log.failed" in s
        assert "agent_takkub" in s

    def test_waits_before_installing(self) -> None:
        # The pid-wait MUST precede the pip install (let the venv free up first).
        for is_win in (True, False):
            s = build_pip_sync_script("py", "/r", "/r/log", is_win, 4242)
            wait_tok = "Get-Process -Id" if is_win else "kill -0"
            assert s.index(wait_tok) < s.index("pip install -e")

    def test_relaunches_after_install_line(self) -> None:
        # Relaunch comes AFTER the install so the new deps are present; and it is
        # unconditional (never gated on the install exit code) → no brick.
        for is_win in (True, False):
            s = build_pip_sync_script("py", "/r", "/r/log", is_win, 4242)
            assert s.index("pip install -e") < s.rindex("agent_takkub")
