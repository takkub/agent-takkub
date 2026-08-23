"""Tests for `PtySession.auth_failure_reason` and its marker tables
(#248/#247 round 2 + the round-2 follow-up that narrowed
`GENERIC_AUTH_ERROR_MARKERS` and gated `_send_when_ready`'s use of it behind
a multi-poll confirmation — see test_delivery_auth_failure.py for the
`_check` integration side of that follow-up)."""

from __future__ import annotations

from agent_takkub.provider_spec import (
    AUTH_TRANSIENT_GRACE_SEC,
    GENERIC_AUTH_ERROR_MARKERS,
    is_ready_marker_calibrated,
    uncalibrated_providers,
)
from agent_takkub.pty_session import _BOOT_MARKER_TAIL_ROWS, _READY_TAIL_ROWS, PtySession


class _FakeScreen:
    """Minimal stand-in for PtySession: `auth_failure_reason` only touches
    `display_lines()` and `seconds_since_output()`, so a real PtySession
    (with its ConPTY/subprocess construction) is unnecessary here."""

    def __init__(self, lines: list[str], seconds_since_output: float = 100.0) -> None:
        self._lines = lines
        self._seconds_since_output = seconds_since_output

    def display_lines(self) -> list[str]:
        return self._lines

    def seconds_since_output(self) -> float:
        return self._seconds_since_output


def _auth_failure_reason(lines: list[str], provider: str, seconds_since_output: float = 100.0):
    return PtySession.auth_failure_reason(
        _FakeScreen(lines, seconds_since_output=seconds_since_output), provider
    )


class TestInstantMarkers:
    def test_instant_marker_matches_with_zero_grace(self) -> None:
        lines = ["", "please sign in again to continue", ""]
        assert _auth_failure_reason(lines, "claude", seconds_since_output=0.0) == (
            "please sign in again"
        )

    def test_no_marker_returns_none(self) -> None:
        lines = ["> ", "welcome back", "type your message"]
        assert _auth_failure_reason(lines, "claude") is None

    def test_marker_outside_ready_region_does_not_match(self) -> None:
        # _READY_TAIL_ROWS (6) non-blank rows from the bottom are scoped; a
        # marker further up the scrollback must not poison the verdict, same
        # reasoning as `_classify_ready`.
        lines = ["please log in again"] + [f"line {i}" for i in range(_READY_TAIL_ROWS + 3)]
        assert len(lines) - _READY_TAIL_ROWS > 1  # sanity: marker really is out of window
        assert _auth_failure_reason(lines, "claude") is None

    def test_marker_inside_ready_region_matches(self) -> None:
        lines = [f"line {i}" for i in range(_READY_TAIL_ROWS + 3)] + ["please log in again"]
        assert _auth_failure_reason(lines, "claude") == "please log in again"


class TestTransientMarkers:
    def test_transient_marker_does_not_fire_before_grace_elapsed(self) -> None:
        lines = ["", "signing in...", ""]
        reason = _auth_failure_reason(
            lines, "gemini", seconds_since_output=AUTH_TRANSIENT_GRACE_SEC - 1
        )
        assert reason is None

    def test_transient_marker_fires_once_grace_elapsed_and_screen_static(self) -> None:
        lines = ["", "signing in...", ""]
        reason = _auth_failure_reason(
            lines, "gemini", seconds_since_output=AUTH_TRANSIENT_GRACE_SEC
        )
        assert reason == "signing in"

    def test_transient_marker_never_fires_for_a_provider_with_none_confirmed(self) -> None:
        lines = ["", "signing in...", ""]
        # "signing in" is only a confirmed transient marker for gemini; for a
        # provider with no confirmed transient list it must never fire, no
        # matter how long the screen has been static.
        reason = _auth_failure_reason(lines, "claude", seconds_since_output=10_000)
        assert reason is None


def _account_pending_reason(lines: list[str], provider: str, seconds_since_output: float = 100.0):
    return PtySession.account_pending_reason(
        _FakeScreen(lines, seconds_since_output=seconds_since_output), provider
    )


