"""#582 — the pane auto-compaction window, and why it ships OFF.

The token maths was real: 417 real sessions averaged a 201k prefix per turn,
with 91.5% of all prefix spend in sessions of 151+ turns, so a 200k window
modelled out at ~22% of prefix spend. 2.1.2 shipped that window and it was
reverted the same day — panes compacted constantly in every role and the
interruptions made them unusable, with Lead explicitly told to run to 1M.

So these tests pin the OFF default and keep the opt-in lever honest, because
the flag still works and someone will want to try a window again.
"""

from __future__ import annotations

import pytest

from agent_takkub.core.providers.claude_plan import assemble_claude_argv
from agent_takkub.provider_spec import PROVIDER_REGISTRY, TEAMMATE_AUTOCOMPACT_TOKENS
from agent_takkub.spawn_engine import _teammate_autocompact


class TestWindowResolution:
    def test_ships_off_so_panes_keep_the_cli_default_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No window by default = no `--autocompact` = the CLI's own 1M, i.e.
        exactly the pre-2.1.2 behaviour the user asked to go back to."""
        monkeypatch.delenv("TAKKUB_AUTOCOMPACT", raising=False)
        assert TEAMMATE_AUTOCOMPACT_TOKENS is None
        assert _teammate_autocompact() is None

    def test_any_shipped_window_would_be_inside_the_cli_accepted_range(self) -> None:
        # `--autocompact` accepts 100k-1M; a value outside it would be rejected
        # by the CLI at spawn, i.e. every pane would fail to start. Guards the
        # day someone sets a number here again.
        if TEAMMATE_AUTOCOMPACT_TOKENS is not None:
            assert 100_000 <= TEAMMATE_AUTOCOMPACT_TOKENS <= 1_000_000

    def test_empty_env_disables_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_AUTOCOMPACT", "")
        assert _teammate_autocompact() is None

    def test_env_overrides_the_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_AUTOCOMPACT", "150000")
        assert _teammate_autocompact() == 150_000

    @pytest.mark.parametrize("bad", ["not-a-number", "50000", "2000000", "-1"])
    def test_unusable_values_fall_back_to_not_passing_the_flag(
        self, bad: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never hand the CLI a value it rejects — a pane that cannot spawn is
        far worse than one that compacts late."""
        monkeypatch.setenv("TAKKUB_AUTOCOMPACT", bad)
        assert _teammate_autocompact() is None


class TestArgv:
    def test_flag_reaches_the_assembled_argv(self) -> None:
        argv = assemble_claude_argv(
            "claude", setting_sources="project,local", autocompact_argv=["--autocompact", "200000"]
        )
        assert "--autocompact" in argv
        assert argv[argv.index("--autocompact") + 1] == "200000"

    def test_absent_when_not_requested(self) -> None:
        argv = assemble_claude_argv("claude", setting_sources="project,local")
        assert "--autocompact" not in argv


class TestProviderCoverage:
    def test_claude_declares_the_flag(self) -> None:
        assert PROVIDER_REGISTRY["claude"].autocompact_flag == "--autocompact"

    @pytest.mark.parametrize("provider", ["codex", "gemini", "opencode", "kimi", "cursor"])
    def test_other_providers_record_the_gap_rather_than_assuming_it_works(
        self, provider: str
    ) -> None:
        """#103: a provider without the flag must say so explicitly, so nobody
        later assumes every pane is compacting."""
        assert PROVIDER_REGISTRY[provider].autocompact_flag is None
