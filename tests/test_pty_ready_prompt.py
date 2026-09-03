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


def test_gemini_account_eligibility_gate_with_trailing_prompt_is_not_ready() -> None:
    """#346: verbatim live-captured screen from the issue (2026-08-22) —
    this exact screen used to read as READY on the (disproven) theory that
    "please try again shortly" means the account check failed and dropped
    back to a normal idle composer. It doesn't: the CLI was genuinely
    frozen, accepting no input, and cockpit blind-delivered a task into it
    that was silently lost. Also asserted NOT a trust/onboarding modal
    (`is_at_trust_prompt`) — this incident must not resurface as
    `blocked:trust-prompt` either, only as the new `blocked:provider-account`
    display state (see test_derive_display_state.py)."""
    s = _feed_screen(
        "Antigravity CLI 1.1.17",
        "monchai500@gmail.com (Google AI Pro)",
        "Gemini 3.7 Flash (High)",
        "~/WebstormProjects/agent-takkub/worktrees/agent-takkub/gemini-1787380071",
        "",
        "⚠ Verifying your account...",
        "  We're finishing verifying your account eligibility.",
        "  This usually takes a moment. Please try again shortly.",
        ">",
    )
    assert s.is_at_ready_prompt() is False
    assert s.is_at_trust_prompt() is False
    assert s.is_blocked_on_tty_prompt() is None


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

    def test_ready_footer_below_stale_modal_rows_wins(self) -> None:
        """#330 regression (2026-09-02→03, saas_admin_amb): agy's Antigravity
        CLI exits the trust modal's alt-screen buffer and erases only from
        the cursor down, so the modal's own rows never get cleared — they
        sit above the real idle footer forever. Verbatim shape replayed from
        the live transcript via pyte: modal text in the top rows, "Antigravity
        CLI ..." banner + empty ">" composer + "? for shortcuts" footer below
        it, all in the SAME screen. A ready footer must win — the pane really
        is idle and already answered its own modal, not stuck on it."""
        s = _feed_screen(
            "Accessing workspace:",
            "",
            "Do you trust the contents of this project?",
            "",
            "Antigravity CLI requires permission to read, edit, and execute files here.",
            "",
            "> Yes, I trust this folder",
            "  No, exit",
            "",
            "  up/down Navigate . enter Confirm",
            "",
            "     Antigravity CLI 1.1.23",
            "     monchai500@gmail.com",
            "     Gemini 3.7 Flash (High)",
            "     ~/WebstormProjects/saas_admin_amb",
            "",
            "-" * 40,
            ">",
            "-" * 40,
            "? for shortcuts",
        )
        assert s.is_at_ready_prompt() is True
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


def test_codex_banner_model_loading_is_a_boot_phase(monkeypatch) -> None:
    """#380: codex 0.149 draws the composer + `? for shortcuts` footer while
    its banner still says `model: loading` — every ready rule matches, so
    the pane read READY and the task was typed into a TUI still booting.
    The banner line sits above the composer box, so it is only visible in
    delivery's taller window (`_BOOT_MARKER_TAIL_ROWS`), like #284's case."""
    s = _feed_screen(
        "╭──────────────────────────────────────────────╮",
        "│ >_ OpenAI Codex (v0.149.1)                   │",
        "│                                              │",
        "│ model:       loading   /model to change      │",
        "│ directory:   loading                         │",
        "│ permissions: YOLO mode                       │",
        "╰──────────────────────────────────────────────╯",
        "",
        "",
        "› Ask Codex to do anything",
        "",
        "  ? for shortcuts",
    )
    from agent_takkub.pty_session import _BOOT_MARKER_TAIL_ROWS

    assert s.shows_boot_phase_marker(rows=_BOOT_MARKER_TAIL_ROWS) is True
    assert "loading" in s.boot_phase_detail()
    # the same screen once codex finished loading is NOT a boot phase
    s2 = _feed_screen(
        "│ model:       gpt-5.6-terra   /model to change │",
        "│ directory:   ~/WebstormProjects/saas_admin    │",
        "│ permissions: YOLO mode                        │",
        "╰──────────────────────────────────────────────╯",
        "",
        "› Ask Codex to do anything",
        "",
        "  ? for shortcuts",
    )
    assert s2.shows_boot_phase_marker(rows=_BOOT_MARKER_TAIL_ROWS) is False
    assert s2.boot_phase_detail() == ""


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