class TestAccountPendingReason:
    """`PtySession.account_pending_reason` (#346) — distinct from
    `auth_failure_reason`: this is a provider-side account/eligibility gate
    (e.g. Google account verification for gemini/agy), not a login/
    credentials problem, so it must never be reported with "log back in"
    wording. Verbatim live-captured incident: agy froze on "Verifying your
    account... We're finishing verifying your account eligibility... Please
    try again shortly." indefinitely, while a since-removed ready-marker
    rule misread that exact text as an idle composer (see
    test_pty_ready_prompt.py's regression test for that half of the fix)."""

    def test_does_not_fire_during_normal_boot(self) -> None:
        lines = ["", "Verifying your account...", ""]
        reason = _account_pending_reason(
            lines, "gemini", seconds_since_output=AUTH_TRANSIENT_GRACE_SEC - 1
        )
        assert reason is None

    def test_fires_once_grace_elapsed_and_screen_static(self) -> None:
        lines = ["", "Verifying your account...", ""]
        reason = _account_pending_reason(
            lines, "gemini", seconds_since_output=AUTH_TRANSIENT_GRACE_SEC
        )
        assert reason == "verifying your account"

    def test_fires_for_the_full_live_captured_banner(self) -> None:
        lines = [
            "⚠ Verifying your account...",
            "  We're finishing verifying your account eligibility.",
            "  This usually takes a moment. Please try again shortly.",
            ">",
        ]
        reason = _account_pending_reason(
            lines, "gemini", seconds_since_output=AUTH_TRANSIENT_GRACE_SEC
        )
        assert reason == "verifying your account"

    def test_never_fires_for_a_provider_with_none_confirmed(self) -> None:
        lines = ["", "Verifying your account...", ""]
        reason = _account_pending_reason(lines, "claude", seconds_since_output=10_000)
        assert reason is None

    def test_fires_when_a_realistic_footer_pushes_the_banner_past_ready_tail_rows(self) -> None:
        # #363 regression: the original #346 fix scanned `_ready_region`
        # (_READY_TAIL_ROWS=6 rows). The live-captured screen that shipped
        # with #346 (test_fires_for_the_full_live_captured_banner above) had
        # nothing below the banner but a bare ">" prompt, so it always fit.
        # A real agy composer renders more chrome below the banner — a
        # bordered input box plus its own status/hint row — which pushes the
        # banner entirely out of a 6-row window while leaving just enough of
        # that chrome inside it for `is_at_ready_prompt()` to misread READY.
        # Proven structurally: this exact screen used to return None here
        # (banner outside `_ready_region`) even though the pane was plainly
        # still frozen on the banner one line further up.
        lines = [
            "⚠ Verifying your account...",
            "  We're finishing verifying your account eligibility.",
            "  This usually takes a moment. Please try again shortly.",
            "",
            "─" * 40,
            "> ",
            "─" * 40,
            "ctx: 12% used  |  tips: ctrl+c to exit",
            "? for shortcuts            Gemini 3.7 Flash (High)",
        ]
        assert len(lines) - _READY_TAIL_ROWS > 1  # sanity: banner really is out of the tight window
        assert len(lines) - _BOOT_MARKER_TAIL_ROWS <= 0  # sanity: still inside the wider window
        reason = _account_pending_reason(
            lines, "gemini", seconds_since_output=AUTH_TRANSIENT_GRACE_SEC
        )
        assert reason == "verifying your account"

    def test_fires_for_the_issue_363_quoted_capture_with_realistic_footer(self) -> None:
        # #363's own report quotes the live banner verbatim (own wording, "└"
        # continuation glyph, wrapped onto one description line rather than
        # #346's two) — the raw transcript file the task pointed at
        # (runtime/sessions/2026-08-23/agent-takkub/gemini-142209.transcript.log)
        # had already rotated out of runtime/sessions by the time of this fix,
        # so this fixture is built from the issue body's own quoted capture
        # instead of re-reading that file.
        lines = [
            "⚠ Verifying your account...",
            "  └ We're finishing verifying your account eligibility. This usually "
            "takes a moment. Please try again shortly.",
            "",
            "─" * 40,
            "> ",
            "─" * 40,
            "? for shortcuts            Gemini 3.7 Flash (High)",
        ]
        reason = _account_pending_reason(
            lines, "gemini", seconds_since_output=AUTH_TRANSIENT_GRACE_SEC
        )
        assert reason == "verifying your account"

    def test_is_not_reported_as_an_auth_failure_reason(self) -> None:
        # #346: moved OUT of gemini's auth_transient_markers entirely — the
        # two tiers are now disjoint, so auth_failure_reason() must not
        # ALSO match this text (that would put "log back in" wording in
        # front of a Lead who cannot log anything in).
        lines = ["", "Verifying your account...", ""]
        reason = _auth_failure_reason(
            lines, "gemini", seconds_since_output=AUTH_TRANSIENT_GRACE_SEC
        )
        assert reason is None


