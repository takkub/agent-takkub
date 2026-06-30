"""Regression: a deleted project folder must not hang the cockpit.

`spawn_pty` is called synchronously on the Qt main thread by
`PtySession.spawn`. pywinpty's ConPTY backend hangs *forever* when handed a
non-existent cwd (it never returns and never raises, so the ConPTY→WinPTY
except-fallback never fires either), freezing the whole GUI. Repro in the
field: delete a project's folder on disk, reopen the cockpit — Lead spawns
into the now-missing `lead_cwd` and the window locks up.

The guard in `spawn_pty` raises `NotADirectoryError` before touching either
backend so spawn()'s try/except turns it into a clean "spawn failed" message
on every platform. See `_pty_backend.spawn_pty` and `config.project_folder_exists`.
"""

from __future__ import annotations

import time

import pytest

from agent_takkub import config
from agent_takkub._pty_backend import spawn_pty


def test_spawn_pty_raises_fast_on_missing_cwd(tmp_path) -> None:
    missing = tmp_path / "deleted-project-folder"
    assert not missing.exists()

    t0 = time.time()
    with pytest.raises(NotADirectoryError):
        spawn_pty(["cmd", "/c", "echo", "hi"], cwd=str(missing))
    # Must fail fast, not hang on the ConPTY call.
    assert time.time() - t0 < 5.0


def test_spawn_pty_raises_when_cwd_is_a_file(tmp_path) -> None:
    f = tmp_path / "not-a-dir.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        spawn_pty(["cmd", "/c", "echo", "hi"], cwd=str(f))


def test_spawn_pty_allows_none_cwd(monkeypatch) -> None:
    # cwd=None means "inherit parent" — must skip the existence guard. Stub the
    # backend so the test doesn't actually launch a process.
    called: dict = {}

    class _Stub:
        @classmethod
        def spawn(cls, argv, cwd, env, rows, cols):
            called["cwd"] = cwd
            return object()

    import agent_takkub._pty_backend as backend

    monkeypatch.setattr(backend, "_WinptyBackend", _Stub)
    monkeypatch.setattr(backend, "_PosixBackend", _Stub)
    spawn_pty(["echo", "hi"], cwd=None)
    assert called["cwd"] is None


def test_project_folder_exists(tmp_path, monkeypatch) -> None:
    real = tmp_path / "live-project"
    real.mkdir()
    projects = {
        "active": "live",
        "projects": {
            "live": {"paths": {"web": str(real)}},
            "dead": {"paths": {"web": str(tmp_path / "gone")}},
            "empty": {"paths": {}},
        },
    }
    monkeypatch.setattr(config, "load_projects", lambda: projects)

    assert config.project_folder_exists("live") is True
    assert config.project_folder_exists("dead") is False
    assert config.project_folder_exists("empty") is False
    assert config.project_folder_exists("unknown") is False
    assert config.project_folder_exists(None) is False