# ── boot marker must survive a tall composer (#284) ──────────────────────────
# `_READY_TAIL_ROWS` (6) is sized for claude's one-line footer. codex draws a
# bordered composer + status bar, so its boot line sits higher and drops out of
# a 6-row window the moment the composer grows — leaving only "Fast off" and a
# READY verdict for a pane that is visibly still starting. That is the cockpit
# sending the task too early.


def _codex_booting_screen(composer_rows: int) -> list[str]:
    return [
        "Tip: Try the Desktop app.",
        "",
        "- Booting MCP server: codex_apps (0s - esc to interrupt)",
        "",
        "composer-top",
        *[f"> line{i}" for i in range(composer_rows)],
        "composer-bottom",
        "gpt-5.6-terra medium - weekly 40% left - Fast off",
    ]


def test_boot_marker_survives_a_composer_tall_enough_to_hide_it() -> None:
    """The exact regression: with a taller composer the ready window no longer
    contains the boot line, so `is_at_ready_prompt()` says True. Delivery's
    widened boot probe must still say "booting" — that is what stops the task
    being pasted into a pane that has not started."""
    from agent_takkub.pty_session import _BOOT_MARKER_TAIL_ROWS

    s = _feed_screen(*_codex_booting_screen(composer_rows=2))
    assert s.is_at_ready_prompt() is True, "precondition: the ready window has lost the boot line"
    assert s.shows_boot_phase_marker() is False, "precondition: the tight window loses it too"
    assert s.shows_boot_phase_marker(rows=_BOOT_MARKER_TAIL_ROWS) is True, (
        "delivery must still see the boot line the ready window dropped"
    )


def test_short_composer_case_still_reads_booting() -> None:
    s = _feed_screen(*_codex_booting_screen(composer_rows=1))
    assert s.shows_boot_phase_marker() is True


def test_widened_window_does_not_pin_a_stale_boot_line_for_delivery_forever() -> None:
    """The widened window is bounded, not unlimited — a boot line that has
    scrolled well up the conversation must eventually stop counting."""
    from agent_takkub.pty_session import _BOOT_MARKER_TAIL_ROWS

    s = _feed_screen(
        "- Booting MCP server: codex_apps (0s - esc to interrupt)",
        *[f"- later output line {i}" for i in range(_BOOT_MARKER_TAIL_ROWS + 2)],
        "gpt-5.6-terra medium - weekly 40% left - Fast off",
    )
    assert s.shows_boot_phase_marker(rows=_BOOT_MARKER_TAIL_ROWS) is False


def test_finished_boot_is_not_reported_as_booting() -> None:
    """The gate must not become a permanent block — once the boot line is gone
    the pane delivers normally."""
    s = _feed_screen(
        "- Ran Get-Content -Raw spec.md",
        "",
        "composer-top",
        "> line0",
        "> line1",
        "composer-bottom",
        "gpt-5.6-terra medium - weekly 40% left - Fast off",
    )
    assert s.is_at_ready_prompt() is True
    assert s.shows_boot_phase_marker() is False


def test_claude_idle_footer_with_background_task_is_ready() -> None:
    """2026-08-25 (#343 false alarm on real Lead panes): Claude Code's idle
    footer shows `· esc to interrupt · ← for agents` while a background
    agent/shell runs. That segment is on the footer line itself, not a
    spinner line — the composer is idle. Was classified busy for the whole
    life of the background task (stale-marker notices every cooldown,
    delivery waiting on a ready pane, compact episodes cut short)."""
    s = _feed_screen(
        "● ผมไม่ได้ค้างครับ — แค่รออยู่เฉยๆ",
        "",
        "─" * 40,
        "❯ ทำ ui เหลี่ยมๆ แบบ A ต่อเลย",
        "─" * 40,
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents",
        "  ⧉  night-report                                 ← for agents",
    )
    assert s.is_at_ready_prompt() is True


def test_claude_spinner_above_background_footer_is_still_busy() -> None:
    # (no blank row: the 80-col test screen wraps the footer onto a second
    # row, and the spinner must stay inside the 6-row ready window)
    s = _feed_screen(
        "✻ Churning… (esc to interrupt)",
        "─" * 40,
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents",
    )
    assert s.is_at_ready_prompt() is False


