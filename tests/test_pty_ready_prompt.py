"""Regression tests for is_at_ready_prompt() busy/idle detection.

The idle watchdog (orchestrator._check_idle_teammates) fires `taktub done`
reminders into any pane that is_at_ready_prompt() reports as idle. gemini and
codex keep their "type your message or @path" input box visible *even while
they are Thinking…* — so the busy state must be detected via the
"esc to cancel" indicator, not the absence of the input box. Without that,
a thinking gemini reads as idle and the watchdog floods it with reminders
(the 2026-05-30 reminder-pileup + search-loop incident).
"""

from __future__ import annotations

from agent_takkub.pty_session import PtySession


def _feed_screen(*lines: str) -> PtySession:
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(("\r\n".join(lines)).encode())
    return s


def test_gemini_thinking_with_input_box_is_not_ready() -> None:
    # gemini renders the input prompt AND the cancel indicator at once.
    s = _feed_screen(
        "Thinking... (esc to cancel, 1h 1m 48s)",
        "Type your message or @path/to/file",
    )
    assert s.is_at_ready_prompt() is False


def test_gemini_idle_input_box_only_is_ready() -> None:
    s = _feed_screen("Type your message or @path/to/file")
    assert s.is_at_ready_prompt() is True


def test_claude_working_esc_to_interrupt_is_not_ready() -> None:
    # Regression guard for the pre-existing claude busy indicator.
    s = _feed_screen("(esc to interrupt) building...", "bypass permissions")
    assert s.is_at_ready_prompt() is False


def test_gemini_idle_with_update_footer_is_ready() -> None:
    # issue #51: once a newer gemini release exists upstream, gemini shows a
    # PASSIVE "Gemini CLI update available! <cur> -> <new>" footer that does
    # NOT block input. A ready gemini wearing this banner must still read as
    # idle so the watchdog can nudge it to run `takkub done`. Previously the
    # blanket "update available!" blocker made it read as perpetually-busy.
    s = _feed_screen(
        "Gemini CLI update available! 0.46.0 -> 0.47.0",
        "Type your message or @path/to/file",
    )
    assert s.is_at_ready_prompt() is True


def test_gemini_idle_with_update_footer_is_ready_even_if_prompt_missing() -> None:
    # Regression guard: even if the "type your message or" hint is missing (e.g.
    # scrolled off-screen or prompt changed), the passive Gemini update footer
    # should NOT trigger the "update available!" blocker.
    s = _feed_screen("Gemini CLI update available! 0.46.0 -> 0.47.0")
    assert s.is_at_ready_prompt() is True


def test_gemini_thinking_with_update_footer_is_not_ready() -> None:
    # The update footer must not flip a *thinking* gemini to ready -- the
    # "esc to cancel" busy indicator still takes precedence.
    s = _feed_screen(
        "Gemini CLI update available! 0.46.0 -> 0.47.0",
        "Thinking... (esc to cancel, 12s)",
        "Type your message or @path/to/file",
    )
    assert s.is_at_ready_prompt() is False


def test_codex_splash_update_modal_is_not_ready() -> None:
    # codex's "update available!" is part of a startup splash modal that must
    # be dismissed before the prompt is usable -- it must still block (the
    # gemini ready marker is absent on a codex screen, so the blocker applies).
    s = _feed_screen(
        "OpenAI Codex (v1.2.3)",
        "update available! run npm i -g @openai/codex",
    )
    assert s.is_at_ready_prompt() is False


# -- is_blocked_on_tty_prompt() -- issue #52 Layer 2 -------------------------


