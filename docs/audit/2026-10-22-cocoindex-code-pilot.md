# cocoindex-code pilot (2026-10-22) — semantic local code search vs graft `ask`

Measure-only pilot on the dev machine (Windows 11, no GPU) against this repo (`src/agent_takkub`, 1,620 files listed / 1,349 indexed). Question: does `ccc search` (AST-chunked + local embeddings, no API key) find code from **non-lexical** questions better than the structural-only graft `ask` the cockpit ships today — and what does it cost?

## Result: accuracy — ccc wins clearly

12 questions phrased so the words do NOT appear in the code (e.g. "avoid the black window that flashes when a child process starts" → `_win_console`). Hit = expected symbol/file in the top-N of source hits (tests excluded).

| | ccc search | graft ask (lexical) |
|---|---|---|
| hit in top-3 | **10 / 12** | 5 / 12 |
| hit in top-5 | **11 / 12** | 6 / 12 |
| median query time | 383 ms (daemon warm) | ~1 s (+18 s on the first call because graft re-walked 1,407 changed files) |

Misses: ccc #10 "estimate how big a job is from the task text before routing it" (wanted `routing_planner.classify`) — both tools missed it. graft missed 7 of 12 outright: anything whose vocabulary differs from the identifiers (quota → `limit_*`, "black window" → `_win_console`, "phone" → `remote/`).

Raw hits: `scratchpad/ccc_results.txt`, `scratchpad/graft_results.txt` (session scratch; the table above is the durable record).

## Result: footprint — the real price

| | cocoindex-code 0.2.42 `[full]` | graft 0.8.2 (structural) |
|---|---|---|
| install | 96 s, **1.1 GB** (torch + transformers) via `uv tool install` | 10 s, 87 MB npm |
| first index of this repo | **2,844 s (47 min)** on CPU — 30,506 chunks | 4.7 s |
| incremental index, no changes | 4 s | ~1 s |
| on-disk index | 217 MB under `~/.agent-takkub/cocoindex-db` | 14 MB |
| resident daemon | **822 MB RAM** embedding subprocess + 2 small procs, idle timeout 180 min | none (MCP spawned per pane, ~180 MB each) |
| query | 0.4 s | 1 s |

Index composition shows the default `include_patterns` pull in markdown/json/html (9,136 md chunks vs 18,467 python) — for the cockpit's use the patterns must be narrowed to code, which also cuts the first-index time.

## What it touches (and what the cockpit must guard)

- `ccc init` writes `<project>/.cocoindex_code/settings.yml` **inside the project** and appends `/.cocoindex_code/` to the project's `.gitignore` — the same class of regression graft caused in #146 (writing into 46 user repos on boot). `COCOINDEX_CODE_DB_PATH_MAPPING=<src>=<dst>` relocates the **databases** (verified: 217 MB landed outside the repo) but not the settings file. Integration must either accept that one ignored file per project or point `ccc` at a staging mirror the way `graft_autobuild` does.
- `~/.cocoindex_code/global_settings.yml` — written by `ccc init` interactively; under the cockpit's shells (Git Bash / no Win32 console) the questionary prompt crashes (`NoConsoleScreenBufferError`), so the cockpit must write this file itself (done here via the package's own `save_initial_user_settings`).
- Telemetry on by default → `COCOINDEX_DISABLE_USAGE_TRACKING=1` in the pane/daemon env.
- Daemon is per machine, shared across projects, 180 min idle timeout; `ccc daemon stop` exists.
- Not tested: macOS (MPS path), repos with > 4 GiB LMDB map (`COCOINDEX_LMDB_MAP_SIZE`), switching models (full re-index).

## Verdict

**Accuracy gap is real and large** (10/12 vs 5/12 on the questions agents actually ask), **but the cost is not free in machine terms**: 1.1 GB install, ~800 MB resident daemon, and a first index measured in tens of minutes per repo on CPU. On this machine, which already freezes for 30–98 s during browser-e2e batches (#884–#887), a background 47-minute embedding job per project at boot would be a regression.

Recommended shape for the dev trial (user asked to try it on dev and remove if not OK):
1. MCP `ccc mcp` as a **second** server next to graft for code-reading roles — graft keeps `skeleton`/`callers` (structure), ccc provides `search` (meaning). They are complementary, not competing, so #641's "opt-in that never gets used" does not apply as long as the role prompt says when to use which.
2. **Per-project opt-in**, never boot-wide: index only when the user enables it for a project; first index runs as a low-priority background job with a visible progress line, never on the UI thread, never at boot for every project.
3. Env from the cockpit: `COCOINDEX_CODE_DB_PATH_MAPPING` → `DATA_HOME/cocoindex-db`, `COCOINDEX_DISABLE_USAGE_TRACKING=1`; include patterns narrowed to code.
4. `takkub doctor` row: ccc present/version/daemon RAM; `takkub ma` already reports main-thread stalls, so a regression shows up there.
5. Acceptance after a week on dev: panes actually call `search` (events.log), no new stall class, user judgment.

## Shipped for the dev trial (same day)

- `src/agent_takkub/ccc_store.py` — launcher lookup, per-project DB dir (`<store>/cocoindex-db/<name>-<hash8>`, store = DATA_HOME or `~/.agent-takkub` on a dev checkout), env pin, `index_ready`, worktree→main-checkout mapping, non-interactive global settings.
- `shared_dev_tools.CCC_MCP` + `ensure_ccc_mcp()` (entry only while the launcher is installed) · `_ROLE_MCP_POLICY`: same roles as graft · per-pane writer drops `ccc` unless `index_ready(root)`, else pins `command` + `env`.
- Launcher is **`cocoindex-code serve`, not `ccc mcp`**: driven over stdio from a non-project cwd, `ccc mcp` exits "Not in an initialized project directory" while `cocoindex-code serve` honours `COCOINDEX_CODE_ROOT_PATH` — initialize → tools `['search']` → a search answered in 2.6 s from the relocated DB with zero new entries in the repo root.
- `takkub ccc index|status|env` — the only sanctioned way to index a project (writes the global settings `ccc init` cannot write without a console, pins root + DB mapping, streams progress).
- Prompt: `lead_context.CCC_TOOL_CAVEATS` (when `search` vs graft, refresh once per task, never index other projects) injected wherever graft's caveats are, counted outside the repo-controlled boot ceiling like `graft_caveats`.
- Not done: auto-index, `takkub doctor` row, macOS run, narrowing `include_patterns` (markdown/json/html still indexed by default).
- Removal: `uv tool uninstall cocoindex-code` (entry disappears on next boot) · `rm -r ~/.agent-takkub/cocoindex-db` · delete `<project>/.cocoindex_code/` and its `.gitignore` line.
