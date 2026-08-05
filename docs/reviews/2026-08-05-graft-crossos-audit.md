# graft auto-build + external store — full-loop cross-OS audit

**Scope:** everything landed in 1.0.46 + commit `099b15d` (1.0.47, not yet published).
**Reviewer:** reviewer pane, 2026-08-05. Code read directly; no other agent's report was trusted.
**Verdict: DO NOT PUBLISH 1.0.47 as-is.** 3 blocking findings (H1–H3), 6 medium, 5 low.
Every finding below carries evidence from this machine or from a controlled test I ran myself.

Files audited: `graft_store.py`, `graft_autobuild.py`, `shared_dev_tools.py` (graft parts),
`mcp_bridge.py` (`--dir` threading), `disk_usage.py` (`graft-graphs`), `doctor.check_graft`,
`orchestrator.py` boot/done triggers, `main_window.py` tab switch, `package.json`, `tests/conftest.py`.

---

## Evidence gathered (raw)

| What | Command / probe | Result |
|---|---|---|
| Live store size | `os.walk` over `~/.agent-takkub/graft-graphs/58c53…/58c53…` | **4954 files · 506.8 MB** |
| Longest path in store | same walk, `max(len(p))` | **308 chars**; **3080 files > 259 chars** |
| Windows long-path setting | `HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled` | **1** on this machine (default Win11 = 0) |
| What got indexed | `.cache/fingerprint.*.json` → top-level dir histogram | `runtime` **3591** · `tests` 244 · `src` **143** · scripts 9 · npm 7 · scratch 2 (3999 total) |
| Is `runtime/` ignored? | `git check-ignore -v runtime/release-smoke-final2-…` | `.gitignore:16  runtime/` → **yes, ignored, indexed anyway** |
| Does graft honour `.gitignore` at all? | scratch git repo, `vendor/` in `.gitignore`, `graft --dir s2 build t2` | **`vendor/junk.py` excluded** — so behaviour is inconsistent, not uniformly "ignores gitignore" |
| Does `graft ask` self-build an empty store? | `graft --dir <empty> ask "hello_unique_marker" <tgt>` | **No** — `no matching nodes`, store left empty ⇒ `graft_autobuild.py` is genuinely load-bearing |
| `graft build` exclude options | `graft build --help` | only `-e/--extensions`; **no `--exclude`** |
| Windows `resolve()` case behaviour | `Path('C:/USERS/monch/WEBSTORMPROJECTS/agent-takkub/src').resolve()` | returns canonical `C:\Users\monch\WebstormProjects\…` |
| `~` handling | `Path('~/foo').resolve()` | `<cwd>\~\foo` (no expansion) |

Store layout confirmed: `~/.agent-takkub/graft-graphs/<sha256(DATA_HOME)>/<sha256(target)>/…`
(both segments are the same hash here because DATA_HOME == REPO_ROOT == the built target on a dev checkout).

---

## H1 — BLOCKER · auto-build indexes gitignored trees; 507 MB for one project, unbounded and unreclaimable

`graft_autobuild.py:136-145` (`_run_build`) → `graft --dir <store> build <target>` with no filtering.

The store built by the **new** code path at 2026-08-05 22:36 for this repo is **506.8 MB / 4954 files**, of which
**3591 cards (72%) are under `runtime/`** — a directory `.gitignore`s at line 16 and which holds release-smoke
venvs (`runtime/release-smoke-final2-1.0.22-…/Lib/site-packages/setuptools/…`). Actual source coverage: 143 cards.
`.cache/` alone is 330 MB, `.graph/` 123 MB.

A controlled counter-test (fresh `git init` repo, `vendor/` in `.gitignore`) **did** exclude the ignored dir, so this
is not "graft never honours gitignore" — it is *unreliable*, which means **per-project cost is unpredictable**.

