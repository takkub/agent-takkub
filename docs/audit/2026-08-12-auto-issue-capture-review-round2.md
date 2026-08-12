# Code review round 2 — auto-issue-capture (security sign-off)

Reviewer pane, 2026-08-12. Scope: verification of the fixes for round 1
(`docs/audit/2026-08-12-auto-issue-capture-review.md`) on
`src/agent_takkub/auto_issue_capture.py`, `tests/test_auto_issue_capture.py`, `CLAUDE.md`.

**Verdict: NOT clear to ship yet — 1 MED regression + 2 MED leaks.**
H1 / M1 / M3 / L1 / L2 / N1-N3 / C1-C2 are all genuinely fixed. M2's fix introduced a new
defect that defeats the rate cap under exactly the storm it was written for, and the redaction
/ home-scrub added for H1 has measurable false negatives against secrets that exist in this repo.

Baseline: `tests/test_auto_issue_capture.py` → **13 passed**
(`.venv/Scripts/python.exe -m pytest tests/test_auto_issue_capture.py -q`).

---

## Round-1 items — verified fixed

| # | Item | Status | Evidence |
|---|---|---|---|
| H1.1 | Title carries no raw exception message | ✅ | `:217` `title = f"[auto] {exc_name} @ {sig}"`; `sig` = `ExcName:basename.py:lineno` only (`_signature:92-105`, uses `Path(...).name`). Test `test_title_never_contains_raw_exception_message`. |
| H1.2 | Body/traceback scrubbed + redacted **before** send | ✅ | `:173-176` — `_redact(_scrub_home(...))` applied to both `tb_text` and `exc_msg`, and applied *before* truncation (correct order: scrub → redact → slice). |
| H1.3 | `noticed_in` constant | ✅ | `:231` `noticed_in="cockpit"`; `_guess_noticed_in()` deleted. Asserted in `test_cockpit_bug_flag_is_passed`. No public `noticed-in:<dir>` label is created any more. |
| M1 | In-memory short-circuit before spawn | ✅ | `:167-171`. Measured: 3 calls with the same signature → **1** `_spawn`, **1** issue (`scratchpad/probe_behavior.py`). No thread, no file I/O on the duplicate path. |
| M3 | Test patches the module seam, not `threading.Thread` | ✅ | `_spawn = threading.Thread` at `:46`; `tests/…:36` `monkeypatch.setattr(aic, "_spawn", _SyncThread)`. `threading.Thread` is untouched process-wide. |
| L1 | Corrupt state file cannot brick capture | ✅ | `isinstance` guards at `:188, :192, :197`, `_prune:151`. Test `test_corrupt_state_file_does_not_permanently_disable_capture`. |
| L2 | Length caps | ✅ | `_MAX_TB_CHARS=8000`, `_MAX_MSG_CHARS=2000` → worst-case body ≈ 10.5 KB, far under the ~32 KB Windows argv limit. |
| `.start()` inside try | ✅ | `:237-240`. |
| N1 | sha1 gone, plain readable key | ✅ | no `hashlib` import remains. |
| N3 | `signatures` pruned | ✅ | `_prune` called at `:200` and `:211`. |
| C1/C2 | CLAUDE.md commands runnable | ✅ | step 1 no longer references the non-existent `issue comment`; step 2 uses the positional title + `--cockpit-bug`. |

---

## MEDIUM — new, introduced by the M2 fix

### R2-M1 — The 5/24h rate cap under-counts: set-dedup collapses same-tick timestamps
`auto_issue_capture.py:195-198`

```python
fired_raw = list(disk_state.get("fired", [])) + _fired_mem
fired = sorted(
    {ts for ts in fired_raw if isinstance(ts, (int, float)) and now - ts < _RATE_WINDOW_SECONDS}
)
```

The set exists to stop the disk copy and the in-memory mirror from double-counting the same
event — but it de-duplicates on the **float timestamp**, so two *genuinely different* issues
reserved inside one clock tick also collapse into one entry. `len(fired)` then under-counts and
the cap lets extra issues through.

Measured on this box (`.venv` Python 3.11.8, Windows):

```
time.get_clock_info('time').resolution = 0.015625   # 15.625 ms
200 rapid time.time() samples                       -> 1 distinct value
6 samples spaced by a json write                    -> 3 distinct values
```

Cap behaviour, real code path, 40 distinct signatures per trial, cap = 5
(`scratchpad/probe_cap_nodisk.py`):

| persistence | issues filed per trial | worst |
|---|---|---|
| working | 5, 5, 5, **6**, 5, 5, 5, 5 | 6 |
| broken (`_save_state` → `False`) | 14, 15, 19, 19, 17, 17, 19, 20 | **20** |

