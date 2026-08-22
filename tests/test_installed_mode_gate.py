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

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import venv
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def _cross_process_lock(lock_path: Path, *, timeout: float = 240.0, poll: float = 0.2):
    """A cross-process mutual-exclusion lock via exclusive file creation.

    ``threading.Lock`` is useless here — under pytest-xdist each worker is a
    *separate process*, not a thread of this one. ``O_CREAT | O_EXCL`` is
    portable (Windows + macOS/Linux) and atomic at the OS level, unlike a
    plain existence check + create.
    """
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out after {timeout}s waiting for lock {lock_path} "
                    "(held by another pytest-xdist worker?)"
                ) from None
            time.sleep(poll)
    try:
        yield
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def _latest_source_mtime() -> float:
    """Newest mtime across everything `python -m build` reads from, so a
    cached wheel can be trusted (or correctly invalidated) without rebuilding
    just to find out."""
    latest = 0.0
    for tracked in (_REPO_ROOT / "pyproject.toml", _REPO_ROOT / "MANIFEST.in"):
        if tracked.exists():
            latest = max(latest, tracked.stat().st_mtime)
    for path in (_REPO_ROOT / "src").rglob("*"):
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
    return latest


def _build_or_reuse_wheel(wheel_cache_dir: Path) -> Path:
    """Build the current source into *wheel_cache_dir*, or reuse whatever's
    already cached there if it's not older than the source tree.

    Caller must hold ``_cross_process_lock`` around this — it mutates a
    directory shared by every pytest-xdist worker in the run.
    """
    cached = sorted(wheel_cache_dir.glob("*.whl"))
    if cached and cached[0].stat().st_mtime >= _latest_source_mtime():
        return cached[0]
    for stale in cached:
        stale.unlink()
    # `python -m build` reuses <repo_root>/build/ as a staging dir across
    # runs; a tree left over from a prior source layout (e.g. a since-renamed
    # _assets file) makes setuptools reference a path that no longer exists
    # and fail with a spurious WinError 2, even though the current source is
    # fine (see build_wheel() in release.py for the same fix). Safe to rmtree
    # unconditionally here: the caller's lock guarantees we're the only
    # worker touching repo_root/build/ right now.
    shutil.rmtree(_REPO_ROOT / "build", ignore_errors=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(wheel_cache_dir),
            str(_REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"wheel build failed:\n{result.stdout}\n{result.stderr}"
    wheels = list(wheel_cache_dir.glob("*.whl"))
    assert wheels, "no wheel produced"
    return wheels[0]


@pytest.fixture(scope="session")
def installed_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway venv with the current source installed as a wheel.

    ``--no-deps``: config/lead_context/pane_env/cli's import chain is stdlib +
    config only (no pyyaml/psutil/PyQt6 needed) — only console-script
    placement + these four modules' behavior matter here.

    Session-scoped, but under pytest-xdist ``scope="session"`` only means
    "once per worker *process*" — loadscope distributes this file's classes
    across workers, so without coordination every worker ran its own
    `python -m build` concurrently into the same repo_root/build/ staging
    dir. One worker's rmtree could delete the tree out from under another
    worker's in-flight build (spurious "does not exist" errors). The lock +
    shared wheel-cache dir (both anchored at ``getbasetemp().parent`` — the
    root all workers in this run share, one level up from each worker's own
    ``.../popen-gwN``) make exactly one worker build per pytest run; the
    rest reuse its wheel.
    """
    shared_root = tmp_path_factory.getbasetemp().parent
    wheel_cache_dir = shared_root / "installed-mode-wheel-cache"
    wheel_cache_dir.mkdir(exist_ok=True)
    lock_path = shared_root / "installed-mode-wheel-build.lock"

    with _cross_process_lock(lock_path):
        wheel = _build_or_reuse_wheel(wheel_cache_dir)

    venv_dir = tmp_path_factory.mktemp("venv-target") / "venv"
    venv.create(venv_dir, with_pip=True)
    vpy = _venv_python(venv_dir)
    assert vpy.exists(), f"venv python missing at {vpy}"

    result = subprocess.run(
        [str(vpy), "-m", "pip", "install", "--no-deps", "--quiet", str(wheel)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"pip install failed:\n{result.stdout}\n{result.stderr}"
    return venv_dir


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
                "has_cache_v27": "takkub-remote-shell-v27" in sw,
            }))
            """,
        )
        assert out == {
            "has_project_state": True,
            "has_sessions_api": True,
            "has_resume_api": True,
            "has_upload_api": True,
            "has_cache_v27": True,
        }


class TestCrossProcessLock:
    """`_cross_process_lock` is what serializes `installed_venv`'s wheel
    build across pytest-xdist worker *processes* — plain fast, no-build unit
    coverage of the locking contract itself."""

    def test_acquire_and_release_removes_lock_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "build.lock"
        with _cross_process_lock(lock_path):
            assert lock_path.exists()
        assert not lock_path.exists()

    def test_times_out_when_lock_already_held(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "build.lock"
        lock_path.write_bytes(b"")  # simulates another worker holding it
        with pytest.raises(TimeoutError):
            with _cross_process_lock(lock_path, timeout=0.05, poll=0.01):
                pytest.fail("should never acquire while the lock file exists")


class TestBuildOrReuseWheel:
    """`_build_or_reuse_wheel` is what makes `python -m build` run once per
    pytest run instead of once per worker (see `installed_venv`'s
    docstring) — proven here without a real build via a fake
    ``subprocess.run`` that drops a placeholder wheel, so this stays fast."""

    def test_second_call_reuses_cached_wheel_without_rebuilding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wheel_cache_dir = tmp_path / "wheel-cache"
        wheel_cache_dir.mkdir()
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            (wheel_cache_dir / "agent_takkub-0.0.0-py3-none-any.whl").write_bytes(b"fake")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)

        first = _build_or_reuse_wheel(wheel_cache_dir)
        second = _build_or_reuse_wheel(wheel_cache_dir)

        assert len(calls) == 1, "second call should have reused the cached wheel, not rebuilt"
        assert first == second

    def test_stale_cached_wheel_triggers_exactly_one_rebuild(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wheel_cache_dir = tmp_path / "wheel-cache"
        wheel_cache_dir.mkdir()
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            (wheel_cache_dir / f"agent_takkub-{len(calls)}-py3-none-any.whl").write_bytes(b"fake")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
        # `_latest_source_mtime` is only consulted once a cached wheel
        # exists (the first call always builds) — always-"infinitely new"
        # source makes every subsequent call see the cache as stale.
        monkeypatch.setattr(f"{__name__}._latest_source_mtime", lambda: float("inf"))

        first = _build_or_reuse_wheel(wheel_cache_dir)
        second = _build_or_reuse_wheel(wheel_cache_dir)

        assert len(calls) == 2, "a wheel older than the source tree must be rebuilt, not reused"
        assert first != second
        assert not first.exists(), "the stale wheel should have been unlinked, not left behind"
