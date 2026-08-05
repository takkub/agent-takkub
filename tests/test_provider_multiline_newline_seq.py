"""ProviderSpec.multiline_newline_seq — per-provider ESC+CR gate (#149).

terminal.html only intercepts Shift+Enter/Alt+Enter as a multiline newline
when this is set; codex's ratatui UI treats a bare ESC as interrupt/clear-
composer, so it (and every provider whose TUI toolkit isn't confirmed safe)
must stay at the default None.
"""

from __future__ import annotations

from agent_takkub import provider_spec


def test_ink_providers_get_esc_cr() -> None:
    assert provider_spec.claude_spec.multiline_newline_seq == "\x1b\r"
    assert provider_spec.gemini_spec.multiline_newline_seq == "\x1b\r"


def test_unconfirmed_providers_default_to_none() -> None:
    for spec in (
        provider_spec.codex_spec,
        provider_spec.opencode_spec,
        provider_spec.kimi_spec,
        provider_spec.cursor_spec,
    ):
        assert spec.multiline_newline_seq is None
