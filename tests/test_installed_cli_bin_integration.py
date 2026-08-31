"""Integration proof for the npm-wrapper -> venv -> console-script path
(docs/audit/2026-07-05-isolation-plan-crosscheck-codex.md, finding 1 / "B.
ASSETS_ROOT and CLI_BIN_DIR").

Builds a real wheel from the current source tree, installs it (--no-deps, so
this stays fast — PyQt6 isn't needed to prove script placement) into a fresh
venv, and asserts the `takkub` console script lands in the SAME directory as
python.exe/pythonw.exe — the invariant config._resolve_cli_bin_dir() depends
on (`Path(sys.executable).resolve().parent`). Both npm launch paths
(`agent-takkub.js` -> venvPythonIfExists() -> python.exe, and the Desktop
shortcut -> venvPythonw() -> pythonw.exe) must resolve to that same dir.

Slow (builds a wheel + creates a venv) — kept as one session-scoped build.

The `installed_venv` fixture itself lives in conftest.py, shared with
test_installed_mode_gate.py (#388: two separate, unlocked copies of this
fixture used to race on the same repo_root/build/ staging dir under
pytest-xdist, producing spurious CI failures).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_takkub import config

# Real `python -m build` + `pip install` + subprocess-per-assertion — not
# deselected by default (still runs in the batch gate), just tagged so the
# cost is visible and `-m "not slow"` is available for a fast local loop.
pytestmark = pytest.mark.slow


class TestNpmWrapperConsoleScriptParity:
    def test_takkub_console_script_exists_next_to_python(self, installed_venv: Path) -> None:
        scripts_dir = installed_venv / ("Scripts" if sys.platform == "win32" else "bin")
        script_name = "takkub.exe" if sys.platform == "win32" else "takkub"
        assert (scripts_dir / script_name).exists(), (
            f"pip install did not generate a takkub console script under {scripts_dir} — "
            "the npm wrapper's venvPythonIfExists()/venvPythonw() launch path and "
            "config.CLI_BIN_DIR both assume it lives there"
        )

    def test_agent_takkub_console_script_exists_next_to_python(self, installed_venv: Path) -> None:
        scripts_dir = installed_venv / ("Scripts" if sys.platform == "win32" else "bin")
        script_name = "agent-takkub.exe" if sys.platform == "win32" else "agent-takkub"
        assert (scripts_dir / script_name).exists()

    def test_python_exe_and_pythonw_exe_share_the_same_scripts_dir(
        self, installed_venv: Path
    ) -> None:
        # Both npm launch paths (CLI via python.exe, Desktop shortcut via
        # pythonw.exe on Windows) must resolve sys.executable's parent to the
        # SAME directory the console script was installed into.
        if sys.platform != "win32":
            pytest.skip("pythonw.exe is Windows-only")
        scripts_dir = installed_venv / "Scripts"
        assert (scripts_dir / "python.exe").exists()
        assert (scripts_dir / "pythonw.exe").exists()
        assert (scripts_dir / "python.exe").resolve().parent == (
            scripts_dir / "pythonw.exe"
        ).resolve().parent

    def test_resolve_cli_bin_dir_matches_scripts_dir_for_either_launcher(
        self, installed_venv: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripts_dir = installed_venv / ("Scripts" if sys.platform == "win32" else "bin")
        candidates = ["python.exe", "pythonw.exe"] if sys.platform == "win32" else ["python"]
        monkeypatch.setattr(config, "REPO_ROOT", installed_venv / "Lib")
        monkeypatch.setattr(config, "DATA_HOME", installed_venv.parent / "agent-takkub-home")
        for exe_name in candidates:
            monkeypatch.setattr(sys, "executable", str(scripts_dir / exe_name))
            assert config._resolve_cli_bin_dir() == scripts_dir.resolve()
