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


@pytest.mark.parametrize(
    "account_gate",
    (
        "⣷  Signing in...",
        "⚠ Verifying your account...",
    ),
)
def test_gemini_account_gate_with_idle_footer_is_not_ready(account_gate: str) -> None:
    """#126: agy paints its idle footer while account checks still swallow Enter."""
    s = _feed_screen(
        account_gate,
        "? for shortcuts",
        "Gemini 3.6 Flash · medium (Google AI Pro)",
    )
    assert s.is_at_ready_prompt() is False


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


def test_kimi_idle_footer_is_ready() -> None:
    # #257: kimi_spec.ready_rules was an empty tuple, so is_at_ready_prompt()
    # could never return True for a kimi pane no matter what its screen
    # showed — every assigned task sat undelivered until the busy-wait
    # ceiling. Captured via direct ConPTY capture against a signed-in
    # kimi-cli 1.49.x session on Windows, 2026-08-16.
    s = _feed_screen(
        "main  @: mention files | ctrl-x: toggle mode | shift-tab: plan mode | ctrl+o: editor"
    )
    assert s.is_at_ready_prompt() is True


class TestIsAtTrustPrompt:
    """#186: agy's own folder-trust modal words the confirm hint "enter
    Confirm" (no "to"), unlike claude/codex's "Enter to confirm" — the
    original exact-substring match silently never fired for it, so
    `_auto_trust` never pressed Enter and a worktree spawn hung."""

    def test_claude_wording_is_trust_prompt(self) -> None:
        s = _feed_screen(
            "Do you trust the files in this folder?",
            "> Yes, I trust this folder",
            "  No, exit",
            "Enter to confirm · Esc to exit",
        )
        assert s.is_at_trust_prompt() is True

    def test_agy_live_captured_wording_is_trust_prompt(self) -> None:
        # Verbatim live capture from the issue #186 incident (2026-08-13
        # 20:40) — this exact screen previously read as NOT a trust prompt.
        s = _feed_screen(
            "> Yes, I trust this folder",
            "  No, exit",
            "  up/down Navigate . enter Confirm",
        )
        assert s.is_at_trust_prompt() is True

    def test_codex_wording_is_trust_prompt(self) -> None:
        s = _feed_screen(
            "Do you trust the contents of this directory?",
            "Press enter to continue",
        )
        assert s.is_at_trust_prompt() is True

    def test_conversation_text_merely_quoting_it_does_not_poison(self) -> None:
        # Whole-screen scan is intentional here (unlike is_at_ready_prompt's
        # bottom-region scoping) — but a plain mention without the paired
        # confirm hint on screen must still read false.
        s = _feed_screen("earlier the pane said trust this folder and moved on", "? for shortcuts")
        assert s.is_at_trust_prompt() is False

    def test_unrelated_screen_is_not_trust_prompt(self) -> None:
        s = _feed_screen("Type your message or @path/to/file")
        assert s.is_at_trust_prompt() is False


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


# -- is_blocked_on_permission_prompt() -- issue #236 -------------------------


