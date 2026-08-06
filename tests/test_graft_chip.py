"""Tests for the 🧠 Graft status-bar chip: `StatusHeaderMixin._refresh_graft_chip`
/ `_graft_progress_snapshot` / `_on_graft_chip_clicked` (M6 — first-run
indicator for graft_autobuild.py's silent background build).

Same lightweight-stub spirit as `test_remote_chip.py`: mix the plain-Python
mixin into a stub with just the attributes it touches, no `MainWindow`/Qt
window construction needed.
"""

from __future__ import annotations

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
            "building": None,
            "failed": None,
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


class TestRefreshGraftChip:
    def test_no_chip_attr_is_a_noop(self):
        stub = _Stub()
        del stub._chip_graft
        stub._refresh_graft_chip()  # must not raise

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
        info = MagicMock()
        monkeypatch.setattr(sh_mod.QMessageBox, "information", info)
        stub._on_graft_chip_clicked()
        info.assert_called_once()
        assert "3/3" in info.call_args[0][2]

    def test_building_now_shows_in_information_dialog(self, monkeypatch):
        stub = _Stub()
        stub._graft_status_cache = {
            "available": True,
            "total": 5,
            "completed": 2,
            "building": 3,
            "failed": [],
        }
        info = MagicMock()
        monkeypatch.setattr(sh_mod.QMessageBox, "information", info)
        stub._on_graft_chip_clicked()
        info.assert_called_once()
        assert "3 building right now" in info.call_args[0][2]

    def test_failures_shows_warning_dialog_listing_them(self, monkeypatch):
        stub = _Stub()
        stub._graft_status_cache = {
            "available": True,
            "total": 3,
            "completed": 2,
            "building": None,
            "failed": ["proj-x"],
        }
        warn = MagicMock()
        monkeypatch.setattr(sh_mod.QMessageBox, "warning", warn)
        stub._on_graft_chip_clicked()
        warn.assert_called_once()
        assert "proj-x" in warn.call_args[0][2]