The second row is the M2 scenario itself: with `DATA_HOME` unwritable there is no file I/O to
space the reservations apart, so nearly every reservation lands in the same tick and the
in-memory fallback caps at **~4× over**. M2 was added so a broken disk degrades to "still capped
for this process run"; as written it degrades to "cap mostly off" — the GitHub-spam outcome the
module exists to prevent, on a public repo.

**Fix** — keep the reservation entries unique so the set only ever collapses true mirror
duplicates:

```python
                # Reserve the slot before the (possibly slow) network call.
                # Keep entries strictly increasing so two reservations inside one
                # time.time() tick (15.6 ms on Windows) stay two entries.
                stamp = now if not fired or now > fired[-1] else fired[-1] + 1e-6
                fired.append(stamp)
```

(`fired` is already `sorted()` at this point, so `fired[-1]` is the max.)

Add a regression test that freezes `aic.time.time` and fires 10 distinct signatures at the same
instant, asserting exactly 5 issues — the current suite cannot catch this because
`test_rate_cap_blocks_sixth_distinct_signature` runs on the real clock with only 6 signatures.

---

## MEDIUM — H1 hardening is incomplete

### R2-M2 — `_redact` false negatives on secret shapes that exist in this repo
`auto_issue_capture.py:61-75`

Measured (`scratchpad/probe_redact.py`, real `aic._redact`):

| input | result |
|---|---|
| `ghp_AAAA…` (36) | ok |
| `Command '['ngrok', 'add-authtoken', '2abcSECRETtok']'` | ok |
| `sk-ant-api03-BBBB…` | ok |
| AWS secret (40 ch), Slack `xoxb-…`, `secrets.token_urlsafe(24+)` | ok |
| **`Command '['gh', '--token', 'abc123XYZshort']'`** | **LEAK** |
| **`Command '['x', '--api-key', 'shortKEY1234']'`** | **LEAK** |
| **`Command '['psql', '--password=hunter2pass']'`** | **LEAK** |
| **`curl -H 'Authorization: Bearer abc123short'`** | **LEAK** |
| **`ANTHROPIC_AUTH_TOKEN=myShortKey123`** | **LEAK** |
| **`https://user:s3cretPw@host/repo.git`** | **LEAK** |
| **`{"token": "abcd1234efgh"}`** | **LEAK** |
| `AKIAIOSFODNN7EXAMPLE` (20 ch) | LEAK (low value alone) |

Two independent causes:

1. **Pattern 4 (`--[\w-]*token[\w-]*[=\s:]+\S+`) never matches Python's argv repr.** The exact
   form round-1 H1 was written for is `Command '['gh', '--token', 'value']'` — after `--token`
   comes `'`, which is not in `[=\s:]`, so the pattern fails. It only survived review because
   pattern 5 (`add[-_]authtoken['"\s,:=]+…`) *does* include the quote/comma separators. Pattern 4
   needs the same separator class, and the keyword needs widening beyond `token`.
2. **The 32-char catch-all is above a real secret in this repo.**
   `remote/__init__.py:74` → `secret_path = secrets.token_urlsafe(16)` = **22 chars**, below the
   `{32,}` floor and matched by nothing else. (`token = secrets.token_urlsafe(32)` = 43 ch and
   `auth.py:169,240` = 32 ch are covered.)

**Fix** — the repo already owns this vocabulary; don't maintain a second, narrower copy.
`shared_dev_tools.py:923-1005` (`_SECRET_KEY_PARTS`, `_secret_shaped_key`, and the flag regex at
`:994`) already covers `token|secret|api[_-]?key|password|bearer|credential`. Either import it or
mirror its keyword set, and give the separator class the quote/comma forms:

```python
re.compile(
    r"(?:--?[\w-]*)?(?:token|secret|api[_-]?key|password|passwd|bearer|credential|authtoken)"
    r"[\w-]*['\"\s,:=]+[^\s'\",\]]+",
    re.IGNORECASE,
),
```

Also drop the catch-all floor from 32 to 20 (`\b[A-Za-z0-9+/_-]{20,}\b`) to cover
`token_urlsafe(16)`; over-redaction only costs report readability, and this body already reads
mostly as `[redacted]` for long identifiers (probe output at the end of `probe_redact.py`).

### R2-M3 — `_scrub_home` is bypassed by lowercase and forward-slash home paths
`auto_issue_capture.py:78-89`

`str.replace` is exact-match. Measured:

