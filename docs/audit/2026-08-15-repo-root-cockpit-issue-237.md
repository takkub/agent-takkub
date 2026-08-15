# #237: cockpit-bug issues silently fell back to local 100% of the time

## Root cause

`takkub issue new --cockpit-bug` (and `close`/`list`/`show`) resolved the
repo to detect via:

```python
detect_cwd = str(REPO_ROOT) if cockpit_bug else cwd
repo = _detect_repo(detect_cwd)   # gh repo view --json nameWithOwner
```

`REPO_ROOT = Path(__file__).resolve().parents[2]` is only the actual
agent-takkub **git checkout** in a dev checkout, where
`DATA_HOME == REPO_ROOT`. On an **installed build** (pip/npm wheel),
`config.py`'s `_resolve_data_home()` deliberately walks up to the `venv`
ancestor for `DATA_HOME`, but `REPO_ROOT` itself stays
`Path(__file__).resolve().parents[2]` — which, for an installed package,
lands on `…/venv/Lib` (`agent_takkub/issues.py` → `agent_takkub` →
`site-packages` → `Lib`). That directory has no `.git` at all — it's just
where the wheel unpacked its files.

Confirmed on the reporting machine:

```
REPO_ROOT = C:\Users\monch\.agent-takkub\venv\Lib
is git repo: False
```

`_detect_repo()` calls `gh repo view` in that directory, which fails with
"not a git repository" → `RuntimeError` → the **outer** `except` in
`new_issue`/`close_issue`/`show_issue`/`list_issues` sets `use_local = True`
and, per the comment at the time, **stays quiet**:

```python
# Repo was detected but the create failed (network/auth/rate
# limit) — this is the dangerous silent-divergence case worth a
# visible warning. A bare no-remote project (outer except) is a
# legit local-only mode and stays quiet.
```

That assumption ("bare no-remote = legit local-only mode") is correct for
`cockpit_bug=False` (an arbitrary user project genuinely might not have a
GitHub remote yet). It is **never** correct for `cockpit_bug=True` — the
cockpit tracker's whole purpose is to route to the real agent-takkub GitHub
repo, which always exists and always has a remote. Hitting the outer except
under `cockpit_bug=True` only ever means path resolution itself is broken —
so staying quiet there is what let every single cockpit-bug issue (29 of
them, on the reporting install) pile up in the local `.takkub_issues.json`
store, indistinguishable from success (`"ok: created #N"` prints either
way — the only tell was the `url` field starting with `local://`).

## Fix

`src/agent_takkub/issues.py`:

- **`_cockpit_repo_cwd() -> Path | None`** — the real resolver, in priority
  order:
  1. dev checkout (`DATA_HOME == REPO_ROOT`) → `REPO_ROOT` unchanged
     (zero behaviour change for every dev/test environment and CI).
  2. `AGENT_TAKKUB_COCKPIT_REPO` env var, if set and it has a `.git` —
     explicit override for installs that want to point the tracker at a
     specific checkout.
  3. the user's own `projects.json` `"agent-takkub"` project entry
     (`paths.main`), if that path exists and has a `.git`. Confirmed
     present and correct on the reporting install:
     `paths.main = "C:/Users/monch/WebstormProjects/agent-takkub"`, a real
     checkout with `git@github.com:takkub/agent-takkub.git` configured —
     this is *why* the fix works immediately for this exact machine without
     any user action.
  4. `None` if nothing resolves.
- **`_resolve_repo_for_op(cwd, cockpit_bug, op)`** — new shared helper
  (replaces four copy-pasted `detect_cwd`/`try`/`except` blocks in
  `new_issue`/`list_issues`/`close_issue`/`show_issue`). For
  `cockpit_bug=True` it calls `_cockpit_repo_cwd()` and — whenever that
  returns `None` **or** the resolved checkout's own `_detect_repo()` call
  fails — prints a loud, actionable `stderr` warning via
  `_warn_cockpit_repo_unresolved()` before falling back to local. The
  `cockpit_bug=False` path is byte-for-byte the previous behaviour: quiet
  fallback for a genuine no-remote project.
- **`_local_store_cwd(cwd, *, cockpit_bug)`** — simplified from a path-value
  comparison (`cwd == REPO_ROOT`, which is no longer a reliable signal now
  that `detect_cwd` can be a real checkout path instead of `REPO_ROOT`) to a
  direct flag: `cockpit_bug=True` always redirects local-fallback writes to
  `DATA_HOME`, regardless of what `_cockpit_repo_cwd()` resolved to.
- **`cmd_issue_new`/`cmd_issue_close`** — the CLI reply `msg` now appends
  `" (LOCAL ONLY — did not reach GitHub …)"` whenever the returned `url`
  starts with `local://`, so `takkub issue new` prints `ok: created #30
  (LOCAL ONLY — …)` instead of a bare `ok: created #30` that reads
  identically to a real GitHub issue.
- **`_scope_desc`** (used by `takkub issue list`'s header line) now prints
  the actually-resolved checkout path from `_cockpit_repo_cwd()` instead of
  the (potentially wrong) `REPO_ROOT`, and an explicit `UNRESOLVED` marker
  when nothing resolves.

No change to `cockpit_bug=False` (project-scoped) routing or to any
call site outside `issues.py` — `new_issue()`/`list_issues()`/
`close_issue()`/`show_issue()` keep their existing signatures and return
types (`update_worker.py`, `auto_issue_capture.py` untouched).

## Item 4 — local backlog sync

Checked both plausible local-store locations on the reporting install:

- `~/.agent-takkub/.takkub_issues.json` (the correct `DATA_HOME` location
  per the pre-existing #12 redirect) — **0 entries**, confirming the 29
  backlog issues mentioned in #237 were already migrated to GitHub by hand
  (GitHub issues #193–#242, per the task brief) before this fix landed.
- `~/.agent-takkub/venv/Lib/.takkub_issues.json` (the old buggy
  `REPO_ROOT`-as-cwd path, in case any file had leaked there before the #12
  redirect existed) — does not exist.

No local issues remain stranded, so **no sync/migration command was built**.
If a future install accumulates a local backlog again (e.g. `gh` down for a
stretch, or `_cockpit_repo_cwd()` genuinely unresolved), the existing
`takkub issue list` already surfaces it via the issue #174 backlog-merge
logic (`⚠ … unreconciled local issue(s) found in …`), and the fix above
means new cockpit-bug ops warn loudly the moment this happens rather than
staying silent — so a large hidden backlog like #237's shouldn't be able to
reaccumulate unnoticed. A dedicated bulk-sync command was judged unnecessary
scope for this fix; if the backlog situation recurs, add one then with real
data to design against instead of speculatively now.

## Verification

- `tests/test_issues.py`: 71 passed (9 new — `_cockpit_repo_cwd()`
  resolution order incl. dev-checkout/env-override/projects.json/
  unresolved cases, the loud-warning regression test, `_local_store_cwd`
  flag-based redirect, and the CLI `LOCAL ONLY` tag).
- `ruff check` / `ruff format --check`: clean.
- `lint-imports`: 25/25 contracts kept.
- Confirmed end-to-end against the real installed cockpit's on-disk state
  (read-only checks, no mutation): `REPO_ROOT` resolves to
  `…\.agent-takkub\venv\Lib` (no `.git`), while
  `projects.json["projects"]["agent-takkub"]["paths"]["main"]` resolves to
  the real checkout with a `.git` present — exactly the case
  `_cockpit_repo_cwd()`'s step 3 now picks up.
