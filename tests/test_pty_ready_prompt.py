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

import pytest

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


class TestShowsPendingInput:
    """#79: distinguish a swallowed paste (input box empty) from a swallowed
    Enter (pasted content still in the box) so the delivery self-heal re-pastes
    vs. only resends the CR."""

    def test_pasted_placeholder_is_pending(self) -> None:
        s = _feed_screen("[Pasted text +42 lines]", "bypass permissions")
        assert s.shows_pending_input() is True

    def test_empty_box_is_not_pending(self) -> None:
        s = _feed_screen("Welcome to Claude Code", "bypass permissions")
        assert s.shows_pending_input("[ROLE: qa] verify the login flow") is False

    def test_inline_fragment_is_pending(self) -> None:
        # Short content rendered inline (no placeholder) is detected via a leading
        # fragment of the expected text.
        s = _feed_screen("> [ROLE: qa] verify the login flow", "bypass permissions")
        assert s.shows_pending_input("[ROLE: qa] verify the login flow") is True

    def test_body_quote_above_footer_does_not_poison(self) -> None:
        # A '[pasted text]' mention scrolled up in the conversation body must not
        # read as pending input — detection is scoped to the bottom region.
        s = _feed_screen(
            "we discussed [Pasted text +1 lines] earlier",
            *["" for _ in range(8)],
            "bypass permissions",
        )
        assert s.shows_pending_input() is False


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
        s = _feed_screen("Username for 'https://github.com': alice", "Password:")
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


# -- orphaned wide-char stub crash (pyte display IndexError) ------------------


class TestOrphanedWideCharStub:
    """A wide (width-2) char that gets overwritten by a narrower one on a TUI
    redraw leaves pyte a `data=""` stub cell. pyte's own `Screen.display` then
    crashes with `IndexError: string index out of range` on `wcwidth(char[0])`.

    That read happens every idle-watchdog tick via display_lines() — when it
    threw, the per-pane watchdog body aborted, so a teammate that forgot
    `takkub done` was never nudged (and Lead-bound done notices stalled). The
    pane looked "finished, closed, never reported back", worsening with many
    panes open. _safe_screen_display() must render the stub as empty instead of
    crashing. Repro: wide char, carriage-return, narrow overwrite on one row.
    """

    @staticmethod
    def _poisoned() -> PtySession:
        s = PtySession(cols=80, rows=24)
        # 中 = CJK width-2 @ col0 (stub "" @ col1); \r returns cursor; x is a
        # width-1 overwrite of col0, orphaning the "" stub at col1.
        s._feed_and_log("中\rx".encode())
        return s

    def test_raw_pyte_display_still_has_the_bug(self) -> None:
        # Guard the guard: prove the constructed screen really triggers pyte's
        # crash, so this regression test stays meaningful if pyte is upgraded.
        s = self._poisoned()
        with pytest.raises(IndexError):
            list(s.screen.display)

    def test_display_lines_does_not_raise(self) -> None:
        s = self._poisoned()
        rows = s.display_lines()  # must not raise
        assert isinstance(rows, list)
        assert rows[0].startswith("x")  # the overwrite survived; stub dropped

    def test_state_detectors_do_not_raise_on_poison_stub(self) -> None:
        s = self._poisoned()
        # All three readers route through _safe_screen_display now.
        assert s.is_at_ready_prompt() in (True, False)
        s.has_unparsed_tool_call()
        s.is_blocked_on_tty_prompt()

    def test_ready_marker_survives_poison_stub(self) -> None:
        # The whole point: a poison stub elsewhere on screen must not block the
        # watchdog from seeing the real ready marker (else the teammate never
        # gets nudged to call `takkub done`).
        s = PtySession(cols=80, rows=24)
        s._feed_and_log("中\rx\r\nbypass permissions".encode())
        assert s.is_at_ready_prompt() is True

    def test_doctor_check_reports_ok(self) -> None:
        from agent_takkub.doctor import Status, check_ready_markers

        findings = check_ready_markers()
        assert len(findings) == 1
        assert findings[0].status is Status.OK
        assert findings[0].category == "markers"


