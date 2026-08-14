# #197 + #193: remote tunnel orphan repro, and the fixes

Backend worktree `wt/backend-2-1786712325`, 2026-08-14. Covers two issues:

- **#197** — cockpit closes, `cloudflared`/tunnel wrapper doesn't die.
- **#193** — dev/prod port collision makes a second instance's remote link
  "die silently" (a QR code that never connects, no error shown).

Both were reproduced for real before any fix — no guessing.

## #197 — repro matrix

Standing code already covers a lot: `remote/__init__.py:108`
(`app.aboutToQuit -> RemoteControl.stop()`), `app.py`'s `_kill_all` (atexit +
SIGINT/SIGTERM/SIGBREAK, `main_window.py`'s `closeEvent`), and
`tunnel.py`'s Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`,
kernel-enforced cleanup on process death). The question was: which of those
four layers has a real gap.

All repros used `ping -t 127.0.0.1` spawned through `tunnel._spawn` (the
exact `cmd /d /c ...` wrapper + Job Object path the real cloudflared spawn
uses) as a cheap stand-in — same process tree shape, no cloudflared binary
needed (none is installed on this dev machine).

| # | Case | Method | Result |
|---|------|--------|--------|
| a | Normal window close | Existing unit tests (`test_close_event_remote_stop.py`, `test_app_remote_teardown.py`) — `aboutToQuit`/`_kill_all` both call `remote.stop()` → `Tunnel.stop()` → `_tree_kill` (`taskkill /PID <pid> /T /F`, synchronous) | **Covered.** No gap. |
| b | Hard-kill (`Stop-Process -Force` / Task Manager End Task) of the process that owns the Job Object | Spawned `cmd.exe → ping.exe` via `tunnel._spawn`, called `_create_kill_on_close_job` + `_assign_to_job` for real, then `Stop-Process -Force` on the **parent Python process only** (no `/T`, no signal) | **Protected.** Both `cmd.exe` and `ping.exe` died automatically — Windows' kernel-level job-close cleanup works exactly as documented, even through the `cmd.exe` wrapper (children inherit job membership by default). |
| b′ | Hard-kill when Job Object creation/assignment fails (the *documented* best-effort fallback — `_create_kill_on_close_job` returns `None` on AV interference / `OpenProcess` denied / old Windows without job nesting) | Same spawn, but `tunnel._create_kill_on_close_job` monkeypatched to return `None` (simulating the failure mode the code itself says can happen), then hard-killed the parent the same way | **ORPHANED.** `cmd.exe` (pid 12216) and `ping.exe` (pid 20840) both kept running indefinitely after the owning process was gone. **This is the real, provable gap** — zero recovery today. |
| c | shim (`.venv\Scripts\pythonw.exe`) + real interpreter as two live processes | Inspected real running processes on this machine (`Get-CimInstance Win32_Process`) — confirmed both prod (pid 11748 shim → 16040 real) and dev (pid 2776 shim → 19952 real) cockpits are running exactly this two-process shape right now. The outer `pythonw.exe` is Python's own venv launcher stub (not cockpit code) — it just execs+waits on the real interpreter as a child. `RemoteControl`/the Job Object live entirely inside the **inner** (real) process, since that's the one that imports PyQt6 and shows the window. | Killing only the outer shim doesn't touch the inner process or its tunnel — not a bug (nothing is orphaned, the tunnel's owner is still alive). Killing the inner process (Task Manager targets whichever process owns the visible window, i.e. the inner one) is the **same case as (b)/(b′)** above — same fix applies regardless of the shim wrapping. |
| d | User-started cloudflared outside the cockpit | Not reproduced (out of scope — the cockpit correctly has zero knowledge of a process it never spawned) | By design, the fix in this issue must never touch this case — see "never touches a process this cockpit didn't itself register" in `reap_orphan_tunnel()`'s docstring. |

**Conclusion:** the orphan bug is real and lives specifically in case (b′) —
the Job Object layer is explicitly documented as best-effort ("never
raises, a failure here just means H-E's Windows layer is absent") and
nothing backstops that failure. `_tree_kill`'s `taskkill /T /F` (item 4 of
the issue, "kill process tree จริงบน Windows") was **already** implemented
correctly before this change — verified via `TestStopTreeKill` and the live
repro above; no changes were needed there.

## #193 — repro (already proven by the reporter, re-verified)

Live processes on this machine right now, both cockpits currently running:

```
prod: C:\Users\monch\.agent-takkub\venv\Scripts\pythonw.exe -m agent_takkub  (pid 11748 → 16040)
dev : .venv\Scripts\pythonw.exe -m agent_takkub                             (pid 2776 → 19952)
```

Root cause traced to `remote/settings_dialog.py:71`:

```python
_FIXED_PORT = 9999
```

