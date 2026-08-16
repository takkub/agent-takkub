# Issue #260 — Manual Disable revokes remote pairing identity

Date: 2026-08-16  
Scope: follow-up security regression from #252 / commit `1a1dd52`

## Problem

The #252 fix correctly kept `RemoteConfig.secret_path` and `token` across an
idle watchdog auto-suspend, allowing a paired phone to reconnect after the next
cockpit launch without scanning a new QR code. However, Settings reused the same
identity for every Enable by default. A user who clicked Disable after losing a
phone could later Enable remote control and unintentionally restore that lost
phone's pairing URL.

Clearing password sessions alone was insufficient: a device that retained the
pairing URL still held the long-lived `secret_path` and bearer `token`, and could
authenticate again after the user supplied or the device otherwise obtained the
password.

## Lifecycle semantics after the fix

| Event | `enabled` on disk | Pairing identity | Next start |
|---|---:|---|---|
| Idle watchdog auto-suspend | unchanged (`true`) | preserved | reuses the existing QR/link (#252) |
| User clicks Disable | `false` | `secret_path` and `token` cleared | mints a new QR/link on the next Enable (#260) |
| User clicks Log out all devices | unchanged | preserved | existing link remains, password session is required again |

The idle watchdog does not call the manual Settings disable path, so the two
meanings are separated by control flow as well as UI wording.

## Implementation

- `UserActionsMixin._apply_remote_config(..., enable=False)` now clears both
  halves of the persisted pairing identity before saving, alongside the existing
  `session_store.clear()` call.
- `RemoteSettingsDialog` no longer offers the unsafe unchecked "Generate a new
  pairing link" option. The live action is labelled **Disable & revoke pairing**.
- The dialog explains that idle auto-suspend preserves pairing, while manual
  Disable invalidates the link and requires phones to scan the newly minted link.
- After a successful manual Disable, the dialog clears its in-memory copy of the
  identity. This closes the same-dialog Disable → Enable path; otherwise the
  stale constructor-time config could have resurrected the old values even
  though disk state was already safe.
- Enable still forwards an existing identity when one is present. This is the
  auto-suspended/restart case required by #252. Blank values continue to use
  `RemoteControl._start()`'s existing cryptographically random minting path.

The UI addition uses inherited cockpit styling and introduces no raw palette,
font, radius, or QSS values, following `cockpit-ui-style`.

## Regression coverage

Updated targeted tests assert that:

- manual Disable persists `enabled=false` and blank `secret_path`/`token`;
- manual Disable still clears all persisted password sessions;
- Disable → Enable in one open dialog forwards blank identity so startup mints a
  new pair;
- an auto-suspended config forwards and persists its existing identity;
- Settings exposes the revocation semantics in the live button label.

Verification command (repo `.venv`, worktree source explicitly prepended because
the shared editable install points at the primary checkout per #202):

```powershell
$env:QT_QPA_PLATFORM='offscreen'
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
& 'C:\Users\monch\WebstormProjects\agent-takkub\.venv\Scripts\python.exe' -m pytest `
  tests/test_remote_chip.py `
  tests/test_remote_settings_dialog.py `
  tests/test_remote_scaffold.py -q
```

Result: all targeted tests passed. Ruff lint and format checks passed for all
five touched Python files, and `git diff --check` passed.

## Constraints checked

- No full-suite run.
- No commit or push.
- No changes to `lead_inbox.py`, `lead_wait.py`, `task_delivery.py`,
  `pty_session.py`, `provider_spec.py`, or `worktree_manager.py`.
- Logic is platform-neutral; the only platform-specific path above documents the
  required local verification interpreter and is not production code.