# -- footer-region scoping: conversation body must not poison detection --------


class TestReadyRegionScoping:
    """Root fix for #20/#70: ready & blocker markers are matched only against the
    bottom footer/status region, so a marker string quoted in the conversation
    BODY (e.g. a Lead discussing 'esc to interrupt' or 'bypass permissions')
    can't poison the verdict. This was the root cause of the #70 false-busy
    stall — a Lead whose visible conversation mentioned a blocker read as busy,
    so the done-notice reaper skipped it forever.
    """

    def test_blocker_quoted_in_body_does_not_read_busy(self) -> None:
        # 'esc to interrupt' in the body (row 0), a real ready footer at bottom.
        s = PtySession(cols=80, rows=24)
        body = (
            "discussing the esc to interrupt marker\r\n"
            + ("filler line\r\n" * 18)
            + "bypass permissions"
        )
        s._feed_and_log(body.encode())
        assert s.is_at_ready_prompt() is True  # body mention must not poison → busy

    def test_real_blocker_at_bottom_still_detected(self) -> None:
        # The genuine spinner sits in the bottom region → must still read busy.
        s = PtySession(cols=80, rows=24)
        body = ("filler line\r\n" * 18) + "Thinking... (esc to interrupt)\r\nbypass permissions"
        s._feed_and_log(body.encode())
        assert s.is_at_ready_prompt() is False

    def test_ready_marker_quoted_in_body_does_not_false_ready(self) -> None:
        # 'bypass permissions' only in the body (row 0); no real footer below.
        s = PtySession(cols=80, rows=24)
        body = "I added bypass permissions to the config\r\n" + ("output line\r\n" * 20)
        s._feed_and_log(body.encode())
        assert s.is_at_ready_prompt() is False

    def test_update_available_in_body_is_not_a_live_splash(self) -> None:
        s = PtySession(cols=80, rows=24)
        body = (
            "the codex update available! message is annoying\r\n"
            + ("x\r\n" * 18)
            + "bypass permissions"
        )
        s._feed_and_log(body.encode())
        assert s.is_at_update_splash() is False  # body mention, not a live splash

    def test_short_screen_unchanged(self) -> None:
        # <= tail rows -> whole screen is the region; legacy behaviour preserved.
        s = PtySession(cols=80, rows=24)
        s._feed_and_log(b"bypass permissions")
        assert s.is_at_ready_prompt() is True


# ── startup / message-queue marker (idle-watchdog gate) ──────────────────────
# The forgot-`takkub done` watchdog suppresses reminders while this reads True.
# It must therefore track the LIVE footer only: a boot line left behind in the
# conversation body must not pin it True forever, or a pane that finished but
# forgot to report would never be reminded.


def test_startup_marker_true_while_codex_boots_mcp() -> None:
    s = _feed_screen(
        "• Booting MCP server: codex_apps (0s • esc to interrupt)",
        "gpt-5.6 high · ~/project · Fast off",
    )
    assert s.shows_startup_marker() is True


def test_startup_marker_true_while_message_is_queued() -> None:
    s = _feed_screen(
        "› [ROLE: codex] task...",
        "tab to queue message",
        "gpt-5.6 high · ~/project · Fast off",
    )
    assert s.shows_startup_marker() is True


def test_stale_boot_line_in_scrollback_does_not_pin_startup_marker() -> None:
    # The boot line has scrolled up out of the footer while the pane sits idle
    # at its composer. Regression guard: scanning the whole screen here kept the
    # marker True indefinitely and starved the idle reminder (codex review M3).
    s = _feed_screen(
        "• Booting MCP server: codex_apps (0s • esc to interrupt)",
        "• Ran Get-Content -Raw -LiteralPath 'spec.md'",
        "• done reading the spec",
        "",
        "some later output line",
        "another later output line",
        "yet another later output line",
        "gpt-5.6 high · ~/project · Fast off",
    )
    assert s.shows_startup_marker() is False
    assert s.is_at_ready_prompt() is True
