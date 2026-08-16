# #252 — remote-control dies every morning (backend half)

## Bug, proven

prod evidence (not guessed):

- Last mobile request: 2026-08-15 20:55:36
- `remote.json` rewritten with `enabled:false`: 2026-08-16 00:56:30 — exactly
  +240 minutes, the default `idle_expire_min`.
- No `cloudflared` process left running; the public URL answered `HTTP 530
  error code: 1033` (tunnel gone).
- Root cause: `RemoteControl._check_idle_expire()`
  (`src/agent_takkub/remote/__init__.py`) called `self.stop()` **and then**
  persisted `config.enabled = False` to disk. The next cockpit boot's
  `RemoteControl.maybe_start()` reads exactly that flag — indistinguishable
  from the user clicking Disable — and returns `None` without starting
  anything. Remote control stayed off until someone noticed and reopened
  Settings to flip it back on by hand.

Three required fixes, addressed below in the same order as the task spec.

## 1. Idle-expire no longer persists `enabled=false`

`_check_idle_expire()` still calls `self.stop()` — the HTTP server, Lead
notifier and tunnel subprocess are torn down for real, exactly as before.
Nothing about the security posture changes: a phone that's gone quiet for
`idle_expire_min` stops being reachable exactly as it did previously.

What changed is that `config.enabled` is left alone. That one bit is the
only thing `maybe_start()` checks at boot, so leaving it at whatever the user
last set it to means:

- **Idle-expired overnight, cockpit reopened next morning** → `enabled` is
  still `True` on disk → `maybe_start()` starts remote control again, no
  user action needed. This is the actual bug fix.
- **User clicks Disable in Settings** → `_apply_remote_config`'s disable
  path (`user_actions.py`) still explicitly sets `enabled=False` and clears
  every session — unchanged, still a real, sticky "off".

