# autoskills installer bridge — 2026-08-13

New module: `src/agent_takkub/autoskills_installer.py`. Bridges the
[autoskills](https://www.autoskills.sh) CLI (scans a project's
`package.json`/config, guesses tech stack, fetches matching skills from the
`skills.sh` registry into `.claude/skills/<name>/`) into the cockpit.

> **Update (same day, review round 2):** the initial cut had three real
> gaps in `install()`'s logic — a silent-overwrite blind spot, a path-escape
> guard that only checked top-level entries, and an unselected-skill window
> where files briefly existed for real on disk before being deleted again.
> All three are fixed below. See "Round 2 fixes" for detail on each.

## Why two functions, not one

A skill written to `.claude/skills/` is a prompt every pane in the project
auto-loads — installing one is equivalent to importing external content
straight into the team's shared context. So the API is split so the UI
*must* show the user what would land before anything is written:

- **`preview(project_root, timeout=60.0) -> PreviewResult`** — runs
  `autoskills --dry-run --agent claude-code` only. Writes nothing, ever.
  Safe to call speculatively/repeatedly.
- **`install(project_root, selected_names, timeout=120.0) -> InstallResult`**
  — the ONLY function that writes files. **Must be called only after the
  user has explicitly confirmed a selection in the UI.** Never call it
  automatically or as a side effect of `preview()`.

Both are synchronous, blocking calls (subprocess + wait, bounded by
`timeout`) — the docstrings on both functions say explicitly: call from a
worker thread, never the Qt main thread. Threading itself is the UI layer's
job; this module doesn't spawn one.

## API

```python
@dataclass(frozen=True)
class SkillCandidate:
    name: str
    source: str = ""          # "author › techs" / "author" / "org/repo/skill", "" if none parsed

@dataclass(frozen=True)
class PreviewResult:
    ok: bool
    stack: list[str]          # detected tech stack, best-effort parsed
    skills: list[SkillCandidate]
    raw_output: str           # full stdout+stderr, always kept for debugging
    error: str = ""
    no_skills_for_stack: bool = False  # True only if the CLI itself said "no skills" (see round 3)

@dataclass(frozen=True)
class InstallResult:
    ok: bool
    written: list[str]          # names actually kept on disk (== selected_names ∩ what the CLI wrote)
    skipped: list[str]          # names the CLI wrote but the user did NOT select
    overwritten: list[str]      # pre-existing entries whose content changed (selected collision,
                                 # or an unselected collision that WAS restored — see round 2)
    overwrite_failed: list[str] # pre-existing entries overwritten AND unselected AND restore failed
                                 # (real data loss — forces ok=False)
    staging_used: bool          # True if install ran against an isolated staging mirror
    raw_output: str
    error: str = ""

def preview(project_root: str | Path, timeout: float = 60.0) -> PreviewResult: ...
def install(project_root: str | Path, selected_names: Iterable[str], timeout: float = 120.0) -> InstallResult: ...
```

## CLI resolution (cross-platform)

`_resolve_autoskills_cmd()`:
1. A direct `autoskills` (or `autoskills.cmd` on Windows, checked first —
   `subprocess.run(shell=False)` can't reliably launch a bare `.cmd` name via
   `CreateProcess`) on `PATH` wins.
2. Otherwise falls back to `npx --yes autoskills@latest`, resolving
   `npx`/`npx.cmd` the same way.
3. Neither found → both `preview()` and `install()` return `ok=False` with a
   readable Thai error (`ไม่พบ autoskills และไม่พบ npx บนเครื่องนี้ — ติดตั้ง
   Node.js ก่อน`). Never raises, never hangs.

All resolution goes through `shutil.which` — no hardcoded paths, so this
works unmodified on Windows and macOS.

## Round 2 fixes

### 1. Silent-overwrite blind spot (now: detected, and restored when unselected)

**The bug:** `install()` computed `new_entries = after_names - before_names`
(a set diff of `.claude/skills/` entry *names*). If `autoskills` overwrote a
pre-existing entry — same name already on disk — that name existed in both
`before` and `after`, so it never showed up in `new_entries` at all. External
content silently replaced local content: not reported, not rolled back, and
the user never selected it.

