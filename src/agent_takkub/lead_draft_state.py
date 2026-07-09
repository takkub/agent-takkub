"""Lead draft-typing state — tracks whether the Lead pane's own input line
currently holds unsubmitted user text, so injected engine messages (done
notices, peer CCs, auto-bridged slash commands) never paste over a draft the
user hasn't submitted yet (issue #3, 2026-07-09 core-upgrade plan).

Pure byte-level state machine — no Qt, no orchestrator, no per-project
bookkeeping. `lead_inbox.py` keeps one `LeadDraftState` per project namespace
and feeds it every byte the Lead pane's terminal emits via
`advance_draft_state()`; `draft_state_allows_injection()` is the read side the
delivery paths gate on.

States:
  * "empty"            — input line is (as far as we can tell) empty.
  * "nonempty"          — input line holds `draft_len` characters we counted
    byte/char-for-char (typed text); a matching number of backspaces returns
    to "empty".
  * "unknown_nonempty"  — conservative hold. Entered on Up/Down (history
    recall can populate the line without emitting a single printable byte)
    or the start of a bracketed paste (pasted length is unknowable from the
    byte stream alone). Only clears on an explicit submit/cancel signal
    (Enter, Esc, Ctrl+C, Ctrl+U) — further typing/backspaces while in this
    state don't move it to "nonempty"/"empty" since the true length was
    never known.

Left/Right arrows and Ctrl+A/Ctrl+E are pure cursor movement and never change
the state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EMPTY = "empty"
NONEMPTY = "nonempty"
UNKNOWN_NONEMPTY = "unknown_nonempty"

# How long a pane may sit "held" (nonempty/unknown_nonempty) before a caller
# gives up waiting on `draft_hold_expired()` and spills to a durable queue
# instead of holding an injection indefinitely.
DRAFT_HOLD_TIMEOUT_S = 180.0

# General CSI/SS3 stripper: matches a whole escape sequence as one token so
# its parameter bytes (digits, ';') are never mis-counted as printable
# keystrokes. Covers arrows, Home/End/Delete/PageUp/Down, and the bracketed
# paste markers (`\x1b[200~` / `\x1b[201~`) alike.
_CSI = re.compile(rb"\x1b\[[0-9;]*[A-Za-z~]")
_SS3 = re.compile(rb"\x1bO[A-Za-z]")

# Mouse-report sequences: `_CSI` above requires digits/';' right after `[`,
# so these never match it (SGR starts with the literal `<`; X10 packs raw,
# non-digit coordinate bytes) and fell through to the printable-run branch
# instead — every digit/';'/M/m of a mouse click or wheel tick got counted as
# typed characters (confirmed repro: a single SGR press/release event read as
# 10 chars of "typed" draft, #108 root cause). Both forms are pure mouse
# telemetry — always a no-op, never a state transition, matching the
# Left/Right-arrow/Home/End precedent above.
# SGR (`\x1b[<Cb;Cx;CyM` press, `...m` release — wheel ticks use the same
# encoding with button codes 64+).
_SGR_MOUSE = re.compile(rb"\x1b\[<[0-9]+;[0-9]+;[0-9]+[Mm]")
# Legacy X10 (`\x1b[M` + 3 raw bytes: button, x, y — not necessarily ASCII).
_X10_MOUSE = re.compile(rb"\x1b\[M[\x00-\xff]{3}")

_UP_DOWN = {b"\x1b[A", b"\x1bOA", b"\x1b[B", b"\x1bOB"}
_PASTE_START = b"\x1b[200~"

_ENTER_BYTES = (0x0D, 0x0A)
_BACKSPACE_BYTES = (0x08, 0x7F)
_CTRL_C = 0x03
_CTRL_U = 0x15
_CTRL_A = 0x01
_CTRL_E = 0x05
_ESC = 0x1B


@dataclass(frozen=True)
class LeadDraftState:
    state: str = EMPTY
    draft_len: int = 0
    # Wall-clock (time.time()) when this project's draft first became
    # non-"empty"; 0.0 while "empty". Drives `draft_hold_expired()`.
    pending_since: float = 0.0


def _cleared() -> LeadDraftState:
    return LeadDraftState()


def _held(state: str, draft_len: int, prev: LeadDraftState, now: float) -> LeadDraftState:
    return LeadDraftState(state=state, draft_len=draft_len, pending_since=prev.pending_since or now)


def advance_draft_state(prev: LeadDraftState, data: bytes, now: float) -> LeadDraftState:
    """Fold one chunk of raw Lead-pane input bytes into `prev`, returning the
    next `LeadDraftState`. `now` is the wall-clock the caller observed the
    bytes at (`time.time()`) — used only to stamp `pending_since` on the
    empty→non-empty transition."""
    st = prev
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == _ESC:
            m_mouse = _SGR_MOUSE.match(data, i) or _X10_MOUSE.match(data, i)
            if m_mouse is not None:
                i = m_mouse.end()  # mouse click/release/wheel — always a no-op
                continue
            m = _CSI.match(data, i) or _SS3.match(data, i)
            if m is None:
                st = _cleared()  # bare Esc key
                i += 1
                continue
            seq = m.group()
            if seq in _UP_DOWN or seq == _PASTE_START:
                st = _held(UNKNOWN_NONEMPTY, st.draft_len, st, now)
            # Left/Right, paste-end, and any other CSI (Home/End/Delete/…)
            # are cursor movement / no-ops — state unchanged.
            i = m.end()
            continue
        if b in _BACKSPACE_BYTES:
            if st.state == NONEMPTY:
                new_len = max(0, st.draft_len - 1)
                st = _cleared() if new_len == 0 else _held(NONEMPTY, new_len, st, now)
            # "empty" stays empty; "unknown_nonempty" length is untracked —
            # a backspace there can't reliably move it toward "empty".
            i += 1
            continue
        if b in _ENTER_BYTES or b == _CTRL_C or b == _CTRL_U:
            st = _cleared()
            i += 1
            continue
        if b == _CTRL_A or b == _CTRL_E:
            i += 1  # cursor movement — no-op
            continue
        if b < 0x20:
            i += 1  # other control bytes — no-op, never counted as printable
            continue
        # Printable run (ASCII or UTF-8 continuation/lead bytes) up to the
        # next control/escape byte, decoded as a whole so a multi-byte
        # character (e.g. Thai) counts as one character, not one per byte.
        j = i
        while j < n and data[j] >= 0x20 and data[j] != 0x7F:
            j += 1
        added = len(data[i:j].decode("utf-8", errors="ignore"))
        i = j
        if added == 0:
            continue
        if st.state == UNKNOWN_NONEMPTY:
            continue  # length already unknowable; stay held
        st = _held(NONEMPTY, st.draft_len + added, st, now)
    return st


def draft_state_allows_injection(state: LeadDraftState | None) -> bool:
    """True when it's safe to paste an engine-originated message into the
    Lead pane right now — i.e. its input line reads empty."""
    return state is None or state.state == EMPTY


def draft_hold_expired(
    state: LeadDraftState | None, now: float, timeout_s: float = DRAFT_HOLD_TIMEOUT_S
) -> bool:
    """True once a held (non-empty) draft has blocked injection for
    `timeout_s` — the caller should give up waiting and spill instead of
    holding forever."""
    if state is None or state.state == EMPTY or not state.pending_since:
        return False
    return now - state.pending_since >= timeout_s
