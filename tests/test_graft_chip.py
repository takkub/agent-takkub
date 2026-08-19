"""Tests for the 🧠 Graft status-bar chip: `StatusHeaderMixin._refresh_graft_chip`
/ `_graft_progress_snapshot` / `_on_graft_chip_clicked` (M6 — first-run
indicator for graft_autobuild.py's silent background build).

Same lightweight-stub spirit as `test_remote_chip.py`: mix the plain-Python
mixin into a stub with just the attributes it touches, no `MainWindow`/Qt
window construction needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QPushButton

import agent_takkub.status_header as sh_mod


def _clock(values):
    """A `time.monotonic`-shaped stub that yields *values* in order, then
    repeats the last one forever — avoids `StopIteration` if anything other
    than the code under test happens to call the real `time.monotonic`
    while it's patched (patching `sh_mod.time.monotonic` patches the
    shared `time` module for the whole process, not just this file)."""
    state = {"i": 0}

    def _next() -> float:
        i = min(state["i"], len(values) - 1)
        state["i"] += 1
        return values[i]

    return _next


@pytest.fixture(autouse=True)
def _reset_graft_chip_caches():
    """MED-6 (2026-08-06 final review) added a process-lifetime CLI-path
    cache and a 10s snapshot TTL to `_graft_progress_snapshot`. Without a
    reset between tests, the first test's monkeypatched result would still
    be sitting in the module-level cache when the next test's own
    monkeypatch expects a fresh read."""
    sh_mod._reset_graft_caches()
    yield
    sh_mod._reset_graft_caches()


class _Stub(sh_mod.StatusHeaderMixin):
    def __init__(self) -> None:
        self._chip_graft = QPushButton()
        self._graft_status_cache: dict | None = None


class TestGraftProgressSnapshot:
    def test_cli_missing_reports_unavailable(self, monkeypatch):
        import agent_takkub.graft_store as graft_store

        monkeypatch.setattr(graft_store, "graft_cli_path", lambda: None)
        snap = sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        assert snap == {
            "available": False,
            "total": 0,
            "completed": 0,
            "eligible_total": 0,
            "building": None,
            "failed": None,
            "skipped": None,
        }

    def test_counts_total_and_completed_from_public_markers(self, monkeypatch, tmp_path):
        import agent_takkub.config as config
        import agent_takkub.graft_store as graft_store

        proj_a = tmp_path / "a"
        proj_b = tmp_path / "b"
        proj_a.mkdir()
        proj_b.mkdir()

        monkeypatch.setattr(graft_store, "graft_cli_path", lambda: "graft.cmd")
        monkeypatch.setattr(
            config,
            "load_projects",
            lambda: {
                "projects": {
                    "demo": {"paths": {"web": str(proj_a), "api": str(proj_b)}},
                }
            },
        )
        # Only proj_a has a completed build.
        monkeypatch.setattr(
            graft_store,
            "has_completed_build",
            lambda store: store == graft_store.graph_store_dir(proj_a.resolve()),
        )

        snap = sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        assert snap["available"] is True
        assert snap["total"] == 2
        assert snap["completed"] == 1
        # graft_autobuild.get_build_status() is shipped now and is called for
        # real (not mocked here) — with no build threads running and no
        # recorded failures it reports the real "0 in flight" / "[] failed",
        # not None. None is reserved for "the getter itself is unavailable".
        assert snap["building"] == 0
        assert snap["failed"] == []

    def test_richer_status_used_when_graft_autobuild_exposes_it(self, monkeypatch, tmp_path):
        import agent_takkub.config as config
        import agent_takkub.graft_autobuild as graft_autobuild
        import agent_takkub.graft_store as graft_store

        monkeypatch.setattr(graft_store, "graft_cli_path", lambda: "graft.cmd")
        monkeypatch.setattr(config, "load_projects", lambda: {"projects": {}})
        monkeypatch.setattr(
            graft_autobuild,
            "get_build_status",
            lambda: {"building": 2, "failed": ["proj-x"]},
            raising=False,
        )

        snap = sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        assert snap["building"] == 2
        assert snap["failed"] == ["proj-x"]

        monkeypatch.delattr(graft_autobuild, "get_build_status", raising=False)

    def test_dedup_uses_graph_key_case_fold_on_darwin(self, monkeypatch, tmp_path):
        """MED-5 (2026-08-06 final review): the dedup key must fold case the
        same way `graft_store.graph_key`/`graph_store_dir` do, or two
        case-variant paths pointing at the same directory on a
        case-insensitive macOS volume overcount `total` and, worse, race
        two `graft build`s into the one store `graph_key` folds them to.
        `os.name == "nt"` (the pre-fix expression) never folds on darwin at
        all — this reproduces that platform via `sys.platform`, independent
        of the OS actually running the test."""
        import pathlib

        import agent_takkub.config as config
        import agent_takkub.graft_store as graft_store

        proj = tmp_path / "Proj"
        proj.mkdir()

        monkeypatch.setattr(graft_store, "graft_cli_path", lambda: "graft.cmd")
        monkeypatch.setattr(graft_store.sys, "platform", "darwin")
        # Stub resolve()/is_dir() to identity so the two configured strings
        # stay distinct pre-fold (a real resolve() on this dev machine's
        # case-insensitive Windows FS would already canonicalize both to
        # one casing, hiding the very bug being tested).
        monkeypatch.setattr(pathlib.Path, "resolve", lambda self, *a, **k: self)
        monkeypatch.setattr(pathlib.Path, "is_dir", lambda self: True)
        monkeypatch.setattr(
            config,
            "load_projects",
            lambda: {
                "projects": {
                    "demo": {
                        "paths": {
                            "a": str(tmp_path / "Proj"),
                            "b": str(tmp_path / "proj"),
                        }
                    },
                }
            },
        )
        monkeypatch.setattr(graft_store, "has_completed_build", lambda store: False)

        snap = sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        assert snap["total"] == 1

    def test_cli_path_resolved_once_and_cached_across_ttl_expiry(self, monkeypatch, tmp_path):
        """MED-6 (2026-08-06 final review): `graft_cli_path()` (a `shutil.which()`
        PATH scan) must resolve once per process, not once per snapshot —
        even after the 10s snapshot TTL expires and the rest of the
        snapshot is recomputed."""
        import agent_takkub.config as config
        import agent_takkub.graft_store as graft_store

        calls = []

        def _which():
            calls.append(1)
            return "graft.cmd"

        monkeypatch.setattr(graft_store, "graft_cli_path", _which)
        monkeypatch.setattr(config, "load_projects", lambda: {"projects": {}})
        # Patching `time.monotonic` patches the real, shared `time` module —
        # anything else in the process that happens to call it during this
        # window would also draw from the sequence, so fall back to the
        # last value forever instead of raising StopIteration on a stray call.
        monkeypatch.setattr(sh_mod.time, "monotonic", _clock([0.0, 100.0]))

        sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        assert len(calls) == 1

    def test_snapshot_reused_within_ttl_no_reload(self, monkeypatch, tmp_path):
        """MED-6: within the 10s TTL, a second call must NOT re-read
        projects.json / re-walk paths — it returns the cached snapshot."""
        import agent_takkub.config as config
        import agent_takkub.graft_store as graft_store

        monkeypatch.setattr(graft_store, "graft_cli_path", lambda: "graft.cmd")
        calls = []

        def _load():
            calls.append(1)
            return {"projects": {}}

        monkeypatch.setattr(config, "load_projects", _load)
        monkeypatch.setattr(sh_mod.time, "monotonic", lambda: 0.0)

        sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        assert len(calls) == 1

    def test_building_never_stale_within_ttl(self, monkeypatch, tmp_path):
        """6.4 (2026-08-06 final review): a stale cached `building: 0` must
        never render as "confirmed zero" while builds are actually running.
        `total`/`completed` (the expensive half) may lag within the 10s TTL,
        but `building`/`failed` (cheap, real in-flight state) must be fresh
        on every single call, cache hit or miss — including the very first
        tick at boot, before the TTL has ever expired once."""
        import agent_takkub.config as config
        import agent_takkub.graft_autobuild as graft_autobuild
        import agent_takkub.graft_store as graft_store

        monkeypatch.setattr(graft_store, "graft_cli_path", lambda: "graft.cmd")
        monkeypatch.setattr(config, "load_projects", lambda: {"projects": {}})
        monkeypatch.setattr(sh_mod.time, "monotonic", lambda: 0.0)

        live = {"building": 0, "failed": []}
        monkeypatch.setattr(graft_autobuild, "get_build_status", lambda: live, raising=False)

        # First call (cache miss) — boot-time tick.
        snap1 = sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        assert snap1["building"] == 0

        # Builds start running before the (still-fresh, same monotonic tick)
        # snapshot cache would ever be re-read for total/completed.
        live["building"] = 3
        live["failed"] = []

        # Second call, still well within the 10s TTL — must NOT report the
        # stale `0` that got cached on the first call.
        snap2 = sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        assert snap2["building"] == 3
        assert snap2["total"] == 0  # the cheap/cached half is unaffected

    def test_skipped_excluded_from_eligible_total(self, monkeypatch, tmp_path):
        """2026-08-06 bug report: non-git-repo dirs must not inflate the
        denominator shown to the user — `eligible_total` = `total` minus
        however many are `skipped`."""
        import agent_takkub.config as config
        import agent_takkub.graft_autobuild as graft_autobuild
        import agent_takkub.graft_store as graft_store

        proj_a = tmp_path / "a"
        proj_b = tmp_path / "b"
        proj_a.mkdir()
        proj_b.mkdir()

        monkeypatch.setattr(graft_store, "graft_cli_path", lambda: "graft.cmd")
        monkeypatch.setattr(
            config,
            "load_projects",
            lambda: {
                "projects": {
                    "demo": {"paths": {"web": str(proj_a), "api": str(proj_b)}},
                }
            },
        )
        monkeypatch.setattr(graft_store, "has_completed_build", lambda store: False)
        monkeypatch.setattr(
            graft_autobuild,
            "get_build_status",
            lambda: {"building": 0, "failed": [], "skipped": ["b"]},
            raising=False,
        )

        snap = sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        assert snap["total"] == 2
        assert snap["skipped"] == ["b"]
        assert snap["eligible_total"] == 1

    def test_missing_getter_degrades_to_unknown_not_zero(self, monkeypatch, tmp_path):
        """If get_build_status() is truly absent (older graft_autobuild, or
        deleted at runtime), building/failed must stay None — "unknown" —
        never silently collapse to 0/[] which would read as "confirmed
        nothing running", a different and misleading claim."""
        import agent_takkub.config as config
        import agent_takkub.graft_autobuild as graft_autobuild
        import agent_takkub.graft_store as graft_store

        monkeypatch.setattr(graft_store, "graft_cli_path", lambda: "graft.cmd")
        monkeypatch.setattr(config, "load_projects", lambda: {"projects": {}})
        monkeypatch.delattr(graft_autobuild, "get_build_status", raising=False)

        snap = sh_mod.StatusHeaderMixin._graft_progress_snapshot()
        assert snap["building"] is None
        assert snap["failed"] is None
        assert snap["skipped"] is None