**The fix:** every pre-existing entry is now fingerprinted with a
content-based signature (`_entry_signature` — SHA-256 over file bytes,
recursively for directories; deliberately NOT mtime/size, which are flaky
across filesystems and don't change on some "preserve timestamps" writers)
*before* the CLI runs, and backed up to a temp dir (`_backup_entry`). After
the run, every name present both before and after is re-fingerprinted:
- Signature unchanged → untouched, ignored.
- Signature changed AND the user selected that name → expected (they asked
  for it) → reported via `InstallResult.overwritten`, left as-is.
- Signature changed AND the user did NOT select that name → the exact bug
  scenario → restored from the pre-run backup (`_restore_entry`) and
  reported via `overwritten`; if the restore itself fails,
  reported via `InstallResult.overwrite_failed` instead and `ok` is forced
  to `False` — this is real data loss and must never be swallowed.

This detection lives in `_install_direct()`, the fallback path (see #3
below) — the staging path structurally avoids the blind spot a different
way: because the CLI never runs against the real `.claude/skills/` at all,
an unselected same-name collision simply never gets copied out of staging in
the first place, so there's nothing to detect or restore.

### 2. Path-escape guard only checked top-level entries

**The bug:** `_escaped_entries` resolved and checked only
`skills_dir / name` for each newly-written top-level entry. A symlink
*nested* inside an otherwise-legitimate directory (e.g.
`skill-a/assets/evil -> C:\Windows`) was invisible to that check — the
top-level `skill-a` is a real directory, not a symlink, so it passed.

**The fix:** `_escaped_entries` still checks the top-level entry first, then
— if it's a real (non-symlink) directory — walks it with
`os.walk(..., followlinks=False)` and checks every nested file/dir name the
same way. `followlinks=False` means a symlinked subdirectory is inspected as
a leaf (its own target checked) rather than recursed into, so this can't
loop or double-count. Any escape at any depth flags the whole top-level
entry name, preserving the existing all-or-nothing rollback behavior.

### 3. Unselected skills briefly existed for real on disk

**The bug:** `install()` let `autoskills` write everything straight into
the real `.claude/skills/`, then deleted whatever the user hadn't selected.
Between those two steps, unselected external content was live on disk in
the actual project — readable by any concurrently-running pane — and if the
process died mid-way (crash, kill, timeout), it stayed there permanently.

**The fix:** `install()` now tries `_build_staging_mirror()` first — an
isolated copy of `project_root` built INSIDE it (guaranteeing the same
filesystem volume) via hardlinks for files (near-zero extra disk — the same
technique this codebase's graft staging mirror already uses) and real
directories, walked with `followlinks=False`. `autoskills` runs there
instead of the real tree; only entries the user selected are ever copied
into the real project afterward (`_install_via_staging`) — an unselected
entry is simply never copied, full stop, so it never touches real disk
regardless of what happens or how long the run takes.

**Two deliberate exclusions from the mirror, both load-bearing:**
- **`.git/`** — irrelevant to `autoskills` per this module's own docstring
  (it scans manifests/config, not VCS state).
- **The real `.claude/skills/`** — NOT mirrored at all; staging gets an
  empty one instead. This is a correctness requirement, not just tidiness:
  hardlinking shares the underlying file bytes, so if `autoskills` opened a
  hardlinked pre-existing skill file for an in-place rewrite, it would
  mutate the REAL project's file too (only deleting/replacing the directory
  *entry* is isolated through a hardlink; editing content through it is
  not). Because staging starts empty, "new entries" in the mirror is simply
  everything the CLI wrote — no diffing needed on the staging side. The
  overwrite check from fix #1 above is instead applied when copying a
  *selected* staged entry back into the real project: if that name already
  existed for real, it's reported via `overwritten`.

**⚠️ Unverified assumption — flagged, not hidden:** this staging approach
rests on the assumption that `autoskills`' stack detection only needs the
mirrored file tree — not, say, the literal real absolute path, or something
that requires `.git` to exist, or a network probe keyed off project
identity that a mirror can't replicate. **There is no network access in
this sandboxed environment to run the real CLI and confirm this.** Per
the "don't guess" instruction this was built under: `install()` treats ANY
failure to build the mirror as non-fatal and falls back to the ORIGINAL
direct in-place approach (`_install_direct`, fix #1's restore logic still
applies there) rather than either guessing silently or breaking installs
outright. `InstallResult.staging_used` tells the caller which path actually
ran, so this is inspectable rather than hidden. **If/when the real CLI is
available to test against, verify stack detection is unaffected when run
from a hardlink mirror before trusting `staging_used=True` results in
production.**

## Safety properties (carried over + updated)

- **No silent auto-install.** `install()` writes files; its docstring says
  explicitly it must be gated behind user confirmation. Enforcement is a
  docstring contract, same as every other install-side function in this
  codebase (`plugin_installer.install_plugin`, etc.) — there is no
  code-level lock, because the "confirm first" boundary is a UI-flow
  concern, not something a backend function can verify on its own.
- **Selective install, with no CLI filter flag.** `autoskills` documents no
  per-skill filter flag. On the (default) staging path, only selected
  entries are ever copied into the real project — unselected ones never
  touch it. On the direct/fallback path, everything is written for real
  first (same as before round 2) and unselected entries are deleted again.
- **Silent overwrite is now detected and, when unselected, restored** — see
  fix #1.
- **Path-escape guard covers nested symlinks, not just top-level** — see
  fix #2.
- **No missing-runtime crash.** Both entry points check
  `_resolve_autoskills_cmd()` first and return a structured error instead of
  raising when neither `autoskills` nor `npx` is on `PATH`.
- **Bounded, non-blocking subprocess calls.** Every invocation goes through
  `_run()`: `timeout=` enforced, `stdin=subprocess.DEVNULL` (a stray
  interactive prompt fails fast via `TimeoutExpired` instead of hanging
  forever), `creationflags=SUBPROCESS_NO_WINDOW` (no console flash on
  Windows), and `npm_config_yes=true` / `GIT_TERMINAL_PROMPT=0` set via
  `setdefault` — the same two blocking-prompt-prevention vars
  `pane_env._apply_non_interactive_env` sets for panes, applied here since
  the `npx` fallback path may need to fetch the package on first run.

## Known limitation: output parsing is best-effort

`autoskills` has no documented `--json` output mode (confirmed via
`--help` on v0.3.6 — see Round 3 below). `_parse_preview_output` does a
heuristic text parse to fill in `PreviewResult.stack` / `.skills`.
`raw_output` is always populated regardless of parse success, specifically
so the UI has a fallback to show the user the literal CLI output if the
parsed fields look wrong or come back empty (round 3 below wires this
fallback all the way into the UI, not just the data model).

This is the same class of limitation as the staging mirror's unverified
assumption above: both are best-effort behavior, and both fail safe
(parsing falls back to `raw_output`; staging falls back to the direct
path). **Round 3 below replaced the parser with one verified against the
real CLI**, closing the specific gap this section originally flagged — the
staging-mirror assumption above is still unverified and should be
re-checked the same way if/when it becomes suspect.

## Round 3 fix (2026-08-13, same day — QA-blocking bug)

**The bug:** QA ran the real CLI for the first time with network access
(`docs/qa/2026-08-13-autoskills-newrole-gate.md` §4.1) and found
`_parse_preview_output` returned `stack=[]`, `skills=[]` against real
`autoskills@0.3.6` output — even though the CLI detected a stack and
proposed 8 skills, exit 0. Every real user hitting "Auto-detect skills"
saw "autoskills ไม่พบ skill ที่เข้ากับ stack ของโปรเจคนี้" (no matching
skill), unconditionally. Root cause: the parser was built (previous
round, no network access) against a **plausible but wrong guess** at the
output format — `key:` header lines + `-`/`*`/`•` bullets. The real CLI
uses box-drawing (`◆`) prompts and a numbered `author › skill-name` list
instead; neither regex ever matched.

**Verification before fixing — no `--json` flag:** `npx autoskills@latest
--help` was run first to check for a machine-readable output mode before
writing a new text parser. None exists (`-y/--yes`, `--dry-run`,
`--clear-cache`, `-v/--verbose`, `-a/--agent`, `-h/--help` — that's the
complete flag set on v0.3.6). Text parsing is genuinely necessary here,
not a shortcut.

**Real output captured live**, `npx autoskills@latest --dry-run --agent
claude-code` against this project root (v0.3.6, 2026-08-13) —
byte-identical copies saved as fixtures for regression testing:

```
   Scanning project...[K   ◆ Detected technologies:
     ✔ Node.js   ✔ Bash      ✔ Python
     ✔ Pytest
   ◆ Skills to install (8)
    1. wshobson › nodejs-backend-patterns               ← Node.js
    4. inferen-sh › python-executor (security check ⚠)  ← Python
    5. wshobson › python-testing-patterns               ← Python, Pytest
    6. aj-geddes › nodejs-express-server
   Agents: claude-code
   --dry-run: nothing was installed.
```

Notable real-world wrinkles the new parser had to handle, none of which
the old one anticipated:
- Headers aren't standalone lines — `◆ Detected technologies:` is glued
  onto the tail of `   Scanning project...[K` (a lost/partial ANSI
  clear-line escape), so headers are matched by keyword search anywhere
  in a line, not by line-start position.
- Multiple stack entries share one line, space-separated
  (`✔ Node.js   ✔ Bash      ✔ Python`), some marked `✔` (matched) vs. `●`
  (detected but no skill combo) — both count as stack.
- Skill entries are numbered, not bulleted, and carry no URL — the
  identifying token is `author › skill-name`; a trailing `← Tech, Tech2`
  is optional (absent on combo-only entries like `nodejs-express-server`
  above) and per-entry annotations like `(security check ⚠)` or
  `(installed)` (seen when a prior run's `skills-lock.json` already
  exists) can appear inline and must not corrupt the name.
- A `◆ Detected combos:` section (e.g. `⚡ Node.js + Express`) can appear
  between the stack and skills sections — must be excluded from both, not
  merged into stack.
- The word "security check" (singular) appears **inline inside a skill
  entry** as an annotation, while the section header is "Security check**s**"
  (plural) — an early version of the new header-matching regex conflated
  the two and truncated the skills list after the first flagged entry;
  caught by the multi-skill fixture test, fixed by requiring the plural.

**The fix** (`_parse_preview_output`, `_split_sections`,
`_NUMBERED_SKILL_RE`, `_TECH_TOKEN_RE` in `autoskills_installer.py`):
rewritten against the verified real format above. ANSI escape sequences
are stripped first (`_ANSI_RE`) as defense-in-depth even though the
captured samples show them already partially lost in transit. A
belt-and-suspenders fallback (`_INSTALLED_PATH_RE`) also recognizes the
CLI's *real-install-completion* listing format (`✔ org/repo/skill-name`,
no `←`/numbering) in case that renderer is ever reached instead of the
dry-run one — confirmed to exist and differ from the dry-run format by
deliberately triggering it (see "also investigated" below).

