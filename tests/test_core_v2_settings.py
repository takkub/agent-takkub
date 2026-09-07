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
        # Default-ON since 1.0.84 (epic #309's last rung before 2.0.0);
        # `v2_authority` (#362) joined this sweep in 2.0.0 after its own soak
        # — see `core_v2_settings._DEFAULT_FLAGS`'s own comment.
        expected = {name: True for name in core_v2_settings.FLAG_NAMES}
        assert payload["flags"] == expected

    def test_set_flag_round_trips(self) -> None:
        assert core_v2_settings.flag_enabled("router") is True  # shipped default
        assert core_v2_settings.set_flag("router", False) is True
        assert core_v2_settings.flag_enabled("router") is False
        # Untouched flags keep the default — a single set_flag must not clobber
        # siblings (the direction that matters now the default is ON).
        assert core_v2_settings.flag_enabled("brain") is True

    def test_every_flag_ships_enabled(self) -> None:
        """The 1.0.84 flip (epic #309's last rung before 2.0.0) plus the
        2.0.0 flip that folded `v2_authority` (#362) into the same sweep: a
        cockpit with no settings file gets every Core V2 subsystem on.

        Pinned as its own test because every OTHER flag test now sets the
        value it wants explicitly, so nothing else would notice if the
        shipped default silently regressed to off.
        """
        for name in core_v2_settings.FLAG_NAMES:
            assert core_v2_settings.flag_enabled(name) is True, name

    def test_explicit_false_on_disk_survives_the_new_default(self) -> None:
        """An operator who turned a flag OFF must keep it off across the
        upgrade — `load()` layers the persisted file over the defaults, so a
        stored `false` is a decision, not an absence."""
        core_v2_settings.set_flag("scheduler", False)
        assert core_v2_settings.load()["flags"]["scheduler"] is False
        assert core_v2_settings.flag_enabled("scheduler") is False

    def test_set_unknown_flag_raises(self) -> None:
        with pytest.raises(ValueError):
            core_v2_settings.set_flag("bogus", True)

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
        assert core_v2_settings.flag_enabled("router") is True  # falls back to defaults


class TestCoreV2SettingsCache:
    """`load()` reloads only when `core-v2-settings.json`'s (mtime, size)
    actually changes — PR #311 review must-fix #1."""

    def test_load_twice_unchanged_file_reads_disk_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        core_v2_settings.set_flag("router", True)  # creates the file, primes nothing
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
        core_v2_settings.set_flag("router", True)
        assert core_v2_settings.load()["flags"]["router"] is True

        # Edit the file directly (bypassing save()) so mtime/size change
        # without going through the cache-invalidating path — proves load()
        # itself detects the change, not just save()'s _reset_cache().
        raw = json.loads(core_v2_settings.path().read_text(encoding="utf-8"))
        raw["flags"]["router"] = False
        core_v2_settings.path().write_text(json.dumps(raw), encoding="utf-8")

        assert core_v2_settings.load()["flags"]["router"] is False

    def test_set_flag_invalidates_cache_immediately(self) -> None:
        assert core_v2_settings.flag_enabled("brain") is True  # shipped default
        core_v2_settings.set_flag("brain", False)
        assert core_v2_settings.flag_enabled("brain") is False

    def test_missing_file_caches_default_and_still_detects_creation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert core_v2_settings.load()["flags"]["router"] is True  # cache = (None, defaults)
        assert core_v2_settings.load()["flags"]["router"] is True  # cache hit, no crash

        core_v2_settings.set_flag("router", False)  # file now exists
        assert core_v2_settings.load()["flags"]["router"] is False

    def test_load_returns_independent_copies(self) -> None:
        core_v2_settings.set_flag("router", True)
        first = core_v2_settings.load()
        first["flags"]["router"] = False  # mutate the caller's copy
        second = core_v2_settings.load()
        assert second["flags"]["router"] is True  # cache itself is untouched


class TestFlagConfigFallback:
    """Env always wins; unset falls back to the persisted config — the exact
    contract each core/*/flag.py docstring now states."""

    def test_router_env_wins_over_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.core.routing.flag import v2_router_enabled

        core_v2_settings.set_flag("router", True)
        monkeypatch.setenv("TAKKUB_V2_ROUTER", "0")
        assert v2_router_enabled() is False

    def test_router_falls_back_to_config_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.routing.flag import v2_router_enabled

        monkeypatch.delenv("TAKKUB_V2_ROUTER", raising=False)
        assert v2_router_enabled() is True  # no file yet -> shipped default
        core_v2_settings.set_flag("router", False)
        assert v2_router_enabled() is False  # config is what env-unset reads

    def test_brain_falls_back_to_config_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.brain.flag import v2_brain_enabled

        monkeypatch.delenv("TAKKUB_V2_BRAIN", raising=False)
        core_v2_settings.set_flag("brain", True)
        assert v2_brain_enabled() is True

    def test_scheduler_falls_back_to_config_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.scheduling.flag import v2_scheduler_enabled

        monkeypatch.delenv("TAKKUB_V2_SCHEDULER", raising=False)
        core_v2_settings.set_flag("scheduler", True)
        assert v2_scheduler_enabled() is True

    def test_conversation_falls_back_to_config_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub.core.conversation.flag import v2_conversation_enabled

        monkeypatch.delenv("TAKKUB_V2_CONVERSATION", raising=False)
        core_v2_settings.set_flag("conversation", True)
        assert v2_conversation_enabled() is True

    # Widget smoke tests for the Core V2 (Routing/Brain/Scheduler/Accounts &
    # Pools) Settings views used to live here — removed along with the whole
    # `settings_core_v2.py` UI module in the #515 settings diet. The
    # equivalent "old VIEW_* constant still routes somewhere sane" coverage
    # now lives in `test_settings_window.py`'s `TestViewRedirects`.