def test_claude_wash_locker_footer_with_shell_count_is_ready() -> None:
    """Second real footer shape from the same day (events.log 08:25:20):
    `1 shell · esc to interrupt`."""
    s = _feed_screen(
        "─" * 40,
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on · 1 shell · esc to interrupt · ← for agents · ↓ to manage",
    )
    assert s.is_at_ready_prompt() is True


# ── #391 (2026-08-26 live finding): "esc to interrupt" alone on the footer
# is busy, not background work ───────────────────────────────────────────────
# d047ab4's first attempt assumed "esc to interrupt" on the footer line ALWAYS
# meant background work. Real evidence from 3 actively-working panes
# (idle_reminder firing every ~90s, harvest_hint at +10min, `takkub status`
# showing fresh progress the whole time) disproved that: the currently-
# shipping Claude Code build renders "esc to interrupt" on that same footer
# line even with NO background task, and its spinner line no longer carries
# its own "(esc to interrupt)" suffix at all. Fixtures below are the 3 real
# shapes: busy (no background task), idle (no background task), idle+background.


def test_claude_busy_footer_without_background_segment_is_busy() -> None:
    """The exact regression: footer merges 'esc to interrupt' onto the
    'bypass permissions' line with NO 'for agents'/'N shell' evidence — this
    is ordinary busy, not a background task, and must NOT read ready."""
    s = _feed_screen(
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt",
    )
    assert s.is_at_ready_prompt() is False
    assert s.has_background_work() is False


def test_claude_bare_spinner_line_with_no_esc_suffix_is_busy() -> None:
    """The build's spinner line no longer carries '(esc to interrupt)' at
    all — captured shape is bare 'sock-hopping… 3'. With a completely plain
    idle footer underneath (no 'esc to interrupt' anywhere), the spinner
    line alone must still be recognised as busy."""
    s = _feed_screen(
        "✻ Sock-hopping… 3",
        "─" * 40,
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    )
    assert s.is_at_ready_prompt() is False


def test_claude_bare_spinner_above_background_footer_is_still_busy() -> None:
    """Busy AND background task at once: the footer's 'esc to interrupt' is
    neutralized (background evidence present), but the bare spinner line
    (no suffix) must still keep this classified busy."""
    s = _feed_screen(
        "✻ Sock-hopping… 3",
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents",
    )
    assert s.is_at_ready_prompt() is False


def test_claude_plain_idle_no_background_no_spinner_is_ready() -> None:
    """Third shape: genuinely idle, no background task, no spinner at all."""
    s = _feed_screen(
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    )
    assert s.is_at_ready_prompt() is True
    assert s.has_background_work() is False


@pytest.mark.parametrize(
    "verb",
    ("thinking", "churning", "bunning", "sock-hopping", "wibbling"),
)
def test_claude_busy_spinner_shape_is_verb_agnostic(verb: str) -> None:
    """#391: structural shape (glyph + word + ellipsis), not a fixed verb
    list — an upstream vocabulary change must not silently break this."""
    s = _feed_screen(
        f"✻ {verb.capitalize()}… 3",
        "─" * 40,
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    )
    assert s.is_at_ready_prompt() is False


def test_claude_background_footer_wrapped_across_80col_rows_is_still_ready() -> None:
    """An 80-col pane wraps the real footer string mid-word — pyte's own
    display() breaks it exactly at "for agent" / "s" onto the next row
    (confirmed directly against PtySession(cols=80)). The background-segment
    evidence check must still recognise "for agents" once its two halves are
    reunited, not miss it because a newline now sits between them."""
    s = _feed_screen(
        "─" * 40,
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents",
    )
    assert s.is_at_ready_prompt() is True
    assert s.has_background_work() is True


# ── has_background_work() (#391/#394/#395/#398) ─────────────────────────────
# d047ab4 correctly stopped the background-task footer segment from reading
# BUSY (is_at_ready_prompt() True above) but that alone can't tell "genuinely
# done" apart from "ready for input AND still babysitting a background
# docker build / vitest --watch / pio run" — has_background_work() is the
# finer signal the idle watchdog needs for that split.


def test_has_background_work_true_for_shell_count_footer_segment() -> None:
    s = _feed_screen(
        "─" * 40,
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on · 1 shell · esc to interrupt · ← for agents · ↓ to manage",
    )
    assert s.is_at_ready_prompt() is True
    assert s.has_background_work() is True


