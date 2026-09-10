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
from collections.abc import Iterable, Sequence
from pathlib import Path

from agent_takkub import config

from ..contracts.migration import MigrationStep
from .backup import BackupManager
from .journal import MigrationJournal
from .promote_v1 import ArchiveV1LegacyStep, PromoteV2RootStep
from .report import StepReport
from .steps import VersionMarkerStep
from .steps_v1 import (
    CoreInternalStoreStep,
    CredentialReferenceStep,
    ProjectMigrationStep,
    RoleAgentMigrationStep,
    RuntimeTriageStep,
    build_capability_step,
    build_readonly_registries_step,
    build_state_step,
)

# #504: `ArchiveV1LegacyStep.step_id`, duplicated as a literal (not imported
# from the class) so this module never needs to construct one just to read
# an attribute — matches `apply_version_marker_only()`'s own "steps[0] is
# version-marker" convention of hardcoding ladder-position knowledge here.
_ARCHIVE_V1_STEP_ID = "archive-v1-legacy"

# The domain steps whose V1 SOURCE lives among what `ArchiveV1LegacyStep`
# archives — their `validate()` re-reads that source live and can never
# agree with an already-populated V2 target again once it's gone (see
# `MigrationEngine.validate()` below). `credential-reference` (provider
# homes), `runtime-triage` and `core-internal-store` (both under
# `RUNTIME_DIR`) read from #504 item 8's explicit never-touch list instead —
# deliberately excluded here, their validate() stays meaningful forever.
_ARCHIVED_SOURCE_STEP_IDS = frozenset(
    {"readonly-registries", "role-agent", "capability", "project", "state"}
)