class TestRefreshGraftChip:
    def test_no_chip_attr_is_a_noop(self):
        stub = _Stub()
        del stub._chip_graft
        stub._refresh_graft_chip()  # must not raise

    def test_main_thread_refresh_reads_cache_without_filesystem_calls(self, monkeypatch):
        """#312: the Qt slot paints worker data; it never resolves/stats paths."""
        stub = _Stub()
        snapshot = {
            "available": True,
            "total": 1,
            "completed": 1,
            "eligible_total": 1,
            "building": 0,
            "failed": [],
            "skipped": [],
        }
        stub._graft_status_cache = snapshot
        calls = []

        def _filesystem_call(*_args, **_kwargs):
            calls.append(1)
            raise AssertionError("filesystem call reached the Qt refresh slot")

        monkeypatch.setattr(Path, "resolve", _filesystem_call)
        monkeypatch.setattr(Path, "stat", _filesystem_call)

        stub._refresh_graft_chip()
        stub._on_graft_snapshot_ready(snapshot)

        assert calls == []
        assert stub._chip_graft.text() == "🧠 Graft ready"

    def test_snapshot_scan_is_dispatched_once_to_thread_pool(self, monkeypatch):
        stub = _Stub()
        stub._graft_snapshot_worker_busy = False
        worker = MagicMock()
        pool = MagicMock()
        worker_type = MagicMock(return_value=worker)
        pool_type = MagicMock()
        pool_type.globalInstance.return_value = pool
        monkeypatch.setattr(sh_mod, "_GraftSnapshotWorker", worker_type)
        monkeypatch.setattr(sh_mod, "QThreadPool", pool_type)

        stub._schedule_graft_snapshot()
        stub._schedule_graft_snapshot()

        worker.signals.finished.connect.assert_called_once_with(stub._on_graft_snapshot_ready)
        pool.start.assert_called_once_with(worker)
        assert stub._graft_snapshot_worker_busy is True

    def test_cli_missing_shows_attention_state(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            stub,
            "_graft_progress_snapshot",
            lambda: {
                "available": False,
                "total": 0,
                "completed": 0,
                "building": None,
                "failed": None,
            },
        )
        stub._refresh_graft_chip()
        assert stub._chip_graft.text() == "🧠 Graft: not installed"
        assert "doctor --fix" in stub._chip_graft.toolTip()

    def test_in_progress_shows_building_count(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            stub,
            "_graft_progress_snapshot",
            lambda: {
                "available": True,
                "total": 5,
                "completed": 2,
                "building": None,
                "failed": None,
            },
        )
        stub._refresh_graft_chip()
        assert stub._chip_graft.text() == "🧠 Building graphs… 2/5"

    def test_all_complete_shows_ready(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            stub,
            "_graft_progress_snapshot",
            lambda: {
                "available": True,
                "total": 5,
                "completed": 5,
                "building": None,
                "failed": None,
            },
        )
        stub._refresh_graft_chip()
        assert stub._chip_graft.text() == "🧠 Graft ready"

    def test_no_projects_configured_shows_bare_label(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            stub,
            "_graft_progress_snapshot",
            lambda: {
                "available": True,
                "total": 0,
                "completed": 0,
                "building": None,
                "failed": None,
            },
        )
        stub._refresh_graft_chip()
        assert stub._chip_graft.text() == "🧠 Graft"

    def test_real_building_count_shown_when_getter_available(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            stub,
            "_graft_progress_snapshot",
            lambda: {"available": True, "total": 5, "completed": 2, "building": 3, "failed": []},
        )
        stub._refresh_graft_chip()
        assert stub._chip_graft.text() == "🧠 Building graphs… 3 now · 2/5"

    def test_zero_building_known_shows_queued_not_building(self, monkeypatch):
        """building == 0 (getter present, confirmed nothing in flight) must
        render differently from building is None (unknown) even though both
        reach the same total/completed branch — "queued" vs "building…"."""
        stub = _Stub()
        monkeypatch.setattr(
            stub,
            "_graft_progress_snapshot",
            lambda: {"available": True, "total": 5, "completed": 2, "building": 0, "failed": []},
        )
        stub._refresh_graft_chip()
        assert stub._chip_graft.text() == "🧠 Graft: 2/5 queued"

    def test_unknown_building_falls_back_to_marker_progress_text(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            stub,
            "_graft_progress_snapshot",
            lambda: {
                "available": True,
                "total": 5,
                "completed": 2,
                "building": None,
                "failed": None,
            },
        )
        stub._refresh_graft_chip()
        assert stub._chip_graft.text() == "🧠 Building graphs… 2/5"

    def test_failures_take_priority_over_progress_text(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            stub,
            "_graft_progress_snapshot",
            lambda: {
                "available": True,
                "total": 5,
                "completed": 3,
                "building": 1,
                "failed": ["proj-x", "proj-y"],
            },
        )
        stub._refresh_graft_chip()
        assert stub._chip_graft.text() == "🧠 Graft: 2 failed"

    def test_skipped_only_shows_ready_not_attention(self, monkeypatch):
        """2026-08-06 bug report: dirs skipped for not being a git repo must
        never turn the chip into the warn/attention style or mention
        "failed" — only a real `failed` entry does that. `total`=5 with 2
        skipped means only 3 are eligible, and all 3 are already built."""
        stub = _Stub()
        monkeypatch.setattr(
            stub,
            "_graft_progress_snapshot",
            lambda: {
                "available": True,
                "total": 5,
                "completed": 3,
                "eligible_total": 3,
                "building": 0,
                "failed": [],
                "skipped": ["img-repo", "docs-repo"],
            },
        )
        stub._refresh_graft_chip()
        assert stub._chip_graft.text() == "🧠 Graft ready"
        assert "failed" not in stub._chip_graft.text()
        assert stub._chip_graft.styleSheet() == stub._graft_chip_style("idle")

    def test_skipped_only_still_queued_uses_eligible_total(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            stub,
            "_graft_progress_snapshot",
            lambda: {
                "available": True,
                "total": 5,
                "completed": 1,
                "eligible_total": 3,
                "building": 0,
                "failed": [],
                "skipped": ["img-repo", "docs-repo"],
            },
        )
        stub._refresh_graft_chip()
        assert stub._chip_graft.text() == "🧠 Graft: 1/3 queued"


