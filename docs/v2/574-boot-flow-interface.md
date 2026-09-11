# #574 boot flow — interface for frontend/mobile

Backend module: `src/agent_takkub/boot_flow.py` (pure, no Qt — passes
`engine-ui-separation`/`core-is-bottom-layer`-style separation; safe to
import from a GUI, a terminal renderer, or headless boot). Terminal
renderer + `takkub migrate run` CLI: `src/agent_takkub/boot_flow_terminal.py`.

Backs the 5-screen mockup (`scratchpad/boot-flow-design/*.dc.html` from the
design pass): **A** provider-update choice, **B** pre-migrate backup plan,
**C** progress, **D** success, **E** failure.

## 1. Provider updates (screen A)

```python
@dataclass(frozen=True, slots=True)
class ProviderUpdateItem:
    name: str            # "claude", "codex", ...
    label: str           # display name, e.g. "Claude Code", "Gemini (agy)"
    current: str | None  # None when not installed or unknown
    latest: str | None   # None when no known latest-version probe exists
    selected: bool        # true only when status == update_available
    status: str           # one of the PROVIDER_STATUS_* constants below

PROVIDER_STATUS_UP_TO_DATE      = "up_to_date"
PROVIDER_STATUS_UPDATE_AVAILABLE = "update_available"
PROVIDER_STATUS_NOT_INSTALLED   = "not_installed"
PROVIDER_STATUS_DISABLED        = "disabled"
PROVIDER_STATUS_NO_MECHANISM    = "no_mechanism"
PROVIDER_STATUS_CHECKING        = "checking"  # UI-only placeholder before check_provider_updates() returns; never returned by it
PROVIDER_STATUS_FAILED          = "failed"

def check_provider_updates(timeout_s: float = 20.0) -> list[ProviderUpdateItem]: ...
def remembered_provider_choice() -> dict | None: ...
def remember_provider_choice(choice: dict) -> None: ...
def run_provider_updates(
    items: list[ProviderUpdateItem],
    progress_cb: Callable[[ProviderUpdateItem], None] | None = None,
) -> list[ProviderUpdateItem]: ...
```

A provider with no known way to check its latest version (uv-managed like
kimi, or no update mechanism at all like gemini/cursor) reports
`latest=None` and status `up_to_date` — never flagged as needing an update
it can't even check for.

`remember_provider_choice(choice)` — `choice = {"mode": "ask"|"update_all"
|"skip"|"selected", "selected": [name, ...]}` (the `selected` key only
matters when `mode == "selected"`). Raises `ValueError` on an invalid
shape. Persisted at `config/boot-provider-choice.json` in the V2 layout.

## 2. Migration plan (screen B)

```python
@dataclass(frozen=True, slots=True)
class MigrationPlanSummary:
    backup_items: list[tuple[str, int, int, str]]  # (label, count, bytes, unit)
    estimated_bytes: int
    free_bytes: int
    backup_dir: Path
    promote_items: list[str]
    archive_items: list[str]
    junk_items: list[str]
    verify_steps: int = 0  # real ladder length (MigrationEngine.step_count()), e.g. 12 — NOT len(promote_items)

def plan_migration() -> MigrationPlanSummary | None: ...
```

`verify_steps` is the actual number of ladder steps `MigrationEngine.validate()` will walk — the same
count `MigrationOutcome.failed_step_total` (below) reports once a validate() pass actually runs. Use it
for any "step X/N" preview shown BEFORE migration starts; it can never drift from the real thing the way
a locally-computed guess (e.g. `len(promote_items)`, which is only `promote-v2-root`'s own candidate
count) could.

`unit` in each `backup_items` row is one of `"ไฟล์"` (files), `"โปรเจค"`
(projects), `"รายการ"` (generic items).

**#574 round11 item 1 (SCOPE)**: `backup_items` no longer lists every V1
artifact the ladder is ABOUT TO TOUCH — only what a later step might
OVERWRITE, MERGE INTO, or DELETE OUTRIGHT this same pass (a `v2/*` promote
candidate whose top-level destination already exists, a domain step's own
V2 target when it already holds pre-existing content, `version-marker`'s
target, and #504 item 5's delete-outright junk). A pure MOVE — a genuine
V1 top-level leftover `archive-v1-legacy` will archive, a non-colliding
`v2/*` promote candidate, `runtime/core` wholesale — is already fully
protected by that step's own copy-verify-then-prune WAL (+ `restore-v1`/
`rollback()`), so it's never duplicated into this backup and never appears
in `backup_items` at all; `PreMigrateBackupStep.skipped_move_only_items()`
lists those skipped items with a reason string, for a UI that wants to show
the full picture. This cut a real ~200k-file, multi-GB backup down to just
the handful of items actually at risk. `PreMigrateBackupStep.plan()`'s
`StepReport.detail` also carries `estimated_files`/`estimated_bytes` and,
above 500 MB or 20,000 files, a `warning` string — surface that in a
PreMigrate screen the same way any other `StepReport.detail` key is read.

