"""Regression cover for #388 — a flaky ubuntu CI failure:

    "[Errno 2] No such file or directory: 'build/bdist.linux-x86_64/wheel'"

Root cause: two DIFFERENT test modules (test_installed_mode_gate.py and
test_installed_cli_bin_integration.py) each defined their own session-scoped
`installed_venv` fixture, and only one of the two guarded its `python -m
build` call with a cross-process lock. `python -m build` always stages into
`<repo_root>/build/` regardless of `--outdir`, so under pytest-xdist a worker
running the locked fixture and a worker running the unlocked one could run
`python -m build` at the same time against the SAME repo_root/build/ dir —
one worker's `shutil.rmtree(repo_root/build)` (or setuptools' own internal
bdist staging) racing another's in-flight build.

Fix: one `installed_venv` fixture, defined once in conftest.py, used by both
modules — so there is exactly one lock and exactly one `python -m build`
per pytest run. These tests cover the pieces of that fixture that don't
require actually building a wheel (that's already exercised by the
`installed_venv`-dependent tests elsewhere, which are slow/opt-in).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tests.conftest import (
    _build_or_reuse_wheel,
    _cross_process_wheel_lock,
)


class TestCrossProcessWheelLock:
    def test_mutual_exclusion_across_threads(self, tmp_path: Path) -> None:
        """Two concurrent lock-holders must never run their critical section
        at the same time — the exact race that produced #388 on CI."""
        lock_path = tmp_path / "wheel-build.lock"
        overlap_detected = threading.Event()
        currently_inside = threading.Event()

        def worker() -> None:
            with _cross_process_wheel_lock(lock_path, timeout=10.0, poll=0.01):
                if currently_inside.is_set():
                    overlap_detected.set()
                currently_inside.set()
                time.sleep(0.05)
                currently_inside.clear()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not overlap_detected.is_set(), (
            "two lock holders ran their critical section concurrently — "
            "the lock is not providing mutual exclusion"
        )
        assert not lock_path.exists(), "lock file must be cleaned up after release"

    def test_second_waiter_times_out_while_first_holds_lock(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "wheel-build.lock"
        with _cross_process_wheel_lock(lock_path, timeout=10.0, poll=0.01):
            with pytest.raises(TimeoutError):
                with _cross_process_wheel_lock(lock_path, timeout=0.2, poll=0.05):
                    pass  # pragma: no cover - must time out before entering

    def test_lock_is_reentrant_across_sequential_uses(self, tmp_path: Path) -> None:
        """Simulates one worker building, releasing, then a second worker
        (or the same worker for a later fixture) acquiring cleanly."""
        lock_path = tmp_path / "wheel-build.lock"
        with _cross_process_wheel_lock(lock_path, timeout=10.0, poll=0.01):
            pass
        with _cross_process_wheel_lock(lock_path, timeout=10.0, poll=0.01):
            pass


class TestBuildOrReuseWheel:
    def test_reuses_cached_wheel_without_rebuilding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh cached wheel must short-circuit the (slow, race-prone)
        `python -m build` subprocess entirely."""
        cache_dir = tmp_path / "wheel-cache"
        cache_dir.mkdir()
        cached_wheel = cache_dir / "agent_takkub-9.9.9-py3-none-any.whl"
        cached_wheel.write_bytes(b"fake wheel")

        monkeypatch.setattr(
            "tests.conftest._latest_wheel_source_mtime", lambda: cached_wheel.stat().st_mtime - 100
        )

        def _boom(*_a, **_k):  # pragma: no cover - must never be called
            raise AssertionError("python -m build should not run when the cache is fresh")

        monkeypatch.setattr("tests.conftest.subprocess.run", _boom)

        result = _build_or_reuse_wheel(cache_dir)
        assert result == cached_wheel

    def test_stale_cached_wheel_is_removed_before_rebuild(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "wheel-cache"
        cache_dir.mkdir()
        stale_wheel = cache_dir / "agent_takkub-0.0.1-py3-none-any.whl"
        stale_wheel.write_bytes(b"stale")

        monkeypatch.setattr(
            "tests.conftest._latest_wheel_source_mtime", lambda: stale_wheel.stat().st_mtime + 100
        )

        new_wheel = cache_dir / "agent_takkub-9.9.9-py3-none-any.whl"

        class _FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_build(*_args, **_kwargs):
            new_wheel.write_bytes(b"freshly built")
            return _FakeResult()

        monkeypatch.setattr("tests.conftest.subprocess.run", _fake_build)
        monkeypatch.setattr("tests.conftest.shutil.rmtree", lambda *a, **k: None)

        result = _build_or_reuse_wheel(cache_dir)

        assert not stale_wheel.exists(), "stale wheel must be removed before rebuilding"
        assert result == new_wheel


class TestSingleSharedFixtureDefinition:
    """Structural guard: exactly one `installed_venv` fixture must exist for
    the whole suite (in conftest.py) — never redefine a local one in an
    individual test module, which is what caused #388's unlocked-build
    race in the first place."""

    def test_no_test_module_redefines_installed_venv_fixture(self) -> None:
        this_file = Path(__file__).resolve()
        repo_tests_dir = this_file.parent
        offenders = []
        for test_file in sorted(repo_tests_dir.glob("test_*.py")):
            if test_file == this_file:
                continue  # this file legitimately mentions the fixture name
            text = test_file.read_text(encoding="utf-8")
            if "def installed_venv(" in text:
                offenders.append(test_file.name)
        assert offenders == [], (
            f"these test modules define their own local `installed_venv` fixture "
            f"instead of depending on the shared one in conftest.py: {offenders} "
            "(this is exactly the #388 race: two builds, only one locked)"
        )

    def test_conftest_defines_installed_venv_fixture(self) -> None:
        from tests import conftest

        assert hasattr(conftest, "installed_venv"), (
            "conftest.py must define the shared `installed_venv` fixture"
        )
