# #196 — remote password sessions now survive a cockpit restart

## Symptom

Remote (mobile) required re-entering the password every time the cockpit
process restarted, even though the phone's `localStorage` still held its old
`X-Session` token (`static/app.js` lines 403-429 were already correct).

## Root cause

`AuthGate._sessions` (`remote/auth.py`) was a plain in-process `dict`. A
fresh `AuthGate` is constructed on every server start
(`http_server.py::RemoteHttpServer.__init__`), so a restart — or having two
cockpit instances (#193), each with its own `AuthGate` — always started with
an empty session table. `RemoteConfig` (including `password_hash`) already
persisted fine; only the per-client session credentials didn't.

## Fix

New module `remote/session_store.py` persists `{token_hash: expiry}` to
`<SETTINGS_HOME>/takkub-remote-sessions.json` (atomic tmp+rename, 0o600 —
same pattern as `RemoteConfig.save()`). `AuthGate` loads it at construction
and writes back on mint/prune, throttled to at most once per 30s on a plain
sliding-expiry touch (the phone polls `/api/pulse` every 5s; unthrottled
writes would mean an fsync-ish write several times a second per connected
phone).

## Threat model — what's stored, for how long, invalidated when

**What's on disk:** a SHA-256 hash of each session token, plus its expiry
epoch, plus a fingerprint of the auth material that minted it. **The raw
session token is never written to disk** — same reasoning as
`RemoteConfig.password_hash` never holding a plaintext password. A read of
the session-store file alone is not enough to authenticate; the attacker
would still need the original token to reproduce its hash.

**How long a session lives:** sliding expiry, same as before this fix —
`idle_expire_min` (default 240 min) from the last successful use, or a 4h
fallback if idle-expire is disabled (`_password_session_ttl_sec`). Restart
does not reset this: a session's remaining TTL survives a restart, it just
doesn't get *extended* by up to 30s of unpersisted sliding-expiry writes
(bounded staleness, not a security loss — worst case a session looks
~30s more idle than it really was).

**Invalidation triggers** (all sessions, not just one):
1. **Idle timeout** — the existing sliding-expiry / idle-expire mechanism,
   unchanged.
2. **Password change** — the settings dialog's Enable flow always calls
   `hash_password()` fresh (a new random salt every call, even for the same
   plaintext — see `TestPasswordHashing.test_hash_is_salted_differently_
   each_time`), which changes `password_hash`. `session_store.py`'s
   fingerprint is `sha256(password_hash|secret_path|token)`, so a changed
   `password_hash` makes every prior session's fingerprint stop matching —
   `load()` returns `{}` for the new `AuthGate`. No explicit invalidation
   call needed; this falls out of the fingerprint design for free, and the
   same reasoning covers a future `secret_path`/`token` rotation.
3. **Disabling remote** — `user_actions.py::_apply_remote_config`'s
   disable branch calls `session_store.clear()` explicitly (deletes the
   file outright), rather than leaning on the fingerprint match a later
   re-enable would produce anyway. "ปิด remote" is its own explicit trigger
   per the issue, not just an implicit side effect.
4. **"Log out all devices"** — new button in the Settings dialog
   (`RemoteSettingsDialog._on_logout_all_clicked` →
   `user_actions.py::_logout_all_remote_sessions`). If the server is live,
   goes through `AuthGate.logout_all_sessions()` so already-connected phones
   are cut off on their very next request; either way the on-disk store is
   cleared, so a later restart can't resurrect anything.

**What did NOT change (must not regress):**
- A bearer token alone is still not enough — `password_ok()` still requires
  a live `X-Session` on top of the token when a password is configured (the
  original H1 fix, 2026-07-07 audit).
- `lockout_after_fails` still arms independently for the token and the
  password (`_fail_count` / `_pw_fail_count`), unaffected by this change.
- Session tokens are still minted with `secrets.token_urlsafe(24)` and
  compared via hash lookup (not `hmac.compare_digest` on the raw string, but
  dict-key lookup on a SHA-256 hash is likewise not vulnerable to a
  practical timing attack over a network round-trip).

## Tests

- `tests/test_remote_session_store.py` — the storage primitives
  (`fingerprint`, `hash_token`, `load`/`save`/`clear`, corrupt-file handling,
  file permissions).
- `tests/test_remote_auth.py::TestPasswordSessionPersistence` — the
  behaviors the issue asked for: a session surviving a simulated restart, an
  expired one not surviving, a password change invalidating everything,
  logout-all clearing the store, and only a hash ever hitting disk.
- `tests/test_remote_chip.py` / `tests/test_remote_settings_dialog.py` —
  disable clears the store; the new "Log out all devices" button wiring,
  both live and not-live.
