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

# Real SGR/X10 mouse and CSI/SS3 sequences never exceed this many bytes
# (the longest realistic case is an SGR report with 4-5 digit coordinates).
# Caps how long an unresolved tail is buffered waiting for a terminator, so
# a stream that never produces one (garbage, or a genuinely bare `[`/`O`
# typed as literal text right after Esc) can't grow the buffer forever.
_MAX_ESCAPE_TAIL = 64


@dataclass(frozen=True)
class LeadDraftState:
    state: str = EMPTY
    draft_len: int = 0
    # Wall-clock (time.time()) when this project's draft first became
    # non-"empty"; 0.0 while "empty". Drives `draft_hold_expired()`.
    pending_since: float = 0.0
    # Raw bytes held back from the *end* of the previous chunk because they
    # were a syntactically valid but not-yet-terminated prefix of an escape
    # sequence (mouse/CSI/SS3) or a UTF-8 multibyte character — a PTY under
    # load can split either across two separate reads (issue #111). The next
    # `advance_draft_state()` call prepends this before parsing so the
    # sequence is reassembled instead of being misread as printable text.
    pending_tail: bytes = b""


def _cleared() -> LeadDraftState:
    return LeadDraftState()


def _held(state: str, draft_len: int, prev: LeadDraftState, now: float) -> LeadDraftState:
    return LeadDraftState(state=state, draft_len=draft_len, pending_since=prev.pending_since or now)


def _incomplete_escape_tail(tail: bytes) -> bool:
    """True when `tail` (the unconsumed bytes from an Esc byte through the
    end of the currently available data — already proven not to fully match
    a mouse/CSI/SS3 sequence) is still a valid, unterminated prefix of one,
    i.e. more bytes are needed before it can be classified either way. Only
    resolves to "definitely not incomplete" (False) once a byte is seen that
    could never continue any of these families — bare Esc is proven, never
    assumed, per issue #111."""
    if len(tail) > _MAX_ESCAPE_TAIL:
        return False  # runaway — stop waiting, fall back to bare-Esc handling
    if len(tail) == 1:
        return True  # just Esc — next byte ('['/'O' vs. anything else) unseen
    c1 = tail[1]
    if c1 not in (0x5B, 0x4F):  # not '[' and not 'O'
        return False  # can only be a genuine bare Esc
    if c1 == 0x4F:  # SS3: \x1bO + exactly one terminator letter
        return len(tail) == 2
    # c1 == '[' — CSI family, including both mouse encodings.
    if len(tail) == 2:
        return True  # '[' seen, but mouse-marker-vs-CSI-param byte unseen
    c2 = tail[2]
    if c2 == 0x3C:  # '<' — SGR mouse: digits ';' digits ';' digits (M|m)
        body = tail[3:]
        return all(0x30 <= ch <= 0x39 or ch == 0x3B for ch in body)
    if c2 == 0x4D:  # 'M' — X10 mouse: exactly 3 raw coordinate bytes follow
        return len(tail) - 3 < 3
    # Generic CSI: [0-9;]* then a terminator letter/`~` (Home/End/Delete/…).
    body = tail[2:]
    return all(0x30 <= ch <= 0x39 or ch == 0x3B for ch in body)


def _split_incomplete_utf8_tail(chunk: bytes) -> tuple[bytes, bytes]:
    """Split a printable-byte run whose end coincides with the end of the
    currently available data into `(complete, incomplete_tail)` — if the run
    ends mid-way through a multibyte UTF-8 character (e.g. Thai, split
    across two PTY reads), the partial lead/continuation bytes are returned
    as `incomplete_tail` instead of being decode-and-dropped by
    `errors="ignore"`, which would silently undercount the character."""
    n = len(chunk)
    for back in range(1, min(4, n) + 1):
        b0 = chunk[n - back]
        if b0 & 0xC0 == 0x80:
            continue  # continuation byte — keep looking further back
        if b0 & 0x80 == 0x00:
            break  # plain ASCII — nothing multibyte pending
        if b0 & 0xE0 == 0xC0:
            need = 2
        elif b0 & 0xF0 == 0xE0:
            need = 3
        elif b0 & 0xF8 == 0xF0:
            need = 4
        else:
            break  # not a valid UTF-8 lead byte — leave to errors="ignore"
        if back < need:
            return chunk[: n - back], chunk[n - back :]
        break
    return chunk, b""


def advance_draft_state(prev: LeadDraftState, data: bytes, now: float) -> LeadDraftState:
    """Fold one chunk of raw Lead-pane input bytes into `prev`, returning the
    next `LeadDraftState`. `now` is the wall-clock the caller observed the
    bytes at (`time.time()`) — used only to stamp `pending_since` on the
    empty→non-empty transition."""
    if prev.pending_tail:
        data = prev.pending_tail + data
    st = LeadDraftState(
        state=prev.state, draft_len=prev.draft_len, pending_since=prev.pending_since
    )
    pending_tail = b""
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == _ESC:
            m_mouse = _SGR_MOUSE.match(data, i) or _X10_MOUSE.match(data, i)
            if m_mouse is not None:
                i = m_mouse.end()  # mouse click/release/wheel — always a no-op
                continue
            tail = data[i:n]
            if _incomplete_escape_tail(tail):
                pending_tail = bytes(tail)  # wait for the rest — see docstring
                i = n
                continue
            m = _CSI.match(data, i) or _SS3.match(data, i)
            if m is None:
                st = _cleared()  # proven bare Esc key
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
        run = data[i:j]
        if j == n:
            run, utf8_tail = _split_incomplete_utf8_tail(run)
        else:
            utf8_tail = b""
        added = len(run.decode("utf-8", errors="ignore"))
        if utf8_tail:
            pending_tail = utf8_tail
            i = n
        else:
            i = j
        if added == 0:
            continue
        if st.state == UNKNOWN_NONEMPTY:
            continue  # length already unknowable; stay held
        st = _held(NONEMPTY, st.draft_len + added, st, now)
    if pending_tail:
        return LeadDraftState(
            state=st.state,
            draft_len=st.draft_len,
            pending_since=st.pending_since,
            pending_tail=pending_tail,
        )
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