**Honest-failure fix (task requirement #4):** `PreviewResult` gained
`no_skills_for_stack: bool`, set by a new `_no_skills_reported()` helper
that checks for the CLI's own "No skills available for your stack yet."
text. `_on_autoskills_preview_ready` in `settings_window.py` now branches
on it: `skills == [] and no_skills_for_stack` → the existing "not found"
message (genuine negative, CLI said so explicitly); `skills == [] and not
no_skills_for_stack` → a *different* message showing `raw_output` verbatim
(capped at 4000 chars), since an empty parse with no explicit CLI
negative most likely means the parser didn't recognize the output —
exactly the class of bug this round fixes, now made visible instead of
silently misreported if it recurs on a future CLI version.

**Also investigated: a suspected `--dry-run` safety bug — ruled out, root
cause was tester error, not the CLI or this module.** While capturing
fixtures, a *different* shell command (typo'd `--version`, which isn't a
flag `autoskills` documents — the CLI falls through to its default
non-interactive install behavior on an unrecognized flag) briefly wrote 8
real skill directories into this worktree's `.claude/skills/`. Before
concluding anything, this was reproduced deterministically in three
isolated scratch projects: (1) the real `--dry-run --agent claude-code`
invocation — the one `preview()` actually uses — never wrote to disk in
any of ~6 repeated attempts, including against a stack matching 8 skills;
(2) the exact same typo'd `--version` invocation reliably reproduced a
real, unconfirmed install in a fresh scratch project on the first try.
This confirms `preview()`'s existing invocation (always `--dry-run
--agent claude-code`, never anything else) is safe as documented — no
change was made to it. The accidentally-written files (in the real
worktree and in scratch dirs) were all cleaned up before this round's
real work began; `git status` was clean before any parser code was
touched.

**Tests:** `tests/fixtures/autoskills/*.txt` — four byte-identical copies
of real `--dry-run --agent claude-code` output (v0.3.6, captured
2026-08-13): `dry_run_v0.3.6_multi_skill.txt` (8 skills, the exact
transcript QA fed the old parser), `dry_run_v0.3.6_no_match.txt` (genuine
"no skills for this stack" case), `dry_run_v0.3.6_single_line_stack.txt`
(3 stack entries on one line, mixed `✔`/`●`), `dry_run_v0.3.6_with_combos.txt`
(exercises the combos-section exclusion and a skill entry with no `←`
suffix). Each fixture is asserted against directly (`_parse_preview_output`)
and end-to-end through `preview()` with `subprocess.run` mocked to return
the fixture text — so a future CLI format change fails these tests
automatically instead of requiring another manual live run to catch. Old
tests built against the fictional `key:`/bullet format were replaced with
equivalents against the real numbered format; the UI-side test for the
empty-skills case was split in two (genuine-negative vs. unparsed) to
match the new branching, since running it unmodified would have called
the real un-mocked `QMessageBox.warning` and hung the test process on a
live modal dialog — caught by actually running the suite, not by
inspection.

Run: `pytest tests/test_autoskills_installer.py tests/test_settings_window.py -q -k "autoskills or Autoskills or parse or Parse"`
— all green; `ruff check` clean on every touched file.

## Tests

`tests/test_autoskills_installer.py` — 39 tests (2 skip gracefully on a
machine where creating symlinks isn't permitted; everything else always
runs), all subprocess calls mocked, no network:

- CLI resolution (direct binary / npx fallback / Windows `.cmd` shim
  priority / nothing available).
- `preview()`: exact argv (`--dry-run --agent claude-code`, confirms `--yes`
  is NOT passed), non-interactive env vars, timeout/OSError/non-zero-exit
  handling, output parsing (stack + skills + source URLs), empty-output
  case.
- `install()` guard clauses: empty selection short-circuits without calling
  the CLI, missing-runtime error.
- `install()` direct/fallback path (staging forced off via patching
  `_build_staging_mirror` to return `None`): exact argv
  (`--yes --agent claude-code`), selective-write-and-cleanup behavior
  (verified via real tmp-dir filesystem state), timeout/OSError/non-zero-exit
  handling, path-escape rollback, **selected-name collision reports
  `overwritten` without restoring, unselected-name collision IS restored
  and reported, restore-failure and backup-failure both surface via
  `overwrite_failed` and force `ok=False`**.
- `install()` staging path (default): confirms the CLI's `cwd` is the
  staging mirror (not the real project), confirms an unselected staged
  entry never appears in the real project at all, confirms staging cleans
  up after itself (no leftover `.autoskills-staging-*` dir), selected-name
  collision against a pre-existing real entry reports `overwritten`,
  path-escape rollback.
- `_build_staging_mirror`: confirms hardlinking (same inode) for a mirrored
  file, confirms `.git` and `.claude/skills` are excluded, confirms
  `None` on root-creation failure.
- `_entry_signature`: changes when file/nested-file content changes, stable
  when untouched.
- `_escaped_entries` — path-escape guard, tested directly (no real symlinks
  needed for the top-level case): normal dir not flagged, top-level symlink
  flagged (real symlink where the OS permits it), **nested symlink at depth
  flagged**, nested non-symlink dir not flagged.
- `_parse_preview_output` edge cases (no headers, empty string).

Run: `pytest tests/test_autoskills_installer.py -q` — 37 passed / 2 skipped
locally.

## Not done (out of scope for this task)

No UI wiring (New Project Wizard step, confirm dialog, worker-thread
plumbing) — this task was the backend bridge module only.
