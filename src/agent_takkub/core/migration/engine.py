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

import json
import os
import shutil
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from agent_takkub import config

from ..contracts.migration import MigrationStep
from .backup import BackupManager
from .journal import MigrationJournal
from .pre_migrate_backup import PreMigrateBackupStep
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

# #504: `ArchiveV1LegacyStep.step_id`/`PromoteV2RootStep.step_id`, duplicated
# as literals (not imported from the classes) so this module never needs to
# construct one just to read an attribute — matches
# `apply_version_marker_only()`'s own "steps[0] is version-marker" convention
# of hardcoding ladder-position knowledge here.
_ARCHIVE_V1_STEP_ID = "archive-v1-legacy"
_PROMOTE_V2_ROOT_STEP_ID = "promote-v2-root"
_VERSION_MARKER_STEP_ID = "version-marker"
_PRE_MIGRATE_BACKUP_STEP_ID = "pre-migrate-backup"


def _remove_if_empty_dir(path: Path) -> None:
    """Remove *path* only if it (and everything under it) is already empty
    of files — never a blanket removal of real content. #504 B5 (acceptance
    review): `MigrationEngine.rollback()` used to unconditionally
    `shutil.rmtree` a stray nested `v2/` root on the theory that it could
    never be anything's only copy — false once `PromoteV2RootStep.rollback()`
    started deliberately RECREATING a real `v2/` tree as part of a normal
    restore-v1 (the reviewed `engine_rollback` repro: a unique file ended up
    with zero copies anywhere in DATA_HOME after a fully-green rollback)."""
    if not path.is_dir():
        return
    for _dirpath, _dirnames, filenames in os.walk(path):
        if filenames:
            return
    shutil.rmtree(path, ignore_errors=True)


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


# Every domain target's required top-level key(s), by which accessor
# computed its path — `RegistryCopyStep` targets (readonly-registries/
# capability/state) and `ProjectMigrationStep._registry_target()` all use
# the ``{"schema", ..., "data": ...}`` envelope
# (`registry_copy_step.write_json_atomic`'s shape); `RoleAgentMigrationStep
# ._routing_target()` is the one exception — `_routing_payload()` writes
# ``{"schema", "migrated_at", "global", "projects"}`` with no "data" key at
# all, so a single universal required-key assumption is wrong for it.
_DOMAIN_TARGET_ACCESSOR_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "_custom_roles_target": ("data",),
    "_routing_target": ("global", "projects"),
    "_registry_target": ("data",),
}


def _domain_target_specs(step: object) -> list[tuple[Path, tuple[str, ...]]]:
    """Every V2 target file *step* (one of `_ARCHIVED_SOURCE_STEP_IDS`)
    writes, paired with its required top-level key(s) — used only once its
    V1 source has been archived and its own `validate()` can no longer
    re-read that source to cross-check against (#504 R2-H9
    `domain_integrity`). `RegistryCopyStep` exposes its targets via
    `.mappings` directly (always the "data"-keyed envelope);
    `RoleAgentMigrationStep`/`ProjectMigrationStep` have their own
    dataclass-specific target accessors — read by name rather than
    duplicating each step's own path-computation logic here."""
    mappings = getattr(step, "mappings", None)
    if mappings is not None:
        return [(m.target, ("data",)) for m in mappings]
    specs: list[tuple[Path, tuple[str, ...]]] = []
    for attr, required_keys in _DOMAIN_TARGET_ACCESSOR_REQUIRED_KEYS.items():
        fn = getattr(step, attr, None)
        if fn is not None:
            specs.append((fn(), required_keys))
    return specs


