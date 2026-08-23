"""session_store.py — on-disk persistence for password-session credentials
(#196: a phone had to re-enter the password every time the cockpit process
restarted, because `AuthGate._sessions` used to live only in RAM).

State file: ``<SETTINGS_HOME>/takkub-remote-sessions.json`` (atomic
tmp+rename, 0o600 — same pattern as `config.py`'s ``remote.json``). Only a
SHA-256 hash of each session token is ever written, never the raw token
(same reasoning as `RemoteConfig.password_hash`: disk contents alone must
never be enough to authenticate).

Invalidation is fingerprint-based, not event-based: every record on disk is
stamped with a hash of the auth identity (`password_hash` + `secret_path` +
`token`) that minted it. `load()` compares that stamp against the *current*
config's fingerprint and discards the whole file on any mismatch. Since the
only way to reach `enabled=True` is through the settings dialog's Enable
flow — which always calls `hash_password()` fresh (a new random salt every
time, even for an unchanged plaintext) — a password change, a disable-then-
re-enable, or a `secret_path`/`token` rotation all change the fingerprint
and so invalidate every prior session for free. A bare process restart with
unchanged config keeps the same fingerprint, which is the one case that
must keep working (#196).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ..config import SETTINGS_HOME
from .config import RemoteConfig

_PATH = SETTINGS_HOME / "takkub-remote-sessions.json"


def path() -> Path:
    """Where state lives. Function form so tests can monkeypatch `_PATH`."""
    return _PATH


def hash_token(token: str) -> str:
    """One-way lookup key for a session token — never write the raw token.

    Not password hashing: `token` is always `secrets.token_urlsafe(24)`
    (192 bits of server-generated randomness — see `AuthGate.
    issue_password_session()`), never a human-chosen secret. A slow KDF
    (PBKDF2/bcrypt/scrypt, as used for `RemoteConfig.password_hash` in
    `hash_password()`) defends against dictionary/brute-force attacks on
    *low*-entropy input; it buys nothing here since exhausting a 192-bit
    space is infeasible at any hash speed, and would add real per-request
    CPU cost to every polling client for zero security gain.
    """
    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()  # codeql[py/weak-sensitive-data-hashing]


def fingerprint(config: RemoteConfig) -> str:
    """Identity stamp for the auth material currently in effect. Sessions
    minted under a different fingerprint (password/secret_path/token
    changed since) are never treated as valid — see module docstring.

    Not a security boundary: this is a local change-detector, never sent
    over the network or accepted as a credential. `config.password_hash`
    here is already the PBKDF2 digest (never the raw password — see
    `auth.hash_password()`), so hashing it again adds no security either
    way; SHA-256 just gives a fixed-length key to compare/store.
    """
    raw = f"{config.password_hash}|{config.secret_path}|{config.token}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()  # codeql[py/weak-sensitive-data-hashing]


def load(current_fingerprint: str) -> dict[str, float]:
    """Read ``{token_hash: expiry_epoch}``. Missing/corrupt/fingerprint-
    mismatched -> ``{}`` — never raises, never creates the file.

    ``TAKKUB_V2_AUTHORITY`` (#362 Phase 10 wave 2, default off): when on and
    the dual-written ``v2/`` mirror exists, sanitizes THAT instead of the V1
    file — same sanitizer either way. Falls back to V1 on any v2 miss.
    """
    from ..core.storage.v2_authority import read_remote_sessions, v2_authority_enabled

    data: dict | None = None
    if v2_authority_enabled():
        v2_doc = read_remote_sessions()
        if isinstance(v2_doc, dict):
            data = v2_doc

    if data is None:
        if not _PATH.exists():
            return {}
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    if not isinstance(data, dict):
        return {}
    if data.get("fingerprint") != current_fingerprint:
        return {}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    return {
        str(token_hash): float(expiry)
        for token_hash, expiry in sessions.items()
        if isinstance(token_hash, str) and isinstance(expiry, (int, float))
    }


def save(current_fingerprint: str, sessions: dict[str, float]) -> None:
    """Persist atomically (tmp+rename), same pattern as `RemoteConfig.save()`."""
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    doc = {"fingerprint": current_fingerprint, "sessions": sessions}
    payload = json.dumps(doc, indent=2) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(_PATH)

    from ..core.storage.dual_write import dual_write_remote_sessions

    dual_write_remote_sessions(doc)


def clear() -> None:
    """ "Log out everywhere": drop the whole store. Safe to call when the
    file doesn't exist (remote never enabled, or already cleared)."""
    try:
        _PATH.unlink()
    except FileNotFoundError:
        pass

    from ..core.storage.dual_write import dual_write_remote_sessions

    dual_write_remote_sessions(None)