Why it blocks a release:
* `build_all_projects_async()` (`:189-211`) fans out across **every** distinct path in projects.json — 46 dirs on
  this machine. At the size measured above that is multiple GB written into `~` on first launch.
* `_instance_key` deliberately namespaces per cockpit instance (`graft_store.py:68-83`), so a machine running dev
  + prod cockpits builds and stores **two full copies** of every project's graph.
* `_prune_graft_graphs` (`disk_usage.py`) only deletes **orphan** stores. A live 507 MB store has **no** reclaim
  path — no `--include-live`, no size cap, no retention.

**Fix (pick at least a+c):**
a. Skip targets that are not a git work-tree (`(target/'.git').exists()` or `git rev-parse --is-inside-work-tree`)
   — this also fixes L5.
b. Pass `-e/--extensions` limited to the languages actually wanted (there is no `--exclude`), or pre-check file
   count and skip above a threshold.
c. Give `graft-graphs` an `--include-live` prune path plus a warning in `takkub disk` when a store exceeds e.g. 200 MB.
d. Log the built size in `_build_one`'s success line so this is visible at all.

---

## H2 — BLOCKER · Windows MAX_PATH: 3080 files already exceed 260 chars

`graft_store.py:91` + `:104` produce `graft-graphs/<64 hex>/<64 hex>/` — **129 characters burned on two full
SHA-256 hex digests** before graft's mirror of the source tree even begins.

