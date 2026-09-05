"""Tests for provider_model_catalog.py — live model-catalog merge for
Settings' model pickers (#493).

`merge_catalog` is pure (no I/O, no Qt) so it's tested directly. The cache
functions touch disk under `config.RUNTIME_DIR`, monkeypatched to `tmp_path`
per test_settings_window.py's own isolation pattern. `refresh_cache` never
runs a real subprocess: `provider_update._discover` and
`provider_model_refresh`'s discovery functions are monkeypatched, mirroring
test_provider_model_refresh.py's own approach.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agent_takkub.provider_model_catalog as pmc
import agent_takkub.provider_model_refresh as pmr
import agent_takkub.provider_update as provider_update
from agent_takkub import config


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")


class TestMergeCatalog:
    def test_none_discovered_returns_snapshot_unchanged(self) -> None:
        snapshot = ("a", "b")
        assert pmc.merge_catalog(snapshot, None) == snapshot

    def test_empty_discovered_returns_snapshot_unchanged(self) -> None:
        snapshot = ("a", "b")
        assert pmc.merge_catalog(snapshot, []) == snapshot

    def test_discovered_ids_come_first_freshest_order_preserved(self) -> None:
        merged = pmc.merge_catalog(("old-1",), ["new-2", "new-1"])
        assert merged == ("new-2", "new-1", "old-1")

    def test_snapshot_entries_already_in_discovery_are_not_duplicated(self) -> None:
        merged = pmc.merge_catalog(("shared", "only-snapshot"), ["fresh", "shared"])
        assert merged == ("fresh", "shared", "only-snapshot")

    def test_falsy_discovered_ids_are_skipped(self) -> None:
        merged = pmc.merge_catalog(("snap",), ["fresh", ""])
        assert merged == ("fresh", "snap")


class TestCacheRoundtrip:
    def test_cached_ids_none_when_no_cache_file(self) -> None:
        assert pmc.cached_ids("gemini") is None

    def test_is_stale_true_when_no_cache_file(self) -> None:
        assert pmc.is_stale("gemini") is True

    def test_cached_ids_and_freshness_after_refresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_update, "_discover", lambda spec: "/fake/agy")
        monkeypatch.setattr(pmr, "_discover_gemini_models", lambda binary: ["gemini-9.9-high"])

        ids = pmc.refresh_cache("gemini")

        assert ids == ["gemini-9.9-high"]
        assert pmc.cached_ids("gemini") == ["gemini-9.9-high"]
        assert pmc.is_stale("gemini") is False

    def test_is_stale_true_once_past_max_age(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(provider_update, "_discover", lambda spec: "/fake/agy")
        monkeypatch.setattr(pmr, "_discover_gemini_models", lambda binary: ["gemini-9.9-high"])
        pmc.refresh_cache("gemini")

        assert pmc.is_stale("gemini", max_age_s=-1.0) is True


class TestRefreshCache:
    def test_unknown_provider_returns_none(self) -> None:
        assert pmc.refresh_cache("kimi") is None
        assert pmc.cached_ids("kimi") is None

    def test_binary_not_found_returns_none_and_does_not_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provider_update, "_discover", lambda spec: None)

        assert pmc.refresh_cache("gemini") is None
        assert pmc.cached_ids("gemini") is None

    def test_discovery_failure_returns_none_and_does_not_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provider_update, "_discover", lambda spec: "/fake/agy")
        monkeypatch.setattr(pmr, "_discover_gemini_models", lambda binary: None)

        assert pmc.refresh_cache("gemini") is None
        assert pmc.cached_ids("gemini") is None


class TestRefreshStale:
    def test_skips_providers_that_are_not_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(provider_update, "_discover", lambda spec: "/fake/agy")
        monkeypatch.setattr(
            pmr,
            "_discover_gemini_models",
            lambda binary: calls.append(binary) or ["gemini-9.9-high"],
        )
        pmc.refresh_cache("gemini")  # now fresh, one call so far
        assert len(calls) == 1

        results = pmc.refresh_stale(("gemini",))

        assert results == {}  # already fresh — not re-discovered
        assert len(calls) == 1  # no second discovery call

    def test_stale_provider_is_refreshed_and_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provider_update, "_discover", lambda spec: "/fake/codex")
        monkeypatch.setattr(pmr, "_discover_codex_models", lambda binary: ["gpt-9.9"])

        results = pmc.refresh_stale(("codex",))

        assert results == {"codex": ["gpt-9.9"]}
        assert pmc.cached_ids("codex") == ["gpt-9.9"]
