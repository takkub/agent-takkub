"""`MigrationEngine` — inspect/plan/dry-run/apply/validate/rollback over a
list of `MigrationStep`-protocol objects (`core.contracts.migration`), each
proven end to end by `VersionMarkerStep` before any real V1-data step is
added (plan §5.3's ladder is applied one release at a time, not all in
Phase 4).

`apply()`/`rollback()` stop-the-line on the first step whose `ok` is False
(plan §5.3's "เกณฑ์หยุด": one step failing on a real machine means stop, not
cascade into later, riskier steps). `inspect()`/`plan()`/`dry_run()` are
read-only and always run every step — a half inventory is worse than a slow
one.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..contracts.migration import MigrationStep
from .backup import BackupManager
from .journal import MigrationJournal
from .report import StepReport
from .steps import VersionMarkerStep


class MigrationEngine:
    def __init__(self, steps: Sequence[MigrationStep] | None = None) -> None:
        if steps is not None:
            self._steps: list[MigrationStep] = list(steps)
        else:
            journal = MigrationJournal()
            backups = BackupManager()
            self._steps = [VersionMarkerStep(journal=journal, backups=backups)]

    def inspect(self) -> list[StepReport]:
        return [s.inspect() for s in self._steps]

    def plan(self) -> list[StepReport]:
        return [s.plan() for s in self._steps]

    def dry_run(self) -> list[StepReport]:
        return [s.dry_run() for s in self._steps]

    def apply(self) -> list[StepReport]:
        reports: list[StepReport] = []
        for s in self._steps:
            r = s.apply()
            reports.append(r)
            if not r.ok:
                break
        return reports

    def validate(self) -> list[StepReport]:
        reports: list[StepReport] = []
        for s in self._steps:
            r = s.validate()
            reports.append(r)
            if not r.ok:
                break
        return reports

    def rollback(self) -> list[StepReport]:
        reports: list[StepReport] = []
        for s in reversed(self._steps):
            r = s.rollback()
            reports.append(r)
            if not r.ok:
                break
        return reports
