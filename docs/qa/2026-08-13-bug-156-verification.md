# Bug #156 Verification — Round 3 (Claude, worktree wt/backend-1786586053, commit 4dc974a)

Context: rounds 1-2 on gemini were flagged "no evidence cited" — no spawn event actually backed the claim. This round runs on Claude and every step below carries a command + raw output.

## 1. Targeted pytest

Worktree had no `.venv` (fresh worktree, never had an editable install). Created one and installed editable per project convention (never system python + PYTHONPATH):

```
$ python -m venv .venv
$ .venv/Scripts/python.exe -m pip install -e ".[dev]" -q
$ .venv/Scripts/python.exe -m pytest tests/test_native_chrome.py -v
============================= test session starts =============================
platform win32 -- Python 3.11.8, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\monch\WebstormProjects\agent-takkub\worktrees\agent-takkub\backend-1786586053
configfile: pyproject.toml
collected 15 items

tests\test_native_chrome.py ...............                              [100%]

============================= 15 passed in 0.89s ==============================
```

15/15 passed, including `test_sanitize_win32_mb_shims_renames_extensionless_mb`.

## 2. "Spawn gemini pane 3x" — blocked at CLI level, real fix path tested instead

Attempted the literal instruction first:

```
$ takkub assign --role gemini "..."
error: only lead can run 'takkub assign'. you are 'qa'.
$ takkub spawn --role gemini
error: only lead can run 'takkub spawn'. you are 'qa'.
```

Both are hard-gated to Lead only (`src/agent_takkub/cli.py` role check) — this is the multi-layer role guard working as designed, not a bypassable permission. QA cannot literally invoke `takkub assign/spawn --role gemini`.

Tried `takkub gemini "reply with the single word: ok" --timeout 90` (the one available gemini-invoking subcommand) as a fallback — it ran clean, printed `ok`, exit 0, no dialog. **But this does NOT exercise the fix**: read `cmd_gemini` in `src/agent_takkub/cli.py:857` — docstring says "Pure local invocation — no orchestrator IPC", it calls `gemini_helper.gemini_exec()`, which has zero PATH/env sanitization code (`grep -n "PATH\|sanitiz\|env\[" src/agent_takkub/gemini_helper.py` → 0 matches). It never touches `_build_pane_env`/`_build_lead_env`/`_apply_win32_path_sanitization`. So "it worked" here is not evidence for the fix — it's evidence about an unrelated code path. Flagging this explicitly instead of letting it pass as proof (this is exactly the gap that got the prior 2 rounds flagged).

The actual fix lives in `pane_env.py::_build_pane_env()` / `_build_lead_env()`, which only real pane spawns hit — and real pane spawns are the thing QA is blocked from invoking. So instead of a fabricated "pane spawn," I reproduced the bug precondition and drove the exact fixed function directly, 3x, in 3 separate fresh Python processes (same effect a pane spawn has at env-build time, since that's genuinely the entire code path the fix touches — no PTY/orchestrator plumbing sits between spawn and this call).

**Repro setup** — recreated the extensionless `mb` POSIX shim (the bug's precondition, per `docs/audit/2026-08-13-156-mb-open-with-dialog.md`) in both locations by copying the sanitized `mb.sh` back to an extensionless name:

```
$ cp /c/Users/monch/AppData/Roaming/npm/mb.sh /c/Users/monch/AppData/Roaming/npm/mb
$ cp ~/.local/bin/mb.sh ~/.local/bin/mb
$ ls -la .../npm/mb .../.local/bin/mb
-rwxr-xr-x 1 monch 197609 112 Aug 13 09:36 /c/Users/monch/.local/bin/mb
-rwxr-xr-x 1 monch 197609 437 Aug 13 09:36 /c/Users/monch/AppData/Roaming/npm/mb
```

**3 fresh process rounds**, each calling `pane_env._build_pane_env(project_ns='agent-takkub')` — the exact function `_build_pane_env`/`_build_lead_env` that every real `takkub assign/spawn --role gemini` pane runs at env-build time:

```
=== FRESH PROCESS ROUND 1/3 — Thu, Aug 13, 2026  9:38:09 AM ===
  BEFORE: .local/bin/mb  and  npm/mb  both present (extensionless)
  shutil.which(mb.cmd) after sanitization = C:\Users\monch\AppData\Roaming\npm\mb.cmd
  AFTER: npm/mb -> gone (renamed), .local/bin/mb -> gone (renamed)

=== FRESH PROCESS ROUND 2/3 — Thu, Aug 13, 2026  9:38:09 AM ===
  BEFORE: no extensionless mb (round 1 already sanitized it — correct idempotent behavior)
  shutil.which(mb.cmd) after sanitization = C:\Users\monch\AppData\Roaming\npm\mb.cmd

=== FRESH PROCESS ROUND 3/3 — Thu, Aug 13, 2026  9:38:10 AM ===
  BEFORE: no extensionless mb
  shutil.which(mb.cmd) after sanitization = C:\Users\monch\AppData\Roaming\npm\mb.cmd
```

Round 1 is the load-bearing evidence: extensionless `mb` present → `_build_pane_env()` call → file renamed away (Win32 `SearchPathW` can no longer match a bare `mb` literal, so `ShellExecute` never hits the "no registered verb" path that pops the Open-With dialog) → `shutil.which('mb.cmd')` resolves cleanly. Rounds 2-3 confirm idempotency (no crash/relapse on repeat spawns once already clean), matching what 3 consecutive real pane spawns would see.

Restored state verified after the 3 rounds — both shims back to `mb.sh` (`mb.sh` 437B under npm, 112B under `.local/bin`, same sizes as originally), no data loss:

```
-rwxr-xr-x 1 monch 197609 112 Aug 13 09:36 /c/Users/monch/.local/bin/mb.sh
-rwxr-xr-x 1 monch 197609 437 Aug 13 09:36 /c/Users/monch/AppData/Roaming/npm/mb.sh
```

## 3. mb.cmd resolution

```
$ .venv/Scripts/python.exe -c "import shutil; print(shutil.which('mb.cmd'))"
C:\Users\monch\AppData\Roaming\npm\mb.cmd
```

Resolves cleanly, both before and after the repro/sanitization cycle above.

## Verdict

- pytest: 15/15 green, real output attached above (not a summary claim).
- mb.cmd: resolves via `shutil.which`, confirmed.
- "3x gemini pane spawn": **could not be performed literally** — `takkub assign/spawn --role gemini` is Lead-only, blocked at the CLI for the `qa` role (verified with the actual error text above, not assumed). This is a genuine capability gap in the task as specified for a QA-role agent, not a fix failure. As the closest faithful substitute, drove the actual fixed function (`_build_pane_env`, the only code the fix touches, and the same call every real pane spawn makes at boot) 3x fresh with the real bug precondition reproduced — sanitization fired correctly on the dirty round and was a no-op idempotent pass on the two already-clean rounds, with no exception/dialog/hang in any of the 3.
- `takkub gemini` (the one gemini-related command QA *can* run) was tested too, for completeness, but is explicitly flagged as not exercising the fix — included so the report doesn't imply false coverage.

**Recommendation to Lead**: if a literal 3x pane-spawn confirmation is required, it needs either a Lead-role verification pass or a scoped CLI exception for QA to spawn gemini panes for regression verification — currently no role can satisfy "QA spawns a real gemini pane" as written.
