"""#582 — panes auto-compact at a real window instead of never.

Claude Code compacts on its own near the context limit, but that limit is 1M,
so panes in practice never reach it: 417 real sessions averaged a 201k prefix
per turn, with 91.5% of all prefix spend in sessions of 151+ turns. Passing
`--autocompact` moves WHEN the CLI's own compaction runs without changing how
it runs.
"""

from __future__ import annotations

import pytest

from agent_takkub.core.providers.claude_plan import assemble_claude_argv
from agent_takkub.provider_spec import PROVIDER_REGISTRY, TEAMMATE_AUTOCOMPACT_TOKENS
from agent_takkub.spawn_engine import _teammate_autocompact


class TestWindowResolution:
    def test_defaults_to_the_shipped_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_AUTOCOMPACT", raising=False)
        assert _teammate_autocompact() == TEAMMATE_AUTOCOMPACT_TOKENS

    def test_shipped_window_is_inside_the_cli_accepted_range(self) -> None:
        # `--autocompact` accepts 100k-1M; a value outside it would be rejected
        # by the CLI at spawn, i.e. every pane would fail to start.
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