class TestIsBlockedOnPermissionPrompt:
    """Claude Code's own numbered tool-permission approval dialog has no
    [y/N] bracket for is_blocked_on_tty_prompt()'s regex to match, so a pane
    wedged on one used to read as ordinary busy generation forever — a real
    incident left a pane stuck 2h51m with `takkub status` reporting "working,
    progress 0s ago" throughout."""

    def test_bash_permission_dialog_detected(self) -> None:
        s = _feed_screen(
            "Bash command",
            "  rtk curl -s -D - -o /dev/null http://localhost:6700/",
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. Yes, and don't ask again for rtk commands in this project",
            "  3. No, and tell Claude what to do differently (esc)",
        )
        result = s.is_blocked_on_permission_prompt()
        assert result is not None
        assert "1. yes" in result.lower()

    def test_edit_permission_dialog_detected(self) -> None:
        # The question line varies per tool — detection must not depend on it.
        s = _feed_screen(
            "Do you want to make this edit to orchestrator.py?",
            "❯ 1. Yes",
            "  2. Yes, allow all edits during this session",
            "  3. No, and tell Claude what to do differently (esc)",
        )
        assert s.is_blocked_on_permission_prompt() is not None

    def test_returns_none_on_claude_ready_prompt(self) -> None:
        s = _feed_screen("What would you like to do next?", "bypass permissions")
        assert s.is_blocked_on_permission_prompt() is None

    def test_returns_none_on_ordinary_busy_spinner(self) -> None:
        s = _feed_screen("Doing... (esc to cancel, 12s)")
        assert s.is_blocked_on_permission_prompt() is None

    def test_returns_none_on_empty_screen(self) -> None:
        s = _feed_screen("")
        assert s.is_blocked_on_permission_prompt() is None

    def test_returns_none_when_option_1_present_without_confirm_companion(self) -> None:
        # A numbered list ("1. Yes") alone, with no "No"/"esc to cancel"
        # nearby, must not false-positive as a permission dialog.
        s = _feed_screen("Steps:", "1. Yes, run the migration", "2. Verify output")
        assert s.is_blocked_on_permission_prompt() is None

    def test_returns_none_on_tty_shell_prompt(self) -> None:
        # Orthogonal to is_blocked_on_tty_prompt() — a generic shell y/N
        # prompt is not this dialog.
        s = _feed_screen("Overwrite? [y/N]")
        assert s.is_blocked_on_permission_prompt() is None
        assert s.is_blocked_on_tty_prompt() is not None

    def test_real_capture_git_reset_hard_dialog_detected(self) -> None:
        """Every other test in this class feeds clean, pre-stripped synthetic
        lines through the pyte screen — they prove the regex is right but
        never exercise it against a genuine PTY byte stream. This is a
        verbatim live capture (`runtime/sessions/2026-08-15/agent-takkub/
        backend#3-082405.transcript.log`, byte offset 44122) of this very
        session's own pane sitting on the `Bash(git reset --hard:*)`
        permission gate: 24-bit SGR color codes, absolute-column typewriter
        jumps (`\\x1b[8G`), and two frames of the busy-dot spinner that
        repositions the cursor via `\\x1b[36;1H` — all still present, unlike
        the hand-written fixtures above. Notably the raw bytes render
        "❯1. Yes" with **no space** between the pointer glyph and "1." (the
        space in the rendered screen line below comes from pyte padding a
        `\\x1b[4G` absolute-column jump, not from the source bytes) — proving
        `_PERMISSION_MENU_OPTION1_RE`'s `[❯>]?\\s*1\\.` really does need its
        `\\s*` to be zero-width-tolerant, not just in theory."""
        raw = (
            b"Permission rule \x1b[1mBash(git reset --hard:*)\x1b[43G\x1b[22mrequires"
            b"\x1b[52Gconfirmation\x1b[65Gfor\x1b[69Gthis\x1b[74Gcommand.\r\x1b[1C\x1b[1B"
            b"\x1b[38;2;153;153;153m/perm\x1b[8Gssi\x1b[12Gns to update rules\r\x1b[2B"
            b"\x1b[39m Do you want to proceed?\x1b[K\r\x1b[1C\x1b[1B"
            b"\x1b[38;2;177;185;249m\xe2\x9d\xaf\x1b[4G\x1b[38;2;153;153;153m1. "
            b"\x1b[38;2;177;185;249mYes\r\x1b[1B\x1b[39m   \x1b[38;2;153;153;153m2. "
            b"\x1b[39mYes, and don\xe2\x80\x99t ask again for: rtk git *\x1b[K\r\x1b[1B  "
            b"\x1b[4G\x1b[38;2;153;153;153m3. \x1b[39mNo\r\x1b[1B\x1b[K\r\x1b[1C\x1b[1B"
            b"\x1b[38;2;153;153;153mEsc to cancel \xc2\xb7 Tab to amend \xc2\xb7 "
            b"ctrl+e to explain\x1b[39m\x1b[K\x1b[36;1H\x1b[32;2H\x1b[H\r\x1b[6B"
            b"\x1b[38;2;153;153;153m\xe2\x97\x8f\x1b[39m\x1b[36;1H\x1b[32;2H\x1b[H\r\x1b[6B"
            b"\x1b[38;2;153;153;153m \x1b[39m"
        )
        # Production panes render at 110x36 (spawn_engine._PANE_COLS/_PANE_ROWS)
        # — the spinner's absolute row jump (`\x1b[36;1H` = row 36) only makes
        # sense reproduced at that size, not the 80x24 used by every other
        # fixture in this file.
        s = PtySession(cols=110, rows=36)
        s._feed_and_log(raw)
        result = s.is_blocked_on_permission_prompt()
        assert result is not None
        assert "1. yes" in result.lower()
        # Option 2's own wording is "Yes, and don't ask again for: rtk git *"
        # — a SECOND "yes" one line below option 1's. The matched line must
        # still anchor to option 1 specifically, not get confused by it.
        assert "don" not in result.lower()  # would mean it matched option 2's line instead
        assert "rtk git" not in result
        # Orthogonal — the generic TTY-prompt regex (no [y/N] bracket in this
        # dialog) must not also fire on the same real screen.
        assert s.is_blocked_on_tty_prompt() is None


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


# ── boot-phase vs queued-message split (#281) ────────────────────────────────
# `shows_startup_marker()` answers "not a genuine work turn" (idle watchdog).
# `shows_boot_phase_marker()` answers "the composer does not exist yet"
# (delivery / boot-stall / `takkub list`). Conflating them made a WORKING codex
# pane read as a stuck boot — proven from events.log: panes flagged
# `[delivery-boot-stall]` at 110s went on to call `done` minutes later.


def test_boot_phase_marker_true_while_codex_boots_mcp() -> None:
    s = _feed_screen(
        "• Booting MCP server: codex_apps (0s • esc to interrupt)",
        "gpt-5.6 high · ~/project · Fast off",
    )
    assert s.shows_boot_phase_marker() is True


def test_boot_phase_marker_false_for_a_working_pane_with_a_queued_message() -> None:
    """The #281 regression in one assertion: codex shows "tab to queue
    message" the whole time it is working, which is not a boot phase."""
    s = _feed_screen(
        "• Ran Get-Content -Raw -LiteralPath 'spec.md'",
        "esc to interrupt · tab to queue message",
        "gpt-5.6 high · ~/project · Fast off",
    )
    assert s.shows_boot_phase_marker() is False
    # the wider marker still reports True — the idle watchdog must keep
    # suppressing its forgot-`takkub done` nag for this pane.
    assert s.shows_startup_marker() is True


def test_boot_phase_marker_ignores_a_stale_boot_line_in_scrollback() -> None:
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
    assert s.shows_boot_phase_marker() is False


def test_boot_phase_detail_names_the_servers_being_waited_on() -> None:
    """#281: `codex mcp list` cannot see cockpit-injected MCPs, so the pane's
    own boot line is the only thing that names what a stuck boot is waiting
    for."""
    s = _feed_screen(
        "OpenAI Codex (v0.147.0)",
        "Starting MCP servers (0/3): codex_apps, context7, figma (12s • esc to interrupt)",
    )
    detail = s.boot_phase_detail()
    assert "context7" in detail and "figma" in detail
    assert len(detail) <= 200


def test_boot_phase_detail_empty_when_not_booting() -> None:
    s = _feed_screen("gpt-5.6 high · ~/project · Fast off")
    assert s.boot_phase_detail() == ""