Measured longest path today: **308 chars**. 3080 of 4954 files are over 259. This machine works only because
`LongPathsEnabled = 1` (verified in the registry) — **a default Windows 11 install has it 0**, and Node's `fs`
does not auto-prefix `\\?\`.

On a default Windows machine that means `graft build` fails partway through a deep repo. The consequence is worse
than a failed build:

* `ensure_project_graph_async` (`graft_autobuild.py:232`) skips a directory whenever `<store>/.graph` merely
  **exists**. A truncated build that created `.graph` and then died is indistinguishable from a good one, so the
  tab-switch trigger will never repair it — the pane keeps answering from a partial graph, silently.
* `_dir_stats` (`disk_usage.py:78-95`) uses plain `os.walk`/`stat` with no `\\?\` prefix → under-reports size on
  such a machine. (`robust_rmtree` *does* handle it — `disk_usage.py:152-181` — so prune itself is fine.)

**Fix:** truncate both hashes to 16 hex chars (64 bits — still collision-safe for the dozens of paths one cockpit
tracks, and `write_store_manifest` already provides the reverse mapping). That reclaims 96 characters. Independently:
replace the `.graph`-exists check with a real completion marker (build exit 0 → write a `built.json` stamp; treat
its absence as "needs rebuild").

---

## H3 — BLOCKER · the MCP tells every agent "this repo is indexed" even when nothing was ever built

Two independent prerequisites, and only one of them is satisfied by default:

| Component | Needs | Gated? |
|---|---|---|
| `GRAFT_MCP` injection (`shared_dev_tools.py:160-167`, roles at `:655-666`) | `npx` only | **No** — `ensure_graft_mcp()` + `warm_graft_mcp()` run unconditionally at boot (`orchestrator.py:620-624`) |
| `graft build` (`graft_autobuild.py:99`) | the **`graft` CLI on PATH** | silently returns when missing (`:197-199`, `_log.debug`) |

`doctor.check_graft` installs the CLI only under `--fix` (`doctor.py:502-511`), which is opt-in. So the default
state for a brand-new user is: **graft MCP wired into reviewer / frontend / backend / mobile / devops / qa / critic,
graph never built.**

I verified `graft ask` against an empty store does **not** build on demand — it returns
`no matching nodes — try different words, or 'graft build' if graft/ is empty`.

That would be tolerable if the failure were legible. It is not: the graft MCP's own server-instruction block states
*"This repo is indexed by graft: a prebuilt graph of every symbol… Prefer these tools over grep/read"*, and its empty
result appends *"no hits — don't re-ask with new wording; switch tool"*. An agent following those instructions treats
an unbuilt graph as proof the symbol does not exist. This is the exact false-negative class already written into
every role file (`.claude/agents/*.md`: *"no callers … ไม่ใช่หลักฐานว่าโค้ดตายแล้ว"*), except here it fires 100% of
the time instead of occasionally.

**Fix (either):**
* Fall back to `npx -y @nanonets/graft@<pin> --dir <store> build <target>` when the CLI is absent — npx is already a
  hard dependency of the MCP, so this costs nothing new and makes "user has Node" sufficient; **or**
* Don't inject the graft MCP into a pane whose store has no completed graph (`browser_profile_mcp_config_path`
  already computes the store path and can drop the entry).

---

## M1 — macOS case-insensitive FS unguarded; the Windows guard is the redundant one

`graft_store.py:57-65`:

```python
if os.name == "nt":
    s = s.lower()
```

Verified on Windows: `Path('C:/USERS/monch/WEBSTORMPROJECTS/agent-takkub/src').resolve()` already returns
`C:\Users\monch\WebstormProjects\agent-takkub\src`. Windows `resolve()` goes through `GetFinalPathNameByHandle`,
so for a directory that exists the canonical case is restored **before** the fold — the fold never changes anything.

On macOS `posixpath.realpath` is lexical + `readlink` only: it does **not** correct case, and APFS/HFS+ default to
case-insensitive. So `takkub assign --cwd /Users/x/proj/api` against a projects.json entry of
`/Users/x/Proj/api` yields two different `graph_key`s → the pane's `--dir` points at a store the boot sweep never
builds → permanently empty graph, dressed up by H3's "this repo is indexed" instruction.

**Fix:** `if sys.platform in ("win32", "darwin"):`. Trade-off to note in the comment: a deliberately
case-sensitive APFS volume with two paths differing only in case would then share one store — far less likely than
a user typing a different case, and the module already accepts a comparable trade-off for `_instance_key`.

---

## M2 — `expanduser()` applied at 2 of the 3 call sites

* `graft_autobuild._dirs_for_project:118` → `Path(raw).expanduser().resolve()`
* `disk_usage._configured_graph_keys` → `Path(raw).expanduser().resolve()`
* `shared_dev_tools.browser_profile_mcp_config_path:430` → **`pathlib.Path(cwd)`**, and `_normalize_for_key` only
  `.resolve()`s.

`Path('~/foo').resolve()` → `<cockpit cwd>\~\foo` (verified). Two of three call sites treating `~` as valid input is
the tell that it *is* valid input. And `default_cwd_for_role` (`config.py:430-454`) returns the **raw** projects.json
string — unlike `lead_cwd`, which absolutizes at `config.py:487-498` precisely because "a raw configured path can be
relative". So a `~`-prefixed teammate path reaches the templating verbatim: pane store ≠ built store, plus a junk
`~` directory `mkdir -p`'d under the store root.

**Fix:** one line — `resolved = target.expanduser().resolve()` inside `_normalize_for_key`. Fixes all call sites at
the single choke point instead of adding a third copy of `.expanduser()`.

---

## M3 — worktree panes get a permanently empty graph (contradicts the module docstring)

`graft_autobuild.py:43-48` claims worktree isolation is "skipped implicitly". That holds for the **build** side —
`orchestrator.py:2157-2168` correctly puts `schedule_rebuild_after_done` in the `else` of `if had_worktree:`.

It does **not** hold for the **MCP** side. `browser_profile_mcp_config_path` is called for every spawn including
`--isolation worktree` panes, with `cwd` = the throwaway checkout under `<DATA_HOME>/worktrees/…`. It computes that
path's `graph_store_dir`, `mkdir`s it, and writes a `source.json` manifest (`shared_dev_tools.py:427-440`). Nothing
ever builds it.

Effect: in PARALLEL / Multi mode — the mode explicitly recommended in CLAUDE.md for several panes on one repo — every
pane's graft is empty for its entire lifetime, and each removed worktree leaves an orphan store dir behind.

**Fix:** skip graft templating when `cwd` is under `worktree_manager.worktree_root()` (drop the server, per H3), or
build the worktree's graph at spawn.

---

## M4 — timeout leaves an unrecoverable partial graph, and orphans `node.exe` on Windows

`_BUILD_TIMEOUT_S = 600` (`graft_autobuild.py:74`). On expiry `_run_build` returns `(False, …, "timed out …")`
(`:149-150`), logs a warning, and that is all — no retry, no cleanup.

1. **Partial graph is sticky.** Whatever `.graph` the killed build left makes `ensure_project_graph_async:232` skip
   that dir forever. Same root cause as H2's second bullet; one completion-marker fix covers both.
2. **Windows process orphan.** `graft` resolves to `graft.cmd`, a batch shim. `subprocess.run(timeout=…)` kills only
   the direct child (`cmd.exe`); the `node.exe` grandchild survives. This project already has a documented history of
   runaway node processes (CLAUDE.md: ~3170 node procs / 18 GB). Use a job object, `taskkill /T /F /PID`, or
   `psutil`-style recursive kill on the timeout path.

Whether 600 s is *enough* is the wrong question given H1 — a monorepo with `node_modules` reachable will blow past
it every boot and re-attempt from scratch on the next launch, holding one of only 3 semaphore slots for 10 minutes
each time.

---

## M5 — `GRAFT_STORE_ROOT` ignores `AGENT_TAKKUB_HOME`, and is frozen at import time

`graft_store.py:91`: `Path.home() / ".agent-takkub" / "graft-graphs" / _instance_key(DATA_HOME)`.

`config._resolve_data_home()` honours `AGENT_TAKKUB_HOME` verbatim (`config.py:129-131`) — that env var exists so a
user can put cockpit data on another drive. Multi-GB of graph store (H1) is exactly the kind of data they moved it
for, and it lands on `C:` / the boot volume regardless.

The docstring's reason for avoiding DATA_HOME is sound (dev checkout ⇒ DATA_HOME == REPO_ROOT ⇒ store nested inside
the target). But the correct expression of that constraint is *"DATA_HOME, unless DATA_HOME == REPO_ROOT"*, or a
dedicated `AGENT_TAKKUB_GRAFT_HOME` override — not a hard `Path.home()`.

Secondary problems with the same line:
* Computed at **import time**, so nothing can redirect it at runtime. Tests only get away with it because
  `tests/conftest.py` monkeypatches the module attribute — and that patch's comment is now **stale**: it still says
  *"graft_store.GRAFT_STORE_ROOT defaults to DATA_HOME/'graft-graphs'"*, which has not been true since this commit.
* `_instance_key(DATA_HOME)` calls `.resolve()` at import time — a filesystem syscall during module import.
* **Unwritable `~`** (locked-down corporate/roaming profile): `store.mkdir` fails, `_run_build` returns the error
  string (`:132-134`) and `browser_profile_mcp_config_path` logs a warning (`shared_dev_tools.py:413-419`) — but it
  still hands graft a `--dir` that does not exist. Result: silently empty graph, no user-visible signal anywhere.
  Combined with H3 this is the worst-case degradation path and it has zero UI surface.

---

## M6 — first-run experience: no feedback of any kind

Boot spawns one daemon thread per distinct path (46 here), 3 building concurrently, each up to 600 s, each capable of
writing hundreds of MB (H1). The **only** signal is `_log.info` / `_log.warning` at `graft_autobuild.py:172-174`.
Grepping `main_window.py` / status bar for graft turns up nothing but a comment. There is no status-bar line, no
Settings indicator, no per-project "graph: building / ready / failed" state.

A user installing 1.0.47 fresh sees: unexplained CPU and disk churn for minutes, several GB appearing under `~`, and
no way to tell whether graft is ready, still building, or permanently broken (H3/M5). `takkub disk` reveals the
result only after the fact.

**Fix:** a single status-bar line ("graft: building 7/46") plus a per-project ready/failed marker would make every
one of H2/H3/M4/M5 diagnosable instead of silent. This is the cheapest high-leverage change in the whole list.

---

## Low

* **L1 — Node floor.** `package.json:41` declares `engines.node >= 18`; graft's own floor is 20. `doctor.check_graft`
  emits a WARN (`doctor.py:430-439`) but nothing blocks: on Node 18 `npx … graft mcp` starts and may die at runtime,
  leaving a dead MCP entry in the pane. Now that graft is injected by default (not opt-in), the Node-20 requirement
  belongs in README/install docs.
* **L2 — disk report scope.** `disk_report` adds `graft-graphs` bytes (which live outside `DATA_HOME` by design) into
  a total labelled `data_home` (`disk_usage.py:703-717`). On a dev checkout the number now spans two roots.
* **L3 — verified NON-issues (recorded so they aren't re-audited).**
  `_graft_cli()` = `which("graft.cmd") or which("graft")` is correct on both OSes (Windows resolves `.cmd` via
  PATHEXT anyway; macOS simply misses the first probe). `SUBPROCESS_NO_WINDOW` is `0` off Windows and
  `creationflags=0` is legal on POSIX. `ensure_gui_path()` runs first in `main()` (`app.py:626-628`), so a
  Finder-launched macOS cockpit *does* find nvm/homebrew/volta binaries before the boot sweep runs. `--dir` is
  correctly placed **before** the subcommand in both the build argv and the MCP argv, and `graft mcp`'s positional
  repo dir correctly defaults to the pane's inherited cwd. `_prune_graft_graphs` is wired into the prune dispatch
  (`disk_usage.py:1160-1161`). No path-separator or `str` vs `Path` defects found.
* **L4 — stale comment** in `tests/conftest.py` (see M5).
* **L5 — non-code project folders.** `_dirs_for_project` only checks `is_dir()`. A documents/images folder in
  projects.json still gets a full tree walk every boot; parsing is extension-gated so the *graph* stays small, but the
  walk is not free and a folder containing a vendored SDK or bundled `node_modules` is indexed wholesale (H1). The
  "is this a git work-tree" gate in H1(a) fixes this too.

## Minimal-code lens

* **Checked and cleared:** `graft_autobuild.py` (266 lines, 3 triggers, semaphore, debounce) is *not* redundant —
  I verified `graft ask` does not build an empty store on demand, so something must run `build`. The design is
  justified.
* **Dead weight:** `_normalize_for_key`'s `nt` branch is a no-op where it is and missing where it's needed (M1).
* **Misplaced side effect:** `write_store_manifest(target)` runs on **every** pane spawn from the templating hot path
  (`shared_dev_tools.py:427`) — a JSON write per spawn, in the same function whose sibling `graph_store_dir` was
  deliberately kept side-effect-free (`graft_store.py:107-113`). Belongs at build time only.

---

## Recommendation

Hold 1.0.47. H1 (unbounded multi-GB writes into `~`), H2 (default-Windows MAX_PATH → silent partial graphs), and
H3 (agents told an unbuilt graph is authoritative) all fail the user's stated bar — *"ใช้ได้ทุก OS และทุกคนที่ใช้จะ
ไม่บั๊ก ได้ของเหมือนฉัน"*. H2 in particular only passes on this machine because `LongPathsEnabled=1`, which is not
the Windows default.

Suggested minimum before publish: H2's hash truncation + completion marker (small, mechanical), H3's npx build
fallback (small), H1's git-work-tree gate (small), M2's one-line `expanduser`, M1's `darwin` fold. M6's status line
would convert the remaining risks from silent to visible.