class TestIsBlockedOnTtyPrompt:
    """Verify that is_blocked_on_tty_prompt() detects interactive shell prompts
    in the bottom tail of the visible screen without false-positives on:
    - claude/codex/gemini ready prompts
    - identical patterns in earlier scrollback
    """

    def test_npx_ok_to_proceed_detected(self) -> None:
        s = _feed_screen(
            "Need to install the following packages:",
            "  create-react-app@5.0.1",
            "Ok to proceed? (y)",
        )
        assert s.is_blocked_on_tty_prompt() is not None

    def test_y_slash_n_bracket_detected(self) -> None:
        s = _feed_screen("Do you want to overwrite the file? [y/N]")
        assert s.is_blocked_on_tty_prompt() is not None

    def test_Y_slash_n_bracket_detected(self) -> None:
        s = _feed_screen("Continue with the operation? [Y/n]")
        assert s.is_blocked_on_tty_prompt() is not None

    def test_y_slash_n_parens_detected(self) -> None:
        s = _feed_screen("Are you sure you want to delete? (y/n)")
        assert s.is_blocked_on_tty_prompt() is not None

    def test_press_any_key_detected(self) -> None:
        s = _feed_screen("Press any key to continue...")
        assert s.is_blocked_on_tty_prompt() is not None

    def test_overwrite_prompt_detected(self) -> None:
        s = _feed_screen("Overwrite? [y/N]")
        assert s.is_blocked_on_tty_prompt() is not None

    def test_are_you_sure_detected(self) -> None:
        s = _feed_screen("Are you sure you want to push? [y/N]")
        assert s.is_blocked_on_tty_prompt() is not None

    def test_password_prompt_detected(self) -> None:
        s = _feed_screen("Username for 'https://github.com': monch", "Password:")
        assert s.is_blocked_on_tty_prompt() is not None

    def test_username_prompt_detected(self) -> None:
        s = _feed_screen("Username:")
        assert s.is_blocked_on_tty_prompt() is not None

    def test_returns_none_on_normal_output(self) -> None:
        s = _feed_screen(
            "Tests passed (42 passed, 0 failed)",
            "Build succeeded in 3.2s",
        )
        assert s.is_blocked_on_tty_prompt() is None

    def test_returns_none_on_empty_screen(self) -> None:
        s = _feed_screen("")
        assert s.is_blocked_on_tty_prompt() is None

    def test_returns_none_on_claude_ready_prompt(self) -> None:
        # Claude's "bypass permissions" footer must NOT be detected as a TTY
        # prompt -- it's a claude UI element, not an interactive shell pause.
        s = _feed_screen(
            "What would you like to do next?",
            "bypass permissions",
        )
        assert s.is_blocked_on_tty_prompt() is None

    def test_prompt_in_scrollback_not_detected(self) -> None:
        # A [y/N] pattern that appeared earlier (not in the bottom 5 rows)
        # must not trigger a false-positive. Simulate by filling 10 rows
        # of normal output above the prompt so it's pushed out of the tail.
        lines = ["Ok to proceed? (y)"] + [f"output line {i}" for i in range(10)]
        s = _feed_screen(*lines)
        # The TTY prompt is now more than 5 rows from the bottom.
        assert s.is_blocked_on_tty_prompt() is None

    def test_returns_matched_line_text(self) -> None:
        # Return value should be the stripped content of the matching line.
        s = _feed_screen("Ok to proceed? (y)")
        result = s.is_blocked_on_tty_prompt()
        assert result is not None
        assert "ok to proceed" in result.lower()

    def test_is_independent_of_is_at_ready_prompt(self) -> None:
        # These two state-detection methods are orthogonal: a pane at its
        # claude ready prompt is NOT blocked on a TTY prompt, and vice versa.
        ready = _feed_screen("bypass permissions")
        blocked = _feed_screen("Ok to proceed? (y)")
        assert ready.is_at_ready_prompt() is True
        assert ready.is_blocked_on_tty_prompt() is None
        assert blocked.is_at_ready_prompt() is False
        assert blocked.is_blocked_on_tty_prompt() is not None


# -- has_unparsed_tool_call() -- issue #59 ------------------------------------