To make the distinction legible in code (not just "the field didn't
change"), added `RemoteControl.auto_suspended: bool` — `False` normally,
flipped to `True` only inside `_check_idle_expire`'s own idle branch. It's
the one attribute a caller can check to tell "still configured on, just
idle-suspended right now" apart from "genuinely never started".

### The stale-chip bug that fix uncovered

`_check_idle_expire()` had no way to tell `MainWindow` it had just killed the
server out from under it — `self._remote` on the Qt side kept pointing at a
now-empty `RemoteControl` shell, so the 🌐 chip kept reading "live" (`self._remote
is not None`) even though the server was gone. This predates #252; fixing
`_check_idle_expire` properly meant also closing this, otherwise "auto-suspend
successfully, chip still shows ●" would have been a new user-visible bug in the
next session.

Fix: `RemoteControl.__init__`/`maybe_start` take an optional
`on_auto_suspend` callback, invoked (wrapped in its own try/except — this
runs inside a QTimer slot) right after `self.stop()`. Wired at both call
sites that create a `RemoteControl`:

- `main_window.py::_boot()` (cold boot)
- `user_actions.py::_apply_remote_config()` (manual re-enable creates a
  fresh instance too — same staleness risk if idle-expire fires again later
  in that instance's lifetime)
- `headless_window.py::boot()` (no chip to repaint, but still drops the
  dead handle so `shutdown()`'s `self._remote is not None` check is
  accurate)

`UserActionsMixin._on_remote_auto_suspended()` is the concrete callback for
the Qt path: clears `self._remote`, repaints the chip, and drops a status-bar
note ("🌐 Remote control auto-suspended (idle) — back on next launch").

## 2. Re-enable no longer forces a re-pair

Root cause: `RemoteSettingsDialog.build_config()` always built a *brand new*
`RemoteConfig()`, defaulting `secret_path`/`token` to `""`. `_on_toggle()`
never carried the dialog's own `current: RemoteConfig` (the on-disk config it
was constructed with) forward into that call. `_apply_remote_config()` then
did `config.enabled = True; config.save()` — overwriting `remote.json`,
wiping the previously-minted `secret_path`/`token` in the process. The next
`RemoteControl._start()` saw both blank and minted a fresh pair (its
`if not self.config.secret_path or not self.config.token:` gate), rotating
the pairing URL/QR under a user who never asked to rotate anything. Every
Enable — including the idle-auto-suspend → re-enable path from fix #1 —
forced every already-paired phone to re-scan.

Fix:

- `build_config()` gained `secret_path: str = ""` / `token: str = ""` kwargs.
  Default stays blank (every existing caller/test that omits them keeps the
  "mint fresh" behavior for a brand-new setup — verified by
  `test_secret_path_and_token_default_blank`).
- The dialog now keeps its constructor's `current` as `self._current` and,
  in `_on_toggle()`, passes `self._current.secret_path` /
  `self._current.token` through by default — so a re-enable reuses exactly
  what was already on disk, and `_start()`'s mint gate never fires.
- Added an explicit, opt-in **"Generate a new pairing link"** checkbox
  (unchecked by default, and reset to unchecked every time the dialog goes
  back to Disabled) for the one case where the user genuinely wants a fresh
  QR — lost/stolen phone, etc. Checking it passes `secret_path=""`/`token=""`
  through instead, restoring the old mint-fresh behavior on demand.

**Design decision, not auto-applied:** rotating on a tunnel-identity change
(new domain, switching Cloudflare↔ngrok, named↔quick) was considered and
rejected. `secret_path`/`token` authenticate a session against *this
server*, independent of which transport carries the request — and the old
pairing link already stops resolving the instant `public_url` changes, since
that's baked into the URL itself (`RemoteConfig.pairing_url()`). Forcing a
rotate on top of an already-broken link adds re-pairing friction with no
matching security benefit. Rotation stays a deliberate, single action
(the checkbox) rather than something inferred from which other fields the
user happened to touch. Documented in `build_config()`'s docstring so this
isn't re-litigated blind next time someone touches this file.

Session invalidation (the existing "Log out all devices" button) is
untouched and remains the right tool for "kick every phone now" — it
operates on `session_store`, a layer above the secret_path/token pairing
identity, so it doesn't need rotation to do its job.

## 3. `idle_expire_min` is now a real setting

Was hardcoded to `_IDLE_EXPIRE_MIN = 240` inside `build_config()` with no UI
control, and — same root cause as #2 — silently reset to 240 on every Enable
because the dialog never read the value back out of `current`.

Fix:

- `build_config()` gained `idle_expire_min: int = _IDLE_EXPIRE_MIN` (default
  unchanged, so every caller that omits it — including the fixed test suite
  — keeps getting 240; only the dialog now passes a live value through).
- Added a `QSpinBox` ("Idle auto-suspend after:", range 0–1440 minutes,
  prefilled from `current.idle_expire_min`) to the dialog, disabled while
  live (same edit-gating pattern as every other field in this dialog).
  `0` uses `setSpecialValueText("off (never auto-suspend)")` — matches
  `AuthGate.idle_expired()`, which already treats `idle_expire_min <= 0` as
  "never expire" (`auth.py:314`, pre-existing — this UI is the first way to
  actually reach that value without hand-editing `remote.json`).
- Removed the stale "Preset: idle-expire 240min · …" note that no longer
  described reality once the value became user-editable.

## Files touched

- `src/agent_takkub/remote/__init__.py` — `_check_idle_expire` no longer
  persists `enabled=False`; `auto_suspended` flag; `on_auto_suspend` hook.
- `src/agent_takkub/remote/auth.py` — comment only, matches new semantics.
- `src/agent_takkub/remote/settings_dialog.py` — `build_config()` gains
  `secret_path`/`token`/`idle_expire_min`; dialog gains the rotate checkbox
  + idle-expire spinbox; `_on_toggle()` wires both through.
- `src/agent_takkub/user_actions.py` — `_on_remote_auto_suspended()`; both
  `maybe_start()` call sites in this file pass it (boot handled in
  `main_window.py`).
- `src/agent_takkub/main_window.py` — boot-time `maybe_start()` call passes
  `on_auto_suspend`.
- `src/agent_takkub/headless_window.py` — same wiring, headless variant.
- `tests/test_remote_scaffold.py` — new `TestIdleAutoSuspend` (5 tests):
  server actually stops, `enabled` stays `True` on disk, callback fires
  (and a failing callback doesn't escape the QTimer slot), not-yet-idle is
  a no-op, and a simulated reboot after idle-suspend starts remote control
  again (the literal field bug, reproduced and asserted fixed).
- `tests/test_remote_settings_dialog.py` — new `build_config` tests for
  `secret_path`/`token`/`idle_expire_min` forwarding + defaults; new
  `TestDialogPairingReuse` (reuse-by-default, rotate-checkbox-mints-fresh,
  checkbox resets after Disable) and `TestDialogIdleExpireSetting`
  (prefill, forwarded on Enable, survives a re-enable round trip).
- `tests/test_remote_chip.py` — pre-existing `maybe_start` monkeypatches
  used bare `lambda orch: fake_remote`; updated to `lambda orch, **kw: ...`
  since production now calls with `on_auto_suspend=`. Behavior-neutral test
  fix, not a functional change to what those tests assert.

## Verification

Ran targeted, not full suite (per repo policy — QA runs the full suite once
at batch gate):

```
tests/test_remote_scaffold.py
tests/test_remote_settings_dialog.py
tests/test_remote_chip.py
tests/test_remote_auth.py
tests/test_headless_entrypoint.py / test_headless_pane.py / test_headless_window.py
tests/test_single_instance_watchdog.py / test_orphan_worktree_prune_guard.py / test_cli_bind_error.py
```

All green. The last three were run because they exercise `MainWindow._boot()`
/ the CLI-bind path this change touches indirectly.

## Out of scope (flagged, not fixed here)

- `src/agent_takkub/remote/static/app.js` — frontend is working this in
  parallel per the task spec; not touched.
- Auto-resuming remote control *within the same still-running session*
  (i.e., without a cockpit restart) after an idle-suspend was not
  implemented — the task's stated bar was "reopen cockpit in the morning
  and it's back", which the `config.enabled` fix satisfies. Resuming
  mid-session would need either a foreground-activity trigger or a second
  timer and wasn't asked for; flagging in case product wants it later.
