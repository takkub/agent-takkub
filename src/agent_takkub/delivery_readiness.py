"""Delivery-readiness predicate (#376).

Pure — no Qt, no orchestrator, no PtySession import. Mirrors
`lead_draft_state.py`'s split for the Lead-inbox injection gate: the
decision logic lives here on plain values, `lead_inbox.py` reads the live
pane/session and feeds them in.
"""

from __future__ import annotations


def can_accept_input(
    *, is_ready: bool, account_pending: bool, prompt_blocked: bool = False
) -> bool:
    """True only when a task is safe to submit into a pane RIGHT NOW.

    Composes the signals `lead_inbox._send_when_ready`'s poll loop must
    check together before advancing its ready-streak toward a submit:

      1. *is_ready* — `PtySession.is_at_ready_prompt()`: the CLI's own
         footer reads idle.
      2. NOT *account_pending* — `PtySession.shows_account_pending_marker()`,
         deliberately UNGATED (see its docstring) — unlike
         `account_pending_reason()`, which answers a different question
         ("has this been stuck long enough to escalate") and must stay
         gated for that.
      3. NOT *prompt_blocked* — `lead_inbox._prompt_block_reason()`,
         likewise ungated: a trust/permission/tty modal can sit directly
         over a footer that still reads idle (#186), so this must be
         re-checked every poll, not just once to fire the Lead warning.

    #346/#363/#376 were each a variant of the same bug shape: a call site
    trusted `is_at_ready_prompt()` alone, or read a grace-gated signal for a
    question that has no grace period, and pasted a task onto a screen that
    only *looked* idle. This is the single place that composes these facts,
    so a pane frozen on its provider's own account-pending banner — or
    still sitting on an interactive prompt — never reads as submit-safe no
    matter how briefly the banner/prompt has been up."""
    return bool(is_ready) and not bool(account_pending) and not bool(prompt_blocked)