class MigrationEngine:
    def __init__(
        self,
        steps: Sequence[MigrationStep] | None = None,
        *,
        data_home: Path | None = None,
        journal: MigrationJournal | None = None,
    ) -> None:
        if steps is not None:
            self._steps: list[MigrationStep] = list(steps)
            # Only known when the caller opts in explicitly — a hand-built
            # step list (e.g. unit-test fakes with no real filesystem) must
            # not have `rollback()` reach for a real V2 root it never wrote.
            self._data_home = data_home
            # Likewise only known when passed explicitly — `apply_pending()`
            # needs the SAME journal instance the hand-built steps were
            # wired to (real callers always share one, see below); a
            # hand-built test that doesn't call apply_pending() can leave
            # this None.
            self._journal = journal
        else:
            home = data_home if data_home is not None else config.DATA_HOME
            self._data_home = home
            journal = journal or MigrationJournal()
            self._journal = journal
            backups = BackupManager()
            # Ladder order (plan §5.3), lowest risk first. Every step shares
            # one journal/backup store so `takkub migrate rollback` can walk
            # the whole ladder in reverse from a single source of truth.
            self._steps = [
                VersionMarkerStep(journal=journal, backups=backups),
                # #504: right after version-marker, before any of the 8 V1->V2
                # steps below run their validate() in the SAME apply_pending()
                # pass — see promote_v1.py's module docstring for why this
                # exact position matters.
                PromoteV2RootStep(journal=journal, backups=backups, data_home=home),
                build_readonly_registries_step(journal, backups, data_home=home),
                RoleAgentMigrationStep(journal=journal, backups=backups, data_home=home),
                build_capability_step(journal, backups, data_home=home),
                ProjectMigrationStep(journal=journal, backups=backups, data_home=home),
                build_state_step(journal, backups, data_home=home),
                CredentialReferenceStep(journal=journal, backups=backups, data_home=home),
                RuntimeTriageStep(journal=journal, backups=backups, data_home=home),
                CoreInternalStoreStep(journal=journal, backups=backups, data_home=home),
                # #504: last — every step above needs its V1 source still on
                # disk to read from; archiving first would starve all of them.
                ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=home),
            ]

    def inspect(self) -> list[StepReport]:
        return [s.inspect() for s in self._steps]

    def plan(self) -> list[StepReport]:
        return [s.plan() for s in self._steps]

    def dry_run(self) -> list[StepReport]:
        return [s.dry_run() for s in self._steps]

    def apply_version_marker_only(self) -> StepReport:
        """Run just step 0 (`version-marker`) — the boot-time fast path once
        the full ladder has already applied once (#361): every later boot
        only needs `system/version.json` re-pinned to the running build, not
        a re-walk of the whole ladder. Reuses the same step object the
        default ladder already built (or `steps[0]` for a hand-built list),
        never a second `VersionMarkerStep` wired to different journal/backup
        stores."""
        return self._steps[0].apply()

    def applied_step_ids(self) -> list[str]:
        """Step ids the journal already has a successful, not-yet-rolled-
        back apply for — what `apply_pending()` (#362) treats as "this
        machine already finished this step". Empty when this engine has no
        journal at all (a hand-built step list built without one)."""
        if self._journal is None:
            return []
        return self._journal.applied_step_ids()

    def apply_pending(self, *, skip_step_ids: Iterable[str] = ()) -> list[StepReport]:
        """Run only the ladder steps that still need it (#362 — the gap
        between #360 adding `core-internal-store` and #361's boot-time
        auto-apply only ever re-running step 0): a step counts as pending
        when the journal has no successful apply for it yet, OR it does but
        the step's own `validate()` says it isn't actually complete right
        now (e.g. `version-marker` after an app upgrade). A step that is
        both journal-applied AND currently valid is skipped outright — no
        `apply()` call, no report, matching "step ที่ applied แล้วข้าม".

        `skip_step_ids` additionally holds back specific steps regardless
        of their pending-ness — the boot-time per-step retry-guard (#362)
        uses this so a step that already failed `apply_pending()` once this
        app version isn't retried every single boot.

        No stop-the-line: every non-skipped pending step gets attempted
        even if an earlier one in this same call failed, so one broken step
        never blocks a later, independent one from ever making progress —
        the caller (`auto_migrate_boot`) decides what a failure means
        per-step, not this method.

        #504: once `archive-v1-legacy` has archived every V1 leftover it
        owns (or there was never any), the 5 domain steps in
        `_ARCHIVED_SOURCE_STEP_IDS` can never validate `ok=True` again on
        their own — their V1 source is gone by design. Without this guard,
        every boot from then on would see them as "went stale" and re-run
        `apply()`, which would re-derive their V2 target from a now-missing
        V1 source and overwrite the real, already-correct migrated content
        with empty defaults. An already-applied step in that set is treated
        as still valid once V1 is retired, without even calling its own
        (permanently broken, post-archival) `validate()`."""
        applied_before = set(self.applied_step_ids())
        skip = set(skip_step_ids)
        archive_step = next(
            (s for s in self._steps if getattr(s, "step_id", "") == _ARCHIVE_V1_STEP_ID), None
        )
        v1_retired = archive_step is not None and archive_step.validate().ok
        reports: list[StepReport] = []
        for s in self._steps:
            step_id = getattr(s, "step_id", "")
            if step_id in skip:
                continue
            if step_id in applied_before:
                if v1_retired and step_id in _ARCHIVED_SOURCE_STEP_IDS:
                    continue
                if s.validate().ok:
                    continue
            reports.append(s.apply())
        return reports

    def rollback_step(self, step_id: str) -> StepReport:
        """Roll back exactly one ladder step by id — `apply_pending()`'s
        per-step failure handling (#362) must never reach for the
        whole-ladder `rollback()`, which would also undo every earlier step
        that already succeeded, possibly release(s) ago."""
        for s in self._steps:
            if getattr(s, "step_id", "") == step_id:
                return s.rollback()
        raise KeyError(f"MigrationEngine has no step {step_id!r}")

    def apply(self) -> list[StepReport]:
        # #504: `archive-v1-legacy` runs its OWN apply() last, same as every
        # other step — but it must be excluded from the `_verify_post_apply`
        # re-validation pass below, and run only after that pass completes.
        # It's the one step whose job is to remove the V1 sources every
        # domain step's own `validate()` re-reads live — running it inside
        # the same re-validated batch would make `_verify_post_apply` see
        # those V1 sources gone and downgrade every earlier domain step's
        # apply report to a false failure, even though each one wrote its
        # V2 target correctly.
        steps = [s for s in self._steps if getattr(s, "step_id", "") != _ARCHIVE_V1_STEP_ID]
        archive_step = next(
            (s for s in self._steps if getattr(s, "step_id", "") == _ARCHIVE_V1_STEP_ID), None
        )
        reports: list[StepReport] = []
        for s in steps:
            r = s.apply()
            reports.append(r)
            if not r.ok:
                return reports
        verified = self._verify_post_apply(steps, reports)
        if archive_step is None:
            return verified
        if any(not r.ok for r in verified):
            return verified
        verified.append(archive_step.apply())
        return verified

    def _verify_post_apply(
        self, steps: Sequence[MigrationStep], reports: list[StepReport]
    ) -> list[StepReport]:
        """A later step's apply() can silently overwrite an earlier step's
        already-written target while both still report ok:true — each
        step's apply() only ever checks its own write, never the final
        on-disk state once the whole ladder has run (#350). Re-validate
        every step now and downgrade any apply report whose target no
        longer matches, so `apply` never claims ok while an immediate
        `validate` would already disagree."""
        verified: list[StepReport] = []
        for s, r in zip(steps, reports, strict=True):
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
        # #504: once `archive-v1-legacy` reports nothing pending (every V1
        # leftover it owns has been archived, or there was never any to
        # begin with), the 5 domain steps whose SOURCE lives among what it
        # archives (`_ARCHIVED_SOURCE_STEP_IDS`) can no longer answer "does
        # my V2 target still match V1?" — their V1 source is gone by
        # design, not by accident. Re-reading a missing file as `{}` and
        # comparing it to a populated V2 target would report a false
        # mismatch forever after every future `validate()` call (`takkub
        # migrate validate`, `doctor --storage-layout`, ...), not just the
        # one apply() pass `_verify_post_apply` above already guards.
        # `credential-reference`/`runtime-triage`/`core-internal-store`
        # read from provider homes / `runtime/` — #504 item 8's explicit
        # never-touch list — so their sources persist forever and their
        # validate() stays meaningful; they are NOT in the skip set.
        archive_step = next(
            (s for s in self._steps if getattr(s, "step_id", "") == _ARCHIVE_V1_STEP_ID), None
        )
        v1_retired = archive_step is not None and archive_step.validate().ok
        reports: list[StepReport] = []
        for s in self._steps:
            step_id = getattr(s, "step_id", "")
            if v1_retired and step_id in _ARCHIVED_SOURCE_STEP_IDS:
                reports.append(
                    StepReport(
                        step_id,
                        "validate",
                        True,
                        "V1 source archived (#504) — nothing left to cross-check against",
                    )
                )
                continue
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
        if self._data_home is None:
            # Every per-step rollback above already reported ok — but a
            # rollback that leaves a stray legacy v2/ root in place is
            # incomplete, not merely partial (#350 qa follow-up: this used
            # to be a quiet `if self._data_home is not None:` skip, which
            # meant a future edit could delete that guard with nothing to
            # catch it — see
            # test_engine_rollback_without_data_home_raises_instead_of_silently_skipping).
            # Refuse outright instead of guessing at a fallback target.
            raise RuntimeError(
                "MigrationEngine.rollback() needs data_home to finish "
                "cleanup; every per-step rollback above succeeded but "
                "leaving a stray legacy v2/ root in place would make this "
                "an incomplete rollback. Pass data_home=... to the "
                "constructor (real callers always do — cli.py and "
                "auto_migrate_boot.py both use the default MigrationEngine() "
                "which resolves it from config.DATA_HOME)."
            )
        # #504: `storage_layout_v2().root` is now DATA_HOME itself (the
        # nested v2/ folder was retired as the V2 root by the promote step
        # above) — a blanket rmtree of "root" here would wipe the whole
        # user data directory, not a disposable copy. Each step's own
        # rollback already reversed exactly what IT wrote; the only
        # still-disposable-by-construction leftover is a legacy pre-#504
        # nested v2/ folder, if `PromoteV2RootStep.rollback()` above didn't
        # already need it (e.g. it was interrupted mid-promote) — always
        # safe to remove since it is, by definition, never anything's only
        # copy (#350's original "don't leave `doctor --storage-layout`
        # stuck reporting stale state forever" concern, scoped to the one
        # thing here that's actually still safe to nuke).
        shutil.rmtree(self._data_home / "v2", ignore_errors=True)
        return reports
