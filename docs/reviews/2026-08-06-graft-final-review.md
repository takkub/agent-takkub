# graft 1.0.48 — pre-release security + correctness review

**Scope:** the uncommitted working tree (24 files, ~1,200 insertions) on top of `31e3599`.
**Reviewer:** reviewer pane, 2026-08-06. Every claim below comes from reading the code in the
working tree or from a probe I ran on this machine. CHANGELOG / done-reports were **not** trusted
as evidence for any closure verdict.
**Predecessor:** `docs/reviews/2026-08-05-graft-crossos-audit.md` (H1–H3 blockers, M1–M6, L1–L5).

**Verdict: 1 HIGH must be fixed before publish. All 3 prior blockers are genuinely closed.**
New: 1 high · 5 medium · 8 low. Targeted suite green (201 passed:
`tests/test_graft_store.py test_graft_autobuild.py test_graft_chip.py test_mcp_bridge.py
test_disk_usage.py test_idle_watchdog.py`).

---

## 1 · Closure audit of the 2026-08-05 findings — verified against code, not reports

| # | Prior finding | Verdict | Evidence I gathered |
|---|---|---|---|
| **H1** | auto-build indexes gitignored trees, 507 MB / project | **CLOSED** | `graft_autobuild._run_build:409-421` builds the **staging mirror**, never *target*; `_git_nonignored_files:245-284` uses `git ls-files -z --cached --others --exclude-standard`, returns `None` (⇒ skip) when not a work-tree. **Measured this repo's live store today: `graft-graphs` 531 files / 43.4 MB + `graft-staging` 951 files / 33.5 MB** vs. 4954 files / 506.8 MB yesterday. |
| **H2** | Windows MAX_PATH — 3080 files > 259 chars | **CLOSED** | `graft_store._KEY_HEX_LEN = 16` (`:74`), applied in `_instance_key:123` + `graph_key:220`. **Measured: longest path in `graft-graphs` = 139, in `graft-staging` = 145, files > 259 chars = 0** (was 308 / 3080). Completion marker `built.json` (`mark_build_complete:347`, written LAST only on exit 0, `:435-437`) replaces the `.graph`-exists check at `ensure_project_graph_async:565` and `shared_dev_tools.py:461`. |
| **H3** | MCP claims "this repo is indexed" with no graph | **CLOSED** | `shared_dev_tools.py:461-467` — `del servers["graft"]` when the CLI is missing **or** `not has_completed_build(store)`. Applies to codex too: `mcp_bridge._role_mcp_servers:250-275` delegates to the same function. See L6 for the residual. |
| **M1** | macOS case-fold missing | **CLOSED** in `_normalize_for_key` | `graft_store.py:103` → `if sys.platform in ("win32", "darwin")`. **But two other call sites still use `os.name == "nt"` — see M5 below, which is a real macOS race, not cosmetic.** |
| **M2** | `expanduser()` at 2 of 3 call sites | **CLOSED** | `graft_store._normalize_for_key:101` — `target.expanduser().resolve()` at the single choke point. |
| **M3** | worktree panes get an empty graph | **CLOSED** | `shared_dev_tools.py:454-461` computes `under_worktree` from `worktree_root(project)` and drops the server; `orchestrator.py:3282-3283` skips resync for worktree panes. `_pane_state` keying verified consistent (`f"{project}::{role}"`, same `key` as the rest of `_check_idle_teammates`). |
| **M4** | timeout leaves partial graph + orphan `node.exe` | **CLOSED** | `_kill_orphan_tree:372-398` → `taskkill /PID <pid> /T /F`, called before `proc.kill()` on `TimeoutExpired` (`:430-434`), Windows-gated. Partial-graph stickiness is covered by H2's marker. |
| **M5** | `GRAFT_STORE_ROOT` ignores `AGENT_TAKKUB_HOME`, frozen at import | **CLOSED** | `_graft_store_base:126-139` honours `DATA_HOME` except the `DATA_HOME == REPO_ROOT` case; PEP-562 `__getattr__:179-184` + `_store_root:187-200` (`sys.modules[__name__].GRAFT_STORE_ROOT`, so `LOAD_GLOBAL` can't bypass the hook). No module in `src/` captures the name at import — every reference is attribute access on the module object (checked all 20 hits under `src/`). **"Unwritable `~` now surfaces" is real, not a claim:** `_run_build:404-407` returns `(False, …)` on `store.mkdir` `OSError` → `_build_one:472-473` records it in `_last_build_failed` → `get_build_status()` → chip goes amber. |
| **M6** | first-run has zero feedback | **CLOSED** | `get_build_status:483-509` + `status_header._refresh_graft_chip:750-788`. Caveats in M2/M3/M4 below. |
| **L1** | `engines.node >= 18` vs graft's 20 | **DOCUMENTED, not fixed** | `package.json:42-44` still `">=18"`; `README.md` now states the Node ≥ 20 requirement in two places. The audit's ask was README/install docs, so this is met — but the manifest and the docs now disagree in-repo. |
| **L2** | disk report spans two roots under one label | **CLOSED** | `disk_usage.disk_report:839-845` adds `graft_store_root` + `graft_store_root_outside_data_home`; `cli.py:406-415` prints the note. |
| **L4** | stale `conftest.py` comment | **CLOSED** | `tests/conftest.py:158-190` rewritten; also adds per-test resets for `_building` / `_debounce_timers` / `_last_build_failed` / `_last_live_resync` / `_version_cache`. |
| **L5** | non-code folders walked wholesale | **CLOSED** | Same `_git_nonignored_files` → `None` gate as H1. |
| **minimal-code** | `write_store_manifest` on every pane spawn | **CLOSED** | `shared_dev_tools.py` no longer imports or calls it (AST-checked: imports only `graft_cli_path, graph_store_dir, has_completed_build, staging_dir_for`). |

Nothing was claimed closed that I found still open.

---

## 2 · New findings

### H1 — HIGH · a git **submodule** (or any unstageable `ls-files` entry) makes trigger 4 rebuild the whole graph forever, every ~15 s

`graft_autobuild.py:591-613` (`_has_new_files`) + `:669-682` (`resync_staging_only._do`).

`_git_nonignored_files` returns every path `git ls-files` reports. A **git submodule is reported as a
single entry, but it is a directory on disk**. `_stage_files:305-322` tries `os.link` then
`shutil.copy2`; both raise on a directory, so it hits `continue` and the entry is *never* staged.
`_has_new_files` then compares git's list against what the mirror actually holds and returns
`True` — permanently.

Proven on this machine with a purpose-built repo (`git submodule add`, scratchpad):

```
rel_paths = ['.gitmodules', 'r.txt', 'vendorsub']
staged    = ['.gitmodules', 'r.txt']
has_new_files after sync  = True
has_new_files after 2nd sync = True     # never converges
```

Consequence: for any working pane whose cwd is such a repo, the idle watchdog's 5 s tick
(`orchestrator.py:3282-3289`) → `resync_staging_only` (15 s per-dir throttle) → `_has_new_files`
True → **`_spawn_build` → a full `graft build`**, restarting roughly every 15 s or every
build-duration, whichever is longer, for the pane's entire lifetime. It holds one of the 3
semaphore slots continuously and rewrites the whole store each pass — precisely the runaway-node /
CPU-churn class this project already has an incident history for (CLAUDE.md: ~3170 node procs).

Same failure mode is reached by any other permanently-unstageable entry: a **broken symlink**, a
**dir symlink**, a file whose staging path exceeds MAX_PATH, or (POSIX only, not reproduced here) a
non-UTF-8 filename — `_git_nonignored_files:283` decodes with `errors="replace"`, so the mangled
name can never match a real file.

**Fix (either, or both):**
- Make `_stage_files` report what it actually staged and have `_has_new_files` compare against
  *stageable* paths, not git's raw list; **or**
- give the escalation a memory: track per-directory `(rel_paths_hash, escalated_at)` and don't
  re-escalate for a set that already failed to converge once (log it instead).
Cheapest correct guard today: in `_stage_files`, skip entries where `src.is_dir()` and record
permanently-unstageable rels in a per-target skip set that `_has_new_files` subtracts.

---

### M1 — MEDIUM · `_sync_staging` re-links **every** file every cycle; the docstring claims a delta

`_LIVE_RESYNC_MIN_INTERVAL_S`'s comment (`graft_autobuild.py:616-630`) says trigger 4 is
*"a `git ls-files` + hardlink-relink of the (usually small) **delta**"*. `_sync_staging:325-369`
computes no delta at all — it walks the mirror twice, then calls `_stage_files` over the **entire**
`rel_paths` list, and `_stage_files:313-322` unconditionally `unlink()`s and re-`link()`s each one.

Measured on this repo (739 non-ignored files, **nothing changed**):

```
git ls-files             :  41 ms
_staging_relpaths walk   :  10 ms
_sync_staging (no changes): 264 ms   (2nd run: 270 ms)
```

≈315 ms of pure disk churn per cycle, per working pane directory, every 15 s, indefinitely. It runs
on a background thread so the UI is safe, but a 10k-file monorepo scales this to ~3.5 s/cycle and
3–4 panes would keep the disk busy near-continuously for no benefit.

Secondary risk (not verified): re-linking bumps the containing directory's mtime every cycle. If
graft's `probeDrift` consults directory mtimes at all, this would fire a graph refresh on every
query — the opposite of what the staging mirror exists to prevent.

**Fix:** in `_stage_files`, `os.stat` both sides first and skip when `st_ino` matches (hardlink case,
exact) or `(st_mtime_ns, st_size)` matches (copy fallback). Drops the 264 ms to ~50 ms of stats.

---

### M2 — MEDIUM · the chip reports **queued** builds as "building": at boot it will say "46 now"

`_build_one:461-465` adds the key to `_building` **before** `with _build_semaphore:` (`:467`).
`_MAX_CONCURRENT_BUILDS = 3`, but `build_all_projects_async:542-543` spawns one thread per distinct
path — 46 on this machine. All 46 land in `_building` immediately; 43 of them are blocked on the
semaphore doing nothing.

`get_build_status()` returns `len(_building)`, and its own docstring says *"how many builds are in
flight right now"*; `status_header._graft_progress_snapshot`'s docstring repeats *"a real,
in-flight count of threads currently building"*. Both are wrong on the exact path the chip was built
for (M6, first-run). The chip will read **`🧠 Building graphs… 46 now · 0/46`** while three builds
are actually running.

**Fix:** keep `_building` as the single-flight key set (it must stay pre-semaphore or single-flight
breaks), and add a separate `_in_flight: set[str]` mutated *inside* the semaphore; report that one.

---

### M3 — MEDIUM · trigger 4 is the only trigger missing the `graft CLI installed` gate

`build_all_projects_async:529`, `ensure_project_graph_async:550`, `schedule_rebuild_after_done:695`
all short-circuit on `_graft_cli() is None`. `resync_staging_only:650` checks only `_skip_env()`.

On a machine without graft (the default — `doctor.check_graft` installs only under `--fix`), every
working pane therefore pays, every 15 s, forever: a `git ls-files` **subprocess**, an `os.walk` of an
empty mirror, and two thread spawns — all so `_build_one` can return at its first line. Pure waste
plus a subprocess-per-15s heartbeat on a feature the user never enabled.

**Fix:** one line — add `or _graft_cli() is None` to the guard at `:650`.

---

### M4 — MEDIUM · `qa.md` and `critic.md` are missing the new-file guard, and `qa` is the role that needs it most

The three new rules landed in full (anchoring / staleness / new-file) in
`backend.md`, `devops.md`, `frontend.md`, `mobile.md`, `reviewer.md` (+6 lines each) and in
`codex_agents_md.py:106-118`. `critic.md` and `qa.md` got **+1 line each — anchoring and staleness
only, no new-file rule.** The CHANGELOG names the same five files, so the omission is deliberate,
but `_ROLE_MCP_POLICY` (`shared_dev_tools.py:692-702`) grants graft to **qa and critic as well**:

```python
"qa":     frozenset({"playwright", "chrome-devtools", "graft"}),
"critic": frozenset({"playwright", "chrome-devtools", "graft"}),
```

`qa` creates brand-new test files as its primary output and then asks "does this symbol exist / who
calls it" — the exact false-negative H1-of-the-old-audit was written to prevent. This is the single
highest-value place for that rule.

**Fix:** add the new-file bullet to `qa.md` and `critic.md` (one line each, matching their compact
style).

Related, still open from the last audit: roles **not** in `_ROLE_MCP_POLICY` (`gemini`, `shell`, and
every custom A6 role) fall through to the full master config and therefore **also get graft**, with
no role file carrying any of these guards on a claude pane.

---

### M5 — MEDIUM · macOS: two case-variant paths → two concurrent `graft build` into the **same** store

`_normalize_for_key` now folds case on darwin (M1 closed) — but the *dedup* keys did not follow:

* `graft_autobuild._dirs_for_project:236` → `str(resolved).lower() if os.name == "nt" else str(resolved)`
* `status_header._graft_progress_snapshot` (same expression)

On macOS, `paths: {"api": "/Users/x/Proj/api", "web": "/Users/x/proj/api"}` (or two projects
pointing at the same dir with different casing) survives dedup as **two** entries, `build_all_projects_async`
spawns **two** threads, and `_build_one`'s single-flight key is `str(target)` — also unfolded — so
both pass. `graph_store_dir` then folds both to the **same** store directory.

Result: two concurrent `graft build --dir <same store>` racing with nothing to stop them. That is
verbatim the corruption `_instance_key`'s own docstring (`graft_store.py:108-122`) says must never be
allowed to happen — it just arrives via a different door.

**Fix:** use `graft_store.graph_key(resolved)` as the dedup key in both places instead of
hand-rolled `os.name` casing. One expression, and it can never drift from the store key again.

---

### M6 — MEDIUM · the chip snapshot runs on the Qt main thread on every 2 s tick **and** every `statusChanged`

`status_header.py:571-574` (2 s `QTimer`) **and** `:563` (`orch.statusChanged`) both call
`_update_status`, which ends in `_refresh_graft_chip()` (`:628`) → `_graft_progress_snapshot()`.

Per call, on the GUI thread: `shutil.which("graft.cmd")` + `shutil.which("graft")` (a PATH scan; 69
PATH entries here), a `load_projects()` JSON read, and for each configured path a `resolve()`, an
`is_dir()`, another `resolve()` + sha256 inside `graph_store_dir`, and a `built.json` stat.

Measured: **14.4 ms per call, 46 project dirs** — ~0.7 % duty cycle at 2 s, more during fan-out
bursts when `statusChanged` fires repeatedly. Tolerable on this machine, but the shape is wrong:
`resolved.is_dir()` against a **disconnected mapped drive or dead UNC path** blocks for the SMB
timeout, on the GUI thread. `projects.json` with one offline network path freezes the cockpit
window every 2 s.

**Fix:** cache the whole snapshot for ~10 s (chip granularity doesn't need 2 s), and resolve
`graft_cli_path()` once per process. Both are trivial and remove the entire class.

---

## 3 · Low

* **L1 — `_codex_cli_version_cached` resolves the binary with the wrong PATH.** `mcp_bridge.py:214`
  uses `shutil.which(provider_bin)`, which reads `os.environ["PATH"]`, while `_codex_cli_version`
  invokes the subprocess with the caller's `env` (`:172-181`). Two panes with different `env["PATH"]`
  and the same bare `provider_bin` share one cache key and can get each other's version.
  Fix: `shutil.which(provider_bin, path=env.get("PATH"))`.
* **L2 — stale `codex --version` cannot weaken the deny-by-default gate.** Checked explicitly
  because the task asked: `_CODEX_DISABLE_ALL_MCP_ARGV` is appended **unconditionally**
  (`mcp_bridge.py:332`) before the version is consulted. The cached version only decides whether the
  *extra* resolve-and-disable-by-name defence also runs (`:336-341`). A too-old cached version costs
  a redundant `codex mcp list`; a too-new one (only reachable by an in-place **downgrade** that
  preserves mtime) skips defence-in-depth but leaves the real gate intact. Not a security regression.
* **L3 — `takkub disk` over-reports staging.** `_dir_stats` sums `st_size` per path, and staging
  files are hardlinks to the source tree — this repo's mirror reports **33.5 MB that costs ~0 real
  bytes**. The number is honest only after a `copy2` fallback (cross-device staging).
* **L4 — pre-M5 stores become invisible garbage when `AGENT_TAKKUB_HOME` is set.**
  `_legacy_orphan_entries` (`disk_usage.py:413-457`) only scans `GRAFT_STORE_ROOT.parent`. With a
  custom `AGENT_TAKKUB_HOME` that root is now `<DATA_HOME>/graft-graphs`, so the old
  `~/.agent-takkub/graft-graphs/<64hex>/…` tree is never seen by scan or prune. Narrow (only affects
  users who set the env var and had a 1.0.46/47 store), disk-only.
* **L5 — chip arithmetic can disagree with itself.** `total`/`completed` come from projects.json
  paths only, while `failed` comes from `_last_build_failed`, whose keys include any `done()`-trigger
  cwd. `🧠 Graft: 2 failed` can name directories that aren't among the counted `N` projects, and
  `_on_graft_chip_clicked:815` prints them as raw absolute paths in a `QMessageBox`.
* **L6 — a pane spawned before its build finishes never gets graft until it is respawned.** Correct
  by H3's design, but there is no remedy hint: `browser_profile_mcp_config_path` is evaluated once at
  spawn. On a fresh install, every pane opened in the first minutes is silently graft-less for its
  whole life. The chip tooltip's "queued for the next tab switch or restart" is about the *build*,
  not about re-templating the pane.
* **L7 — a stale `built.json` survives later failures.** `mark_build_complete` is never invalidated,
  so once a store has ever completed, a permanently-failing rebuild leaves the MCP injected against
  an old graph. Only the chip's `failed` count surfaces it. Acceptable, but worth a line in the
  tooltip.
* **L8 — `graft_store_root_outside_data_home` uses a raw `startswith`** (`disk_usage.py:842-844`), so
  a sibling like `<home>-old` would read as "inside". Use `Path.is_relative_to`.

## 4 · Minimal-code lens

* **Cleared:** the staging mirror, the completion marker, `_kill_orphan_tree`, and the H3 drop-gate
  are each load-bearing — I re-verified the store/path numbers above rather than taking them on
  faith. The PEP-562 `__getattr__` is subtle but earns it (it is what makes `AGENT_TAKKUB_HOME`
  work post-import, and `_store_root`'s `sys.modules` indirection is genuinely required — a bare
  global read would bypass the hook).
* **Dead weight:** the `os.name == "nt"` casing in `_dirs_for_project` and `_graft_progress_snapshot`
  is both redundant with and now *inconsistent* with `_normalize_for_key` — deleting it in favour of
  `graph_key()` removes code and fixes M5.
* **Duplication:** `_graft_progress_snapshot` re-implements `_dirs_for_project`'s projects.json walk
  verbatim in the UI layer. The stated reason (file ownership between panes) was a task-splitting
  constraint, not a design one; it is now a second copy that has already drifted (M5).
* **Not over-engineered:** `_has_new_files` looked like a candidate, but the CHANGELOG's empirical
  note plus H1 above show the escalation is real — the bug is in *how* it decides, not that it exists.

## 5 · Recommendation

**Fix H1 before publishing.** It is a background-CPU/disk runaway on any repo with a submodule, and
submodules are common in exactly the multi-repo setups this cockpit targets. It is also a small,
contained change inside `_stage_files`/`_has_new_files`.

M3 (one line), M4 (two lines of prose), and M5 (one expression, twice) are cheap enough to take in
the same pass. M1, M2 and M6 are quality-of-implementation issues that can follow, but M2 will make
the brand-new first-run chip visibly wrong on the very first launch it was built for, so it is worth
taking now.

No security defects found. The deny-by-default codex gate is intact (L2), the H3 false-authority
trap is closed, no path-traversal reachable through `git ls-files -z` output, and `_assert_under`
still bounds every prune target.

---
---

# Re-review — 2026-08-06 (same day, after the fix pass)

**Scope:** the working tree after the fix round (1,554 insertions / 26 files vs. `31e3599`).
**Method:** I read the post-fix code myself and re-ran my own probes on this machine. Done-reports
and CHANGELOG entries were again **not** accepted as evidence for any closure verdict; where a
verdict below says CLOSED, it is because I executed something that would have failed if it weren't.

**Verdict: no publish blocker. 5 of 7 findings genuinely closed; 1 closed-but-narrowed, 1 closed
but it introduced a new silent-staleness bug of its own. Two one-line fixes recommended before
publish.** Full suite green on my own run (`.venv\Scripts\python.exe -m pytest -q`, 0 failures);
targeted graft suite 292 passed.

## 6 · Closure audit of my own findings — verified against post-fix code

| # | Verdict | What I actually ran / read |
|---|---|---|
| **HIGH-1** submodule rebuild loop | **PARTIALLY CLOSED → now MEDIUM** | Source-side classes closed; the MAX_PATH class is not. See 6.1. |
| **M1** re-links every file | **CLOSED**, but the fix introduces **new MEDIUM R-1** | Measured this repo: `_sync_staging` 264–270 ms → **67–79 ms** (3 consecutive runs, 741 files, nothing changed). `_has_new_files` 14 ms → `False`. See 6.2 for the regression. |
| **M2** chip counts queued as building | **CLOSED** | `_in_flight` is added *inside* `with _build_semaphore` and discarded in a `finally` (`graft_autobuild.py:536-543`); `get_build_status()` returns `len(_in_flight)` (`:587`). `test_get_build_status_counts_in_flight_builds` pins `building == 2` while `len(_building) == 5` at cap 2. `conftest.py` clears `_in_flight` per test. I traced every exit path out of `_build_one` — no path leaves the set stale. |
| **M3** trigger 4 missing the CLI gate | **CLOSED** functionally, **test coverage regressed** | Gate present at `graft_autobuild.py:735`. See 6.3. |
| **M4** `qa.md` / `critic.md` new-file rule | **CLOSED** | Both files carry the full third bullet (ranked-list + staleness + **new-file**), matching the wording in `codex_agents_md.py:109-118`. |
| **M5** two case-variant paths → one store | **CLOSED** | `_dirs_for_project:258` and `_graft_progress_snapshot:757` both key on `graft_store.graph_key()`. Grepped `src/` for `os.name == "nt"`: the only survivors are `disk_usage.py:844` (that is L8, a different expression) and `shared_dev_tools.py:61` (unrelated to graft keying). No hand-rolled fold left on any graft path. |
| **M6** snapshot on the Qt main thread | **CLOSED**, with a caveat worth one line | 14.4 ms → cached. Answer to the TTL question in 6.4. |
| **L1** `engines-note` | **DOCUMENTED** | `package.json` carries the note. npm ignores unknown top-level fields, so this is inert at install time — it records the disagreement rather than resolving it, which is what was asked. |

### 6.1 · HIGH-1 is narrowed, not closed — the gate checks the source, the failure is on the destination

`_stageable()` (`graft_autobuild.py:315-328`) is `stat()` + `S_ISREG` on `target / rel`. That closes
every class whose **source** is the problem, and I confirmed the ones I can create here:

```
plain dir      : _stageable = False      # git submodule — the reproduced H1 case
plain file     : _stageable = True
missing path   : _stageable = False      # covers the POSIX non-UTF-8 name (errors="replace")
broken symlink : could not create ([WinError 1314] privilege not held)
dir symlink    : could not create ([WinError 1314] privilege not held)
```

The two symlink classes I could not create on this box (no `SeCreateSymbolicLinkPrivilege`), so that
part is **reasoned, not measured** — but it is sound by construction: `Path.stat()` follows symlinks,
so a broken link raises `OSError` → `False`, and a dir symlink resolves to a directory → `S_ISREG`
false → `False`.

**What is still open: MAX_PATH.** That entry never failed because its *source* was unstageable — it
failed because the *staging destination* is longer. Measured here:

```
target  prefix :   45  C:\Users\monch\WebstormProjects\agent-takkub
staging prefix :   77  C:\Users\monch\.agent-takkub\graft-staging\58c53152d5ffab59\58c53152d5ffab59
delta          :  +32 chars
```

The staging prefix is a **fixed 77 chars** regardless of target, so a target rooted shorter than this
repo's gets an even bigger delta. Any rel path over **182 chars** produces a `dst` over 259 while
`src` still stats fine — `_stageable` says `True`, `dst.parent.mkdir`/`os.link`/`copy2` all fail,
the file never lands in the mirror, and `_has_new_files` reads `True` forever. Same runaway, same
15 s cadence.

I forced the destination-side failure deterministically (a file occupying the path where the dst
directory must be created, so `mkdir` fails exactly as a MAX_PATH `mkdir` does) rather than needing
a `LongPathsEnabled=0` machine:

```
cycle 0: _has_new_files = True  (stageable(pkg/deep.py) = True)
cycle 1: _has_new_files = True  (stageable(pkg/deep.py) = True)
cycle 2: _has_new_files = True  (stageable(pkg/deep.py) = True)
cycle 3: _has_new_files = True  (stageable(pkg/deep.py) = True)
-> True forever == the H1 infinite-escalation loop, unchanged
```

Reachability: this repo's longest rel path is 68 chars (145 as a staged path), so *this* repo is
nowhere near it. It needs a repo with a >182-char rel path on a machine with `LongPathsEnabled=0` —
i.e. a **default Windows 11 install**, which is precisely the machine class H2's whole `_KEY_HEX_LEN`
fix was written for. Uncommon trigger, unchanged consequence.

**Severity: MEDIUM** (was HIGH — the common trigger really is gone, and Lead's submodule
verification is genuine). **Recommended fix** — the second option from my original write-up, which
covers destination-side failures the predicate structurally cannot: give the escalation a memory.
Track per-target `(rel, first_escalated_at)`; if a rel is still absent from the mirror after a
`_build_one` for that target has *completed*, stop escalating on it and log it once. A source-side
predicate can never see a destination-side failure, so no amount of extending `_stageable` closes
this class.

### 6.2 · R-1 (new, MEDIUM) — the M1 fix compares `st_ino` without `st_dev`

The reasoning behind dropping my `(st_mtime_ns, st_size)` fallback is **correct** and I withdraw that
suggestion: Windows does batch nearby writes onto one timestamp tick, so that heuristic can silently
skip a real content change. Inode equality is the right test.

But `_stage_files:375` is:

```python
if dst_stat is not None and src_stat.st_ino != 0 and dst_stat.st_ino == src_stat.st_ino:
    continue
```

`st_ino` alone does not identify a file — inode/file-index numbers are allocated **per volume**. The
docstring says "already hardlinked to the CURRENT source", but a hardlink is exactly what the
`copy2` fallback path does *not* produce, and that path is reached whenever staging is on a different
volume from the target. That configuration is not hypothetical — it is the documented reason
`AGENT_TAKKUB_HOME` exists (`graft_store.py`'s module docstring: *"so a user can move cockpit data
off a small/boot drive"*). This machine has two volumes, and the cross-device path is live:

```
=== A. real cross-device staging (C: target -> D: staging) ===
  src  st_dev=3660818680 st_ino=9851624185021578
  dst  st_dev=377050461  st_ino=562949953483604
  copy2 fallback used: True          # independent inode spaces
```

When those two independent counters happen to agree, the sync is skipped **permanently**:

```
=== B. simulate that coincidence: dst on another volume, same st_ino ===
  target content : 'v2 CHANGED'
  staged content : 'v1'
  -> STALE: sync silently skipped
  os.path.samestat(src, dst) would have said: False
```

On Windows the index is a wide 64-bit MFT value, so a collision is unlikely. On **macOS and Linux**
— both supported, both in the CI matrix — inode numbers are small sequential integers allocated from
the low end of each filesystem, so two files created early on two different filesystems colliding is
entirely plausible. The failure is silent and permanent: agents keep getting confidently wrong
answers from a graph built on stale content, with nothing anywhere reporting a problem.

**Fix (stdlib, one line, removes code):** `os.path.samestat(src_stat, dst_stat)` — it *is* the
`st_ino and st_dev` test, and it also subsumes the hand-rolled `st_ino != 0` guard.

### 6.3 · M3 closed, but four tests now pass for the wrong reason on CI

The gate is real and it is a net win — I measured `_graft_cli()` at **0.60 ms** vs. the ~40 ms
`git ls-files` subprocess it replaces on a graft-less machine.

The coverage problem: `.github/workflows/ci.yml` never installs graft (I grepped all three
workflows), so `_graft_cli()` is `None` on **every** CI runner, and the new gate is the **first**
statement in `resync_staging_only`. Four tests don't mock `_graft_cli` and therefore return before
reaching the behavior they name:

`test_resync_staging_only_noop_when_env_set` · `..._noop_without_cwd` · `..._noop_for_missing_dir` ·
`..._skips_dir_with_full_build_in_flight`

The first two are harmless (their own guard short-circuits first anyway). The last one matters —
it is the only test of the `_building` in-flight guard, and it asserts a *positive* behavior. I ran
it with graft stripped from `PATH` (CI conditions) and then removed the precondition it exists to
test:

```
graft CLI seen by module: None
WITH the _building precondition (as the test writes it)
   calls == []  -> assertion 'calls == []' PASSES
WITHOUT it (guard under test removed)
   calls == []  -> assertion 'calls == []' PASSES
```

A test that passes identically with the thing it tests deleted is not testing it. All 8 resync tests
pass locally *for the right reason* only because graft is installed on this box
(`C:\Users\monch\AppData\Roaming\npm\graft.cmd`) — the gap is invisible in local runs and silent in
CI. **Fix:** add `monkeypatch.setattr(gab, "_graft_cli", lambda: r"C:\fake\graft.cmd")` to
`..._skips_dir_with_full_build_in_flight` and `..._noop_for_missing_dir`. Two lines.

Minor, same area: `_graft_cli()` is an uncached `shutil.which()` ×2 PATH scan (59 entries here) and
it now runs on the **Qt main thread** — `_check_idle_teammates` is a `QTimer` slot
(`orchestrator.py:794`) — every 5 s per working pane. 0.60 ms × panes is negligible, but it is the
exact cost M6's fix just finished caching away on the chip path, reintroduced two files over. Worth
reusing the same one-per-process resolution.

### 6.4 · M6 — the TTL question, answered

Perf fix is real and correct. The caveat is *what* got cached, not how long.

`get_build_status()` documents itself as deliberately cheap — *"two dict/set snapshots under the same
lock … so polling this on a short interval (e.g. every 2 s from the UI thread) costs nothing."*
The expensive part M6 was filed against is the `load_projects()` + per-path `resolve()`/`is_dir()`/
sha256 walk. The fix puts **both** behind the same 10 s TTL (`status_header.py:699-703` returns the
whole cached dict early), discarding the one genuinely live signal for no saving.

Direct answer to "build finished but the chip still says building for 10 s — acceptable?": **yes,
that direction is fine.** Nobody is harmed by a chip that lags 10 s into "ready". The direction that
does mislead is the opposite one, at boot — the case the chip was built for:

* t=0, first tick: 46 dirs enumerated, `building=0` (threads haven't reached the semaphore yet),
  `completed=0` → cached.
* For the next 10 s the chip renders **`🧠 Graft: 0/46 queued`** in *idle* styling while three builds
  are actually running.

`_refresh_graft_chip`'s own docstring says the three states MUST render differently so *"'don't know'
never looks identical to 'confirmed zero'"* — and `building == 0` is deliberately routed to the
"queued, not building" text. A **stale** zero now renders as a **confirmed** zero, which is exactly
the conflation that docstring forbids. Same shape delays the amber `N failed` chip by up to 10 s.

**Fix:** keep the TTL on the expensive half; call `get_build_status()` fresh on every invocation and
merge it into the cached snapshot. It is cheaper than the cache lookup it currently skips.

Also noted, not a defect: `_graft_cli_cache` is resolved once per process and never invalidated, so
installing graft mid-session leaves the chip on "not installed" until a restart. The tooltip already
tells the user to restart, so this is consistent — just no longer self-healing the way it was.

## 7 · Regression sweep over the rest of the 1,554 lines

Read every diffed hunk in `mcp_bridge.py`, `orchestrator.py`, `codex_agents_md.py`, `doctor.py`,
`disk_usage.py`, `cli.py`, `conftest.py`. Only R-1 (6.2) came out of it. Specifically checked:

* **`_codex_cli_version_cached`** (`mcp_bridge.py:190-221`) — new this round. `_version_cache` is a
  plain dict mutated from spawn threads; get/setitem are GIL-atomic and a duplicate compute is
  harmless, so no race. The deny-by-default gate is untouched: `_CODEX_DISABLE_ALL_MCP_ARGV` is still
  appended unconditionally at `:332`, before the version is ever consulted — caching cannot weaken it.
  `conftest.py` resets `_version_cache` per test, which is necessary and correct on a box with a real
  `codex` on PATH.
* **`orchestrator.py:3271-3289`** — the resync hook. `_ps_wt is None or not _ps_wt.worktree` means a
  pane with no recorded state still resyncs (right default), the whole block is `except Exception`-
  wrapped, and `pane._session_cwd` is read defensively. Correct.
* **`doctor._graft_store_size_finding`** — appended only in the graft-installed branch (`:500`), and
  it passes `DATA_HOME` into `scan_graft_graphs`, which documents that argument as accepted-and-
  ignored (the root always comes from `graft_store.GRAFT_STORE_ROOT`), so the `DATA_HOME ==
  REPO_ROOT` fallback is not mis-scanned. Measured **95 ms** with 2 live stores — it is a full
  `os.walk` per store, so it scales with store count; on a 46-store machine `takkub doctor` gains a
  couple of seconds. Fine for a one-shot diagnostic; worth remembering if `doctor --live` ever
  re-polls it.
* **`cli.py:1693`** — `graft-graphs` really is a prune category (`disk_usage.py:1306`), so the help
  string now matches the accepted values.
* **`conftest.py`** — resets `_building`, `_in_flight`, `_debounce_timers`, `_last_build_failed`,
  `_last_live_resync`, and cancels pending timers, by mutating in place rather than rebinding. Right
  call: other modules hold references to those same objects.

## 8 · The 8 LOWs — which are still open

| # | State | Take before publish? |
|---|---|---|
| **L1** wrong PATH in `shutil.which(provider_bin)` | **OPEN** — and now also the cache key (`mcp_bridge.py:207`), so two panes with different `env["PATH"]` share one entry | No. Per L2 the version only gates defence-in-depth; worst case is a redundant `codex mcp list`. One-line fix (`path=env.get("PATH")`) whenever this file is next touched. |
| **L2** stale version can't weaken the gate | n/a — informational, re-confirmed above | — |
| **L3** `takkub disk` over-reports hardlinked staging | **OPEN** | No. Cosmetic; the number is honest for the cross-device case. |
| **L4** pre-M5 stores invisible under `AGENT_TAKKUB_HOME` | **OPEN** (`_legacy_orphan_entries:421` still scans only `GRAFT_STORE_ROOT.parent`) | No. Narrow (env-var users with a 1.0.46/47 store), disk-only. |
| **L5** chip arithmetic can disagree with itself | **OPEN** | No. |
| **L6** pane spawned pre-build never gets graft | **OPEN** (`shared_dev_tools.py` untouched this round) | No — correct by H3's design, only the remedy hint is missing. |
| **L7** stale `built.json` survives later failures | **OPEN** | No. |
| **L8** raw `startswith` for "outside DATA_HOME" | **OPEN** (`disk_usage.py:842-844`) | **Optional yes** — one line, `Path.is_relative_to`, and it is right there in code this round already edited. |

## 9 · Recommendation

**Nothing blocks publish.** The three things I would take first, in order, are all small:

1. **R-1 → `os.path.samestat`** (6.2). One line, deletes code, and it is the only finding here whose
   failure mode is *silently wrong answers* rather than wasted CPU. Cheapest fix with the worst
   downside if skipped.
2. **The two missing `_graft_cli` mocks** (6.3). Two lines. Without them the `_building` in-flight
   guard is untested on every CI run and nobody will notice when it breaks.
3. **Move `get_build_status()` outside the 10 s cache** (6.4). One line, and it stops the first-run
   chip — the thing M6 exists for — from asserting a confirmed zero it hasn't confirmed.

HIGH-1's residual (6.1) and L8 are fair to defer; the residual's trigger genuinely is uncommon now,
and it is worth doing properly (escalation memory) rather than bolting another predicate onto
`_stageable` that structurally cannot see the failure.

Security posture unchanged from §5: codex deny-by-default intact and re-verified against the new
version cache, no new trust-boundary surface, `_assert_under` still bounds every prune target.

*Probes for this section: `<scratchpad>/reviewer/probe_h1.py`, `probe_inode.py`, `probe_vacuous.py`.*
