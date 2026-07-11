"""Tests for `_apply_artifacts_dir`, the helper that stamps
``TAKKUB_ARTIFACTS_DIR`` into every spawned pane's env (issue #1).

Reuses the existing `runtime/exports/<date>/<project>/` convention the
screenshot scanner already reads, so shots keep landing where they always
have. Stamped explicitly at spawn time (mirrors `_apply_port_file`) rather
than relying on the allowlist alone, and the directory is created up front
so a pane never sees a dangling path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_takkub import config
from agent_takkub.pane_env import _PANE_ENV_ALLOWLIST, _apply_artifacts_dir


@pytest.fixture
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    return runtime


class TestApplyArtifactsDir:
    def test_stamps_expected_path(self, runtime_dir: Path) -> None:
        env: dict[str, str] = {}
        with patch("agent_takkub.pane_env.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = "2026-07-09"
            _apply_artifacts_dir(env, "myproj")
        expected = runtime_dir / "exports" / "2026-07-09" / "myproj"
        assert env["TAKKUB_ARTIFACTS_DIR"] == str(expected)

    def test_creates_directory(self, runtime_dir: Path) -> None:
        env: dict[str, str] = {}
        _apply_artifacts_dir(env, "myproj")
        assert Path(env["TAKKUB_ARTIFACTS_DIR"]).is_dir()

    def test_matches_screenshot_scanner_convention(self, runtime_dir: Path) -> None:
        # orchestrator._compute_last_progress_ts / harvest_info scan
        # RUNTIME_DIR/exports/<today>/<project>/ — the artifacts dir must be
        # exactly that directory (screenshots/ lives one level under it) so
        # existing scanners keep finding shots without any changes.
        from datetime import datetime

        env: dict[str, str] = {}
        _apply_artifacts_dir(env, "myproj")
        today = datetime.now().strftime("%Y-%m-%d")
        assert env["TAKKUB_ARTIFACTS_DIR"] == str(runtime_dir / "exports" / today / "myproj")

    def test_no_crash_on_mkdir_failure(
        self, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pathlib import Path as _Path

        def _boom(self, *a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(_Path, "mkdir", _boom)
        env: dict[str, str] = {}
        _apply_artifacts_dir(env, "myproj")  # must not raise
        assert "TAKKUB_ARTIFACTS_DIR" in env

    def test_allowlisted_for_clarity(self) -> None:
        assert "TAKKUB_ARTIFACTS_DIR" in _PANE_ENV_ALLOWLIST


class TestApplyDocsDir:
    """Central-home item C: the same spawn-time stamp also sets
    ``TAKKUB_DOCS_DIR`` (runtime/docs/<project>/) so LLM-authored design-
    review/reviews/guides/system-overview docs land out of the user's repo."""

    def test_stamps_and_creates_docs_dir(self, runtime_dir: Path) -> None:
        env: dict[str, str] = {}
        _apply_artifacts_dir(env, "myproj")
        expected = runtime_dir / "docs" / "myproj"
        assert env["TAKKUB_DOCS_DIR"] == str(expected)
        assert Path(env["TAKKUB_DOCS_DIR"]).is_dir()

    def test_docs_dir_allowlisted(self) -> None:
        assert "TAKKUB_DOCS_DIR" in _PANE_ENV_ALLOWLIST

    def test_no_crash_on_docs_mkdir_failure(
        self, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pathlib import Path as _Path

        def _boom(self, *a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(_Path, "mkdir", _boom)
        env: dict[str, str] = {}
        _apply_artifacts_dir(env, "myproj")  # must not raise
        assert "TAKKUB_DOCS_DIR" in env
