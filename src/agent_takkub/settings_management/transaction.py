"""Multi-file snapshot/rollback for cross-store writes.

A Role's Access tab touches up to four separate JSON stores (custom-roles,
role-providers, pane-tools policy, skill-policy) plus a role ``.md`` file.
Each store already writes its own file atomically (tmp+replace) — what's
missing is atomicity ACROSS stores: if store #3 fails to write, #1 and #2
must not be left half-applied. ``FileTransaction`` snapshots the raw bytes
of every path involved before any write, and restores them all if the
``with`` block raises.
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger(__name__)


class FileTransaction:
    """Snapshot a set of paths; restore all of them if the block raises.

    Usage::

        with FileTransaction([path_a, path_b]) as txn:
            if not write_a():
                raise RuntimeError("write_a failed")
            if not write_b():
                raise RuntimeError("write_b failed")
        # txn.rollback() already ran if either write raised
    """

    def __init__(self, paths: list[Path]) -> None:
        self._paths = list(paths)
        self._snapshots: dict[Path, bytes | None] = {}

    def __enter__(self) -> FileTransaction:
        for p in self._paths:
            self._snapshots[p] = p.read_bytes() if p.is_file() else None
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object
    ) -> bool:
        if exc_type is not None:
            self.rollback()
        return False

    def rollback(self) -> None:
        """Best-effort restore of every snapshotted path to its pre-``with`` state."""
        for p, content in self._snapshots.items():
            try:
                if content is None:
                    p.unlink(missing_ok=True)
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(content)
            except OSError as e:
                _log.warning("FileTransaction.rollback: could not restore %s: %s", p, e)