class TestHasUnparsedToolCall:
    """Verify that has_unparsed_tool_call() detects literal tool-call XML on
    screen without false-positives on normal prose that mentions the word.

    Note: test strings use bare tag names (no namespace prefix) because that
    is the primary failure mode described in issue #59. The regex also catches
    namespace-prefixed variants (e.g. with 'antml:' prefix) since any tag that
    renders as visible text was not consumed by the harness.
    """

    def test_bare_invoke_tag_detected(self) -> None:
        s = _feed_screen('<invoke name="Bash">')
        assert s.has_unparsed_tool_call() is not None

    def test_bare_parameter_tag_detected(self) -> None:
        s = _feed_screen('<parameter name="command">ls -la</parameter>')
        assert s.has_unparsed_tool_call() is not None

    def test_bare_function_calls_open_tag_detected(self) -> None:
        s = _feed_screen("<function_calls>")
        assert s.has_unparsed_tool_call() is not None

    def test_closing_invoke_tag_detected(self) -> None:
        s = _feed_screen("</invoke>")
        assert s.has_unparsed_tool_call() is not None

    def test_closing_function_calls_tag_detected(self) -> None:
        s = _feed_screen("</function_calls>")
        assert s.has_unparsed_tool_call() is not None

    def test_multiline_tool_call_block_detected(self) -> None:
        # Typical malformed output: model printed the full XML block as text.
        s = _feed_screen(
            "<function_calls>",
            '<invoke name="Read">',
            '<parameter name="file_path">/tmp/foo.txt</parameter>',
            "</invoke>",
            "</function_calls>",
        )
        assert s.has_unparsed_tool_call() is not None

    def test_returns_matched_line_text(self) -> None:
        # Return value should be the stripped content of the matched line.
        s = _feed_screen('<invoke name="Bash">')
        result = s.has_unparsed_tool_call()
        assert result is not None
        assert "invoke" in result.lower()

    def test_returns_none_on_normal_output(self) -> None:
        # Regular prose that doesn't contain XML tags must not fire.
        s = _feed_screen(
            "I will now invoke the Bash tool to list files.",
            "The parameter value is the command string.",
        )
        assert s.has_unparsed_tool_call() is None

    def test_returns_none_on_empty_screen(self) -> None:
        s = _feed_screen("")
        assert s.has_unparsed_tool_call() is None

    def test_returns_none_on_claude_ready_prompt(self) -> None:
        # Claude's ready-prompt text must NOT trip the detector.
        s = _feed_screen("bypass permissions")
        assert s.has_unparsed_tool_call() is None

    def test_returns_none_on_build_output(self) -> None:
        # Build/test output that mentions parameter in prose must not trip.
        s = _feed_screen(
            "Running tests... 42 passed",
            "No errors found in parameter handling",
        )
        assert s.has_unparsed_tool_call() is None

    def test_scrollback_xml_not_detected(self) -> None:
        # XML that appeared many rows ago (above the cursor window) must
        # not trigger a false-positive once the session has moved on.
        # Fill enough lines so the tag is pushed above the 10-row scan window.
        lines = ['<invoke name="Bash">'] + [f"output line {i}" for i in range(12)]
        s = _feed_screen(*lines)
        assert s.has_unparsed_tool_call() is None


# -- M4#17: central marker table — env override + doctor self-test ------------


class TestReadyMarkerTable:
    def test_selftest_passes_on_shipped_table(self) -> None:
        from agent_takkub.pty_session import ready_marker_selftest

        assert ready_marker_selftest() == []

    def test_env_override_rescues_reworded_prompt(self, monkeypatch) -> None:
        from agent_takkub.pty_session import _classify_ready

        # Simulate an upstream reword the shipped table doesn't know.
        reworded = "» send a message (ctrl+j newline)"
        assert _classify_ready(reworded) is False
        monkeypatch.setenv("TAKKUB_EXTRA_READY_MARKERS", "send a message")
        assert _classify_ready(reworded) is True

    def test_env_override_does_not_beat_hard_blocker(self, monkeypatch) -> None:
        from agent_takkub.pty_session import _classify_ready

        # An active interrupt must still win even with a matching extra marker.
        monkeypatch.setenv("TAKKUB_EXTRA_READY_MARKERS", "send a message")
        assert _classify_ready("send a message\n(esc to interrupt) working") is False

    def test_selftest_ignores_env_override(self, monkeypatch) -> None:
        # The self-test validates the SHIPPED table, not whatever the operator
        # patched in — so a bogus override can't mask a real regression.
        from agent_takkub.pty_session import ready_marker_selftest

        monkeypatch.setenv("TAKKUB_EXTRA_READY_MARKERS", "zzz-not-a-real-marker")
        assert ready_marker_selftest() == []

    def test_doctor_check_reports_ok(self) -> None:
        from agent_takkub.doctor import Status, check_ready_markers

        findings = check_ready_markers()
        assert len(findings) == 1
        assert findings[0].status is Status.OK
        assert findings[0].category == "markers"
