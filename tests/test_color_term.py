"""Tests for `_apply_color_term`, the helper that advertises a truecolor
terminal so claude/ink renders ANSI colours inside a spawned pane.

Why this matters: on macOS the text inside a pane (claude's TUI, qa output)
rendered monochrome while the window chrome was fine. The cockpit front-end
is xterm.js on every OS with a full 256-colour + truecolor palette, so the
screen can show colour — the gap was colour *detection*. A GUI-launched
cockpit on macOS (Finder/.app/Dock) inherits no `TERM`, `COLORTERM` was
never on the pane allowlist, and nothing defaulted them — so claude saw a
non-colour terminal and dropped to monochrome. Windows was unaffected
because claude forces colour through the Win32 console API regardless.
"""

from __future__ import annotations

from agent_takkub.orchestrator import _apply_color_term, _build_pane_env


class TestApplyColorTerm:
    def test_sets_defaults_on_empty_env(self) -> None:
        # The macOS GUI-launch case: no TERM/COLORTERM inherited at all.
        env: dict[str, str] = {}
        _apply_color_term(env)
        assert env["TERM"] == "xterm-256color"
        assert env["COLORTERM"] == "truecolor"

    def test_preserves_real_terminal_term(self) -> None:
        # Cockpit launched from iTerm/Terminal: a real TERM is present and
        # must win — setdefault, first writer keeps its value.
        env = {"TERM": "screen-256color"}
        _apply_color_term(env)
        assert env["TERM"] == "screen-256color"
        # COLORTERM was still absent, so it gets the default.
        assert env["COLORTERM"] == "truecolor"

    def test_preserves_user_provided_colorterm(self) -> None:
        env = {"COLORTERM": "24bit"}
        _apply_color_term(env)
        assert env["COLORTERM"] == "24bit"

    def test_no_return_value(self) -> None:
        # Mutates in place — spawn() relies on this, not a return value.
        env: dict[str, str] = {}
        assert _apply_color_term(env) is None


class TestColorTermInAllowlist:
    def test_host_term_passes_through_pane_env(self, monkeypatch) -> None:
        # TERM is on the allowlist, so a host-level TERM reaches the pane
        # (and the helper's setdefault then becomes a no-op for it).
        monkeypatch.setenv("TERM", "xterm-kitty")
        env = _build_pane_env()
        assert env.get("TERM") == "xterm-kitty"

    def test_colorterm_is_stripped_by_allowlist(self, monkeypatch) -> None:
        # COLORTERM is intentionally NOT on the allowlist — this is exactly
        # why the helper must default it. Pin that the allowlist strips it,
        # so the helper stays load-bearing (if someone adds COLORTERM to the
        # allowlist later this test flags that the helper's role changed).
        monkeypatch.setenv("COLORTERM", "truecolor")
        env = _build_pane_env()
        assert "COLORTERM" not in env
