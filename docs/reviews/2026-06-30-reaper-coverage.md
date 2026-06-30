# Audit: Process-Tree Reaper (`_tree_kill`) coverage

**Date:** 2026-06-30 · **Author:** Lead (Claude) · **Scope:** read-only audit, no code change
**Question:** Is the red-team's "Process-Tree Reaper (DO FIRST)" already implemented? Is it complete on both OSes and called on auto-recover (not just manual close)? Does CLI 2.1.196's Windows daemon hand-off break it?

---

## TL;DR — the Reaper **already exists and is complete** on both platforms and on every teardown path. The red-team's "DO FIRST" item is **DONE**. One genuinely-new risk to verify empirically: CLI 2.1.196's background-job daemon hand-off may decouple jobs from the claude tree.

---

## What exists (`pty_session.py:_tree_kill` + `terminate()._teardown`)

| Aspect | Status |
|---|---|
| **Windows** | `taskkill /PID <pid> /T /F` — walks the live parent→child tree. Run **synchronously before** pywinpty's own `terminate(force=True)` (which only reaps claude.exe itself), so the node dev-server subtree can't orphan. Correct ordering is documented and was the fix for the 3170-proc / 18 GB leak. ✓ |
| **macOS / Linux** | `os.killpg(os.getpgid(pid), SIGKILL)`. `_PosixBackend` spawns via `ptyprocess.PtyProcess.spawn`, which `setsid()`s the child → claude is its own session/process-group leader → `killpg` reaps the whole descendant group in one signal (the POSIX equivalent of `taskkill /T`). ✓ |
| **Called on manual close** | `orchestrator.close()` → `pane.session.terminate()` → `_teardown` → `_tree_kill`. ✓ |
| **Called on auto-recover** | `_auto_recover_stuck()` → `self.close(...)` → same chain. So a watchdog respawn reaps the old tree before spawning the new pane. ✓ |
| **Called on app shutdown** | `app.py` / `main_window.py` call `terminate(wait=True)` so the heavy `taskkill` finishes inline before the process exits (a detached daemon thread would be killed mid-`taskkill` and orphan the tree). ✓ |
| **Off the Qt main thread** | `taskkill /T` (seconds on a big tree) runs on a background daemon thread by default so close() never freezes the GUI. ✓ |

**Conclusion:** the reaper is well-built — correct kill primitive per OS, correct ordering, every teardown path covered, non-blocking. **No rebuild needed.**

## Known residual limits (pre-existing, acceptable)

- A grandchild that does its **own** `setsid` / `CREATE_NEW_PROCESS_GROUP` escapes both `killpg` and `taskkill /T` (it's no longer in the tree/group). This is inherent to tree-walking kills and is an edge case for normal dev servers.

## NEW risk to verify — CLI 2.1.196 background-job daemon hand-off

CLI 2.1.196 changelog: *"Long-running commands and workflows survive process stop/restart/update, including on Windows via daemon hand-off instead of kill."*

The cockpit **encourages** backgrounded long-runners in teammate panes (CLAUDE.md: "ทุก long-running command ต้อง background หรือ detach" — `nohup npm run dev &`, `docker compose up -d`, claude's own background bash). If 2.1.196 now **hands a background job off to a daemon deliberately decoupled from the claude.exe process tree** (so it survives a claude restart), then `taskkill /PID <claude> /T /F` may **no longer reach it** → the exact orphan class the reaper was built to prevent could reappear through a new door.

- This is **distinct** from the node dev-server subtree the reaper currently targets (those are claude's direct descendants and are still reaped).
- Severity unknown without observation — depends on the daemon's process-tree topology (child of claude vs. re-parented to a system daemon).

## Recommendation

1. **No change to `_tree_kill`** — it is correct for what it targets.
2. **Empirical check (no build, ~5 min when awake):** in a teammate pane, start a backgrounded dev server (`nohup npm run dev &` or a claude background-bash job), then close the pane. Run `tasklist | findstr /i "node claude"` (Win) / `pgrep -fl "node|claude"` (mac) before vs. after. If a job survives that didn't pre-2.1.196, the hand-off is orphaning.
3. **If orphans confirmed:** augment the reaper to also reap claude's handed-off background daemons — e.g. scan claude's background-job registry / PID files, or run a `claude`-provided cleanup, in `_teardown` after the tree kill. Until observed, do not pre-build.

## Verdict for the feature backlog

Red-team item **"Process-Tree Reaper — DO FIRST" is already shipped and complete.** Cross it off. The only open thread is the *2.1.196 daemon-hand-off verification* above — a measurement, not a feature. Pairs with `[[cli-2196-stream-watchdog-overlap]]` (same "new CLI internals interacting with cockpit lifecycle" theme).