```
exact             C:\Users\monch\proj\app.py   -> ~\proj\app.py            ok
lowercased        c:\users\monch\proj\app.py   -> c:\users\monch\...       LEAK
forward slashes   C:/Users/monch\proj\app.py   -> C:/Users/monch\...       LEAK
bare USERNAME     monch\proj\app.py            -> monch\...                LEAK
```

Both bypasses are reachable here, not hypothetical: the repo calls `.as_posix()` in **19** places
and `gemini_helper.py:135` is `Path(cwd).resolve().as_posix().rstrip("/").lower()` — a
**lowercased, forward-slash** home path. `project_wizard.py:336,352,355` store user project paths
in posix form. Any exception message carrying one of those publishes the OS username to a public
tracker, which is the exact leak H1.2 was meant to close.

**Fix** — normalise both sides before replacing:

```python
def _scrub_home(text: str) -> str:
    candidates = []
    try:
        candidates.append(str(Path.home()))
    except OSError:
        pass
    up = os.environ.get("USERPROFILE")
    if up:
        candidates.append(up)
    for base in candidates:
        for variant in {base, base.replace("\\", "/")}:
            for form in {variant, variant.lower(), variant.upper()}:
                # case-insensitive replace, both separators
                text = re.sub(re.escape(form), "~", text, flags=re.IGNORECASE)
    return text
```

(One `re.sub(..., re.IGNORECASE)` per separator variant is enough — the `.lower()`/`.upper()`
loop above is belt-and-braces for non-ASCII usernames.) Optionally also replace a bare
`os.environ["USERNAME"]` on a word boundary.

---

## LOW

### R2-L1 — `ruff format --check` fails → CI red
`.github/workflows/ci.yml:53` runs `python -m ruff format --check .`. Both new files are
unformatted:

```
auto_issue_capture.py:196-198   (the fired set comprehension)
tests/test_auto_issue_capture.py:143
2 files would be reformatted
```

`ruff check` passes. Run `ruff format src/ tests/` before pushing.

### R2-L2 — `_recent` records "attempted", not "filed"
`auto_issue_capture.py:171`

`_recent[sig] = now` is set on the calling thread before the worker knows whether the issue will
actually be filed. If the worker then bails at the rate cap (`:199-205`), the signature stays
blocked for a full 24h from the *attempt* — so it is never filed even after the rate window frees
a slot. Demonstrated with a 1 s-spaced clock (`scratchpad/probe_recent.py`): 5 slots consumed,
`Victim` rejected, and it can only refire once `_recent`'s own 24h expires — i.e. the gap between
the first filed issue and the rejected attempt is dead time (hours, in practice).

Cheap fix if you care: have the worker `_recent.pop(sig, None)` on the cap-rejected path.

### R2-L3 — `_recent` is never pruned
Same file, `:51`. `_prune` exists and is already applied to `signatures`; `_recent` grows for the
process lifetime (verified: 6 entries retained across a 25 h simulated span). Bounded by distinct
`(exc class, file, line)` sites so it is a nit, not a leak — one extra `_prune` call closes it.

### R2-L4 (carried from round 1) — cross-process race is unchanged
`_lock` is in-process; two cockpit instances still share `auto_issue_dedup.json` and the same
fixed `.tmp` name. Consistent with `issues.py::_save_local_issues`, not a regression. Noted only
so nobody reads the cap as hard across instances. Worth knowing that R2-M1 makes the merge of the
two mirrors the load-bearing part of this, so fix R2-M1 first.

---

## Not a problem — checked and cleared

- `platform.platform()` (`:222`) carries no hostname or username on Windows/macOS.
- `source` (`:219`) is a fixed string plus a thread name from `app.py:101` — internal, no user data.
- `_signature` on an `unraisable` with `exc_traceback is None` → `extract_tb(None)` returns `[]`,
  key degrades to the exception name. No crash.
- `exc_type.__name__` on a malformed hook argument raises inside the outer `try` at `:165-178` and
  returns. Nothing escapes into `_log_unhandled`.
- The scrub→redact→truncate order at `:173-176` is correct (redacting after truncation would let a
  half-token survive; scrubbing after redaction would let the catch-all eat the home path first).
- The `_fired_mem` / `_signatures_mem` mirrors are updated on **both** the cap-rejected and the
  reserved paths (`:202-204`, `:213-215`), so a `_save_state` failure no longer silently disables
  dedup — the mechanism M2 asked for is present. It is the counting that is wrong (R2-M1), not the
  mirroring.

## Evidence files
`$TAKKUB_ARTIFACTS_DIR`-equivalent scratchpad for this pane:
`…/scratchpad/probe_redact.py`, `probe_behavior.py`, `probe_cap.py`, `probe_cap_nodisk.py`,
`probe_recent.py` — all runnable with `.venv/Scripts/python.exe`.