`plan_migration()` returns `None` once this machine is fully on the V2
layout with nothing left to migrate — screens B–E have nothing to show at
that point (the "boot flow" is a one-time thing per machine).

## 3. Progress (screen C)

```python
@dataclass(frozen=True, slots=True)
class ProgressEvent:
    phase: int              # 1..5
    phase_label: str        # Thai label, see PHASES below
    phases_total: int       # always 5 today
    done: int | None        # None = indeterminate; never > total (clamped)
    total: int | None
    unit: str                # per-phase/per-entry word — see "unit" below, NOT hardcoded
    percent_overall: float   # 0..100, weighted across phases 1/2/4; never > 100
    eta_s: float | None      # seconds remaining in the CURRENT phase — see "eta_s" below
    log_line: str             # legacy combined string — DO NOT re-parse; use the structured fields below
    backup_dir: Path | None
    current_path: str | None = None  # short path, relative to DATA_HOME, of the entry/file currently being moved
    files_done: int | None = None    # #574 round11 item 3 — progress WITHIN one large directory entry
    files_total: int | None = None   # (added after this dataclass first shipped — read both defensively via getattr)
    log_operation: str | None = None  # machine token — a step_id, or "validate"/"info" (#574 round12, see "log structure")
    log_detail: str = ""              # free-form human explanation (Thai) — the 2nd log part, see "log structure"
    log_timestamp: str | None = None  # wall-clock "HH:MM:SS", local time — the 1st log part

PHASES = {1: "สำรองข้อมูล", 2: "คัดลอกขึ้นโครงใหม่", 3: "ตรวจสอบ", 4: "เก็บของเก่าเข้า archive", 5: "เสร็จ"}

def run_migration(progress_cb: Callable[[ProgressEvent], None] | None = None) -> MigrationOutcome: ...
```

