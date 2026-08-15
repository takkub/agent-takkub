"""#248/#247: `Orchestrator._pane_display_state` refines the raw
`pane.state == "active"` value into spawning/active/ready for
`takkub list`/`status`, using the content-fingerprint signal from
pty_session.py (see test_output_content_fingerprint.py for that layer).

`_pane_display_state` doesn't touch `self` at all, so these tests call it
unbound (`Orchestrator._pane_display_state(None, pane)`) instead of paying
for a full Orchestrator()/QApplication instantiation.
"""

from __future__ import annotations

import types

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.pty_session import PtySession


def _pane(state: str, session: object | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(state=state, session=session)


def test_non_active_states_pass_through_unchanged() -> None:
    for state in ("working", "done", "empty"):
        assert Orchestrator._pane_display_state(None, _pane(state)) == state


def test_active_with_no_session_is_spawning() -> None:
    assert Orchestrator._pane_display_state(None, _pane("active", session=None)) == "spawning"


def test_active_session_with_no_content_yet_is_spawning() -> None:
    """Only a terminal-init escape sequence has been fed so far — the CLI
    process is up but hasn't rendered anything, indistinguishable from a
    hung spawn without this."""
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(b"\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001h\x1b[?2004h")
    assert Orchestrator._pane_display_state(None, _pane("active", session=s)) == "spawning"


def test_active_session_with_content_not_ready_is_active() -> None:
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(b"(esc to interrupt) working...")
    assert Orchestrator._pane_display_state(None, _pane("active", session=s)) == "active"


def test_active_session_at_ready_prompt_is_ready() -> None:
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(b"bypass permissions")
    assert Orchestrator._pane_display_state(None, _pane("active", session=s)) == "ready"


def test_session_missing_accessors_fails_open_to_active() -> None:
    """A stand-in session object without first_content_ts()/
    is_at_ready_prompt_cached() (e.g. a loose test double elsewhere in the
    suite) must not raise — falls back to the pre-#248 "active" label."""

    class _BareSession:
        pass

    assert (
        Orchestrator._pane_display_state(None, _pane("active", session=_BareSession())) == "active"
    )
