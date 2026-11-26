"""Errors that mean "the machine has nothing left right now" (#1063).

Stdlib-only leaf: the PTY backend waits these out, and the crash reporter
keeps them out of the public issue tracker — an exhausted machine is not a
cockpit bug, and five of them in a row would spend the day's report budget.
"""

from __future__ import annotations

# ERROR_NOT_ENOUGH_MEMORY (8), ERROR_OUTOFMEMORY (14),
# ERROR_NO_SYSTEM_RESOURCES (1450), ERROR_COMMITMENT_LIMIT (1455).
RESOURCE_EXHAUSTION_WINERRORS = frozenset({8, 14, 1450, 1455})
# POSIX: ENOMEM (12), EAGAIN on fork/thread creation (11 Linux, 35 macOS).
_RESOURCE_EXHAUSTION_ERRNOS = frozenset({12})


def is_resource_exhaustion(exc: BaseException | None) -> bool:
    """True for an error worth waiting out, never worth a bug report."""
    if exc is None:
        return False
    if isinstance(exc, MemoryError):
        return True
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "winerror", None) in RESOURCE_EXHAUSTION_WINERRORS:
        return True
    return getattr(exc, "winerror", None) is None and exc.errno in _RESOURCE_EXHAUSTION_ERRNOS