This is **hardcoded** — every cockpit instance's Settings dialog always
builds `RemoteConfig(bind_port=9999, ...)` on Apply, regardless of dev vs.
prod vs. a second install. That's exactly why both `remote.json` files the
reporter found have `bind_port: 9999` — it's not a coincidence, it's the
only value the UI is capable of writing. `http_server.start_server` already
scans forward on a bind conflict (`port = bind_port + offset`) so a second
instance *can* come up on a different port — but that fallback was
completely silent: `config.bind_port` on disk is never updated, the
Settings dialog's port label is a static `9999`, and nothing told the user
which instance actually owns which port or whether the pairing link/QR
they're looking at is live.

## Fixes shipped

### #197
- `tunnel.py`: `_write_pid_file`/`_clear_pid_file`/`_clear_pid_file_if_matches`/`reap_orphan_tunnel` — a PID file (`RUNTIME_DIR/tunnel/tunnel_pid.json`) written on every successful `Tunnel.start()`, holding `pid`, `started_at`, `config_path`, `instance_lock_id` (uuid4, per-start, log/debug correlation only), `owner_pid`, `owner_create_time` (psutil `create_time()`, guards against PID reuse — a bare "is this pid alive" check would wrongly call a reused PID "still owned" forever).
- `remote/__init__.py`: `RemoteControl.maybe_start()` calls `tunnel.reap_orphan_tunnel()` **unconditionally, before the `enabled` check** — an orphan from a previous session must be cleaned up on this boot even if this session doesn't want remote control on.
- `Tunnel.stop()` now also clears the pid file (only if it still matches this tunnel's own pid — never blows away a different `Tunnel` instance's file).
- `RemoteControl.stop_tunnel_only()` (new) — kills just the tunnel subprocess, keeps the HTTP server/notifier alive.
- UI: sidebar tunnel dot tooltip now includes the pid (`status_header.py::_refresh_tunnel_indicator`); Settings dialog gained a "⏹ Stop tunnel only" button wired through `user_actions.py::_stop_remote_tunnel_only`.
- `_tree_kill` (Windows `taskkill /T /F`) — already correct, no change needed (see repro table).

### #193
- `remote/diagnostics.py` (new): `describe_port_owner` (psutil, who's on a given port), `probe_http`/`probe_local`/`probe_public` (real GET, not "the subprocess launched"), `check_ingress_mismatch` (compares `public_url`'s hostname against the ingress hostname baked into the on-disk `config.yml`).
- `RemoteControl._start()`: after binding, compares the actual bound port (`self._server.port`) against `config.bind_port` — if `start_server`'s scan-forward silently picked a different port, `port_conflict_note` names who's holding the requested one. Also runs a synchronous loopback probe (`local_probe_note`) and the ingress-hostname-mismatch check (`hostname_mismatch_note`).
- `user_actions.py::_apply_remote_config`/`_remote_start_warning`: collects those notes plus (when the public URL is already known — named-tunnel/ngrok-fixed) a real probe through the tunnel edge, joined into one warning string returned alongside `ok=True` and a working pairing URL — Enable still succeeds, this is a heads-up, not a failure.
- `settings_dialog.py::_on_toggle`: shows that warning via `QMessageBox.warning` after a successful Enable.
- Multi-instance auto-pick (item 4): `start_server`'s existing scan-forward already does this — no behavior change there, only the above transparency around it. A hard-block-on-collision mode was considered and rejected: the auto-pick already produces a working instance, blocking it would be strictly worse UX for zero correctness gain.

## Known follow-up (not done here, flagged not silently dropped)

- The public-URL probe only runs synchronously for tunnel modes whose URL is known upfront (named/ngrok-fixed). Quick-tunnel and ngrok-random capture their URL asynchronously (`_poll_remote_public_url`) and are not currently probed once captured — same diagnostic could be added to that poll's success branch.
- `_FIXED_PORT = 9999` in `settings_dialog.py` is still a single hardcoded default across all instances — the actual collision is now transparent (auto-pick + a clear note) rather than eliminated at the source. Making it configurable per-instance was out of scope for this pass.

## Tests

New: `tests/test_remote_diagnostics.py` (18 cases — port-owner lookup, HTTP
probe, ingress-mismatch), plus additions to `tests/test_remote_tunnel.py`
(pid-file write/clear, `reap_orphan_tunnel` — dead tunnel, live-matching-owner,
dead-owner, PID-reuse, corrupt file), `tests/test_remote_scaffold.py`
(boot-time reap wiring, diagnostic notes on `_start()`, `stop_tunnel_only`),
`tests/test_remote_chip.py` (warning-message aggregation, stop-tunnel-only
plumbing, pid-in-tooltip), `tests/test_remote_settings_dialog.py`
(stop-tunnel button visibility/click, warning-on-success dialog).

`pytest -k remote`: **550 passed**, 0 failed.
`lint-imports`: 24 contracts kept, 0 broken.
`ruff check`: clean on all touched files.
