"""Tests for _NpmUpdateThread and npm update helpers in update_panel.py."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

import agent_takkub.update_panel as up_mod
from agent_takkub.update_panel import (
    _NpmUpdateThread,
    _find_global_postinstall,
    _find_node,
    _find_npm,
)


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


class TestFindHelpers:
    def test_find_npm_delegates_to_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(up_mod.config, "find_npm", lambda: "/custom/bin/npm")
        assert _find_npm() == "/custom/bin/npm"

    def test_find_node_via_which(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            up_mod.shutil, "which", lambda cmd: "/usr/bin/node" if "node" in cmd else None
        )
        assert _find_node() == "/usr/bin/node"

    def test_find_node_next_to_npm(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(up_mod.shutil, "which", lambda cmd: None)
        node_file = tmp_path / "node"
        node_file.write_text("#!/bin/sh\n", encoding="utf-8")
        node_file.chmod(0o755)
        monkeypatch.setattr(up_mod, "_find_npm", lambda: str(tmp_path / "npm"))
        assert _find_node() == str(node_file)

    def test_find_node_in_gui_binary_dirs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(up_mod.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(up_mod, "_find_npm", lambda: None)
        node_file = tmp_path / "node"
        node_file.write_text("#!/bin/sh\n", encoding="utf-8")
        node_file.chmod(0o755)
        monkeypatch.setattr(up_mod.config, "_expand_gui_binary_dirs", lambda: [str(tmp_path)])
        assert _find_node() == str(node_file)

    def test_find_node_returns_none_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(up_mod.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(up_mod, "_find_npm", lambda: None)
        monkeypatch.setattr(up_mod.config, "_expand_gui_binary_dirs", lambda: [])
        assert _find_node() is None

    def test_find_global_postinstall_via_npm_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        postinstall = tmp_path / "agent-takkub" / "npm" / "scripts" / "postinstall.js"
        postinstall.parent.mkdir(parents=True, exist_ok=True)
        postinstall.write_text("// postinstall", encoding="utf-8")

        def fake_run(cmd, **kw):
            if cmd == ["npm", "root", "-g"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=str(tmp_path))
            return subprocess.CompletedProcess(args=cmd, returncode=1)

        with patch("subprocess.run", side_effect=fake_run):
            found = _find_global_postinstall("npm")
            assert found == postinstall

    def test_find_global_postinstall_fallback_to_repo_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo_postinstall = tmp_path / "npm" / "scripts" / "postinstall.js"
        repo_postinstall.parent.mkdir(parents=True, exist_ok=True)
        repo_postinstall.write_text("// postinstall", encoding="utf-8")
        monkeypatch.setattr(up_mod, "REPO_ROOT", tmp_path)

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            found = _find_global_postinstall("npm")
            assert found == repo_postinstall


class TestNpmUpdateThreadCheck:
    def test_check_success(self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(up_mod, "_find_npm", lambda: "/usr/local/bin/npm")
        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("check")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            fake_proc = subprocess.CompletedProcess(
                args=["/usr/local/bin/npm", "view", "agent-takkub", "version"],
                returncode=0,
                stdout="1.0.61\n",
                stderr="",
            )
            with patch("subprocess.run", return_value=fake_proc) as mock_run:
                worker.run()

            assert mock_run.call_count == 1
            cmd = mock_run.call_args[0][0]
            assert cmd == ["/usr/local/bin/npm", "view", "agent-takkub", "version"]
            assert emitted == [(True, "1.0.60", "1.0.61", "")]

    def test_check_failure_nonzero_exit(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(up_mod, "_find_npm", lambda: "/usr/local/bin/npm")
        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("check")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            fake_proc = subprocess.CompletedProcess(
                args=["/usr/local/bin/npm", "view", "agent-takkub", "version"],
                returncode=1,
                stdout="",
                stderr="npm ERR! 404 Not Found",
            )
            with patch("subprocess.run", return_value=fake_proc):
                worker.run()

            assert emitted == [(False, "1.0.60", "", "npm ERR! 404 Not Found")]

    def test_check_failure_empty_version(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(up_mod, "_find_npm", lambda: "/usr/local/bin/npm")
        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("check")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            fake_proc = subprocess.CompletedProcess(
                args=["/usr/local/bin/npm", "view", "agent-takkub", "version"],
                returncode=0,
                stdout="   \n",
                stderr="",
            )
            with patch("subprocess.run", return_value=fake_proc):
                worker.run()

            assert emitted == [(False, "1.0.60", "", "registry check failed")]

    def test_check_npm_not_found(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(up_mod, "_find_npm", lambda: None)
        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("check")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            worker.run()

            assert emitted == [(False, "1.0.60", "", "npm not found on PATH")]

    def test_check_exception_handling(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(up_mod, "_find_npm", lambda: "/usr/local/bin/npm")
        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("check")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            with patch("subprocess.run", side_effect=RuntimeError("connection reset")):
                worker.run()

            assert emitted == [(False, "1.0.60", "", "connection reset")]


class TestNpmUpdateThreadInstall:
    def test_install_success_with_postinstall_trigger(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        postinstall_js = tmp_path / "postinstall.js"
        postinstall_js.write_text("// postinstall", encoding="utf-8")

        monkeypatch.setattr(up_mod, "_find_npm", lambda: "/usr/local/bin/npm")
        monkeypatch.setattr(up_mod, "_find_node", lambda: "/usr/local/bin/node")
        monkeypatch.setattr(up_mod, "_find_global_postinstall", lambda npm: postinstall_js)

        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("install")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            run_calls: list[list[str]] = []

            def fake_run(cmd, **kw):
                run_calls.append(cmd)
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            with patch("subprocess.run", side_effect=fake_run):
                worker.run()

            assert len(run_calls) == 2
            # 1. npm install -g --foreground-scripts agent-takkub@latest
            assert run_calls[0] == [
                "/usr/local/bin/npm",
                "install",
                "-g",
                "--foreground-scripts",
                "agent-takkub@latest",
            ]
            # 2. node <postinstall.js>
            assert run_calls[1] == ["/usr/local/bin/node", str(postinstall_js)]
            assert emitted == [(True, "1.0.60", "", "updated")]

    def test_install_success_when_postinstall_not_found(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(up_mod, "_find_npm", lambda: "/usr/local/bin/npm")
        monkeypatch.setattr(up_mod, "_find_node", lambda: "/usr/local/bin/node")
        monkeypatch.setattr(up_mod, "_find_global_postinstall", lambda npm: None)

        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("install")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            fake_proc = subprocess.CompletedProcess(
                args=[
                    "/usr/local/bin/npm",
                    "install",
                    "-g",
                    "--foreground-scripts",
                    "agent-takkub@latest",
                ],
                returncode=0,
                stdout="added 1 package",
                stderr="",
            )
            with patch("subprocess.run", return_value=fake_proc) as mock_run:
                worker.run()

            assert mock_run.call_count == 1
            assert emitted == [(True, "1.0.60", "", "updated")]

    def test_install_npm_fails(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(up_mod, "_find_npm", lambda: "/usr/local/bin/npm")
        monkeypatch.setattr(up_mod, "_find_node", lambda: "/usr/local/bin/node")
        monkeypatch.setattr(
            up_mod, "_find_global_postinstall", lambda npm: Path("/fake/postinstall.js")
        )

        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("install")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            fake_proc = subprocess.CompletedProcess(
                args=[
                    "/usr/local/bin/npm",
                    "install",
                    "-g",
                    "--foreground-scripts",
                    "agent-takkub@latest",
                ],
                returncode=1,
                stdout="",
                stderr="npm ERR! EACCES: permission denied",
            )
            with patch("subprocess.run", return_value=fake_proc) as mock_run:
                worker.run()

            # npm failed → postinstall trigger must not be called
            assert mock_run.call_count == 1
            assert emitted == [(False, "1.0.60", "", "npm ERR! EACCES: permission denied")]

    def test_install_postinstall_fails(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        postinstall_js = tmp_path / "postinstall.js"
        postinstall_js.write_text("// postinstall", encoding="utf-8")

        monkeypatch.setattr(up_mod, "_find_npm", lambda: "/usr/local/bin/npm")
        monkeypatch.setattr(up_mod, "_find_node", lambda: "/usr/local/bin/node")
        monkeypatch.setattr(up_mod, "_find_global_postinstall", lambda npm: postinstall_js)

        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("install")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            def fake_run(cmd, **kw):
                if "install" in cmd:
                    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
                # postinstall failed
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="[agent-takkub] pip install failed."
                )

            with patch("subprocess.run", side_effect=fake_run):
                worker.run()

            assert emitted == [(False, "1.0.60", "", "[agent-takkub] pip install failed.")]

    def test_install_npm_not_found(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(up_mod, "_find_npm", lambda: None)
        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("install")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            worker.run()

            assert emitted == [(False, "1.0.60", "", "npm not found on PATH")]

    def test_install_exception_handling(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(up_mod, "_find_npm", lambda: "/usr/local/bin/npm")
        with patch("importlib.metadata.version", return_value="1.0.60"):
            worker = _NpmUpdateThread("install")
            emitted: list[tuple] = []
            worker.done.connect(lambda ok, cur, latest, msg: emitted.append((ok, cur, latest, msg)))

            with patch("subprocess.run", side_effect=TimeoutError("npm install timed out")):
                worker.run()

            assert emitted == [(False, "1.0.60", "", "npm install timed out")]
