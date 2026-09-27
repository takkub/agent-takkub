"""Machine-wide shared provider-usage cache + per-instance auto-fetch switch.

Why: dev and prod cockpits run at the same time, each with its own DATA_HOME
and (for Claude) its own config dir, yet they poll the SAME account's usage
endpoint. The old dedupe file lived inside the polled config dir, so the two
instances never saw each other: both fetched, a 429 seen by one was not
honoured by the other, and the shared server-side penalty stayed armed.

Here one JSON record per ``(provider, account identity)`` lives in a neutral
per-user cache dir that every instance on the machine can reach. It holds the
last good payload, ``fetched_at``, ``backoff_until``, a short fetch *lease*
(so N instances collapse to ~1 real fetch per interval) and small per-account
extras (e.g. the server-side plan label). All times are wall-clock epochs
(monotonic clocks do not compare between processes). No Qt dependency.

The auto-fetch switch is the opposite: per INSTANCE (under this instance's
``SETTINGS_HOME``), so dev and prod each decide for themselves whether they
may touch the network at all. OFF still renders from this shared cache.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import config

_log = logging.getLogger(__name__)

CACHE_ENV = "TAKKUB_USAGE_CACHE_DIR"
_LOCK_STALE_S = 10.0
_LEASE_TTL_S = 45.0
# One id per process so a lease is attributable to who took it.
_PROC_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


# ── location ─────────────────────────────────────────────────────────────────


def cache_dir() -> Path:
    """Neutral per-user cache dir, identical for dev and prod cockpits.

    ``TAKKUB_USAGE_CACHE_DIR`` overrides (tests set it so they never touch the
    real one). Resolved on every call, never cached.
    """
    override = os.environ.get(CACHE_ENV, "").strip()
    if override:
        return Path(override)
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = home / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    return base / "agent-takkub" / "usage-cache"


def _record_path(provider: str, identity: str) -> Path:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    safe = "".join(c if c.isalnum() else "_" for c in provider)[:24] or "x"
    return cache_dir() / f"{safe}-{digest}.json"


# ── identity ─────────────────────────────────────────────────────────────────


def claude_identity(config_dir: Path | str | None) -> str:
    """Stable key for the Claude account behind *config_dir*.

    ``oauthAccount.accountUuid`` from the dir's ``.claude.json`` (for the
    implicit ``~/.claude`` profile Claude Code may also keep it at
    ``~/.claude.json``); falls back to the resolved config-dir path so a
    logged-out / unreadable dir still gets a private, deterministic record.
    """
    cd = Path(config_dir) if config_dir is not None else Path.home() / ".claude"
    candidates = [cd / ".claude.json"]
    try:
        if cd.resolve() == (Path.home() / ".claude").resolve():
            candidates.append(Path.home() / ".claude.json")
    except OSError:
        pass
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            uid = (raw.get("oauthAccount") or {}).get("accountUuid")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(uid, str) and uid.strip():
            return f"acct:{uid.strip().lower()}"
    try:
        return f"dir:{cd.resolve()}"
    except OSError:
        return f"dir:{cd}"


def codex_identity(home: Path | str) -> str:
    """Key for a Codex home: the ChatGPT account id from ``auth.json`` when
    present (only that one field is read; no token is returned or logged),
    else the resolved home path."""
    h = Path(home)
    acct = None
    try:
        raw = json.loads((h / "auth.json").read_text(encoding="utf-8"))
        tokens = raw.get("tokens") if isinstance(raw, dict) else None
        acct = tokens.get("account_id") if isinstance(tokens, dict) else None
    except (OSError, ValueError):
        pass
    if isinstance(acct, str) and acct.strip():
        return f"acct:{acct.strip().lower()}"
    try:
        return f"dir:{h.resolve()}"
    except OSError:
        return f"dir:{h}"


# ── record I/O ───────────────────────────────────────────────────────────────


def _blank() -> dict[str, Any]:
    return {
        "payload": None,
        "fetched_at": 0.0,
        "backoff_until": 0.0,
        "lease_until": 0.0,
        "lease_owner": "",
        "last_error": "",
        "last_attempt_at": 0.0,
        "extra": {},
    }


def read_record(provider: str, identity: str) -> dict[str, Any]:
    """Best-effort read; missing/corrupt → blank record (never raises)."""
    out = _blank()
    try:
        raw = json.loads(_record_path(provider, identity).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    for key in ("fetched_at", "backoff_until", "lease_until", "last_attempt_at"):
        with contextlib.suppress(TypeError, ValueError):
            out[key] = float(raw.get(key) or 0.0)
    out["payload"] = raw.get("payload") if isinstance(raw.get("payload"), (dict, list)) else None
    out["lease_owner"] = str(raw.get("lease_owner") or "")
    out["last_error"] = str(raw.get("last_error") or "")
    extra = raw.get("extra")
    out["extra"] = extra if isinstance(extra, dict) else {}
    return out


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[bool]:
    """Cross-process mutex via an O_EXCL lock file. Yields False (caller
    proceeds best-effort) if it cannot be taken within ~2s; a lock older than
    ``_LOCK_STALE_S`` (holder died) is broken."""
    lock = path.with_suffix(".lock")
    got = False
    deadline = time.monotonic() + 2.0
    with contextlib.suppress(OSError):
        lock.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            got = True
            break
        except (FileExistsError, PermissionError):
            # Windows reports a lock file mid-delete as PermissionError, not
            # FileExistsError — that is still "held", never "unusable".
            try:
                if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                break
            time.sleep(0.005)
        except OSError:
            break
    try:
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                lock.unlink()


def update_record(
    provider: str, identity: str, mutate: Callable[[dict[str, Any]], Any]
) -> tuple[dict[str, Any], Any]:
    """Locked read-modify-write; returns (record after, mutate's return).
    Atomic replace (never append — Windows concurrent-append lost writes)."""
    path = _record_path(provider, identity)
    with _file_lock(path):
        rec = read_record(provider, identity)
        result = mutate(rec)
        body = {k: rec[k] for k in _blank()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{_PROC_ID}.tmp")
            tmp.write_text(json.dumps(body, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            _log.debug("could not persist usage cache %s", path, exc_info=True)
    return rec, result


def try_acquire_lease(provider: str, identity: str, ttl_s: float = _LEASE_TTL_S) -> str | None:
    """Claim the right to do the one real fetch for this account. Returns a
    lease token, or None when another live holder has it."""
    token = f"{_PROC_ID}-{uuid.uuid4().hex[:6]}"

    def mut(rec: dict[str, Any]) -> bool:
        now = time.time()
        if rec["lease_until"] > now and rec["lease_owner"]:
            return False
        rec["lease_until"] = now + ttl_s
        rec["lease_owner"] = token
        return True

    _, ok = update_record(provider, identity, mut)
    return token if ok else None


def _drop_lease(rec: dict[str, Any], token: str | None) -> None:
    if token is None or rec["lease_owner"] == token:
        rec["lease_until"] = 0.0
        rec["lease_owner"] = ""


def record_fetch_ok(provider: str, identity: str, token: str | None, payload: Any) -> None:
    def mut(rec: dict[str, Any]) -> None:
        rec["payload"] = payload
        rec["fetched_at"] = time.time()
        rec["last_attempt_at"] = rec["fetched_at"]
        rec["backoff_until"] = 0.0
        rec["last_error"] = ""
        _drop_lease(rec, token)

    update_record(provider, identity, mut)


def record_backoff(
    provider: str, identity: str, token: str | None, until: float, reason: str = "rate limited"
) -> None:
    def mut(rec: dict[str, Any]) -> None:
        rec["backoff_until"] = max(rec["backoff_until"], float(until))
        rec["last_error"] = reason
        rec["last_attempt_at"] = time.time()
        _drop_lease(rec, token)

    update_record(provider, identity, mut)


def record_failure(provider: str, identity: str, token: str | None, reason: str) -> None:
    def mut(rec: dict[str, Any]) -> None:
        rec["last_error"] = reason
        rec["last_attempt_at"] = time.time()
        _drop_lease(rec, token)

    update_record(provider, identity, mut)


def release_lease(provider: str, identity: str, token: str | None) -> None:
    update_record(provider, identity, lambda rec: _drop_lease(rec, token))


def set_extra(provider: str, identity: str, key: str, value: Any) -> None:
    def mut(rec: dict[str, Any]) -> None:
        rec["extra"][key] = value

    update_record(provider, identity, mut)


# ── per-instance auto-fetch switch ───────────────────────────────────────────

_SETTINGS_FILE = "usage-settings.json"
_wake_hooks: list[Callable[[], None]] = []


def _settings_path() -> Path:
    return config.SETTINGS_HOME / _SETTINGS_FILE


def auto_fetch_enabled() -> bool:
    """Whether THIS instance may hit the network/subprocess for usage.
    Default ON; read fresh each call so a toggle is live (file is tiny)."""
    try:
        raw = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (isinstance(raw, dict) and raw.get("auto_fetch") is False)


def add_wake_hook(hook: Callable[[], None]) -> None:
    """Register a callback fired when the switch flips ON (LimitStore uses it)."""
    if hook not in _wake_hooks:
        _wake_hooks.append(hook)


def set_auto_fetch_enabled(enabled: bool) -> None:
    path = _settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{_PROC_ID}.tmp")
        tmp.write_text(json.dumps({"auto_fetch": bool(enabled)}, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        _log.warning("could not persist usage auto-fetch switch", exc_info=True)
        return
    if enabled:
        for hook in list(_wake_hooks):
            try:
                hook()
            except Exception:
                _log.debug("usage wake hook failed", exc_info=True)
