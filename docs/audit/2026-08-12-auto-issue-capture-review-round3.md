# Code review round 3 — auto-issue-capture (final sign-off)

Reviewer pane, 2026-08-12. Scope: verification of the three round-2 FAILs
(`docs/audit/2026-08-12-auto-issue-capture-review-round2.md`) on
`src/agent_takkub/auto_issue_capture.py` + `tests/test_auto_issue_capture.py`.

**Verdict on the three requested items: all three PASS — the security posture is clear to ship.**
**One new MEDIUM (non-security) regression found outside those three**, introduced by the
round-2 **L2** fix: once the 5/24h cap is full, the M1 thread-storm short-circuit is disabled.
Measured below. It leaks nothing and cannot spam GitHub — it is a local resource/liveness
defect during a crash storm. Ship/hold on it is Lead's call.

Baseline: `tests/test_auto_issue_capture.py` → **24 passed**
(`.venv/Scripts/python.exe -m pytest tests/test_auto_issue_capture.py -q`).
`ruff check` + `ruff format --check` on both in-scope files → **clean** (R2-L1 closed for this change).

---

## R2-M1 — rate cap under a frozen clock — ✅ PASS

`auto_issue_capture.py:242` `stamp = now if not fired or now > fired[-1] else fired[-1] + 1e-6`

Measured through the real `capture_cockpit_crash` path
(`scratchpad/reviewer/probe_r3_cap.py`, `probe_r3_cap_storm.py`):

