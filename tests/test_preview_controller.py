"""preview_controller.py (#365 phase 5 — Live Preview backend: loopback-only
URL gate, containment+extension-checked file gate, per-project state, and
the navigation policy a future widget must consult)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication, QUrl

from agent_takkub.preview_controller import (
    DEVICE_PRESETS,
    PreviewController,
    PreviewState,
    approved_artifact_roots,
    is_loopback_url,
    navigation_allowed,
    resolve_preview_file,
)
from agent_takkub.project_file_index import PathEscapesRootsError


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


# ── is_loopback_url ─────────────────────────────────────────────────────


class TestIsLoopbackUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:3000",
            "http://127.0.0.1:3000/path?q=1",
            "http://localhost:8080",
            "https://localhost",
            "http://[::1]:3000",
            "http://127.5.5.5:9000",  # whole 127.0.0.0/8 range is loopback
        ],
    )
    def test_accepts_loopback(self, url: str) -> None:
        assert is_loopback_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com:3000",
            "http://192.168.1.5:3000",
            "ftp://127.0.0.1",
            "http://",
            "not a url",
            "",
            "javascript:alert(1)",
        ],
    )
    def test_rejects_non_loopback(self, url: str) -> None:
        assert is_loopback_url(url) is False


# ── resolve_preview_file ────────────────────────────────────────────────


class TestResolvePreviewFile:
    def test_valid_html_under_root(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "index.html"
        f.write_text("<html></html>", encoding="utf-8")

        resolved = resolve_preview_file(f, [root])

        assert resolved == f.resolve()

    def test_escapes_roots_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        outside = tmp_path / "outside.html"
        outside.write_text("<html></html>", encoding="utf-8")

        with pytest.raises(PathEscapesRootsError):
            resolve_preview_file(outside, [root])

    def test_wrong_extension_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "notes.md"
        f.write_text("# hi", encoding="utf-8")

        with pytest.raises(ValueError, match="html"):
            resolve_preview_file(f, [root])

    def test_htm_extension_accepted(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "index.htm"
        f.write_text("<html></html>", encoding="utf-8")

        assert resolve_preview_file(f, [root]) == f.resolve()


# ── approved_artifact_roots ──────────────────────────────────────────────


class TestApprovedArtifactRoots:
    def test_includes_project_paths_and_docs_dir(self, tmp_path: Path, monkeypatch) -> None:
        web_root = tmp_path / "web"
        web_root.mkdir()

        def _fake_load_projects() -> dict:
            return {"projects": {"demo": {"paths": {"web": str(web_root)}}}}

        docs_dir = tmp_path / "docs" / "demo"

        monkeypatch.setattr(
            "agent_takkub.preview_controller.config.load_projects", _fake_load_projects
        )
        monkeypatch.setattr(
            "agent_takkub.preview_controller.config.project_docs_dir", lambda ns: docs_dir
        )

        roots = approved_artifact_roots("demo")

        assert web_root.resolve() in roots
        assert docs_dir in roots

    def test_unknown_project_still_returns_docs_dir(self, tmp_path: Path, monkeypatch) -> None:
        docs_dir = tmp_path / "docs" / "ghost"
        monkeypatch.setattr(
            "agent_takkub.preview_controller.config.load_projects", lambda: {"projects": {}}
        )
        monkeypatch.setattr(
            "agent_takkub.preview_controller.config.project_docs_dir", lambda ns: docs_dir
        )

        roots = approved_artifact_roots("ghost")

        assert roots == [docs_dir]


# ── PreviewController ────────────────────────────────────────────────────


class TestPreviewController:
    def test_open_url_rejects_non_loopback(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        with pytest.raises(ValueError, match="loopback"):
            controller.open_url("demo", "http://example.com:3000")

    def test_open_url_sets_state_and_emits_opened(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        seen = []
        controller.opened.connect(lambda project, state: seen.append((project, state)))

        state = controller.open_url("demo", "http://127.0.0.1:3000")

        assert state.mode == "url"
        assert state.target == "http://127.0.0.1:3000"
        assert state.device == "desktop"
        assert controller.status("demo") == state
        assert seen == [("demo", state)]

    def test_reopen_emits_updated_not_opened(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        opened_calls = []
        updated_calls = []
        controller.opened.connect(lambda p, s: opened_calls.append((p, s)))
        controller.updated.connect(lambda p, s: updated_calls.append((p, s)))

        controller.open_url("demo", "http://127.0.0.1:3000")
        controller.open_url("demo", "http://127.0.0.1:4000")

        assert len(opened_calls) == 1
        assert len(updated_calls) == 1
        assert updated_calls[0][1].target == "http://127.0.0.1:4000"

    def test_open_file_uses_containment(self, tmp_path: Path, qapp: QCoreApplication) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        f = root / "index.html"
        f.write_text("<html></html>", encoding="utf-8")
        controller = PreviewController()

        state = controller.open_file("demo", f, [root])

        assert state.mode == "file"
        assert state.target == str(f.resolve())

    def test_open_file_outside_roots_raises(self, tmp_path: Path, qapp: QCoreApplication) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        outside = tmp_path / "outside.html"
        outside.write_text("<html></html>", encoding="utf-8")
        controller = PreviewController()

        with pytest.raises(PathEscapesRootsError):
            controller.open_file("demo", outside, [root])

    def test_close_emits_and_clears_state(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("demo", "http://127.0.0.1:3000")
        closed = []
        controller.closed.connect(lambda project: closed.append(project))

        result = controller.close("demo")

        assert result is True
        assert controller.status("demo") is None
        assert closed == ["demo"]

    def test_close_no_open_preview_returns_false(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        assert controller.close("demo") is False

    def test_set_device_validates_preset(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("demo", "http://127.0.0.1:3000")

        state = controller.set_device("demo", "mobile")

        assert state.device == "mobile"
        assert state.device in DEVICE_PRESETS

    def test_set_device_unknown_preset_raises(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("demo", "http://127.0.0.1:3000")
        with pytest.raises(ValueError):
            controller.set_device("demo", "watch")

    def test_set_device_without_open_preview_raises(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        with pytest.raises(ValueError, match="no open preview"):
            controller.set_device("demo", "mobile")

    def test_set_approved(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("demo", "http://127.0.0.1:3000")

        state = controller.set_approved("demo", True)

        assert state.approved is True

    def test_projects_are_independent(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("a", "http://127.0.0.1:3000")
        controller.open_url("b", "http://127.0.0.1:4000")

        assert controller.status("a").target == "http://127.0.0.1:3000"
        assert controller.status("b").target == "http://127.0.0.1:4000"

        controller.close("a")
        assert controller.status("a") is None
        assert controller.status("b") is not None

    def test_all_states_reflects_open_and_closed_projects(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("a", "http://127.0.0.1:3000")
        controller.open_url("b", "http://127.0.0.1:4000")

        assert set(controller.all_states()) == {"a", "b"}

        controller.close("a")
        assert set(controller.all_states()) == {"b"}


# ── check_navigation (#365 phase 10 diagnostics) ──────────────────────────


class TestCheckNavigation:
    def test_no_open_preview_raises(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        with pytest.raises(ValueError, match="no open preview"):
            controller.check_navigation("demo", "http://127.0.0.1:3000/")

    def test_allowed_navigation_does_not_increment_counter(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("demo", "http://127.0.0.1:3000")

        allowed = controller.check_navigation("demo", "http://127.0.0.1:3000/about")

        assert allowed is True
        assert controller.nav_block_counts() == {}

    def test_blocked_navigation_increments_counter(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("demo", "http://127.0.0.1:3000")

        allowed1 = controller.check_navigation("demo", "http://evil.example.com/")
        allowed2 = controller.check_navigation("demo", "http://127.0.0.1:4000/")

        assert allowed1 is False
        assert allowed2 is False
        assert controller.nav_block_counts() == {"demo": 2}

    def test_block_counts_are_per_project(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("a", "http://127.0.0.1:3000")
        controller.open_url("b", "http://127.0.0.1:4000")

        controller.check_navigation("a", "http://evil.example.com/")

        assert controller.nav_block_counts() == {"a": 1}


# ── navigation_allowed ────────────────────────────────────────────────────


class TestNavigationAllowed:
    def test_url_mode_same_origin_allowed(self) -> None:
        state = PreviewState(project="demo", mode="url", target="http://127.0.0.1:3000/")
        assert navigation_allowed(state, "http://127.0.0.1:3000/about") is True

    def test_url_mode_cross_origin_denied(self) -> None:
        state = PreviewState(project="demo", mode="url", target="http://127.0.0.1:3000/")
        assert navigation_allowed(state, "http://127.0.0.1:4000/") is False
        assert navigation_allowed(state, "http://evil.example.com/") is False

    def test_file_mode_same_file_allowed(self, tmp_path: Path) -> None:
        target = str((tmp_path / "index.html").resolve())
        state = PreviewState(project="demo", mode="file", target=target)
        assert navigation_allowed(state, target) is True

    def test_file_mode_different_file_denied(self, tmp_path: Path) -> None:
        target = str((tmp_path / "index.html").resolve())
        other = str((tmp_path / "other.html").resolve())
        state = PreviewState(project="demo", mode="file", target=target)
        assert navigation_allowed(state, other) is False


# ── navigation_allowed: file:// URL comparison (#369 BUG-001) ────────────
#
# The widget reports a navigation target as a `file://` URL (via
# `QUrl.fromLocalFile`/`acceptNavigationRequest`), never as the bare path
# string `PreviewState.target` stores — the old code compared those two
# representations directly and could never match, even for the exact same
# file.


class TestNavigationAllowedFileUrlComparison:
    def test_file_url_for_the_same_path_is_allowed(self, tmp_path: Path) -> None:
        target = tmp_path / "index.html"
        state = PreviewState(project="demo", mode="file", target=str(target.resolve()))

        nav_url = QUrl.fromLocalFile(str(target)).toString()

        assert navigation_allowed(state, nav_url) is True

    def test_file_url_for_a_different_path_is_denied(self, tmp_path: Path) -> None:
        target = tmp_path / "index.html"
        other = tmp_path / "other.html"
        state = PreviewState(project="demo", mode="file", target=str(target.resolve()))

        nav_url = QUrl.fromLocalFile(str(other)).toString()

        assert navigation_allowed(state, nav_url) is False

    def test_anchor_only_navigation_on_the_same_file_url_is_allowed(self, tmp_path: Path) -> None:
        target = tmp_path / "index.html"
        state = PreviewState(project="demo", mode="file", target=str(target.resolve()))

        nav_url = QUrl.fromLocalFile(str(target)).toString() + "#section"

        assert navigation_allowed(state, nav_url) is True

    def test_windows_drive_letter_path_round_trips_through_file_url(self) -> None:
        # QUrl("C:/proj/index.html").scheme() returns "c" — RFC 3986 treats a
        # drive letter + ":" as a syntactically valid one-letter scheme, so a
        # naive scheme() check would misclassify every Windows path. This
        # pins the actual fix (a "://" substring check) against exactly the
        # string shape a Windows path has, independent of the host OS the
        # test suite happens to run on.
        target = r"C:\Users\demo\proj\index.html"
        state = PreviewState(project="demo", mode="file", target=target)

        nav_url = QUrl.fromLocalFile(target).toString()
        assert nav_url.startswith("file:///C:")

        assert navigation_allowed(state, nav_url) is True
        other_url = QUrl.fromLocalFile(r"C:\Users\demo\proj\other.html").toString()
        assert navigation_allowed(state, other_url) is False

    def test_unicode_path_round_trips_through_file_url(self, tmp_path: Path) -> None:
        target = tmp_path / "โปรเจกต์" / "ไฟล์.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        state = PreviewState(project="demo", mode="file", target=str(target.resolve()))

        nav_url = QUrl.fromLocalFile(str(target)).toString()

        assert navigation_allowed(state, nav_url) is True

    def test_http_target_is_never_treated_as_a_local_file_match(self) -> None:
        state = PreviewState(project="demo", mode="file", target="http://127.0.0.1:3000/")
        assert navigation_allowed(state, "http://127.0.0.1:3000/") is True  # exact-string fallback
        assert navigation_allowed(state, "http://127.0.0.1:3000/about") is False

    @pytest.mark.skipif(
        sys.platform != "win32", reason="case-insensitive path comparison is a Windows-only concern"
    )
    def test_case_only_difference_is_treated_as_the_same_file(self) -> None:
        # #369 follow-up (reviewer finding #2+#3): both sides of this
        # comparison now resolve through project_file_index._safe_resolve
        # (RESOLVE_LOCK) instead of a bare Path.resolve() — this pins that
        # the os.path.normcase comparison in navigation_allowed still treats
        # a drive-letter-and-name case difference as the same file afterward.
        state = PreviewState(project="demo", mode="file", target=r"c:\x\INDEX.HTML")

        nav_url = QUrl.fromLocalFile(r"C:\x\index.html").toString()

        assert navigation_allowed(state, nav_url) is True


# ── PreviewController.close: nav_block_counts cleanup (#369 BUG-003) ─────


class TestCloseClearsNavBlockCounts:
    def test_close_pops_the_project_nav_block_counter(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("demo", "http://127.0.0.1:3000")
        controller.check_navigation("demo", "http://evil.example.com/")
        assert controller.nav_block_counts() == {"demo": 1}

        controller.close("demo")

        assert controller.nav_block_counts() == {}

    def test_reopening_after_close_starts_the_counter_fresh(self, qapp: QCoreApplication) -> None:
        controller = PreviewController()
        controller.open_url("demo", "http://127.0.0.1:3000")
        controller.check_navigation("demo", "http://evil.example.com/")
        controller.close("demo")

        controller.open_url("demo", "http://127.0.0.1:4000")

        assert controller.nav_block_counts() == {}
