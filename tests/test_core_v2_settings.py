"""`core_v2_settings` store: flag round-trip/cache tests + config-fallback
flag tests (env always wins; unset falls back to the persisted config).

Was also the widget smoke-test home for the Core V2 Settings views (epic
#309 Phase 9, `settings_core_v2.CoreV2SettingsMixin`, Routing/Brain/
Scheduler) — that whole UI module was removed outright in the #515 settings
diet (flags default-on since 1.0.84 made the pages redundant with `takkub
doctor`; see `settings_window._VIEW_REDIRECTS`'s own comment for where the
old VIEW_CORE_V2_* constants land now). This file is pure store-level tests
now and was renamed off the deleted module's name to match — no Qt/
QApplication needed here at all, same reasoning
`test_core_v2_settings_context_strategy.py` already gives for staying Qt-free.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import config, core_v2_settings


@pytest.fixture(autouse=True)
def _isolate_core_v2_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    core_v2_settings._reset_cache()
    yield
    core_v2_settings._reset_cache()


class TestCoreV2SettingsStore:
    def test_load_on_missing_file_is_all_defaults(self) -> None:
        payload = core_v2_settings.load()
        assert "flags" not in payload  # #515: no longer a persisted section
        assert payload["context_strategy"] == "automatic"

    def test_every_flag_is_always_enabled(self) -> None:
        """#515 Settings diet: `flag_enabled()` always returns True — every
        Core V2 subsystem is unconditionally on, with no Settings toggle
        left to flip any of them off (the env var per `core/*/flag.py`
        remains the only escape hatch — see `TestFlagConfigFallback`)."""
        for name in (
            "router",
            "conversation",
            "context",
            "brain",
            "scheduler",
            "auto_migrate",
            "v2_authority",
            "some-made-up-name",  # name is accepted but unused — see docstring
        ):
            assert core_v2_settings.flag_enabled(name) is True, name

    def test_scheduler_policy_round_trips(self) -> None:
        policy = core_v2_settings.SchedulerPolicyConfig(
            max_agents_global=4,
            provider_max_concurrent={"codex": 2},
            default_priority="high",
        )
        assert core_v2_settings.save_scheduler_policy(policy) is True
        reloaded = core_v2_settings.load_scheduler_policy()
        assert reloaded.max_agents_global == 4
        assert reloaded.provider_max_concurrent == {"codex": 2}
        assert reloaded.default_priority == "high"

    def test_corrupt_file_falls_back_to_defaults(self) -> None:
        core_v2_settings.path().parent.mkdir(parents=True, exist_ok=True)
        core_v2_settings.path().write_text("not json", encoding="utf-8")
        assert core_v2_settings.flag_enabled("router") is True  # always true anyway
        assert core_v2_settings.load_context_strategy() == "automatic"  # falls back


class TestCoreV2SettingsCache:
    """`load()` reloads only when `core-v2-settings.json`'s (mtime, size)
    actually changes — PR #311 review must-fix #1. Exercised via
    `context_strategy` (#515 turned "flags" into a non-persisted section, so
    a flag write can no longer touch the cached file the way it used to)."""

    def test_load_twice_unchanged_file_reads_disk_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        core_v2_settings.save_context_strategy("deep")  # creates the file
        core_v2_settings._reset_cache()

        calls = []
        real_read_text = Path.read_text

        def counting_read_text(self, *a, **kw):
            calls.append(self)
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", counting_read_text)

        first = core_v2_settings.load()
        second = core_v2_settings.load()
        assert first == second
        assert len(calls) == 1  # second load() is a pure cache hit

    def test_load_after_file_edit_rereads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        core_v2_settings.save_context_strategy("deep")
        assert core_v2_settings.load()["context_strategy"] == "deep"

        # Edit the file directly (bypassing save()) so mtime/size change
        # without going through the cache-invalidating path — proves load()
        # itself detects the change, not just save()'s _reset_cache().
        raw = json.loads(core_v2_settings.path().read_text(encoding="utf-8"))
        raw["context_strategy"] = "fast"
        core_v2_settings.path().write_text(json.dumps(raw), encoding="utf-8")

        assert core_v2_settings.load()["context_strategy"] == "fast"

    def test_save_invalidates_cache_immediately(self) -> None:
        assert core_v2_settings.load_context_strategy() == "automatic"  # shipped default
        core_v2_settings.save_context_strategy("fast")
        assert core_v2_settings.load_context_strategy() == "fast"

    def test_missing_file_caches_default_and_still_detects_creation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (
            core_v2_settings.load()["context_strategy"] == "automatic"
        )  # cache = (None, defaults)
        assert core_v2_settings.load()["context_strategy"] == "automatic"  # cache hit, no crash

        core_v2_settings.save_context_strategy("deep")  # file now exists
        assert core_v2_settings.load()["context_strategy"] == "deep"

    def test_load_returns_independent_copies(self) -> None:
        core_v2_settings.save_context_strategy("deep")
        first = core_v2_settings.load()
        first["context_strategy"] = "fast"  # mutate the caller's copy
        second = core_v2_settings.load()
        assert second["context_strategy"] == "deep"  # cache itself is untouched


class TestFlagConfigFallback:
    """#515 Settings diet: `core_v2_settings.flag_enabled()` always returns
    True now (no persisted toggle survives it) — the env var per
    `core/*/flag.py` is the ONLY thing left that can still disable one, and
    it must keep winning."""

    def test_router_env_zero_wins_over_always_on_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.routing.flag import v2_router_enabled

        monkeypatch.setenv("TAKKUB_V2_ROUTER", "0")
        assert v2_router_enabled() is False

    def test_router_enabled_by_default_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.routing.flag import v2_router_enabled

        monkeypatch.delenv("TAKKUB_V2_ROUTER", raising=False)
        assert v2_router_enabled() is True

    def test_brain_enabled_by_default_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.core.brain.flag import v2_brain_enabled

        monkeypatch.delenv("TAKKUB_V2_BRAIN", raising=False)
        assert v2_brain_enabled() is True

    def test_scheduler_enabled_by_default_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.scheduling.flag import v2_scheduler_enabled

        monkeypatch.delenv("TAKKUB_V2_SCHEDULER", raising=False)
        assert v2_scheduler_enabled() is True

    def test_conversation_enabled_by_default_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.conversation.flag import v2_conversation_enabled

        monkeypatch.delenv("TAKKUB_V2_CONVERSATION", raising=False)
        assert v2_conversation_enabled() is True

    # Widget smoke tests for the Core V2 (Routing/Brain/Scheduler/Accounts &
    # Pools) Settings views used to live here — removed along with the whole
    # `settings_core_v2.py` UI module in the #515 settings diet. The
    # equivalent "old VIEW_* constant still routes somewhere sane" coverage
    # now lives in `test_settings_window.py`'s `TestViewRedirects`.
