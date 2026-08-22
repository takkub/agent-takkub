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

import shutil
from collections.abc import Sequence
from pathlib import Path

from agent_takkub import config

from ..contracts.migration import MigrationStep
from ..storage.layout import storage_layout_v2
from .backup import BackupManager
from .journal import MigrationJournal
from .report import StepReport
from .steps import VersionMarkerStep
from .steps_v1 import (
    CredentialReferenceStep,
    ProjectMigrationStep,
    RoleAgentMigrationStep,
    RuntimeTriageStep,
    build_capability_step,
    build_readonly_registries_step,
    build_state_step,
)


class MigrationEngine:
    def __init__(
        self,
        steps: Sequence[MigrationStep] | None = None,
        *,
        data_home: Path | None = None,
    ) -> None:
        if steps is not None:
            self._steps: list[MigrationStep] = list(steps)
            # Only known when the caller opts in explicitly — a hand-built
            # step list (e.g. unit-test fakes with no real filesystem) must
            # not have `rollback()` reach for a real V2 root it never wrote.
            self._data_home = data_home
        else:
            home = data_home if data_home is not None else config.DATA_HOME
            self._data_home = home
            journal = MigrationJournal()
            backups = BackupManager()
            # Ladder order (plan §5.3), lowest risk first. Every step shares
            # one journal/backup store so `takkub migrate rollback` can walk
            # the whole ladder in reverse from a single source of truth.
            self._steps = [
                VersionMarkerStep(journal=journal, backups=backups),
                build_readonly_registries_step(journal, backups, data_home=home),
                RoleAgentMigrationStep(journal=journal, backups=backups, data_home=home),
                build_capability_step(journal, backups, data_home=home),
                ProjectMigrationStep(journal=journal, backups=backups, data_home=home),
                build_state_step(journal, backups, data_home=home),
                CredentialReferenceStep(journal=journal, backups=backups, data_home=home),
                RuntimeTriageStep(journal=journal, backups=backups, data_home=home),
            ]

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
                return reports
        return self._verify_post_apply(reports)

    def _verify_post_apply(self, reports: list[StepReport]) -> list[StepReport]:
        """A later step's apply() can silently overwrite an earlier step's
        already-written target while both still report ok:true — each
        step's apply() only ever checks its own write, never the final
        on-disk state once the whole ladder has run (#350). Re-validate
        every step now and downgrade any apply report whose target no
        longer matches, so `apply` never claims ok while an immediate
        `validate` would already disagree."""
        verified: list[StepReport] = []
        for s, r in zip(self._steps, reports, strict=True):
            v = s.validate()
            if v.ok:
                verified.append(r)
            else:
                verified.append(
                    StepReport(
                        r.step_id,
                        "apply",
                        False,
                        f"{r.summary}; post-ladder validate failed: {v.summary}",
                        detail={"apply": r.detail, "validate": v.detail},
                    )
                )
        return verified

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
                return reports
        if self._data_home is not None:
            # copy-never-move (module docstring): everything under v2/ is a
            # copy of V1 data, never its only home — once every step's own
            # rollback reports ok, the whole V2 root is safe to remove
            # outright rather than trust each step's own bookkeeping to have
            # deleted every file/empty dir it ever created (#350: leftover
            # v2/ content otherwise kept `doctor --storage-layout` stuck on
            # "mixed" forever, permanently failing the plan's own pre-flight
            # check on any machine that ever ran `apply`).
            shutil.rmtree(storage_layout_v2(self._data_home).root, ignore_errors=True)
        return reports
