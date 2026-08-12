# Code review — auto-issue-capture (pre-PR)

Reviewer pane, 2026-08-12. Scope: `src/agent_takkub/auto_issue_capture.py` (new),
`src/agent_takkub/app.py` (`_log_unhandled` hook), `tests/test_auto_issue_capture.py` (new),
`CLAUDE.md` (cockpit self-bug auto-issue policy).

Verdict: **1 HIGH (blocking), 3 MED, 3 LOW, 2 CLAUDE.md correctness bugs, 3 minimal-code cuts.**
The core design (background thread, atomic state write, rolling cap, reuse of `issues.new_issue`)
is sound — the problems are at the edges.

Baseline: `tests/test_auto_issue_capture.py` → **4 passed** (`.venv/Scripts/python.exe -m pytest`).

---

## HIGH

### H1 — Unredacted exception text + host paths go to a PUBLIC repo
`auto_issue_capture.py:127-134`, `:77-81,140`

`gh repo view --json nameWithOwner,visibility` → `{"nameWithOwner":"takkub/agent-takkub","visibility":"PUBLIC"}`.

Three separate leaks land there:

1. **Title is the raw exception message.** `title = f"[auto] {exc_name}: {exc_msg[:80]}"` — no
   filtering at all. This repo *already knows* exception strings carry secrets: it explicitly
   redacts them at `remote/settings_dialog.py:195,198`
   (`str(exc).replace(token, "[redacted]")`) because `subprocess.TimeoutExpired`/`SubprocessError`
   stringifies the whole argv. Reproduced what the auto-capture would publish:

   ```
   [auto] RuntimeError: Command '['ngrok','add-authtoken','2abcSECRETtoken']' timed out after 15 seconds
   ```

   Same mechanism reaches any `_gh()` failure (`issues.py:81` re-raises raw `result.stderr`).

2. **Traceback carries absolute paths** → `C:\Users\<username>\...` in every frame line of the body.
   OS username on a public tracker, every time.

3. **`noticed_in` creates a permanent public label.** `_guess_noticed_in()` returns
   `Path.cwd().name`; `new_issue` turns that into `noticed-in:<dir>` (`issues.py:244`) and
   `_ensure_labels` **creates the label on the public repo** (`issues.py:138-149`). A label object
   outlives the issue — deleting the issue does not remove it. Leaks project/client directory names.

**Fix**
- Scrub `Path.home()` / `%USERPROFILE%` → `~` in `tb_text` before building the body.
- Drop the message from the title: `f"[auto] {exc_name} @ {sig}"`; put the (scrubbed) message in
  the body only.
- Regex-redact token-shaped runs in title+body (`ghp_…`, `sk-…`, long base64/hex runs,
  `--*token*=<v>` / `add-authtoken <v>` argv forms).
- `noticed_in="cockpit"` constant — see N2, it is also *wrong* today, not just leaky.

---

## MEDIUM

### M1 — Thread-per-exception: 3437 live OS threads measured under a GC storm
`auto_issue_capture.py:146`

Every call spawns a thread and does a file read, **even when dedup will reject it in the first
millisecond**. `sys.unraisablehook` fires from GC / `__del__`, which is exactly the bursty case.

Measured (bench, deduped signature, real code path):

| calls | main-thread total | peak live threads |
|---|---|---|
| 200 | 52 ms | — (1 issue filed, correct) |
| 5000 | **1282 ms** | **3437** |

Thread creation (~0.26 ms) outruns worker completion because every worker serializes on `_lock`
and does file I/O. 3437 threads ≈ 3.4 GB of reserved stack address space plus kernel objects, and
1.28 s of the Qt main thread — during an already-degraded crash path.

**Fix** — cheap in-memory short-circuit *before* spawning:

```python
_recent: dict[str, float] = {}          # module level

# in capture_cockpit_crash, on the calling thread:
with _lock:
    last = _recent.get(sig)
    if last is not None and time.time() - last < _COOLDOWN_SECONDS:
        return                          # no thread, no file I/O
    _recent[sig] = time.time()
threading.Thread(...).start()
```

The JSON file stays as the cross-restart backstop; the dict absorbs the storm.

### M2 — A `_save_state` failure silently disables the entire rate cap
`auto_issue_capture.py:63-74`