def _mock_message_box(monkeypatch, viewer_target=None):
    """Stand in for the manually-built `QMessageBox` `_on_graft_chip_clicked`
    constructs (needed for the "Open Graph Viewer" button — the static
    `QMessageBox.information`/`.warning` convenience functions can't add a
    custom button). Returns the box instance mock; `.setText.call_args[0][0]`
    is the rendered body text. `viewer_btn` is a distinct sentinel object
    returned by `addButton("Open Graph Viewer", ...)` so a test can simulate
    the user clicking it via `box.clickedButton.return_value = viewer_btn`.
    """
    box = MagicMock()
    viewer_btn = object()
    close_btn = object()

    def _add_button(label, *_a, **_k):
        return viewer_btn if label == "Open Graph Viewer" else close_btn

    box.addButton.side_effect = _add_button
    box.clickedButton.return_value = close_btn
    cls = MagicMock(return_value=box)
    monkeypatch.setattr(sh_mod, "QMessageBox", cls)
    return box, viewer_btn


class TestGraftViewerTarget:
    def test_no_tabs_attr_returns_none(self):
        stub = _Stub()
        assert stub._graft_viewer_target() is None

    def test_tab_without_project_name_returns_none(self):
        stub = _Stub()
        stub.tabs = MagicMock()
        stub.tabs.currentWidget.return_value = object()  # no .project_name
        assert stub._graft_viewer_target() is None

    def test_returns_first_path_with_completed_build(self, monkeypatch, tmp_path):
        import agent_takkub.config as config
        import agent_takkub.graft_store as graft_store

        proj_a = tmp_path / "a"
        proj_b = tmp_path / "b"
        proj_a.mkdir()
        proj_b.mkdir()

        stub = _Stub()
        tab = MagicMock()
        tab.project_name = "demo"
        stub.tabs = MagicMock()
        stub.tabs.currentWidget.return_value = tab

        monkeypatch.setattr(
            config,
            "load_projects",
            lambda: {
                "projects": {
                    "demo": {"paths": {"web": str(proj_a), "api": str(proj_b)}},
                }
            },
        )
        monkeypatch.setattr(
            graft_store,
            "has_completed_build",
            lambda store: store == graft_store.graph_store_dir(proj_b.resolve()),
        )

        target = stub._graft_viewer_target()
        assert target == proj_b.resolve()

    def test_no_completed_build_returns_none(self, monkeypatch, tmp_path):
        import agent_takkub.config as config
        import agent_takkub.graft_store as graft_store

        proj_a = tmp_path / "a"
        proj_a.mkdir()

        stub = _Stub()
        tab = MagicMock()
        tab.project_name = "demo"
        stub.tabs = MagicMock()
        stub.tabs.currentWidget.return_value = tab

        monkeypatch.setattr(
            config, "load_projects", lambda: {"projects": {"demo": {"paths": {"web": str(proj_a)}}}}
        )
        monkeypatch.setattr(graft_store, "has_completed_build", lambda store: False)

        assert stub._graft_viewer_target() is None