def test_has_background_work_false_for_plain_idle_footer() -> None:
    """No background segment on the footer line at all — ordinary idle."""
    s = _feed_screen(
        "─" * 40,
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    )
    assert s.is_at_ready_prompt() is True
    assert s.has_background_work() is False


def test_has_background_work_false_when_esc_to_interrupt_is_the_spinner_line() -> None:
    """The spinner's own 'esc to interrupt' (busy, not background-task) must
    NOT be mistaken for the footer-line background segment — has_background_work
    only looks at the footer chrome line, same scope as _blocker_scan_text."""
    s = _feed_screen(
        "✻ Churning… (esc to interrupt)",
        "─" * 40,
        "❯ ",
        "─" * 40,
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    )
    assert s.has_background_work() is False


def test_has_background_work_false_for_non_claude_provider_shapes() -> None:
    """No other provider renders claude's 'bypass permissions'/'shift+tab to
    cycle' footer, so the marker never matches — always False elsewhere."""
    codex = _feed_screen("gpt-5.5 medium · ~/project · 5h 79% left · Fast off")
    assert codex.has_background_work() is False
    gemini = _feed_screen("Type your message or @path/to/file")
    assert gemini.has_background_work() is False


class TestTrustPromptSelectsNo:
    _NO_DEFAULT = (
        "Quick safety check: Is this a project you created or one you trust?",
        "⚠ This folder pre-approves 4 tool permissions in .claude/settings.local.json:",
        "",
        "❯ No, exit",
        "  Yes, I trust this folder",
        "",
        "Enter to confirm · Esc to cancel",
    )
    _YES_DEFAULT = (
        "Do you trust the files in this folder?",
        "❯ 1. Yes, I trust this folder",
        "  2. No, exit",
        "Enter to confirm · Esc to cancel",
    )

    def _session(self, lines):
        from agent_takkub.pty_session import PtySession

        s = PtySession.__new__(PtySession)
        s.display_lines = lambda: list(lines)  # type: ignore[method-assign]
        return s

    def test_cursor_on_no_detected(self) -> None:
        s = self._session(self._NO_DEFAULT)
        assert s.is_at_trust_prompt()
        assert s.trust_prompt_selects_no()

    def test_cursor_on_yes_not_flagged(self) -> None:
        s = self._session(self._YES_DEFAULT)
        assert s.is_at_trust_prompt()
        assert not s.trust_prompt_selects_no()

    def test_prose_mentioning_no_does_not_match(self) -> None:
        s = self._session(["> Nothing to see here", "❯ Note: retry"])
        assert not s.trust_prompt_selects_no()

    # #457: claude's "Bypass Permissions mode" one-time disclaimer — shown
    # the first time a given CLAUDE_CONFIG_DIR profile launches with
    # --dangerously-skip-permissions (every cockpit pane spawn passes it).
    # A brand-new sandbox profile hit this before ever reaching the
    # trust-directory modal above; with nothing recognizing it, headless had
    # no way to answer it and the pane hung forever at boot.
    _BYPASS_PERMISSIONS_NO_DEFAULT = (
        "WARNING: Claude Code running in Bypass Permissions mode",
        "By proceeding, you accept all responsibility for actions taken while",
        "running in Bypass Permissions mode.",
        "❯ 1. No, exit",
        "  2. Yes, I accept",
        "Enter to confirm · Esc to cancel",
    )
    _BYPASS_PERMISSIONS_YES_SELECTED = (
        "WARNING: Claude Code running in Bypass Permissions mode",
        "❯ 1. Yes, I accept",
        "  2. No, exit",
        "Enter to confirm · Esc to cancel",
    )

    def test_bypass_permissions_dialog_cursor_on_no_detected(self) -> None:
        s = self._session(self._BYPASS_PERMISSIONS_NO_DEFAULT)
        assert s.is_at_trust_prompt()
        assert s.trust_prompt_selects_no()

    def test_bypass_permissions_dialog_cursor_on_yes_not_flagged(self) -> None:
        s = self._session(self._BYPASS_PERMISSIONS_YES_SELECTED)
        assert s.is_at_trust_prompt()
        assert not s.trust_prompt_selects_no()