| scenario | issues filed |
|---|---|
| frozen clock (`time.time()` = const), disk OK, 10 distinct signatures | **5** |
| frozen clock, `_save_state → False` (M2's broken-disk scenario) | **5** |
| real clock, broken disk | **5** |
| 40 distinct signatures × 8 trials, disk OK | 5,5,5,5,5,5,5,5 — worst **5** |
| 40 distinct signatures × 8 trials, disk BROKEN | 5,5,5,5,5,5,5,5 — worst **5** |

Round 2 measured worst **6** (disk OK) / **20** (disk broken) on the same methodology.

**Non-vacuity proved**, not assumed: the module source was re-`exec`'d with the one line reverted
to `stamp = now`; the identical input then files **10** issues instead of 5 (both disk states).
So `test_rate_cap_holds_exact_count_when_clock_does_not_advance` has real signal.

Float precision checked at real epoch scale: `1e-6` is ~4 ulps at `t≈1.79e9` and ~2 ulps at
year-2100 epoch; 21 consecutive bumps stay strictly increasing and distinct in both cases. The
list is capped at 5 entries, so total drift is ≤ 5 µs. Breaks only past `t≈2^34` (year ~2514).

---

## R2-M2 — redaction vocabulary + separators — ✅ PASS

`auto_issue_capture.py:64-78`. Every shape round 2 measured as a LEAK is now redacted
(`scratchpad/reviewer/probe_r3_redact.py`, real `aic._redact(aic._scrub_home(...))`):

| input | result |
|---|---|
| `Command '['gh', '--token', 'abc123XYZshort']'` | `Command '['gh', '[redacted]']'` |
| `Command '['gh', '--token', 'v']'` (1-char value) | redacted |
| `Command '['x', '--api-key', 'shortKEY1234']'` | redacted |
| `Command '['psql', '--password=hunter2pass']'` | redacted |
| `curl -H 'Authorization: Bearer abc123short'` | redacted |
| `ANTHROPIC_AUTH_TOKEN=myShortKey123` | redacted |
| `https://user:s3cretPw@host/repo.git` | `https://[redacted]host/repo.git` |
| `{"token": "abcd1234efgh"}` | redacted |
| `AKIAIOSFODNN7EXAMPLE` | redacted |
| `--credential pw123`, `X-Api-Key: qq11ww22`, `args=['--api-key=tok_abc']`, `PASSWORD = 'x'` | redacted |
| `ghp_…`, `sk-ant-api03-…`, `add-authtoken` argv form | still redacted (no round-1 regression) |

Catch-all floor at 20 confirmed to cover the real repo shapes: `token_urlsafe(16)` (22 ch, the
`remote/__init__.py:74` secret round 2 called out), `token_urlsafe(24/32)`, `token_hex(16)` — all
redacted. The parametrised test `test_redact_covers_round2_leak_shapes` cannot pass via the
catch-all (every secret in it is < 20 chars), so it is non-vacuous by construction.

---

## R2-M3 — `_scrub_home` case/separator normalisation — ✅ PASS

`auto_issue_capture.py:88-109`. Measured against the real function on this box
(`home = C:\Users\monch`):

| variant | result |
|---|---|
| exact `C:\Users\monch\…` | `~\…` ok |
| lowercased `c:\users\monch\…` | `~\…` ok (was LEAK) |
| UPPERCASED | ok |
| posix `C:/Users/monch/…` | `~/…` ok (was LEAK) |
| posix lowercased (the `gemini_helper.py:135` shape) | ok (was LEAK) |
| posix home + backslash tail | ok |

---

## MEDIUM — new, introduced by the round-2 L2 fix

### R3-M1 — once the rate cap is full, `_recent.pop` re-opens the M1 thread/disk storm
`auto_issue_capture.py:232-235` (the `_recent.pop(sig, None)` on the cap-rejected path),
interacting with the fast path at `:187-194`.

The pop makes a cap-rejected signature refire as soon as a slot frees (R2-L2 — correct, and its
test passes). But it also means that signature is **no longer in `_recent`**, so the next crash
with the same signature sails past the `:188` short-circuit, spawns a worker thread, and the
worker does a full `_load_state` + `_save_state` (json write + `os.replace`) before rejecting it
again. That is precisely the guarantee the `_recent` comment at `:48-50` claims:
*"rejects a duplicate signature before a thread is even spawned, so a GC-triggered exception
storm can't burn thousands of threads."*

Measured (`scratchpad/reviewer/probe_r3_storm_after_cap.py`) — cap already full at 5, then **one**
repeating signature crashes 1000 times:

| code | worker spawns | dedup-state writes | wall |
|---|---|---|---|
| **current** (`_recent.pop` on cap-reject) | **1000** | **1000** | 3476 ms |
| round-2 code (no pop) | **1** | **1** | 433 ms |

~3.5 ms of in-`_lock` file I/O per crash. Under a real `unraisablehook` storm (GC finalizer /
paint loop, thousands per second) workers queue on the global `_lock` faster than they drain, so
live threads accumulate — during a crash, which is when the cockpit can least afford it. Window:
up to 24h, i.e. until the oldest reservation ages out.

No secret leak, no GitHub spam (`issues=5` in both rows) — resource/liveness only.

**Fix** — gate the fast path on when the cap actually frees, which keeps *both* properties:

```python
# module level, next to _fired_mem
_cap_blocked_until: float = 0.0
```
```python
        with _lock:
            if now < _cap_blocked_until:      # cap is full — nothing to do until it frees
                return
            last = _recent.get(sig)
            ...
```
```python
                if len(fired) >= _RATE_CAP:
                    ...
                    _recent.pop(sig, None)
                    global _cap_blocked_until
                    # fired is sorted -> fired[0] is the oldest reservation.
                    _cap_blocked_until = fired[0] + _RATE_WINDOW_SECONDS
                    return
```

Verified against the existing suite by hand: `test_cap_rejected_signature_can_refire_once_a_slot_frees_up`
still passes (block expires at `0 + 86400`, the refire attempt is at `86401`). The autouse fixture
needs `monkeypatch.setattr(aic, "_cap_blocked_until", 0.0)`, and a regression test should assert
`_spawn` is called once, not N times, for N repeats after the cap fills.

---

## LOW / NIT — non-blocking

### R3-L1 — `_redact` still misses a keyword immediately followed by `"]`
`auto_issue_capture.py:69-70`. `os.environ["ANTHROPIC_API_KEY"] = "shortSecret1"` → **not**
redacted: `]` is (deliberately) excluded from the value class `[^\s'\",\]]+`, but it is also
missing from the *separator* class, so the match dies on it.
Not reachable in this repo — every such site (`claude_auth_config.py:66,68,74`,
`limit_status.py:167-180`, `spawn_engine.py:1083,1675,2142`) assigns a **variable**, and a
traceback source line shows source text, not values. The dict-**repr** form
(`{'api_key': 'x'}`) is already covered. One-char fix if you want it closed:
`['\"\s,:=\]]+`.

### R3-L2 — the 20-char catch-all eats long snake_case identifiers (readability)
Round 2 asked for the floor drop to 20 and called the cost "report readability"; measured, that
cost is larger than it sounds — the diagnostically useful parts of the traceback go:

```
File "src/agent_takkub/orchestrator.py", line 794, in _check_idle_teammates
  ->  File "[redacted].py", line 794, in [redacted]
... in capture_cockpit_crash   ->   ... in [redacted]
```

The crash site is still recoverable from the title (`sig` = `ExcName:basename.py:lineno`, never
redacted), so this is not a functional break. If you want the frame names back without weakening
the floor, exempt pure snake_case runs (`^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$`) — `token_urlsafe`
output effectively never takes that shape, unlike a "must contain a digit" heuristic which would
miss ~2.6% of real 22-char tokens.

### R3-L3 — `_scrub_home` residuals
Mixed-separator home (`C:\Users/monch\…`) and a bare `USERNAME` (`monch\proj\app.py`) still
survive. Round 2 listed the bare-username replace as optional; the mixed-separator form needs the
prefix to be built by concatenating a backslash root with a posix tail, which nothing in this repo
does. Note also that the issue is filed **from the user's own GitHub account**, so authorship is
already public — the residual username leak adds little.

### R3-L4 — `_prune(_recent, now)` prunes on the wrong constant
`auto_issue_capture.py:192`. `_recent` is governed by `_COOLDOWN_SECONDS`, but `_prune` hardcodes
`_RATE_WINDOW_SECONDS`. Identical today (both 24h); if the cooldown is ever raised, `_recent` would
expire early and let duplicates through the fast path (still caught by the on-disk `signatures`
map, so it degrades safely).

### R3-L5 — two scrub tests only carry signal on Windows
`test_scrub_home_removes_lowercase_home_path` / `..._forward_slash_home_path` compare against
`home.lower()` / `home.replace("\\","/")`; where the home path is already lowercase and posix
(Linux `/home/runner`) both equal the original and the tests pass vacuously. CI is
`windows-latest` + `macos-latest`, so they are covered there — noted only so nobody moves the
matrix to Linux and assumes the coverage travels.

---

## Round-2 LOW items — status

| # | Status | Evidence |
|---|---|---|
| R2-L1 (`ruff format`) | ✅ | both in-scope files: `ruff check` clean, `ruff format --check` → "2 files already formatted" |
| R2-L2 (`_recent` blocks cap-rejected sig) | ✅ fixed, ⚠️ see **R3-M1** | `:234` + `test_cap_rejected_signature_can_refire_once_a_slot_frees_up` |
| R2-L3 (`_recent` never pruned) | ✅ | `_prune(_recent, …)` at `:192-194` (see R3-L4 nit) |
| R2-L4 (cross-process race) | unchanged, as accepted | `_lock` is still in-process |

## Evidence files
`…/scratchpad/reviewer/probe_r3_cap.py`, `probe_r3_cap_storm.py`, `probe_r3_redact.py`,
`probe_r3_storm_after_cap.py` — all runnable with `.venv/Scripts/python.exe`.