`except OSError: pass`. If `DATA_HOME` is unwritable or the disk is full, `_load_state()` returns
`{}` on *every* subsequent call → dedup never matches, `fired` is always empty → **every crash
files an issue**. That is precisely the GitHub-spam scenario the module exists to prevent, and it
fails silently in the direction of more spam.

Deviates from the sibling it was modelled on: `issues.py::_save_local_issues:197-202` raises
`RuntimeError` on failure rather than swallowing.

**Fix** — module-level in-memory mirror used when persistence fails (the M1 `_recent` dict covers
the dedup half; add the same for `fired`). Optionally one `_boot_log`-style stderr line on the
first persist failure so it is not invisible.

### M3 — Test monkeypatch replaces `threading.Thread` process-wide
`tests/test_auto_issue_capture.py:35`

Verified: `aic.threading is threading` → `True`. So
`monkeypatch.setattr(aic.threading, "Thread", _SyncThread)` sets `threading.Thread` for the
**whole process** for the duration of every test in this file. Anything else that constructs a
thread in that window — PyQt, pytest plugins, other fixtures — gets `_SyncThread`, which runs the
target inline and has no `join` / `is_alive` / `daemon` / `ident`.

Harmless in a targeted run; the full-suite gate is where it bites, and this is the same class of
failure CLAUDE.md flags as invisible to targeted runs (PyQt6 abort → exit 127).

**Fix** — give the module a seam and patch *that*:

```python
# auto_issue_capture.py
_spawn = threading.Thread              # module-level, patchable
...
_spawn(target=_worker, name="auto-issue-capture", daemon=True).start()

# test
monkeypatch.setattr(aic, "_spawn", _SyncThread)
```

---

## LOW

### L1 — A corrupt state file kills auto-capture permanently and silently
`auto_issue_capture.py:108,112`

`_load_state` validates only that the top level is a `dict`. A non-numeric entry in `fired`, or a
string `signatures[sig]`, makes `now - ts` raise `TypeError` → swallowed by `_worker`'s
`except Exception: pass` → the file is never repaired → auto-capture is dead forever with zero
signal.

**Fix** — filter in place: `if isinstance(ts, (int, float)) and now - ts < _RATE_WINDOW_SECONDS`,
and `isinstance(last_filed, (int, float))` before comparing.

### L2 — No length cap on title/body
`auto_issue_capture.py:127-134`

`new_issue` passes the body as an **argv element** (`--body <text>`, `issues.py:258`). Verified on
this box: `subprocess.run` with a ~40 000-char argument →
`FileNotFoundError: [WinError 206] The filename or extension is too long`. GitHub separately
rejects bodies > 65 536 chars.

That `OSError` is not converted by `_gh` (only `TimeoutExpired` is, `issues.py:76-79`), so it
escapes `new_issue`'s `except RuntimeError` (`issues.py:271`) and is swallowed by `_worker` — a
silent failure that has already burned a rate-cap slot.

Not a recursion problem: measured a 1500-frame `RecursionError`-shaped traceback at only **262
chars** (3.11 collapses repeated frames). The realistic source of bulk is a long `str(exc_value)`,
e.g. `_gh`'s `RuntimeError(result.stderr)`.

**Fix** — `tb_text = tb_text[-8000:]` and cap `exc_msg` before it reaches the title.

### L3 — Cross-process race on the dedup file (awareness only)
`auto_issue_capture.py:30,63-69`

`_lock` is a `threading.Lock` — in-process only. Two cockpit instances
(`_restart_env.configure_multi_instance_port_file` exists, so this is supported) read-modify-write
the same `auto_issue_dedup.json`; a lost update can push past the 5/24h cap. Both also use the
same fixed `.tmp` name, so one process can `os.replace` the other's half-written temp file.

`os.replace` keeps the file from being *corrupt*, and `issues.py::_save_local_issues` has the
identical shape — so this is consistent with the existing pattern, not a regression. Not a
blocker; noting it so nobody assumes the cap is hard across instances.

---

## CLAUDE.md — the new policy block does not run as written

Both commands in the new "Cockpit self-bug auto-issue" section fail at argparse.