Granularity ponytail (documented in `boot_flow.py`'s module docstring):
`ProgressEvent`s for phases 1/2/4 fire once per TOP-LEVEL item copy-verify
(one `v2/`, one `providers/`, ... — not once per underlying file within
one of those). Phase 3 (validate) now fires a start+done pair per domain
step (#574 round12 item 3, below) — still not a fine-grained WITHIN-step
counter (these write in one shot with nothing to report mid-step). Phase 5
(done) stays indeterminate (`done=None, total=None`).

`current_path` carries the same entry name `on_entry(step_id, name)` fires
with — set on EVERY event that has an item behind it (every phase 1/2/4
event past the initial kickoff), `None` for every event that doesn't
(phase 5, phase 3's domain-step events, and the very first phase-1 event
emitted before any entry has fired yet).

**#574 round11 item 3**: `files_done`/`files_total` fill the ONE gap the
ponytail above calls out — progress WITHIN one large directory entry's own
copy+verify, from a NEW `on_file_progress` observer (`(step_id,
entry_name, files_done, files_total, current_path)`, threaded through
`MigrationEngine`/the copy/archive/backup steps/`_copy_phase`/
`verify_copy.copy_verified`). Throttled to roughly every 200 files or every
2 seconds, whichever comes first, plus always once more on the final file
(`verify_copy._FileProgressThrottle`) — a real production rehearsal (real
prod data, ~200k files) previously sat on ONE entry for 17+ minutes with
zero signal on screen. `current_path` for these events is
`"<entry_name>/<file-relative-path>"`; `files_done`/`files_total` are
`None` for every event that isn't WITHIN a large directory entry (a
`file`-kind entry, an `on_entry`-only event, phases 3/5). Both fields were
added after this dataclass first shipped — a consumer must read them
defensively (`getattr(event, "files_done", None)`), the same way
`current_path` above already documents.

### done/total is always monotonic and bounded (#574 round12 item 2)

`promote-v2-root`/`archive-v1-legacy` each fire `on_entry` TWICE per
top-level entry over a full apply — once from their own copy phase, once
from their deferred prune phase (`_copy_phase`/`_prune_phase` in
`promote_v1.py` share the same bound `on_entry`). A real production
rehearsal observed `done` climb to exactly 2x `total` (9→18, 27→54)
before this fix, because every notify counted as new progress. Fixed two
ways, both defensive: `on_entry` now counts each entry NAME once per step
(a repeat notify for the same name refreshes `current_path`/`log_line`
but does not advance `done` again), and `emit()` unconditionally clamps
`done = min(done, total)` before building the event. A consumer can trust
`done <= total` and `0 <= percent_overall <= 100` on every single event,
never only at phase end.

### unit (#574 round12 item 6, R3-M4)

No longer hardcoded `"รายการ"` for every event. Phase 3 uses `"ขั้น"`
(steps). Phase 1/2/4 events use the SAME per-entry-name convention
`MigrationPlanSummary.backup_items`' own rows already use: `"projects"` →
`"โปรเจค"`, `"runtime/core"` → `"รายการ"`, everything else → `"ไฟล์"`. A
consumer showing "X/Y <unit>" now reads the same unit word the B-screen
plan table already promised for that same entry.

### eta_s (#574 round12 item 4, R3-M3)

Populated from the CURRENT phase's own `done`/elapsed-time rate — NOT a
whole-migration average (an early slow phase must never poison a later,
unrelated phase's estimate). Stays `None` until there is enough signal to
trust it: at least 2 seconds elapsed in the current phase, OR at least 5
events emitted in it, whichever comes first. Resets (clock and event
count) every time `phase` changes. Phases with no per-item `done`/`total`
(phase 5, and any event where `total` is falsy) always report `None`.

### log structure (#574 round12 item 7, R3-M5)

A UI must NEVER re-parse `log_line` itself — a prior review found that
fragile ("two NBSPs measuring 14px", "parser requires double spaces
while backend emits `step_id: name`"). Read the 4 parts as their own
fields instead:

1. `log_timestamp` — wall-clock `"HH:MM:SS"`, local time.
2. `log_operation` — a machine token: a step_id (`"promote-v2-root"`,
   `"archive-v1-legacy"`, `"pre-migrate-backup"`, or any of the 8 domain
   step ids) for an entry/step event, or `"validate"`/`"info"` for a
   plain text message from `run_boot_stage`'s own progress callback
   (`"validate"` when the message mentions validating, `"info"`
   otherwise).
3. `current_path` (already documented above) — the path/entry name, or
   `None` when there isn't one.
4. `log_detail` — the free-form human explanation (Thai). Empty string
   for a plain entry-copy event (the path IS the detail); the full
   message text for an `"info"`/`"validate"` event; `"n/m"` (as
   `files_done`/`files_total`) for a within-entry file-progress event.

`log_line` itself is kept only as the legacy combined string for the
terminal renderer, which has no need to re-split it — it is NOT a stable
machine-parseable format and its exact wording may change.

### Domain-step phase 3 progress (#574 round12 item 3)

The 8 V1→V2 "domain" steps (`readonly-registries`, `role-agent`,
`capability`, `project`, `state`, `credential-reference`,
`runtime-triage`, `core-internal-store`) write in one shot and used to
produce ZERO progress events — a real rehearsal's phase-3 event count was
0, so a wizard watching for phase 3 jumped straight from 2 to 4.
`MigrationEngine` now accepts an `on_step: Callable[[str, str], None]`
observer (`(step_id, "start"|"done")`), fired around EVERY ladder step's
own apply in both `apply()` and `apply_pending()`. `boot_flow.py` wires
this into a start+done pair per DOMAIN step only (backup/marker/promote/
archive already have their own phase 1/2/4 signal), positioned by the
step's fixed ladder index out of `plan.verify_steps` — the same "step
X/N" scheme `MigrationOutcome.failed_step_index/_total` already uses.
`unit` for these events is `"ขั้น"`; `current_path` is always `None`
(no single file/entry behind a domain step's own apply).

## 4. Outcome (screens D/E)

```python
@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    ok: bool
    duration_s: float
    promoted: list[str]
    archived: list[str]
    junk_deleted: int
    projects_count: int
    backup_dir: Path | None
    archive_dir: Path | None
    failed_phase: int | None
    failed_step: str | None
    error: str | None
    rolled_back: bool
    data_intact: bool
    log_paths: list[Path]
    validated_steps: int = 0             # count of validate() steps that passed
    failed_step_index: int | None = None  # 1-based position within the validate pass, e.g. 7
    failed_step_total: int | None = None  # total validate steps that pass ran over, e.g. 11
    previous_version: str | None = None   # #574 round12 item 8 — see below
```

`previous_version` (#574 round12 item 8, R3-M6): the "app" component
`version.json` held BEFORE this run's own version-marker overwrite — read
at the very start of `run_migration()`, before `run_boot_stage()` does
anything. `None` on a from-scratch install with no prior version.json (or
any read failure — fails open). This is genuinely the version a
`takkub migrate restore-v1` on this same run would put back — screen D's
downgrade note ("takkub migrate restore-v1 puts back `<previous_version>`
if needed") and screen E's failure context (currently running the OLD
build still, migration to the new one failed) both need it.

`failed_step_index`/`failed_step_total` are populated ONLY for a full
first-time apply that reached (and failed) its validate() pass — the
mockup's "ขั้นที่ 3 ตรวจสอบ (7/11) ไม่ผ่าน" wording. They stay `None` for
every other failure shape (an apply-stage failure never reached validate;
a routine `apply_pending()` boot has no single validate pass to report a
position within).

`ok=False` can happen even when `auto_migrate_boot`'s own internal action
string looks success-shaped (`"pending_applied"`) — see `boot_flow.py`'s
R6-H1 comment: a step that was already applied before and whose
re-attempt failed again is never auto-rolled-back by design, but
`MigrationOutcome.ok` still reports that failure honestly rather than
repeating a false positive.

## CLI

- `takkub migrate run [--providers ask|all|none|<csv>] [--remember]
  [--yes] [--json]` — the whole flow (A→E) as text screens, or `--json`
  for one JSON object per line (`type` in `provider`, `provider_update`,
  `provider_prompt_skipped`, `auto_confirmed`, `plan`, `progress`,
  `outcome`). `--json`'s stdout is EVERY line — a consumer can
  `json.loads()` each one unconditionally. #574 round12 item 1: `cli.py`'s
  own epilogue used to print an unconditional bare `"ok: migrate run
  finished"`/`"err: migrate run failed"` line AFTER all of the above —
  `cmd_migrate_run` now opts out of that (returns `quiet=True` with an
  empty `msg`) whenever `--json` is set; the exit code (0/1) still
  reflects success/failure exactly as before. Non-`--json` (text-screen)
  runs keep the human status line unchanged.
  - `--no-backup` was **removed** (#574 round11 R7-M2): it only ever
    printed a warning and never actually skipped the backup step — a
    flag that silently does nothing is worse than no flag at all.
  - `--providers ask` (the default) now actually asks — one provider at
    a time, `y`/`n`/`all`/`none` — on a real TTY. A non-interactive
    caller (`--json`, or no TTY at all) resolves to "none" instead of
    either hanging on a prompt or silently updating whatever
    `check_provider_updates()` happened to pre-select (#574 round11
    R7-M3); look for `type: "provider_prompt_skipped"` in `--json`
    output to see that this happened.
  - `--remember` now persists for EVERY resolved choice (`none` →
    `"skip"`, `all` → `"update_all"`, an explicit csv or an interactive
    "all"/per-item answer → `"selected"`), not only when something ended
    up selected (#574 round11 R7-M4) — and the default `ask` path now
    reads a remembered non-`"ask"` choice back via
    `remembered_provider_choice()` before ever prompting.
  - `--json` implies `--yes` (no human to answer the confirm prompt) —
    look for `type: "auto_confirmed"` to see when that happened rather
    than assuming it silently (#574 round11 R7-L1).
- `takkub migrate restore-v1 --from-backup <dir>` — restore from a
  `pre-migrate-backup/` directory instead of the normal `v1-archive-<ts>`
  generation walk (an alternate recovery path).
- `takkub migrate restore-v1 [--archive <ts>] [--json]` — unchanged
  contract (stdout is still exactly one JSON array of step reports) PLUS
  new progress: with `--json`, one JSON object per line on **stderr**
  (`type` in `restore_phase`, `restore_entry`) so per-phase timing can be
  captured (`--json 2>timing.jsonl`) without disturbing the existing
  stdout contract any downstream tooling already parses.