class TestGraftViewerLifecycle:
    def test_stop_with_no_process_is_a_noop(self):
        stub = _Stub()
        stub.stop_graft_viewer()  # must not raise

    def test_stop_kills_the_whole_process_tree(self, monkeypatch):
        """`Popen.terminate()` alone isn't enough — `graft` is a `.cmd` shim
        on Windows, so the tracked PID is the shim, not the real `node`
        server underneath it. Confirmed live: after a plain `terminate()`
        the server kept answering HTTP requests. `stop_graft_viewer` must
        kill the whole tree: `taskkill /T /F` on win32, `os.killpg` on
        POSIX — assert whichever this platform actually uses."""
        import subprocess
        import sys

        stub = _Stub()
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 424242
        stub._graft_viewer_proc = proc

        if sys.platform == "win32":
            run = MagicMock()
            monkeypatch.setattr(subprocess, "run", run)
            stub.stop_graft_viewer()
            run.assert_called_once()
            argv = run.call_args[0][0]
            assert argv[:3] == ["taskkill", "/T", "/F"]
            assert str(proc.pid) in argv
        else:
            import os

            killed = []
            monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
            monkeypatch.setattr(os, "getpgid", lambda pid: pid)
            stub.stop_graft_viewer()
            assert killed == [(proc.pid, __import__("signal").SIGTERM)]
        assert stub._graft_viewer_proc is None

    def test_stop_skips_already_exited_process(self):
        stub = _Stub()
        proc = MagicMock()
        proc.poll.return_value = 0  # already exited
        stub._graft_viewer_proc = proc
        stub.stop_graft_viewer()
        proc.terminate.assert_not_called()

    def test_open_reuses_running_server_for_same_target(self, monkeypatch, tmp_path):
        stub = _Stub()
        proc = MagicMock()
        proc.poll.return_value = None
        target = tmp_path
        stub._graft_viewer_proc = proc
        stub._graft_viewer_port = 4444
        stub._graft_viewer_target_path = target

        import subprocess as _subprocess
        import webbrowser as _webbrowser

        opened = []
        monkeypatch.setattr(_webbrowser, "open", opened.append)
        popen = MagicMock()
        monkeypatch.setattr(_subprocess, "Popen", popen)

        stub._open_graft_viewer(target)
        popen.assert_not_called()
        assert opened == ["http://127.0.0.1:4444"]