class TestNarrowedGenericMarkers:
    """The round-2 follow-up dropped several phrases that are ordinary
    HTTP/test-framework vocabulary, not CLI chrome — a backend pane running
    its own auth-feature test suite would otherwise trip these on unrelated
    output. Missing a real failure is acceptable; convicting normal dev
    output is not."""

    def test_ambiguous_dev_output_phrases_were_removed(self) -> None:
        removed = (
            "unauthorized",
            "invalid credentials",
            "invalid api key",
            "session expired",
            "login required",
            "authentication required",
            "authentication failed",
            "not authenticated",
        )
        for phrase in removed:
            assert phrase not in GENERIC_AUTH_ERROR_MARKERS, phrase

    def test_fastapi_default_401_body_no_longer_false_positives(self) -> None:
        # FastAPI's own default 401 `detail` is the literal string "Not
        # authenticated" — a backend pane's own test suite printing this
        # about ITS project's auth feature must not convict the pane itself.
        lines = ["FAILED tests/test_auth.py::test_401 - assert 'Not authenticated' in body"]
        assert _auth_failure_reason(lines, "claude") is None

    def test_remaining_markers_still_read_as_first_person_cli_chrome(self) -> None:
        for marker in GENERIC_AUTH_ERROR_MARKERS:
            assert "sign" in marker or "log in" in marker or "authenticate" in marker, marker


class TestGeminiColdBootNotSignedIn:
    """#256: agy's own cold-start banner ('You are currently not signed in.')
    used to be an instant, zero-grace failure via GENERIC_AUTH_ERROR_MARKERS
    — every single agy spawn tripped it, well before the CLI had even begun
    signing in, let alone failed to. Moved to gemini_spec's own
    auth_transient_markers (grace-gated), same tier as its existing
    'signing in' entry. ('verifying your account' used to also live in this
    tuple but moved OUT to its own account_pending_markers tier in #346 —
    see TestAccountPendingReason below — because it is not a login problem
    at all, unlike the two entries that remain here.)"""

    def test_not_signed_in_is_no_longer_a_generic_instant_marker(self) -> None:
        assert "not signed in" not in GENERIC_AUTH_ERROR_MARKERS

    def test_not_signed_in_does_not_fire_during_a_normal_boot(self) -> None:
        # Banner is on screen but the pane just spawned (low
        # seconds_since_output) — must not convict a normal cold boot.
        lines = ["", "You are currently not signed in.", ""]
        reason = _auth_failure_reason(lines, "gemini", seconds_since_output=0.5)
        assert reason is None

    def test_not_signed_in_fires_once_stuck_past_grace(self) -> None:
        # If agy genuinely never gets past this banner, it is exactly as
        # legitimate a stall as 'signing in' hanging forever (#247) — same
        # grace-gated tier, same eventual conviction.
        lines = ["", "You are currently not signed in.", ""]
        reason = _auth_failure_reason(
            lines, "gemini", seconds_since_output=AUTH_TRANSIENT_GRACE_SEC
        )
        assert reason == "not signed in"

    def test_not_signed_in_never_fires_for_a_provider_with_none_confirmed(self) -> None:
        # Removed from the generic table entirely (see the class above) —
        # unlike the old behavior, no other provider gets this marker at
        # all, instant or transient, until it confirms its own.
        lines = ["", "You are currently not signed in.", ""]
        reason = _auth_failure_reason(lines, "claude", seconds_since_output=10_000)
        assert reason is None

    def test_banner_scrolling_out_of_the_ready_region_overrides_it(self) -> None:
        # #256 point 2: once real output (a signed-in identity header, then
        # normal task output) has pushed the banner out of the bottom
        # _READY_TAIL_ROWS window, the marker simply cannot match anymore —
        # the same tail-scoping that protects every other check in this
        # module doubles as the "a newer identity overrides the stale
        # marker" mechanism, with no separate identity parsing needed.
        lines = (
            ["You are currently not signed in.", "Signing in..."]
            + [f"assistant output line {i}" for i in range(_READY_TAIL_ROWS)]
            + ["? for shortcuts            Gemini 3.1 Pro (High)"]
        )
        reason = _auth_failure_reason(lines, "gemini", seconds_since_output=1_000.0)
        assert reason is None


