"""Contract one migration step must satisfy (Phase 8 CLI —
`takkub migrate {inspect,plan,dry-run,apply,validate,rollback}`, plan §5.1:
copy-never-move, every step idempotent with pre/post-check + journal).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MigrationStep(Protocol):
    def inspect(self) -> Any: ...

    def plan(self) -> Any: ...

    def dry_run(self) -> Any: ...

    def apply(self) -> Any: ...

    def validate(self) -> bool: ...

    def rollback(self) -> None: ...
