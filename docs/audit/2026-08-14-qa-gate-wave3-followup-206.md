# #206 — qa gate wave 3 follow-up (2 findings)

Two gaps qa found while gating #205's merge. Both fixed on `wt/backend-2-1786723382`.

## 1. `test_subprocess_text_encoding_guard.py` only checked kwarg *presence*, not value

**Bug:** `_find_violations()` collected the set of kwarg *names* on a text-mode
subprocess call and only asserted `encoding`/`errors` were present. A call with
`encoding="utf-8", errors="strict"` passed the guard — but `errors="strict"` is
exactly the failure mode #205 exists to prevent (an unmappable byte in a child
process's output still raises `UnicodeDecodeError` and kills the reader thread).

**Fix:** `_find_violations()` now resolves the literal string value of both
kwargs (`_str_literal()`) and asserts `encoding == "utf-8"` and
`errors == "replace"` exactly, with a dedicated violation message for a
wrong-value case vs. a missing-kwarg case. Non-literal values (e.g. a variable)
resolve to `None` and also fail, which is correct — the guard can only verify
what it can statically see, so anything it can't prove passes must be marked
with the existing `# subprocess-encoding-ok: <reason>` escape hatch.

**Verification (self-proving, per task instructions):** used a real call site
(`worktree_manager.py:233`, `subprocess.run(...)`) and confirmed the guard
turns red for all three cases, then reverted:

| Case | Change | Result |
|---|---|---|
| `errors="strict"` | `errors="replace"` → `errors="strict"` | **FAIL** — `errors= must be literal "replace" (found 'strict')` |
| missing both | dropped `encoding=`/`errors=` entirely | **FAIL** — `missing encoding, errors=` |
| wrong encoding | `encoding="utf-8"` → `encoding="cp874"` | **FAIL** — `encoding= must be literal "utf-8", found 'cp874'` |

`git diff --stat src/agent_takkub/remote/worktree_manager.py` confirmed empty
after revert. Full guard suite (`test_subprocess_text_encoding_guard.py`,
148 parametrized cases) is green on the restored tree.

## 2. `test_image_upload_in_view_mode_is_forbidden_before_dispatch` — real bug, not test flakiness

qa reported this test failing with `ConnectionAbortedError [WinError 10053]`
under full-suite load but passing standalone. Root cause was in production
code, not the test.

**Root cause** (`http_server.py`'s `do_POST`, `/api/lead/upload` route):
every other view-mode-forbidden branch (`/api/lead/say`, `/api/open`,
`/api/close`, `/api/lead/resume`) sends its 403 *after* the handler has
already unconditionally read `self.rfile.read(length)`. `/api/lead/upload`'s
`allows_control()` check was the one exception — it answered 403 and
`return`ed *before* that read ever ran, specifically to avoid buffering a
multi-megabyte body from an unauthenticated client. But this check runs
*after* bearer + password-gate auth already succeeded, so the client is
already known-legitimate; skipping the drain here bought nothing and cost
correctness.

The server uses `protocol_version = "HTTP/1.0"` (no keep-alive), so the
handler closes the socket immediately after writing the 403. With the
client's small JSON body (`b"{}"`) still unread in the kernel receive
buffer at close time, Windows can turn the close into an RST instead of a
clean FIN — which the client's `urlopen()` surfaces as
`ConnectionAbortedError: [WinError 10053]`. This is inherently a race
(depends on exact thread/socket scheduling), which is why it only showed up
under full-suite load and never running the file alone.

**Fix:** added `_RemoteHandler._drain_request_body()` — reads and discards
the body (bounded to `_MAX_IMAGE_BODY_BYTES`, the largest this route ever
accepts) — and calls it in the `/api/lead/upload` forbidden-mode branch
before sending the 403, matching what every other forbidden-route branch
already does implicitly.

**Verification — reproduced the exact bug, then proved the fix, both with a
stress harness (temporarily appended to the test file, reverted after,
`git diff --stat tests/test_remote_http_server.py` confirmed empty):**

The harness fires the same request as the failing test in a tight loop
(300 iterations, fresh ephemeral-port server each time) to recreate
full-suite-like scheduling pressure without needing the whole suite:

- **Pre-fix** (`git stash` on `http_server.py` only): **6/300** iterations
  raised `ConnectionAbortedError: [WinError 10053] An established connection
  was aborted by the software in your host machine` — exact match to qa's
  report.
- **Post-fix** (`git stash pop`): **0/300**, run twice (600 iterations
  total, 0 failures).

Additionally ran the **full suite 5 consecutive times** (`pytest -q`, no
`-k`/`-x` filtering) with the fix in place — `test_remote_http_server.py`
fully green all 5 runs. Each run left the same 8 pre-existing, unrelated
failures (`test_installed_cli_bin_integration.py`,
`test_installed_mode_gate.py`, `test_performance_stress_harness.py` — all
require an editable install pointing at this checkout, which this session
intentionally avoided per the `pip install -e` ban; not in scope for #206).

No `xfail`/`skip` used — the fix addresses the actual race, not the test.

## Files changed

- `src/agent_takkub/remote/http_server.py` — `_drain_request_body()` +
  call site in `/api/lead/upload`'s forbidden-mode branch.
- `tests/test_subprocess_text_encoding_guard.py` — value-level assertions
  on `encoding=`/`errors=`.

## Commands used

```bash
PYTHONPATH="<repo>/src" PYTHONIOENCODING=utf-8 python -m pytest -q   # full suite
PYTHONPATH="<repo>/src" python -m pytest tests/test_remote_http_server.py -q
PYTHONPATH="<repo>/src" python -m pytest tests/test_subprocess_text_encoding_guard.py -q
```

`PYTHONPATH` override was required because the shared venv's editable
install points at a different checkout (`tests/conftest.py`'s
`_assert_agent_takkub_matches_this_checkout()` guard, #202) — `pip install
-e .` was intentionally not run against the shared venv from this worktree,
per this session's constraints.
