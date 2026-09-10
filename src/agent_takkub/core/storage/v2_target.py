"""Direct V2 storage access (#504 "cut" half). Every domain writer/reader
that used to dual-write into the V2 layout after committing its own V1 file
(``core.storage.dual_write``, retired) or gate a V2 read behind
``TAKKUB_V2_AUTHORITY`` (``core.storage.v2_authority``, also retired) now
touches ITS OWN V2 target directly — one location, no mirror, no fallback.
This module keeps only the two bits every one of those call sites still
needs: which ``data_home`` to resolve ``storage_layout_v2(...)`` against,
and a plain wrap/write + read/unwrap pair for the ``{"schema", "data"}``
envelope some targets use (kept for continuity with
``core.migration.steps_v1``'s own ``RegistryCopyStep``, whose
``validate()``/``dry_run()`` still expect that shape on a target file).

Target *paths* are never recomputed here — every caller resolves its own via
``storage_layout_v2()``/``core.migration.steps_v1``'s mapping builders,
exactly like ``core.storage.dual_write`` used to, so a domain module and
the migration ladder can never disagree about where a file lives.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ..migration.registry_copy_step import write_json_atomic
from .legacy_reader import read_json


def _primary_data_home() -> Path | None:
    """Best-effort recovery of the PRIMARY cockpit's own ``config.DATA_HOME``
    from inside a worktree pane process (carried over from
    ``core.storage.dual_write``'s #504-pre-req fix). A worktree checkout's
    own ``config.DATA_HOME`` resolves to ITS OWN checkout root in dev mode —
    a different directory per worktree, never the primary cockpit's
    ``DATA_HOME`` that a global-scope V1 domain (``SETTINGS_HOME``-sourced:
    provider-models, role-models, routing) needs its V2 target resolved
    against, since ``SETTINGS_HOME`` itself is shared across every dev
    checkout on the machine.

    ``pane_env._apply_port_file`` stamps every spawned pane's env with the
    HOST cockpit's own port-file path (normally ``<primary DATA_HOME>/
    runtime/port``), so its grandparent recovers the primary DATA_HOME.
    Returns ``None`` when not derivable (no override present, or the
    per-PID multi-instance temp file, which lives outside any DATA_HOME) —
    callers fall back to the caller-supplied/default resolution."""
    override = os.environ.get("TAKKUB_PORT_FILE", "").strip()
    if not override:
        return None
    if os.environ.get("_TAKKUB_AUTO_PORT_FILE", "").strip() == override:
        return None
    path = Path(override)
    if path.name != "port" or path.parent.name != "runtime":
        return None
    return path.parent.parent


def effective_data_home(
    data_home: Path | None = None, *, prefer_primary: bool = False
) -> Path | None:
    """The ``data_home`` argument a caller should pass into
    ``storage_layout_v2(...)``. A plain pass-through in the common case —
    deliberately NOT resolving ``None`` to ``config.DATA_HOME`` itself, so
    that job stays ``storage_layout_v2``'s own (its bare no-arg default is
    what ``tests/conftest.py``'s autouse isolation patches; resolving it
    here instead would silently bypass that patch — same class of bug
    ``core.routing.router`` already had to avoid, see its own history).

    ``prefer_primary`` — set by the handful of callers whose domain is
    itself ``SETTINGS_HOME``-scoped (global, shared across every dev
    checkout on the machine) rather than ``DATA_HOME``-scoped: a worktree
    pane process resolves the V2 target against the PRIMARY cockpit's
    DATA_HOME (:func:`_primary_data_home`) instead of its own checkout-local
    one. Only applies to the bare no-arg default — an explicit ``data_home``
    (every test) always wins outright, and when no primary is derivable this
    still returns ``None`` (the caller-supplied/default resolution)."""
    if data_home is None and prefer_primary:
        primary = _primary_data_home()
        if primary is not None:
            return primary
    return data_home


def write_data(target: Path, data: Any) -> None:
    """Atomically write *data* into *target*, wrapped in the same
    ``{"schema", "updated_at", "data"}`` envelope every V2 target has always
    used (``core.migration.steps_v1``'s ``RegistryCopyStep``/fan-out steps
    write the same shape on first migration)."""
    write_json_atomic(target, {"schema": 1, "updated_at": time.time(), "data": data})


def read_data(target: Path) -> Any | None:
    """Unwrap a V2 target's ``.data`` field. ``None`` on a missing target or
    a present-but-unreadable/unwrapped one — never raises."""
    if not target.exists():
        return None
    raw = read_json(target)
    if not isinstance(raw, dict) or "data" not in raw:
        return None
    return raw["data"]