class TestOnGraftChipClicked:
    def test_cli_missing_shows_warning_dialog(self, monkeypatch):
        stub = _Stub()
        stub._graft_status_cache = {
            "available": False,
            "total": 0,
            "completed": 0,
            "building": None,
            "failed": None,
        }
        warn = MagicMock()
        monkeypatch.setattr(sh_mod.QMessageBox, "warning", warn)
        stub._on_graft_chip_clicked()
        warn.assert_called_once()
        assert "doctor --fix" in warn.call_args[0][2]

    def test_no_failures_shows_information_dialog(self, monkeypatch):
        stub = _Stub()
        stub._graft_status_cache = {
            "available": True,
            "total": 3,
            "completed": 3,
            "building": None,
            "failed": None,
        }
        monkeypatch.setattr(stub, "_graft_viewer_target", lambda: None)
        box, _ = _mock_message_box(monkeypatch)
        stub._on_graft_chip_clicked()
        box.setText.assert_called_once()
        assert "3/3" in box.setText.call_args[0][0]

    def test_building_now_shows_in_information_dialog(self, monkeypatch):
        stub = _Stub()
        stub._graft_status_cache = {
            "available": True,
            "total": 5,
            "completed": 2,
            "building": 3,
            "failed": [],
        }
        monkeypatch.setattr(stub, "_graft_viewer_target", lambda: None)
        box, _ = _mock_message_box(monkeypatch)
        stub._on_graft_chip_clicked()
        assert "3 building right now" in box.setText.call_args[0][0]

    def test_failures_shows_warning_dialog_listing_them(self, monkeypatch):
        stub = _Stub()
        stub._graft_status_cache = {
            "available": True,
            "total": 3,
            "completed": 2,
            "building": None,
            "failed": ["proj-x"],
        }
        monkeypatch.setattr(stub, "_graft_viewer_target", lambda: None)
        box, _ = _mock_message_box(monkeypatch)
        stub._on_graft_chip_clicked()
        text = box.setText.call_args[0][0]
        assert "proj-x" in text
        assert "graft build <path>" in text
        box.setIcon.assert_called_once_with(sh_mod.QMessageBox.Icon.Warning)

    def test_skipped_only_shows_information_dialog_not_failed(self, monkeypatch):
        """2026-08-06 bug report: skipped-not-git-repo dirs must render in
        their own "Skipped" section, never under the word "Failed", and the
        dialog stays informational (not a warning) when nothing actually
        failed."""
        stub = _Stub()
        stub._graft_status_cache = {
            "available": True,
            "total": 5,
            "completed": 3,
            "eligible_total": 3,
            "building": None,
            "failed": [],
            "skipped": ["img-repo", "docs-repo"],
        }
        monkeypatch.setattr(stub, "_graft_viewer_target", lambda: None)
        box, _ = _mock_message_box(monkeypatch)
        stub._on_graft_chip_clicked()
        text = box.setText.call_args[0][0]
        assert "img-repo" in text
        assert "Skipped" in text
        assert "Failed" not in text
        assert "3/3" in text
        box.setIcon.assert_called_once_with(sh_mod.QMessageBox.Icon.Information)

    def test_skipped_and_failed_both_shown_in_separate_sections(self, monkeypatch):
        stub = _Stub()
        stub._graft_status_cache = {
            "available": True,
            "total": 6,
            "completed": 2,
            "eligible_total": 4,
            "building": None,
            "failed": ["broken-repo"],
            "skipped": ["img-repo", "docs-repo"],
        }
        monkeypatch.setattr(stub, "_graft_viewer_target", lambda: None)
        box, _ = _mock_message_box(monkeypatch)
        stub._on_graft_chip_clicked()
        text = box.setText.call_args[0][0]
        assert "Skipped" in text
        assert "img-repo" in text
        assert "Failed:" in text
        assert "broken-repo" in text

    def test_viewer_button_offered_when_target_available(self, monkeypatch):
        stub = _Stub()
        stub._graft_status_cache = {
            "available": True,
            "total": 3,
            "completed": 3,
            "building": None,
            "failed": None,
        }
        sentinel_target = object()
        monkeypatch.setattr(stub, "_graft_viewer_target", lambda: sentinel_target)
        opened = []
        monkeypatch.setattr(stub, "_open_graft_viewer", lambda t: opened.append(t))
        box, viewer_btn = _mock_message_box(monkeypatch)
        box.clickedButton.return_value = viewer_btn
        stub._on_graft_chip_clicked()
        assert any(c.args[:1] == ("Open Graph Viewer",) for c in box.addButton.call_args_list)
        assert opened == [sentinel_target]

    def test_viewer_button_omitted_when_no_target(self, monkeypatch):
        stub = _Stub()
        stub._graft_status_cache = {
            "available": True,
            "total": 3,
            "completed": 3,
            "building": None,
            "failed": None,
        }
        monkeypatch.setattr(stub, "_graft_viewer_target", lambda: None)
        opened = []
        monkeypatch.setattr(stub, "_open_graft_viewer", lambda t: opened.append(t))
        box, _ = _mock_message_box(monkeypatch)
        stub._on_graft_chip_clicked()
        assert not any(c.args[:1] == ("Open Graph Viewer",) for c in box.addButton.call_args_list)
        assert opened == []
