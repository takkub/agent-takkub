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
    done: int | None        # None = indeterminate (phases 3/5 have no per-item count)
    total: int | None
    unit: str                # "รายการ" today
    percent_overall: float   # 0..100, weighted across phases 1/2/4
    eta_s: float | None      # not populated yet (ponytail — see module docstring)
    log_line: str
    backup_dir: Path | None
    current_path: str | None = None  # short path, relative to DATA_HOME, of the entry/file currently being moved
    files_done: int | None = None    # #574 round11 item 3 — progress WITHIN one large directory entry
    files_total: int | None = None   # (added after this dataclass first shipped — read both defensively via getattr)

PHASES = {1: "สำรองข้อมูล", 2: "คัดลอกขึ้นโครงใหม่", 3: "ตรวจสอบ", 4: "เก็บของเก่าเข้า archive", 5: "เสร็จ"}

def run_migration(progress_cb: Callable[[ProgressEvent], None] | None = None) -> MigrationOutcome: ...
```

Granularity ponytail (documented in `boot_flow.py`'s module docstring):
`ProgressEvent`s for phases 1/2/4 fire once per TOP-LEVEL item copy-verify
(one `v2/`, one `providers/`, ... — not once per underlying file within
one of those), and NOT AT ALL for the 5 simpler V1→V2 domain steps that
write in one shot. Phases 3 (validate) and 5 (done) are indeterminate
(`done=None, total=None`), driven off text messages, not a real counter.
A fully granular byte-level meter would need every domain step wired the
same way the copy/archive/backup steps now are — out of #574's scope.

`current_path` carries the same entry name `on_entry(step_id, name)` fires
with — set on EVERY event that has an item behind it (every phase 1/2/4
event past the initial kickoff), `None` for every event that doesn't
(phases 3/5, and the very first phase-1 event emitted before any entry has
fired yet).

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
```

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
  `outcome`).
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
