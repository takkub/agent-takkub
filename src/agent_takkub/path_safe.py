"""One shared sanitizer for turning an arbitrary name (project, role, ...)
into a single filesystem-safe path segment (#294).

Before this module, `role_memory._safe`, `core.brain.store._safe_project`,
`task_ledger._ledger_dir`, and `lead_context.render_lead_settings` each
handled (or didn't handle) this differently. The original `_safe` collapsed
every non-`[A-Za-z0-9._-]` character to `_` independently, so two distinct
non-ASCII names of the same length (e.g. two different Thai project names)
could collapse to the identical string of underscores and silently share a
directory. It also didn't guard Windows reserved device names (`CON`,
`NUL`, ...) or cap length, risking MAX_PATH.

`safe_segment` fixes all three: any input that isn't already fully safe
gets a short hash of the raw name appended so collisions are astronomically
unlikely, reserved device names get a trailing underscore, and the result
is capped at 64 chars (truncated with a hash suffix). A name that was
already a fully safe, non-reserved, <=64-char segment is returned
unchanged, so every existing on-disk path for an ASCII project/role name is
preserved byte-for-byte.
"""

from __future__ import annotations

import hashlib
import re

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

_MAX_LENGTH = 64
_HASH_LEN = 8

# Windows reserved device names — blocked for a file/dir of ANY extension,
# case-insensitively, on the bare name before the first dot.
_RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _short_hash(raw: str) -> str:
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:_HASH_LEN]


def safe_segment(name: str, *, default: str = "default") -> str:
    """Sanitize `name` into one safe path segment. See module docstring."""
    if not name:
        return default

    changed = bool(_UNSAFE_RE.search(name))
    s = _UNSAFE_RE.sub("_", name)

    while ".." in s:
        s = s.replace("..", "_")
        changed = True

    stripped = s.strip(".")
    if stripped != s:
        changed = True
    s = stripped

    if not s:
        return default

    if changed:
        s = f"{s}-{_short_hash(name)}"

    base = s.split(".", 1)[0]
    if base.upper() in _RESERVED_WINDOWS_NAMES:
        s = f"{s}_"

    if len(s) > _MAX_LENGTH:
        keep = _MAX_LENGTH - _HASH_LEN - 1
        s = f"{s[:keep]}-{_short_hash(name)}"

    return s
