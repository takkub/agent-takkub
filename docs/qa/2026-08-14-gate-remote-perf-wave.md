# Release gate — remote/perf wave

**Final verdict (รอบ 2, HEAD `95365bb`): GO.** See [รอบ 2 (final)](#รอบ-2-final--head-95365bb) below.
รอบ 1 (HEAD `7dc71b9`) resulted in NO-GO on the STR-A backoff regression; that was
fixed by #201 (`bf7397f`) and re-verified in รอบ 2, along with #200 (Pulse shows
every open pane), before shipping.

---

## รอบ 1 (HEAD 7dc71b9)

Covers #196, #198, #194, #195, #199, #197, #193. Gate run on `main` directly
(no worktree), HEAD confirmed at `7dc71b9a3d0b74d9d7ed110034f46b5be1b14ecb`
(`git rev-parse HEAD` == `git rev-parse 7dc71b9`).

**Verdict: NO-GO** — one deterministic, reproducible test failure on `main`
blocks the gate (root-caused below, #195-related). Everything else passes.

## 1. Full pytest suite

`.venv/Scripts/python.exe -m pytest` (editable install, no PYTHONPATH hack).

```
1 failed, 5928 passed, 7 skipped in 471.93s (0:07:51)
```

### FAILED: `tests/test_performance_stress_harness.py::test_deterministic_stress_harness_a_through_i`

- **File/line**: `tests/test_performance_stress_harness.py:31` (assertion) driving
  `tools/performance_reliability_stress.py::scenario_a` (STR-A), which exercises
  `src/agent_takkub/resource_governor.py::ResourceGovernor.dispatch_waiting`
  (lines 393–458, the #195 backoff logic).
- **Reproduce** (deterministic, seed=12345, fails 100% of runs, isolated in 1.45s):
  ```
  .venv/Scripts/python.exe -m pytest tests/test_performance_stress_harness.py -v
  ```
- **Observed** (`stress-summary.md`): STR-A `FAIL` —
  `{"admitted": 5, "final_active": 0, "final_queued": 25, "max_heavy": 4, "max_per_project": 2}`
  — expected `admitted == 30`, `final_queued == 0`. All 8 other scenarios (STR-B..I) pass.
- **Root cause**: `scenario_a` enqueues 30 tasks across 10 projects (global cap 4,
  per-project cap 2), then does `while active: release_slot(token); dispatch_waiting()`
  in a tight loop with **no sleep** between iterations — real wall-clock
  (`self._clock()` default) barely advances between calls. On the very first
  `dispatch_waiting()` call, every one of the ~26 still-queued items gets tried once
  in the same round-robin pass; the ones that don't fit get `next_retry_at = now + 1.0s`
  (issue #195's backoff, `resource_governor.py:434-438`). Every subsequent
  `dispatch_waiting()` call in the loop happens within milliseconds of the first, so
  `item.next_retry_at > now` is true for **all** of them — freed slots sit idle because
  no queued item is eligible for a retry attempt yet, and the loop ends (all `active`
  tokens released) with 25 items still parked in backoff.
- **Is this a real regression or a stale test?** The #195 backoff is intentional and
  the code comment (`resource_governor.py:119-124`) explicitly documents the tradeoff:
  *"Spacing retries out this way still notices freed capacity within 15s worst case"*.
  So the *design* is deliberate — but this specific deterministic gate scenario (STR-A)
  was written to assert the old "freed slot is reused immediately" guarantee and was
  **not updated** when #195 shipped. Either way, this is a failing test on `main` at
  HEAD, not a flake — it must be resolved (update STR-A's expectations/timing model to
  match the documented backoff, or clock-inject `dispatch_waiting`'s notion of "now" so
  the harness can fast-forward past backoff windows) before this wave ships.
- **Practical prod-impact reading**: worst case, freed heavy-task capacity across a
  burst of denied assigns can sit unused for up to the backoff ceiling (15s) before a
  queued task picks it up — a real (if bounded and previously-accepted) throughput
  regression versus pre-#195 behavior, not just a test artifact.

No other failures. All tests for #196 (session_store/auth), #197
(`tests/test_remote_tunnel.py`), #193 (`tests/test_remote_diagnostics.py`), and the
#198 structural checks (`tests/test_remote_pwa_scroll_pin.py`, 13 tests) passed clean.

## 2. Browser test — #198 mobile scroll pin

The live prod remote instance at `http://127.0.0.1:9999/KCwH-ccLz9cCmngZ_FqNJQ/`
answers (HTTP 200) but requires a password (`RemoteConfig.password_hash` is set on
this machine's `~/.takkub/remote.json`) that QA does not have and must not guess —
so real end-to-end login against the live instance was not attempted.

Instead: a throwaway fixture server
(`runtime/exports/2026-08-14/agent-takkub/qa-198-harness/harness.py`, not committed)
served the **real, unmodified** `src/agent_takkub/remote/static/{index.html,app.js}`
behind a fake `api/bootstrap|projects|usage|lead/history|sse-ticket|lead|verify-password`
backend (no password gate) plus a polling `cmd.json` command channel to push live SSE
events (`working`/`lead` lines) mid-test — same recipe as the existing
`role-memory/qa.md` note, filled in from scratch since the file endpoint list had
drifted (bootstrap/usage/projects-shape/sse-ticket-is-POST all needed fixing before the
app would authenticate — see harness.py history). Driven via Playwright MCP at a
390×844 mobile viewport with 60 seeded scrollback messages.

| Case | Result | Evidence |
|---|---|---|
| (a) scroll up mid-`working` → not yanked down | **PASS** — scrollTop held at 500 exactly (before: 500, after two `working`/new-lead-line pushes: still 500), floating button showed (`#new-msg-btn` gets `.show`, `display:flex`) | `screenshots/198-a-scroll-up-during-working.png` |
| (b) at bottom → new message autoscrolls | **PASS** — pinned at scrollHeight, new `push_lead_line` event auto-scrolled to new bottom, button stayed hidden (`display:none`, no `.show` class) | `screenshots/198-b-autoscroll-pinned.png` |
| (c) first open → lands at bottom | **PASS** — fresh load: `scrollTop 2815 == scrollHeight(3399) - clientHeight(584)` | `screenshots/198-c-first-open-bottom.png` |
| (d) switch view (Projects → Lead) → force-scrolls to bottom | **PASS** — scrolled to 300 before switch, landed at 2961/3577 (within `SCROLL_PIN_PX=48` of true bottom, i.e. `renderSelectedProject(true)` fired) | `screenshots/198-d-switch-view-force-bottom.png` |
| (e) backgrounded refresh (`visibilitychange`, simulated >30s away via `Date.now` fast-forward) → no yank | **PASS** — scrollTop 800 → 790 after silent `refreshOpenProjectHistories()` (natural DOM-height drift only, not a jump to bottom ~2989) | `screenshots/198-e-background-refresh-no-yank.png` |
| Floating "new messages ↓" button click → scrolls to bottom + hides | **PASS** — click moved scrollTop to exact bottom (2987/3571) and cleared the `.show` class | (covered by (a)→click sequence, see harness session) |

Note: (d) was exercised as a **view switch** (Projects↔Lead) rather than a true
cross-project switch, because the fixture only seeds one project — but it drives the
exact same code path (`switchView("lead") → renderSelectedProject(true)`) that a real
project switch uses, and the source-level test
(`test_switch_view_and_select_project_force_scroll`) confirms both call sites share
the identical `renderSelectedProject(true)` call. Reconnect (`es.onopen`) not-force-
scrolling was not independently re-driven live (a real EventSource reconnect mid-test
is awkward to stage against this fixture's single long-lived SSE stream) but is
covered by both the source-level test (`test_reconnect_does_not_force_scroll`) and is
functionally identical to the (e) code path already verified live.

No console errors during any of the above (only a benign Chrome DOM-accessibility
notice on the password-less pairing form, unrelated to app code).

## 3. Smoke — remaining issues (code + test read, no live process killed)

**#197 (tunnel PID file + orphan reaper)** — `src/agent_takkub/remote/tunnel.py`.
Read `Tunnel.start()`→`_write_pid_file` (writes `pid`, `owner_pid`,
`owner_create_time`, `instance_lock_id` to `runtime/tunnel/tunnel_pid.json` on every
successful start) and `reap_orphan_tunnel()` (boot-time: only kills a PID if the file
parses, the PID is alive, **and** the recorded `owner_pid`/`owner_create_time` no
longer matches a live process — i.e. only ever touches a tunnel *this* cockpit wrote
the file for; a user-started cloudflared with no PID file is never touched). Backed by
`tests/test_remote_tunnel.py` (8 dedicated tests: pid-file-written-with-owner-identity,
no-pid-file-when-verification-fails, stop-clears-matching-pid-file,
stop-leaves-other-tunnels-pid-file-alone, no-op-when-no-pid-file,
corrupt-pid-file-cleared-without-raising, live-tunnel-with-dead-owner-is-reaped,
live-tunnel-with-reused-owner-pid-is-reaped) — all passed in the full suite. Did not
kill a real tunnel process per task instructions; code+test coverage is convincing at
unit level. **PASS**.

**#193 (remote diagnostics)** — `src/agent_takkub/remote/diagnostics.py`. Three
read-only/best-effort probes: `describe_port_owner` (who's listening on the bind
port via psutil), `probe_http`/`probe_local`/`probe_public` (real GET, HTTP error
still counts as reachable), `check_ingress_mismatch` (compares `public_url` hostname
against the on-disk named-tunnel `config.yml`, flags a stale/orphaned cloudflared
routing an old hostname). Backed by `tests/test_remote_diagnostics.py` (17 tests
covering found/none/permission-denied/exception-safety for each probe) — all passed.
**PASS**.

**#194/#195 (main-thread stall throttle + gate-block flood)** — checked
`runtime/events.log` (2404 lines, live cockpit instance actively spawning panes
during this gate run). `main_thread_stall` events: 240 total across the whole log,
recent ones (last 10) spaced minutes apart and tied to spawn bursts (e.g. two at
20:08:45 during a heavy spawn, then quiet until 20:50:15/20:50:49 for the
backend#1/qa spawns of this very gate) — not a tight per-second flood.
`resource_gate_block` events: **0** occurrences in the entire log — confirms the
#195 dedupe/backoff (`resource_gate_unblocked` summary line replacing the old
per-retry-second flood) is working in this live instance, consistent with the
`dispatch_waiting()` design read above. **PASS at the live-log/smoke level** — but
see §1: the *deterministic* stress-test regression (STR-A) shows the same
backoff mechanism has an edge case (mass-denial burst + tight release loop) that
isn't exercised by this live instance's actual spawn pattern, so the live-log
"looks clean" observation does not contradict the pytest failure — different code
paths under different load shapes.

## Ship decision

**NO-GO.** Everything except one item is clean: #196/#197/#193/#198 all pass at
both unit and (for #198) live-browser level, and #194/#199 show no live-log
regressions. The blocker is narrow and precisely scoped: fix or intentionally
retire/update `tests/test_performance_stress_harness.py`'s STR-A scenario
(`tools/performance_reliability_stress.py::scenario_a`) so it reflects the #195
backoff design on purpose, rather than shipping with a known-red deterministic gate
test on `main`. Recommend routing back to whoever owns #195
(`resource_governor.py`) to either (a) inject a controllable clock into the stress
harness so the test can fast-forward through backoff windows, or (b) rewrite STR-A's
pass/fail assertions to match the documented "worst case 15s to notice freed
capacity" behavior instead of expecting immediate reuse.

---

## รอบ 2 (final) — HEAD 95365bb

Re-run on `main` directly (no worktree), HEAD confirmed via `git log` at
`95365bb` (`Merge wt/backend-2: capacity-epoch invalidation stops backoff
starving freed slots (#201)`), which also includes #200 (`71b5a88`, Pulse shows
every open pane). Adds two fixes since รอบ 1: **#201** (backoff-eats-freed-slot,
`bf7397f`) and **#200** (Pulse team visibility, `ed2d466`).

### 0. Environment check

`.venv/Scripts/python.exe -c "import agent_takkub; print(agent_takkub.__file__)"`
→ `C:\Users\monch\WebstormProjects\agent-takkub\src\agent_takkub\__init__.py`. Clean —
no worktree/other-repo contamination.

### 1. Full pytest suite

```
5937 passed, 7 skipped in 434.67s (0:07:14)
```

Zero failures — includes `test_deterministic_stress_harness_a_through_i` (STR-A) now
green. Confirmed the fix did **not** weaken the assertion to pass: `git show bf7397f`
and `git log` show #201 touched only `resource_governor.py` +
`tests/test_resource_governor.py` (+docs) — **not**
`tools/performance_reliability_stress.py` at all (no commits to that file since the
1.0.60 release that introduced it). Read the live assertions directly
(`tools/performance_reliability_stress.py:107-111`): still `len(set(admitted_ids)) ==
30`, `max_heavy <= limits.max_heavy_global`, `max_per_project <=
limits.max_heavy_per_project`, `active_heavy_tasks == 0`, `queued_resource_tasks ==
0` — the exact same strict admitted=30/queued=0 bar as รอบ 1, unmodified. The fix
(capacity-epoch invalidation, `resource_governor.py:409-472`) genuinely resolves the
mass-denial-burst timing gap รอบ 1 found, not a test relaxation.

### 2. Browser test — #200 Pulse shows every open pane (Playwright MCP, 390×844)

The live prod remote instance still requires a password QA doesn't have (same
constraint as รอบ 1's #198 pass), so a fresh throwaway fixture server was built:
`runtime/exports/2026-08-14/agent-takkub/qa-200-harness/harness.py` (not committed),
serving the **real, unmodified** `static/{index.html,app.js}` behind a fake
`/api/activity` payload covering: a project with working+idle teammates mixed, non-
claude providers (codex/gemini/opencode), and a second project with only an idle
lead and no roles.

| Case | Result | Evidence |
|---|---|---|
| (a) every open pane shown, working + idle, not just Lead | **PASS** — `agent-takkub` card shows lead + frontend + backend + qa + devops (5 chips); `side-project` card shows its idle lead alone | `screenshots/pulse-mobile-full.png` |
| (b) each chip shows role + provider + state/time | **PASS** — `frontend · OpenAI 2:00`, `backend · Gemini idle`, `qa · Claude 0:45`, `devops · OpenCode idle`, lead chip shows provider identity + `12:34` | same |
| (c) headline count = working only, not total panes | **PASS** — `"3 ตำแหน่งกำลังทำงาน"` = lead+frontend+qa (all `state:"working"`); the 2 idle roles and side-project's idle lead correctly excluded | same |
| (d) no task/cwd/command leakage | **PASS** — fixture's `/api/activity` payload (role/state/runtime_sec/provider only, no task/cwd/command fields exist to leak) is what the page renders verbatim; screenshot shows no such text anywhere | same, `curl` of the endpoint |
| (e) non-claude providers (codex/gemini/opencode) render without crashing/mislabeling | **PASS** — all three render distinct provider identity + color, none fall back to "claude" | same |
| (f) new caption text | **PASS** — `#pulse-caption` shows exactly `"แสดงเฉพาะ role + สถานะ + เวลาทำงาน — ดู task จริงที่หน้า Lead"` | same |

Console log clean (only a benign Chrome password-field accessibility notice, same as
รอบ 1's #198 harness — unrelated to app code).

### 3. Regression spot-check

**#198 scroll pin vs #200's app.js edit**: `git diff 71b5a88~1 71b5a88 --
src/agent_takkub/remote/static/app.js` (the actual #200 code commit, `ed2d466`) shows
**zero hunks** — #200 was a backend-only fix (`api.py` + new `config.py` flag
`PULSE_SHOW_TEAM`); the frontend `renderPulse`/`makeRoleChip` code that consumes the
now-unlocked data already existed pre-#200 and was untouched by it. `isPinnedToBottom`
et al. (`app.js:906-1943`) are still present and unmodified. Confirmed #201
(`bf7397f`) doesn't touch `app.js` at all either (backend-only). **Risk: none found**
— did not need to re-run the full รอบ-1 #198 fixture since there's no code-level
overlap to regress.

**events.log — live-instance check (`runtime/events.log`, this project's cockpit):**
Found an **active, ongoing `resource_gate_block` flood** at the time of this gate:
240 consecutive lines for the same `pane_id=backend#3`,
`task_id=1745b3b00b974b1bb5cf11ad39aefd68`, `resource_class=package_install`,
`reason=heavy_project_limit` — every interval between lines measured **exactly
1.0s** (computed from all 240 deltas, zero variance), never growing to the
2s/5s/15s the #201-fixed backoff schedule requires. The last capacity-changing event
(`resource_slot_released`) in the log was at `21:22:47`, ~8 minutes before the flood
started at `21:30:21` and continuing — so this isn't even the "epoch churn from
unrelated releases" tradeoff #201's own docstring calls out
(`resource_governor.py:426-431`); there's no epoch bump happening at all during the
flood window, yet retries never back off past 1s.

**Root-caused, not guessed**: cross-checked against `Get-CimInstance Win32_Process`
— the two running `pythonw.exe -m agent_takkub` cockpit orchestrator processes have
`CreationDate` `19:35:35` and `19:40:19`, both **before** #201's commit timestamp
(`bf7397f`, `21:20:19`). A long-running Python process doesn't hot-reload edited
source files, so this live orchestrator is still executing the **pre-#201**
`dispatch_waiting` in memory — the exact stale-backoff symptom the fix targets,
reproducing live because the fix hasn't been loaded yet, not because the fix is
broken. The STR-A unit test (fresh interpreter per pytest run) correctly exercises
the fixed code and passes.

**Not a code blocker** — but flagging because it's a real, user-visible operational
gap: `takkub restart` (or otherwise relaunching the cockpit) is required for #201 to
actually take effect on this or any other already-running instance. Recommend Lead
call this out when shipping/announcing #201, not just merging it.

`main_thread_stall`: sparse, 5s–20min gaps tied to active-pane output bursts, not a
tight flood — consistent with รอบ 1's read. No new concern here.

### Ship decision (รอบ 2, final)

**GO.** Full suite green (5937 passed/7 skipped/0 failed) including the previously
red STR-A, with its strict assertions confirmed unweakened by reading #201's diff.
#200 verified live on a mobile viewport across all 6 acceptance criteria with
screenshot evidence, DATA-MIN payload confirmed, non-claude providers render
correctly. No regression found in #198's scroll-pin code from either #200 or #201
(zero app.js overlap). One non-blocking operational note: the currently-running
cockpit orchestrator process(es) predate #201's fix and will keep exhibiting the old
1s-flat retry-flood behavior on live `resource_gate_block` events until restarted —
recommend a `takkub restart` as part of shipping this wave, not a code change.