### C1 — `takkub issue comment` does not exist
```
takkub issue: error: argument issue_command: invalid choice: 'comment'
              (choose from 'new', 'list', 'close', 'show')
```
Step 2 ("ถ้ามี `takkub issue comment` เพิ่มแทนเปิดใหม่") is unrunnable.

### C2 — `issue new` takes the title positionally, not `--title`
`cli.py:1929` → `sin.add_argument("title", ...)`.
```
takkub: error: unrecognized arguments: --title
```

**Fix** — corrected line:
```bash
takkub issue new "<title>" --cockpit-bug --severity <s> --noticed-in <project> --body "..."
```
and for step 2, either add an `issue comment` subcommand or (simpler, no new code) change it to
"ถ้ามี issue เปิดอยู่แล้ว topic เดียวกัน → ข้าม ไม่เปิดใหม่".

`takkub issue list --open` (step 1) is correct — `cli.py:1959`.

---

## Minimal-code lens

### N1 — the sha1 in `_signature` buys nothing (`:40,49`)
The key is already short and readable (`ValueError:app.py:57`). Hashing it to `f1be8bbdfa39` only
makes the on-disk JSON undebuggable and adds a lazy `import hashlib` on the crash path (an import
inside a hook that can fire during GC). Store the plain key; delete the import.

### N2 — delete `_guess_noticed_in()` (`:77-81`)
Five lines producing a value that is both leaky (H1) and **wrong**: it reports the *cockpit
process's* cwd, not the active project tab. For a cockpit crash the honest answer is the constant
`"cockpit"` — which also removes the public-label spam.

### N3 — `signatures` is never pruned (`:106,123`)
`fired` is pruned by window; `signatures` grows forever. The cap bounds it to ~5/day so this is a
nit, but it is one predicate inside a prune that already exists.

---

## Confirmed correct — no action

- **Rate cap is genuinely rolling** (`:111-114`). Entries are re-pruned by `now - ts < window` on
  every check; there is no fixed-window reset. Reserving the slot *before* the network call, under
  the lock, is the right ordering, and `issues.new_issue` is correctly called **outside** the lock
  so a 60 s `gh` stall never blocks another crash.
- **Atomic write** (`:63-69`) — tmp + `os.replace` + tmp cleanup, matching
  `issues.py::_save_local_issues:190-202`. Correct.
- **`cockpit_bug=True` routing reuses the existing path** (`:142` → `issues.py:205-233`,
  `detect_cwd = REPO_ROOT`). Nothing reinvented. Also correctly passes `severity="high"` (a valid
  `_SEVERITY_VALUES` member) and `tags=["auto-captured"]`.
- **Main thread is not blocked by the network.** Measured 0.258 ms per call on the calling thread
  including thread spawn; all `gh` work is on the worker. (M1 is about *volume*, not per-call cost.)
- **No exception escapes back into the hook.** `app.py:67-72` wraps the whole call in
  `try/except Exception: pass`, so re-entrancy into `_log_unhandled` is impossible.
  Minor doc nit: the docstring at `:88-91` claims the module swallows everything *itself*, but
  `threading.Thread(...).start()` at `:146` is outside any try/except. On 3.11.8 (verified) that
  does not raise at shutdown; on 3.12+ `Thread.start()` raises
  `RuntimeError: can't create new thread at interpreter shutdown`, and `pyproject.toml` declares
  `requires-python = ">=3.11"`. Covered by app.py — but move the `.start()` inside the existing
  `try` so the docstring is true standalone.

## Test gaps worth one round
Current 4 tests cover dedup, distinct signatures, the cap, and swallowed `new_issue` failure. Missing:
- `cockpit_bug=True` is never asserted, even though the fake already captures `kwargs`. One line.
- No time-travel test: nothing proves the window *rolls* (monkeypatch `aic.time.time` to `now +
  25h` and assert the same signature re-fires). This is the property the task asked to verify and
  it is currently unverified by the suite.
- No corrupt-state-file test (L1), no oversize-body test (L2), no redaction test (H1).
- `test_new_issue_failure_is_swallowed` asserts nothing — at minimum assert the slot was consumed
  (or deliberately is not).

## Out of scope
`settings_window.py`, `tests/test_settings_window.py`, `agent-takkub.bat` are also modified in this
working tree but were not in the review scope and were not reviewed.
