"""Phase D — installed-mode integration gate.

Guards against the "prod cockpit breaks, dev tests stay green" bug class
(fixed for real in 8a06c52 / the TAKKUB_PORT_FILE stamping bug): a dev
checkout has ``DATA_HOME == REPO_ROOT`` so every installed-only code path
(``ASSETS_ROOT`` under ``_assets/``, ``CLI_BIN_DIR`` under the venv's own
Scripts/bin, isolated ``SETTINGS_HOME``/``CLAUDE_CONFIG_DIR``) is silently
skipped by every other test in this suite — none of them prove the installed
branch even imports, let alone works.

Builds one real wheel + venv per test session (see
``test_installed_cli_bin_integration.py`` for the same pattern) and runs
every assertion FROM the venv's own interpreter via subprocess — importing
the installed package into this dev-venv pytest process would just
re-exercise the dev-checkout code paths (``config.REPO_ROOT`` is derived from
``Path(__file__)``, so it always points at wherever the *running* interpreter
loaded the module from).

``--no-deps``: verified at commit time that the whole config → lead_context →
pane_env → cli import chain is stdlib + intra-package only (no third-party
runtime deps) — see the individual test docstrings below. Keeps this test
fast and keeps CI from needing to download PyQt6 just to prove pane-env/CLI
wiring.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The `installed_venv` session fixture (real wheel build + throwaway venv)
# lives in conftest.py, shared with test_installed_cli_bin_integration.py —
# see conftest.py's `installed_venv` docstring for why (#388: two separate,
# unlocked copies of this fixture used to race on the same repo_root/build/
# staging dir under pytest-xdist).


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if sys.platform == "win32" else "bin")


@pytest.fixture()
def installed_home(tmp_path: Path) -> Path:
    home = tmp_path / "agent-takkub-home"
    home.mkdir()
    return home


def _run_in_venv(venv_dir: Path, home: Path, code: str) -> dict:
    """Run *code* under the venv's OWN interpreter with ``AGENT_TAKKUB_HOME``
    set, returning the JSON dict printed on stdout.

    Must run through the venv's interpreter, not be imported here — see
    module docstring.
    """
    env = dict(os.environ)
    # A caller's own PYTHONPATH (e.g. a worktree pane's documented workaround
    # for the shared-venv/#202 checkout-mismatch guard in this suite's own
    # conftest.py) would otherwise leak into this subprocess and shadow the
    # installed wheel with source files straight off PYTHONPATH — silently
    # defeating the whole point of this test file (isolation from exactly
    # that class of bug, see the module docstring). Strip it so this always
    # exercises the real installed package, regardless of the parent
    # process's own PYTHONPATH.
    env.pop("PYTHONPATH", None)
    env["AGENT_TAKKUB_HOME"] = str(home)
    result = subprocess.run(
        [str(_venv_python(venv_dir)), "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, f"venv script failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestInstalledConfigIdentity:
    """config.py must resolve DATA_HOME/ASSETS_ROOT/CLI_BIN_DIR to the
    installed layout, not fall back to (or collide with) a dev checkout."""

    def test_data_and_settings_home_isolated_to_installed_home(
        self, installed_venv: Path, installed_home: Path
    ) -> None:
        out = _run_in_venv(
            installed_venv,
            installed_home,
            """
            import json
            from agent_takkub import config
            print(json.dumps({
                "data_home": str(config.DATA_HOME),
                "settings_home": str(config.SETTINGS_HOME),
                "repo_root": str(config.REPO_ROOT),
                "is_installed": config.is_installed_package(),
            }))
            """,
        )
        assert Path(out["data_home"]) == installed_home
        assert Path(out["settings_home"]) == installed_home
        assert out["data_home"] != out["repo_root"]
        assert out["is_installed"] is True

    def test_assets_root_ships_claude_md_and_role_files(
        self, installed_venv: Path, installed_home: Path
    ) -> None:
        out = _run_in_venv(
            installed_venv,
            installed_home,
            """
            import json
            from agent_takkub import config
            agent_files = (
                sorted(p.name for p in config.AGENTS_DIR.glob("*.md"))
                if config.AGENTS_DIR.is_dir() else []
            )
            print(json.dumps({
                "assets_root": str(config.ASSETS_ROOT),
                "claude_md_exists": (config.ASSETS_ROOT / "CLAUDE.md").is_file(),
                "agent_files": agent_files,
            }))
            """,
        )
        assert Path(out["assets_root"]) != _REPO_ROOT
        assert "_assets" in out["assets_root"]
        assert out["claude_md_exists"] is True
        assert len(out["agent_files"]) >= 10

    def test_cli_bin_dir_has_real_takkub_console_script(
        self, installed_venv: Path, installed_home: Path
    ) -> None:
        script_name = "takkub.exe" if sys.platform == "win32" else "takkub"
        out = _run_in_venv(
            installed_venv,
            installed_home,
            """
            import json
            from agent_takkub import config
            print(json.dumps({"cli_bin_dir": str(config.CLI_BIN_DIR)}))
            """,
        )
        assert Path(out["cli_bin_dir"]) == _venv_bin_dir(installed_venv).resolve()
        assert (Path(out["cli_bin_dir"]) / script_name).exists()

    # app._instance_lock_key (two different DATA_HOMEs → different lock keys)
    # already has direct unit coverage in TestInstanceLockKey
    # (test_single_instance_watchdog.py) — it's a pure function of DATA_HOME,
    # so a dev-process unit test already proves the invariant; re-deriving it
    # against an installed venv here would need importing the Qt-heavy `app`
    # module into this no-deps venv for no additional coverage.


class TestInstalledLeadContext:
    def test_render_lead_context_produces_a_real_prompt(
        self, installed_venv: Path, installed_home: Path
    ) -> None:
        out = _run_in_venv(
            installed_venv,
            installed_home,
            """
            import json
            from pathlib import Path
            from agent_takkub.lead_context import _render_lead_context
            path = _render_lead_context()
            text = Path(path).read_text(encoding="utf-8") if path else ""
            print(json.dumps({
                "path": path,
                "has_assign": "takkub assign" in text,
                "length": len(text),
            }))
            """,
        )
        assert out["path"] is not None
        rendered = Path(out["path"])
        assert rendered.is_absolute()
        assert installed_home in rendered.parents
        assert out["has_assign"] is True
        assert out["length"] > 500

    def test_docs_lead_shipped_and_rewritten_to_resolvable_paths(
        self, installed_venv: Path, installed_home: Path
    ) -> None:
        """Regression cover: an installed build's CLAUDE.md references
        `docs/lead/patterns.md` / `docs/lead/cli-reference.md` by relative
        path, which only resolves from a dev checkout's cwd. Proves both
        halves of the fix from the real, packaged wheel: the files are
        staged inside ASSETS_ROOT/docs/lead/, and the rendered Lead prompt
        points at those real, readable absolute paths instead of the bare
        (dangling, on an installed build) relative reference."""
        out = _run_in_venv(
            installed_venv,
            installed_home,
            """
            import json
            from pathlib import Path
            from agent_takkub import config
            from agent_takkub.lead_context import _render_lead_context
            docs_lead_dir = config.ASSETS_ROOT / "docs" / "lead"
            staged = sorted(p.name for p in docs_lead_dir.glob("*.md"))
            path = _render_lead_context()
            text = Path(path).read_text(encoding="utf-8") if path else ""
            print(json.dumps({
                "docs_lead_dir": str(docs_lead_dir),
                "staged": staged,
                "has_bare_reference": "`docs/lead/patterns.md`" in text,
                "has_rewritten_reference": (docs_lead_dir / "patterns.md").as_posix() in text,
            }))
            """,
        )
        assert "patterns.md" in out["staged"]
        assert "cli-reference.md" in out["staged"]
        docs_lead_dir = Path(out["docs_lead_dir"])
        assert (docs_lead_dir / "patterns.md").is_file()
        assert (docs_lead_dir / "cli-reference.md").is_file()
        assert out["has_bare_reference"] is False
        assert out["has_rewritten_reference"] is True


class TestInstalledPaneEnv:
    def test_pane_and_lead_env_stamp_port_file_and_claude_config_dir(
        self, installed_venv: Path, installed_home: Path
    ) -> None:
        out = _run_in_venv(
            installed_venv,
            installed_home,
            """
            import json
            from agent_takkub import config
            from agent_takkub.pane_env import (
                _build_pane_env,
                _build_lead_env,
                inject_user_profile_env,
            )
            pane_env = _build_pane_env()
            lead_env = _build_lead_env()
            inject_user_profile_env(pane_env, "smoke-project")
            print(json.dumps({
                "expected_port_file": str(config._get_port_file()),
                "pane_port_file": pane_env.get("TAKKUB_PORT_FILE"),
                "lead_port_file": lead_env.get("TAKKUB_PORT_FILE"),
                "pane_claude_config_dir": pane_env.get("CLAUDE_CONFIG_DIR"),
                "expected_claude_config_dir": str(config.DATA_HOME / "claude-config"),
            }))
            """,
        )
        assert out["pane_port_file"] == out["expected_port_file"]
        assert out["lead_port_file"] == out["expected_port_file"]
        assert Path(out["pane_port_file"]) == installed_home / "runtime" / "port"
        assert out["pane_claude_config_dir"] == out["expected_claude_config_dir"]


class TestInstalledCliPortFileWiring:
    """Proves the ACTUAL packaged `takkub` console script — not
    `python -m agent_takkub.cli` — reads TAKKUB_PORT_FILE end to end. A
    connection-refused error (not "no port file") proves the file's contents
    were read; the two error messages come from different code paths
    (config.read_port() returning a real port vs. returning None)."""

    def test_status_reads_takkub_port_file_and_fails_with_connection_refused(
        self, installed_venv: Path, installed_home: Path, tmp_path: Path
    ) -> None:
        script_name = "takkub.exe" if sys.platform == "win32" else "takkub"
        takkub_bin = _venv_bin_dir(installed_venv) / script_name
        assert takkub_bin.exists()

        # A port nothing is listening on: bind ephemeral, then release it.
        # 127.0.0.1 refuses connections to a closed port immediately (no
        # listen backlog to time out on), so this is fast and non-flaky in
        # practice.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        fake_port = probe.getsockname()[1]
        probe.close()

        fake_port_file = tmp_path / "fake-port"
        fake_port_file.write_text(str(fake_port), encoding="utf-8")

        env = dict(os.environ)
        env["AGENT_TAKKUB_HOME"] = str(installed_home)
        env["TAKKUB_PORT_FILE"] = str(fake_port_file)

        result = subprocess.run(
            [str(takkub_bin), "status"],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )

        assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        combined = (result.stdout + result.stderr).lower()
        assert "refused" in combined
        assert "no port file" not in combined
        assert "cockpit is not running" not in combined


class TestInstalledRemoteAssets:
    """The wheel used in production must include the current Remote PWA."""

    def test_wheel_ships_resume_and_project_stream_assets(
        self, installed_venv: Path, installed_home: Path
    ) -> None:
        out = _run_in_venv(
            installed_venv,
            installed_home,
            """
            import json
            from pathlib import Path
            import agent_takkub

            # Locate packaged data without importing ``agent_takkub.remote``;
            # this deliberately no-deps production probe does not install Qt.
            static = Path(agent_takkub.__file__).parent / "remote" / "static"
            app = (static / "app.js").read_text(encoding="utf-8")
            sw = (static / "sw.js").read_text(encoding="utf-8")
            print(json.dumps({
                "has_project_state": "leadByProject: {}" in app,
                "has_sessions_api": "api/lead/sessions" in app,
                "has_resume_api": "api/lead/resume" in app,
                "has_upload_api": "api/lead/upload" in app,
                "has_cache_v33": "takkub-remote-shell-v33" in sw,
            }))
            """,
        )
        assert out == {
            "has_project_state": True,
            "has_sessions_api": True,
            "has_resume_api": True,
            "has_upload_api": True,
            "has_cache_v33": True,
        }


class TestInstalledEditorAssets:
    """The wheel must ship the editor page + its vendored Monaco bundle
    (#365 phase 2) — package_data is additive/opt-in per pattern, so a typo'd
    or missing pattern silently drops files with no build-time error."""

    def test_wheel_ships_editor_page_and_vendored_monaco(
        self, installed_venv: Path, installed_home: Path
    ) -> None:
        out = _run_in_venv(
            installed_venv,
            installed_home,
            """
            import json
            from pathlib import Path
            import agent_takkub

            editor = Path(agent_takkub.__file__).parent / "static" / "editor"
            vendor_vs = editor / "vendor" / "monaco-editor" / "min" / "vs"
            print(json.dumps({
                "index_html": (editor / "index.html").is_file(),
                "loader_js": (vendor_vs / "loader.js").is_file(),
                "editor_main_js": (vendor_vs / "editor" / "editor.main.js").is_file(),
                "monaco_license": (editor / "vendor" / "monaco-editor" / "min" / "LICENSE").is_file(),
            }))
            """,
        )
        assert out == {
            "index_html": True,
            "loader_js": True,
            "editor_main_js": True,
            "monaco_license": True,
        }


# `_cross_process_lock`/`_build_or_reuse_wheel` unit coverage moved to
# tests/test_wheel_build_lock.py (#388) — those functions now live in
# conftest.py (as `_cross_process_wheel_lock`/`_build_or_reuse_wheel`),
# shared with test_installed_cli_bin_integration.py.
