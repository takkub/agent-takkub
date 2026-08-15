"""Tests for `PtySession.auth_failure_reason` and its marker tables
(#248/#247 round 2 + the round-2 follow-up that narrowed
`GENERIC_AUTH_ERROR_MARKERS` and gated `_send_when_ready`'s use of it behind
a multi-poll confirmation — see test_delivery_auth_failure.py for the
`_check` integration side of that follow-up)."""

from __future__ import annotations

from agent_takkub.provider_spec import (
    AUTH_TRANSIENT_GRACE_SEC,
    GENERIC_AUTH_ERROR_MARKERS,
)
from agent_takkub.pty_session import _READY_TAIL_ROWS, PtySession


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
        lines = ["not signed in"] + [f"line {i}" for i in range(_READY_TAIL_ROWS + 3)]
        assert len(lines) - _READY_TAIL_ROWS > 1  # sanity: marker really is out of window
        assert _auth_failure_reason(lines, "claude") is None

    def test_marker_inside_ready_region_matches(self) -> None:
        lines = [f"line {i}" for i in range(_READY_TAIL_ROWS + 3)] + ["not signed in"]
        assert _auth_failure_reason(lines, "claude") == "not signed in"


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