def _domain_target_problems(specs: list[tuple[Path, tuple[str, ...]]]) -> list[str]:
    """Presence + JSON-readability + required-key check for every
    ``(path, required_keys)`` in *specs* — NOT a byte/sha256 match against
    the (now-archived, gone) V1 source: a domain target is a live, mutable
    file after migration (a project gets renamed, a role gets added), so
    only its own basic health can be required forever, never that it still
    equals some historical V1 snapshot."""
    problems: list[str] = []
    for path, required_keys in specs:
        if not path.is_file():
            problems.append(f"missing: {path}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems.append(f"unreadable ({e}): {path}")
            continue
        if not isinstance(data, dict):
            problems.append(f"not a JSON object: {path}")
            continue
        # #504 R3 `domain_null_data`: a present-but-null required key (e.g.
        # `{"data": null}`) used to pass this check — `k not in data` is
        # true only when the key is absent, not when it holds a
        # legitimately-impossible value.
        missing = [k for k in required_keys if k not in data or data[k] is None]
        if missing:
            problems.append(f"missing required key(s) {missing}: {path}")
    return problems


class MigrationEngine:
    def __init__(
        self,
        steps: Sequence[MigrationStep] | None = None,
        *,
        data_home: Path | None = None,
        journal: MigrationJournal | None = None,
        on_entry: Callable[[str, str], None] | None = None,
        on_file_progress: Callable[[str, str, int, int, str], None] | None = None,
        on_step: Callable[[str, str], None] | None = None,
        on_validate_step: Callable[[str, bool], None] | None = None,
    ) -> None:
        """*on_entry* (#574): best-effort ``(step_id, entry_name)``
        progress observer, forwarded ONLY into the three steps whose
        `apply()` moves real, possibly-large file trees
        (`pre-migrate-backup`, `promote-v2-root`, `archive-v1-legacy`) —
        each gets its own step_id-bound partial of *on_entry*, since those
        steps' own `on_entry` field only ever passes the entry name. A pure
        additive wiring: `None` (the default) reproduces every existing
        caller's behavior exactly.

        *on_file_progress* (#574 round11 item 3): the SAME three steps'
        own `on_file_progress` field — best-effort ``(step_id, entry_name,
        files_done, files_total, current_path)``, for progress WITHIN one
        directory entry's own copy+verify (`on_entry` above only fires
        once per whole entry, not fine-grained enough for a directory
        holding tens of thousands of files).

        *on_step* (#574 round12 item 3): best-effort ``(step_id, "start"|
        "done")`` fired around EVERY ladder step's own copy-apply, in
        `apply()`/`apply_pending()` below — unlike *on_entry*/
        *on_file_progress* (bound only into the 3 steps that move real file
        trees), this fires for every step, so a caller can also observe the
        8 domain steps (`readonly-registries`, `role-agent`, `capability`,
        `project`, `state`, `credential-reference`, `runtime-triage`,
        `core-internal-store`) that otherwise produce zero progress signal
        at all — they write in one shot, with nothing to report mid-step,
        so "start" then "done" is the most granular truthful signal
        available. Stored directly (never per-step-bound like *on_entry*
        above) since every call site already has `step_id` in hand from
        its own loop over `self._steps`.

        *on_validate_step* (#574 round14, R5-M3/R8-M2): best-effort
        ``(step_id, ok)``, fired once per step as its own REAL
        `validate()` call resolves, in `validate()`/`validate_ok_steps()`
        below — unlike *on_step* (apply-time, no pass/fail of its own to
        report), this carries the actual verdict, so a caller can show
        "validated" only for a step that genuinely was, and label a real
        failure honestly instead of repeating a false "validated" claim.
        Also stored directly, same reasoning as *on_step*."""

        def _bound(step_id: str) -> Callable[[str], None] | None:
            if on_entry is None:
                return None
            return lambda name: on_entry(step_id, name)

        def _bound_file(step_id: str) -> Callable[[str, int, int, str], None] | None:
            if on_file_progress is None:
                return None
            return lambda name, done, total, path: on_file_progress(
                step_id, name, done, total, path
            )

        self._on_step = on_step
        self._on_validate_step = on_validate_step
        # #574 round14b: the REAL, correctly-timed validate() reports
        # `apply_pending()` itself produces for each step it just applied —
        # see that method's own notes for why the separate
        # `validate_ok_steps()` call `auto_migrate_boot` used to make
        # right after it fired `on_validate_step` too late (after
        # `archive-v1-legacy`'s own copy phase, run inline in that SAME
        # per-step loop, had already advanced `boot_flow.py`'s phase past
        # 3). `apply()`'s own `_downgrade_on_health` re-validate already
        # runs at the right time for free (before `archive-v1-legacy`
        # starts) but is deliberately NOT wired to fill this in — the "v1"
        # first-apply boot path needs its OWN separate, independent
        # `engine.validate()` call after `apply()` for defense-in-depth
        # (a monkeypatched/overridden `validate()` must still be able to
        # catch something `apply()`'s own inline checks did not).
        self.last_validate_reports: list[StepReport] = []
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
                # #574: first, ahead of even version-marker — a failed
                # backup must abort the whole ladder before anything else
                # is touched. `apply()` already stops at the first non-ok
                # step from ladder POSITION alone; `apply_pending()` does
                # NOT (its own "No stop-the-line" contract, #504/#574
                # R8-H1) — see its own explicit `_PRE_MIGRATE_BACKUP_STEP_ID`
                # check below for the guarantee on THAT path.
                PreMigrateBackupStep(
                    journal=journal,
                    backups=backups,
                    data_home=home,
                    on_entry=_bound("pre-migrate-backup"),
                    on_file_progress=_bound_file("pre-migrate-backup"),
                ),
                VersionMarkerStep(journal=journal, backups=backups),
                # #504: right after version-marker, before any of the 8 V1->V2
                # steps below run their validate() in the SAME apply_pending()
                # pass — see promote_v1.py's module docstring for why this
                # exact position matters.
                PromoteV2RootStep(
                    journal=journal,
                    backups=backups,
                    data_home=home,
                    on_entry=_bound("promote-v2-root"),
                    on_file_progress=_bound_file("promote-v2-root"),
                ),
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
                ArchiveV1LegacyStep(
                    journal=journal,
                    backups=backups,
                    data_home=home,
                    on_entry=_bound("archive-v1-legacy"),
                    on_file_progress=_bound_file("archive-v1-legacy"),
                ),
            ]

    def _notify_step(self, step_id: str, kind: str) -> None:
        """Best-effort `on_step(step_id, "start"|"done")` — never lets an
        observer failure affect the ladder step it's observing (#574
        round12 item 3, matching `_notify_entry`'s own swallow-ok
        contract in `promote_v1.py`)."""
        if self._on_step is None:
            return
        try:
            self._on_step(step_id, kind)
        except Exception:
            return  # swallow-ok: R9-L2 — an observer failure must never affect the step it's observing.

    def _notify_validate_step(self, step_id: str, ok: bool) -> None:
        """Best-effort `on_validate_step(step_id, ok)` — never lets an
        observer failure affect the validate() call it's observing (#574
        round14, matching `_notify_step`'s own swallow-ok contract)."""
        if self._on_validate_step is None:
            return
        try:
            self._on_validate_step(step_id, ok)
        except Exception:
            return  # swallow-ok: a progress-observer failure must never affect the validate() call it's observing.

    def step_count(self) -> int:
        """Ladder length — the same step list `validate()` below walks, so
        a caller previewing "step X/N" (#574's `MigrationPlanSummary
        .verify_steps`) before anything has run can never drift from what
        an actual `validate()` pass would later report `failed_step_total`
        as."""
        return len(self._steps)

    def inspect(self) -> list[StepReport]:
        return [s.inspect() for s in self._steps]

    def plan(self) -> list[StepReport]:
        return [s.plan() for s in self._steps]

    def dry_run(self) -> list[StepReport]:
        return [s.dry_run() for s in self._steps]

    def apply_version_marker_only(self) -> StepReport:
        """Run just `version-marker` — the boot-time fast path once the full
        ladder has already applied once (#361): every later boot only needs
        `system/version.json` re-pinned to the running build, not a re-walk
        of the whole ladder. Reuses the same step object the default ladder
        already built, never a second `VersionMarkerStep` wired to different
        journal/backup stores. Looked up by step_id (not `steps[0]`) so this
        stays correct regardless of ladder ordering."""
        for s in self._steps:
            if getattr(s, "step_id", "") == _VERSION_MARKER_STEP_ID:
                return s.apply()
        raise KeyError(f"MigrationEngine has no step {_VERSION_MARKER_STEP_ID!r}")

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
        per-step, not this method. The ONE exception (#504 B2, acceptance
        review): `archive-v1-legacy` structurally depends on
        `promote-v2-root` having actually finished (or had nothing to do)
        in THIS SAME pass — it treats the legacy `v2/` root as safe to
        remove the instant it exists, and a `promote-v2-root` that failed
        mid-copy leaves its SOURCE exactly as full as before it started
        (its own two-phase copy-then-remove never got to the removal half).
        Letting the pass continue into `archive-v1-legacy` anyway used to
        have it destroy that still-full source outright — the reviewed
        `pending_failure` repro: a unique file present nowhere else ended up
        in the nested source, the promoted target, AND every archive. A
        `promote-v2-root` failure now stops this whole pass right here —
        the OTHER already-applied domain steps from a PRIOR pass are
        untouched, and the caller's existing per-step rollback/retry-guard
        (`auto_migrate_boot._run_apply_pending`) still handles
        `promote-v2-root` itself exactly like any other new failure.

        #504 H6 (acceptance review): `version-marker` (ladder position 0)
        runs before `promote-v2-root` can have materialized
        `core.storage.paths.core_home()`'s post-#504 top-level `system/` for
        the first time — its write can land at the pre-flip fallback
        location moments before `promote-v2-root` copies an OLD nested
        `v2/system/version.json` over the spot `core_home()` NOW resolves
        to, leaving the running build's own version stamp shadowed by a
        stale one on the very first boot after an upgrade. When
        `promote-v2-root` just ran (successfully) in this pass, re-apply
        `version-marker` once more right after it, so this pass ends with
        the current version at wherever `core_home()` ends up resolving —
        not whatever `promote-v2-root` happened to copy up from the old
        nested root.

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
        version_marker_step = next(
            (s for s in self._steps if getattr(s, "step_id", "") == _VERSION_MARKER_STEP_ID), None
        )
        steps_run: list[MigrationStep] = []
        reports: list[StepReport] = []
        self.last_validate_reports = []
        version_marker_reapply: StepReport | None = None
        promote_idx: int | None = None
        # #574 round14b (R8-M2 residual-catch-up case): whether THIS pass's
        # `promote-v2-root` has anything real left to promote, sampled
        # before its own apply below drains it. Phase 3 ("ตรวจสอบ") is
        # part of the migration WIZARD's own screen sequence (backup ->
        # copy -> verify -> archive -> done) — when nothing is left to
        # promote, a domain step reached only for residual catch-up
        # (added-to-the-ladder-since / a lagging step on an otherwise
        # fully-promoted machine, #362's own motivating scenario) still
        # gets a real `validate()` counted into `last_validate_reports`
        # (so `MigrationOutcome.validated_steps` is never falsely 0), but
        # is not notified into the live wizard UI — surfacing a quiet
        # background catch-up as its own multi-row "step X/12 ตรวจสอบแล้ว"
        # screen would misrepresent it as an active migration in progress.
        promote_step_for_gate = next(
            (s for s in self._steps if getattr(s, "step_id", "") == _PROMOTE_V2_ROOT_STEP_ID), None
        )
        promote_candidates_fn = getattr(promote_step_for_gate, "_promote_candidates", None)
        if not callable(promote_candidates_fn):
            promote_has_pending_work = True
        else:
            try:
                promote_has_pending_work = bool(promote_candidates_fn())
            except Exception:
                promote_has_pending_work = True  # fail open: never suppress on a probe error
        for s in self._steps:
            step_id = getattr(s, "step_id", "")
            if step_id in skip:
                continue
            if step_id in applied_before:
                if v1_retired and step_id in _ARCHIVED_SOURCE_STEP_IDS:
                    continue
                if s.validate().ok:
                    continue
            self._notify_step(step_id, "start")
            r = self._copy_only_apply(s)
            self._notify_step(step_id, "done")
            steps_run.append(s)
            reports.append(r)
            if r.ok and not self._prune_deferred(s):
                # #574 round14b: validate THIS step right here, in ladder
                # order — chronologically before any LATER step's own
                # apply/on_entry fires (specifically `archive-v1-legacy`'s
                # own copy phase, last in the ladder). Unlike `apply()`,
                # this method runs `archive-v1-legacy`'s copy INLINE in
                # this same per-step loop rather than deferring it until
                # after every other step's health-check, so a validate
                # pass batched at the very end of this method (the old
                # `validate_ok_steps()` call `auto_migrate_boot` used to
                # make afterward) always landed after that copy phase had
                # already advanced `boot_flow.py`'s phase past 3 —
                # `promote-v2-root`/`archive-v1-legacy` are excluded here
                # (`_prune_deferred`): their real validate must wait for
                # THIS pass's own deferred prune to finish, below.
                v = self._validate_one(s, v1_retired=v1_retired)
                # `last_validate_reports` always gets the real result
                # (`MigrationOutcome.validated_steps` must count it either
                # way) — the LIVE notify is what's gated on
                # `promote_has_pending_work` above, for domain steps only.
                if promote_has_pending_work or step_id in (
                    _PRE_MIGRATE_BACKUP_STEP_ID,
                    _VERSION_MARKER_STEP_ID,
                ):
                    self._notify_validate_step(step_id, v.ok)
                self.last_validate_reports.append(v)
            if step_id == _PRE_MIGRATE_BACKUP_STEP_ID and not r.ok:
                # #504/#574 R8-H1: a failed `pre-migrate-backup` must abort
                # the whole ladder before anything else is touched — true
                # for free on `apply()` (ladder position 0 + its own
                # stop-the-line), but `apply_pending()` has no stop-the-line
                # of its own by design (see this method's docstring) and
                # would otherwise walk every remaining step, mutating
                # `data_home` with no usable pre-migrate backup in
                # existence. This is the one explicit exception, mirroring
                # `_PROMOTE_V2_ROOT_STEP_ID`'s own below.
                break
            if step_id == _PROMOTE_V2_ROOT_STEP_ID:
                promote_idx = len(steps_run) - 1
                if not r.ok:
                    break
                if version_marker_step is not None:
                    version_marker_reapply = version_marker_step.apply()
        # #504 round4 B1/B3: a hand-built ladder with no promote/archive
        # step (or FakeStep stand-ins with matching ids but no copy/prune
        # split) has nothing to defer — behave exactly as before, no extra
        # `validate()` probing beyond what this method already did.
        if not any(self._prune_deferred(s) for s in steps_run):
            return reports
        finished = self._finish_deferred_prune(
            steps_run, self._downgrade_on_health(steps_run, reports)
        )
        # #574 round14b: `promote-v2-root`/`archive-v1-legacy` were excluded
        # from the inline validate above — their real validate() only means
        # something once THIS pass's own deferred prune (just above) has
        # settled whether they actually finished pruning their V1 source.
        # Not surfaced to `boot_flow.py`'s phase-3 UI (`_DOMAIN_STEP_IDS`
        # excludes both), so timing relative to phase 4 doesn't matter for
        # them — only completeness of `last_validate_reports`' step count.
        for s, r in zip(steps_run, finished, strict=True):
            if self._prune_deferred(s) and r.ok:
                step_id = getattr(s, "step_id", "")
                v = self._validate_one(s, v1_retired=v1_retired)
                self._notify_validate_step(step_id, v.ok)
                self.last_validate_reports.append(v)
        if version_marker_reapply is not None and promote_idx is not None:
            finished = [
                *finished[: promote_idx + 1],
                version_marker_reapply,
                *finished[promote_idx + 1 :],
            ]
        return finished

    def _prune_deferred(self, s: object) -> bool:
        """*s* supports the copy-then-defer-prune split (real
        `PromoteV2RootStep`/`ArchiveV1LegacyStep` only — a hand-built test
        step, even one sharing the same `step_id`, never does) — #504
        round4 B1: `MigrationEngine`'s ladder barrier only ever defers
        pruning for a step that actually has a `prune()` to defer."""
        return callable(getattr(s, "prune", None)) and callable(
            getattr(s, "_health_problems", None)
        )

    def _copy_only_apply(self, s: MigrationStep) -> StepReport:
        """*s*'s own apply, but copy-only when *s* supports the deferred-
        prune split — every OTHER step's `apply()` already never deletes
        any V1 source (#504's own domain steps only ever write V2
        targets), so it runs exactly as before."""
        fn = getattr(s, "apply_copy_only", None)
        return fn() if callable(fn) else s.apply()

    def _post_copy_health(self, s: MigrationStep) -> StepReport:
        """Re-validate *s* right after its own apply this pass, WITHOUT
        assuming its (possibly still-deferred) prune has run — a
        deferred-prune step's `validate()` is inherently prune-completion-
        gated (`_pending()`-based), so its OWN copy-target health check is
        used instead (#504 round4 B1); every other step's `.validate()` is
        unchanged."""
        if self._prune_deferred(s):
            problems = s._health_problems()
            step_id = getattr(s, "step_id", "")
            return StepReport(
                step_id,
                "validate",
                not problems,
                problems[0] if problems else "copy target healthy",
            )
        return s.validate()

    def _downgrade_on_health(
        self, steps: Sequence[MigrationStep], reports: list[StepReport]
    ) -> list[StepReport]:
        """A later step's apply() can silently overwrite an earlier step's
        already-written target while both still report ok:true — each
        step's apply() only ever checks its own write, never the final
        on-disk state once the whole ladder has run (#350). Re-validate
        every ALREADY-ok step now (via `_post_copy_health`) and downgrade
        any apply report whose target no longer matches, so `apply` never
        claims ok while an immediate check would already disagree. A step
        whose OWN apply already failed keeps that report untouched."""
        out: list[StepReport] = []
        for s, r in zip(steps, reports, strict=True):
            if not r.ok:
                out.append(r)
                continue
            v = self._post_copy_health(s)
            if v.ok:
                out.append(r)
            else:
                out.append(
                    StepReport(
                        r.step_id,
                        "apply",
                        False,
                        f"{r.summary}; post-ladder validate failed: {v.summary}",
                        detail={"apply": r.detail, "validate": v.detail},
                    )
                )
        return out

    def _finish_deferred_prune(
        self, steps: Sequence[MigrationStep], reports: list[StepReport]
    ) -> list[StepReport]:
        """#504 round4 B1 (Gemini cross-check (B)(1)): finish every
        deferred-prune step's `prune()` — but ONLY once every step in
        *steps* (domain steps and every OTHER archive generation included,
        via `_downgrade_on_health`/`_post_copy_health` above) has an
        ok:true report. One late failure anywhere in the SAME pass means
        NO step in it prunes — every V1 source copy-verified this pass
        stays fully intact, safely retryable next pass, matching #504's
        own Pass-1/Pass-2 contract (never delete until the whole ladder
        validates).

        #504 round5 R5-H1: that gate only decides whether pass 2 STARTS.
        Once started, a step's own `prune()` can itself fail (a denied
        removal, recorded DUPLICATE) partway through the loop — every
        LATER step in this SAME pass must stop pruning too, never keep
        deleting further V1 sources on top of an already-half-failed
        pass. Every step at and after the failure keeps its pass-1 report
        untouched (its own `prune()` never ran), so its V1 source stays
        fully intact, retryable next pass, exactly like the whole-pass
        gate above already guarantees for a pass-1 failure."""
        if any(not r.ok for r in reports):
            return list(reports)
        out: list[StepReport] = []
        stopped = False
        for s, r in zip(steps, reports, strict=True):
            if stopped or not self._prune_deferred(s):
                out.append(r)
                continue
            pruned = s.prune()
            out.append(pruned)
            if not pruned.ok:
                stopped = True
        return out

    def rollback_step(self, step_id: str) -> StepReport:
        """Roll back exactly one ladder step by id — `apply_pending()`'s
        per-step failure handling (#362) must never reach for the
        whole-ladder `rollback()`, which would also undo every earlier step
        that already succeeded, possibly release(s) ago."""
        return self.get_step(step_id).rollback()

    def get_step(self, step_id: str) -> MigrationStep:
        """The ladder step object with this id — #504 H1: `cli.py`'s
        `restore-v1` needs to call `ArchiveV1LegacyStep.rollback(archive_ts=...)`
        with an argument `rollback_step()` above has no way to pass through
        (every `MigrationStep.rollback()` in the protocol takes none)."""
        for s in self._steps:
            if getattr(s, "step_id", "") == step_id:
                return s
        raise KeyError(f"MigrationEngine has no step {step_id!r}")

    def apply(self) -> list[StepReport]:
        """#504 round4 B1/B3 (Gemini cross-check): a full-ladder apply is
        now genuinely 2-pass. Pass A copies every step (`archive-v1-legacy`
        still last, still copy-only — its job is to remove V1 sources
        every domain step's own `validate()`/health-check re-reads live,
        so it must never run inside the same re-checked batch as them).
        Once every step's copy has ok'd AND `_downgrade_on_health` finds
        the whole pass still clean (including `archive-v1-legacy`'s own
        copy, re-checked the same way), Pass B (`_finish_deferred_prune`)
        removes every V1 source in one final sweep. Any failure anywhere
        in Pass A means NO step's Pass B ever runs — every source stays
        fully intact, retryable.

        R9-L3 (#574 round14, reviewed and left as-is): unlike
        `apply_pending()`, this method has no H6-style `version-marker`
        re-apply after `promote-v2-root`. That gap IS real (forcing a
        legacy nested `v2/` root onto this path reproduces a red
        `version-marker`/`core-internal-store` validate) but not
        reachable in production: this method only ever runs when
        `layout_state()` is exactly `"v1"`, which requires NO `v2/`
        directory to exist at all — `promote-v2-root` then has nothing to
        flip `core_home()` away from, and the ladder validates green end
        to end on every real first-time apply. A machine that DOES have a
        legacy nested `v2/` root reads `"mixed"`, not `"v1"`, and always
        takes `apply_pending()` instead, which already has the fix."""
        steps = [s for s in self._steps if getattr(s, "step_id", "") != _ARCHIVE_V1_STEP_ID]
        archive_step = next(
            (s for s in self._steps if getattr(s, "step_id", "") == _ARCHIVE_V1_STEP_ID), None
        )
        reports: list[StepReport] = []
        for s in steps:
            step_id = getattr(s, "step_id", "")
            self._notify_step(step_id, "start")
            r = self._copy_only_apply(s)
            self._notify_step(step_id, "done")
            reports.append(r)
            if not r.ok:
                return reports
        verified = self._downgrade_on_health(steps, reports)
        all_steps, all_reports = steps, verified
        if archive_step is not None and not any(not r.ok for r in verified):
            self._notify_step(archive_step.step_id, "start")
            archive_report = self._copy_only_apply(archive_step)
            self._notify_step(archive_step.step_id, "done")
            all_steps = [*steps, archive_step]
            all_reports = [*verified, *self._downgrade_on_health([archive_step], [archive_report])]
        return self._finish_deferred_prune(all_steps, all_reports)

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
            r = self._validate_one(s, v1_retired=v1_retired)
            self._notify_validate_step(step_id, r.ok)
            reports.append(r)
            if not r.ok:
                break
        return reports

    def _validate_one(self, s: MigrationStep, *, v1_retired: bool) -> StepReport:
        """One step's own `validate()`, v1-retired-aware — factored out of
        `validate()` above so `validate_ok_steps()` below (#504/#574 R8-M2)
        can reuse the exact same "V1 source archived" special case instead
        of re-deriving it."""
        step_id = getattr(s, "step_id", "")
        if v1_retired and step_id in _ARCHIVED_SOURCE_STEP_IDS:
            # #504 R2-H9 `domain_integrity`: "nothing left to cross-check
            # against" used to be an unconditional True — corrupting or
            # deleting the V2 target itself (`projects/registry.json`
            # turning into invalid JSON, say) still validated green
            # forever. Check the target's own basic health instead of
            # skipping straight to success.
            problems = _domain_target_problems(_domain_target_specs(s))
            if problems:
                return StepReport(
                    step_id,
                    "validate",
                    False,
                    f"V1 source archived (#504) but V2 target unhealthy: {problems[0]}",
                    detail={"problems": problems},
                )
            return StepReport(
                step_id,
                "validate",
                True,
                "V1 source archived (#504) — target present, readable, correctly shaped",
            )
        return s.validate()

    def validate_ok_steps(self, step_ids: Iterable[str]) -> list[StepReport]:
        """Real, v1_retired-aware `validate()` for just *step_ids* — #504/
        #574 R8-M2: `apply_pending()` has no whole-ladder `validate()` pass
        the way `apply()` does (its own per-step failure handling already
        isolates one step from the rest, so a single stop-on-first-failure
        walk would be wrong here), which used to leave
        `MigrationOutcome.validated_steps` permanently 0 on every promoted
        machine even though `boot_flow.py`'s phase-3 UI claimed each domain
        step had been "ตรวจสอบแล้ว" (validated). A caller
        (`auto_migrate_boot._run_apply_pending`) passes the step ids
        `apply_pending()` just applied successfully, and gets back their
        REAL validate() verdicts — never truncated by an unrelated step
        elsewhere in the ladder, and always in ladder order regardless of
        *step_ids*' own order."""
        archive_step = next(
            (s for s in self._steps if getattr(s, "step_id", "") == _ARCHIVE_V1_STEP_ID), None
        )
        v1_retired = archive_step is not None and archive_step.validate().ok
        wanted = set(step_ids)
        out: list[StepReport] = []
        for s in self._steps:
            step_id = getattr(s, "step_id", "")
            if step_id not in wanted:
                continue
            r = self._validate_one(s, v1_retired=v1_retired)
            self._notify_validate_step(step_id, r.ok)
            out.append(r)
        return out

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
        # #504 B5 (acceptance review): `storage_layout_v2().root` is now
        # DATA_HOME itself (the nested v2/ folder was retired as the V2
        # root by the promote step above) — a blanket rmtree of "root" here
        # would wipe the whole user data directory, not a disposable copy.
        # This USED TO also assume a leftover `v2/` here is always
        # disposable-by-construction ("never anything's only copy") — false
        # the moment `PromoteV2RootStep.rollback()` (just run, above, in
        # reverse order) deliberately RECREATES real content under `v2/` as
        # part of a normal restore: the reviewed `engine_rollback` repro
        # showed a fully-green rollback leaving a unique file with zero
        # copies anywhere in DATA_HOME. Only remove `v2/` when it is
        # genuinely empty — a leftover shell from an interrupted operation,
        # never anything's only copy — matching #350's original "don't
        # leave `doctor --storage-layout` stuck reporting stale state
        # forever" concern without ever destroying real data to do it.
        _remove_if_empty_dir(self._data_home / "v2")
        return reports