class TestKimiNotLoggedIn:
    """#257: a fresh kimi pane spawned with no credentials shows "Model: not
    set, send /login to login" instead of ever reaching the idle footer —
    unlike gemini's transient boot banner, this is a genuine dead end (no
    model selected, nothing will ever run), so it belongs at the instant-fail
    tier as kimi's own confirmed `auth_error_markers` entry."""

    def test_send_login_to_login_is_an_instant_marker_for_kimi(self) -> None:
        lines = ["", "Model: not set, send /login to login", ""]
        assert _auth_failure_reason(lines, "kimi", seconds_since_output=0.0) == (
            "send /login to login"
        )

    def test_send_login_to_login_is_not_a_generic_marker(self) -> None:
        # Confirmed only for kimi's own exact wording — must not leak into
        # every other provider's baseline the way the #256 follow-up
        # explicitly avoided doing for "not signed in".
        assert "send /login to login" not in GENERIC_AUTH_ERROR_MARKERS
        lines = ["", "Model: not set, send /login to login", ""]
        assert _auth_failure_reason(lines, "claude", seconds_since_output=0.0) is None


class TestIsHardBlockedFor:
    """`PtySession.is_hard_blocked_for` (#263) — same hard-blocker matching
    `_classify_ready_for_provider` already runs for `ready_marker_selftest`,
    exposed as its own query so a caller (`Orchestrator._derive_display_state`)
    can ask "is this provider's screen showing an active interrupt/generation
    indicator right now" independent of the ready/not-ready verdict."""

    def _is_hard_blocked(self, lines: list[str], provider: str) -> bool:
        return PtySession.is_hard_blocked_for(_FakeScreen(lines), provider)

    def test_codex_esc_to_interrupt_is_hard_blocked(self) -> None:
        lines = ["gpt-5.5 medium", "Working (0s - esc to interrupt)"]
        assert self._is_hard_blocked(lines, "codex") is True

    def test_codex_idle_composer_is_not_hard_blocked(self) -> None:
        lines = ["gpt-5.5 medium · ~/project · weekly 86% left · Fast on"]
        assert self._is_hard_blocked(lines, "codex") is False

    def test_unknown_provider_is_never_hard_blocked(self) -> None:
        lines = ["esc to interrupt"]
        assert self._is_hard_blocked(lines, "not-a-real-provider") is False

    def test_provider_with_no_hard_blockers_never_matches(self) -> None:
        # cursor_spec has no calibrated ready_hard_blockers at all yet.
        lines = ["esc to interrupt"]
        assert self._is_hard_blocked(lines, "cursor") is False

    def test_marker_outside_ready_region_does_not_match(self) -> None:
        lines = ["esc to interrupt"] + [f"line {i}" for i in range(_READY_TAIL_ROWS + 3)]
        assert self._is_hard_blocked(lines, "codex") is False

    def test_verifying_your_account_is_hard_blocked_even_with_try_again_shortly(self) -> None:
        # CORRECTED (#346): this used to assert False via a carve-out that
        # assumed "please try again shortly" meant the check had already
        # failed and dropped back to a normal prompt. A live incident proved
        # that wrong — the CLI can show exactly this text while genuinely
        # frozen. The carve-out was removed from both this method and
        # _classify_ready_for_provider; both must agree it's hard-blocked.
        lines = ["verifying your account", "please try again shortly"]
        assert self._is_hard_blocked(lines, "gemini") is True


class TestReadyMarkerCalibrationStatus:
    """#257 point 3: a provider whose ready_rules is empty can never satisfy
    is_at_ready_prompt(), so delivery silently stalls — this predicate lets a
    future spawn-time caller warn Lead instead of staying silent. Wiring an
    actual spawn-time warning is out of scope here (spawn_engine.py /
    lead_inbox.py); only the data-layer signal is added by this change."""

    def test_kimi_is_now_calibrated(self) -> None:
        # The #257 fix: kimi_spec.ready_rules went from () to a real entry.
        assert is_ready_marker_calibrated("kimi") is True
        assert "kimi" not in uncalibrated_providers()

    def test_cursor_is_still_uncalibrated(self) -> None:
        # cursor_spec.ready_rules is still () — no TUI has been observed yet
        # (unrelated to this change; documents the predicate's current truth
        # so a future cursor calibration flips this test, not silently).
        assert is_ready_marker_calibrated("cursor") is False
        assert "cursor" in uncalibrated_providers()

    def test_unknown_provider_reads_as_uncalibrated(self) -> None:
        assert is_ready_marker_calibrated("not-a-real-provider") is False
