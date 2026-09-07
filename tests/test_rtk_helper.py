"""Tests for the rtk install helper.

#515 Settings diet: rtk injection is pure auto-detect now — `rtk_should_
inject()` is just `rtk_binary_available()`, no separate central "enabled"
toggle/flag file, no per-project hook to install/uninstall any more.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import config, rtk_helper


@pytest.fixture(autouse=True)
def _isolate_settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path / "settings-home")


class TestShouldInject:
    def test_true_when_binary_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)
        assert rtk_helper.rtk_should_inject() is True

    def test_false_when_binary_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: False)
        assert rtk_helper.rtk_should_inject() is False

    def test_archives_legacy_enabled_file_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-#515 `rtk-enabled.json` is moved to `backups/`, never
        deleted, the first time `rtk_should_inject()` runs."""
        legacy = config.SETTINGS_HOME / "rtk-enabled.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('{"enabled": true}', encoding="utf-8")
        monkeypatch.setattr(rtk_helper, "rtk_binary_available", lambda: True)

        rtk_helper.rtk_should_inject()

        assert not legacy.exists()
        backup = config.SETTINGS_HOME / "backups" / "rtk-enabled.json"
        assert backup.is_file()

    def test_hook_fragment_shape(self) -> None:
        frag = rtk_helper.rtk_hook_fragment()
        assert frag["matcher"] == "Bash"
        assert frag["hooks"][0]["command"] == "rtk hook claude"
        # Fresh dict each call — a caller can't mutate shared state.
        assert rtk_helper.rtk_hook_fragment() is not frag


class TestFindRtkBinaryCache:
    """find_rtk_binary() runs on the Qt main thread per pane spawn; it caches
    a found path (re-validated) so repeated spawns don't re-scan PATH."""

    def test_caches_positive_result_no_rescan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "_RTK_BINARY_CACHE", None, raising=False)
        real = tmp_path / "rtk.exe"
        real.write_text("", encoding="utf-8")
        calls = {"n": 0}

        def fake_which(name: str):
            calls["n"] += 1
            return str(real) if name == "rtk" else None

        monkeypatch.setattr(rtk_helper, "which", fake_which)

        assert rtk_helper.find_rtk_binary() == str(real)
        first = calls["n"]
        assert first >= 1
        # Second call must hit the cache — `which` not invoked again.
        assert rtk_helper.find_rtk_binary() == str(real)
        assert calls["n"] == first

    def test_does_not_cache_negative_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rtk_helper, "_RTK_BINARY_CACHE", None, raising=False)
        monkeypatch.setattr(rtk_helper, "_FALLBACK_RTK_PATHS", [], raising=False)
        monkeypatch.setattr(rtk_helper, "which", lambda name: None)
        assert rtk_helper.find_rtk_binary() is None
        # A later install is picked up (no negative caching).
        real = tmp_path / "rtk.exe"
        real.write_text("", encoding="utf-8")
        monkeypatch.setattr(rtk_helper, "which", lambda name: str(real) if name == "rtk" else None)
        assert rtk_helper.find_rtk_binary() == str(real)

    def test_revalidates_stale_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A cached path that no longer exists must be re-resolved, not returned.
        stale = tmp_path / "gone.exe"
        monkeypatch.setattr(rtk_helper, "_RTK_BINARY_CACHE", str(stale), raising=False)
        real = tmp_path / "rtk.exe"
        real.write_text("", encoding="utf-8")
        monkeypatch.setattr(rtk_helper, "which", lambda name: str(real) if name == "rtk" else None)
        assert rtk_helper.find_rtk_binary() == str(real)
