from __future__ import annotations

import dataclasses
import json
import logging
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_HEADERS_BASE = {
    "anthropic-beta": "oauth-2025-04-20",
    "x-app": "cli",
}
_DEFAULT_USER_AGENT = "claude-cli/2.1.146 (external, cli)"
_TIMEOUT_S = 10.0
_WINDOW_NAMES = ("five_hour", "seven_day", "seven_day_sonnet")

_log = logging.getLogger(__name__)
_cached_ua: str | None = None


@dataclass
class LimitWindow:
    name: str
    # None = the API response carried no utilization figure for this window.
    # Callers must render that as "unknown" (—), never as 0% — a fabricated
    # 0% reads as "plenty of quota left", the exact opposite of the truth
    # when the field is missing because the endpoint changed shape.
    utilization: float | None
    resets_at: datetime


@dataclass
class UsageData:
    plan: str
    windows: list[LimitWindow]
    extra_usage_enabled: bool
    status: str = "ok"
    fetched_at: datetime | None = None


class RateLimited(Exception):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__(f"HTTP 429 - retry after {retry_after}s")
        self.retry_after = retry_after


def _detect_cli_version() -> str | None:
    try:
        executable = shutil.which("claude")
        if not executable:
            return None
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            # CREATE_NO_WINDOW: never flash a console window for this probe
            # (0 on non-Windows). Matches the rest of the codebase's subprocess
            # calls; without it the `claude --version` probe pops a terminal.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
        return match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def _resolve_user_agent() -> str:
    global _cached_ua
    if _cached_ua is None:
        version = _detect_cli_version()
        _cached_ua = f"claude-cli/{version} (external, cli)" if version else _DEFAULT_USER_AGENT
    return _cached_ua


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(int(value.strip()))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        delta = (retry_at - datetime.now(tz=UTC)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError, OverflowError):
        return None


def _plan_label(subscription_type: str | None, rate_limit_tier: str | None) -> str:
    subscription = (subscription_type or "").lower().strip()
    tier = (rate_limit_tier or "").lower().strip()
    if "max" in tier and "20x" in tier:
        return "Max 20x"
    if "max" in tier and "5x" in tier:
        return "Max 5x"
    if "pro" in tier:
        return "Pro"
    if "free" in tier:
        return "Free"
    if subscription == "max":
        return "Max"
    return (
        subscription_type.title() if subscription_type and subscription_type.strip() else "Unknown"
    )


def _normalize_credentials(raw: dict[str, Any]) -> dict[str, Any]:
    credentials = raw.get("claudeAiOauth") or raw.get("claudeAiOauthAccount") or raw
    expires_at = credentials.get("expiresAt") or credentials.get("expires_at") or 0
    if expires_at > 1e12:
        expires_at = int(expires_at) // 1000
    return {
        "access_token": credentials.get("accessToken") or credentials.get("access_token"),
        "refresh_token": credentials.get("refreshToken") or credentials.get("refresh_token"),
        "expires_at": int(expires_at),
        "plan": _plan_label(
            credentials.get("subscriptionType"),
            credentials.get("rateLimitTier"),
        ),
    }


def _request_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    form_data: dict[str, str] | None = None,
) -> dict[str, Any]:
    body = urllib.parse.urlencode(form_data).encode() if form_data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def _write_refreshed_token(
    credentials_path: Path,
    raw: dict[str, Any],
    access_token: str,
    refresh_token: str | None,
    expires_in: int,
) -> None:
    expires_at_ms = int((datetime.now(tz=UTC).timestamp() + expires_in) * 1000)
    if "claudeAiOauth" in raw:
        target = raw["claudeAiOauth"]
        target["accessToken"] = access_token
        if refresh_token:
            target["refreshToken"] = refresh_token
        target["expiresAt"] = expires_at_ms
    elif "claudeAiOauthAccount" in raw:
        target = raw["claudeAiOauthAccount"]
        target["access_token"] = access_token
        if refresh_token:
            target["refresh_token"] = refresh_token
        target["expires_at"] = expires_at_ms // 1000
    else:
        raw["access_token"] = access_token
        if refresh_token:
            raw["refresh_token"] = refresh_token
        raw["expires_at"] = expires_at_ms // 1000
    credentials_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _refresh_and_persist(
    credentials_path: Path,
    raw: dict[str, Any],
    refresh_token: str,
) -> str:
    token_data = _request_json(
        _TOKEN_URL,
        form_data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _CLIENT_ID,
        },
    )
    access_token = str(token_data["access_token"])
    new_refresh_token = token_data.get("refresh_token")
    expires_in = int(token_data.get("expires_in", 3600))
    _write_refreshed_token(
        credentials_path,
        raw,
        access_token,
        str(new_refresh_token) if new_refresh_token else None,
        expires_in,
    )
    return access_token


def _parse_window(name: str, value: dict[str, Any] | None) -> LimitWindow | None:
    if not value or value.get("resets_at") is None:
        return None
    raw_util = value.get("utilization")
    try:
        # Preserve "field absent" as None instead of coercing to 0.0 — the
        # UI shows "—" for unknown. (`or 0.0` here once turned a schema
        # change into a permanent, confident-looking "0%".)
        utilization = float(raw_util) if raw_util is not None else None
    except (TypeError, ValueError):
        utilization = None
    return LimitWindow(
        name=name,
        utilization=utilization,
        resets_at=datetime.fromisoformat(str(value["resets_at"])),
    )


# macOS stores Claude Code's OAuth credentials in the login Keychain, NOT in
# ~/.claude/.credentials.json (the Windows/Linux location). Without this the
# usage meter showed "—" on Mac because the file read just failed. (M-xplat)
_KEYCHAIN_SERVICE = "Claude Code-credentials"


def _read_keychain_credentials() -> str | None:
    """Return the raw credentials JSON from the macOS login Keychain, or None.

    No-op (returns None) off macOS, or when `security` / the entry is absent.
    Read-only: we never write the Keychain, so Claude Code's own credential
    management is never disturbed.
    """
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = proc.stdout.strip()
    return blob if proc.returncode == 0 and blob else None


def _has_access_token(raw: dict[str, Any]) -> bool:
    """True when *raw* normalises to a usable OAuth access token."""
    try:
        return bool(_normalize_credentials(raw).get("access_token"))
    except (AttributeError, TypeError, ValueError):
        return False


def _load_raw_credentials(credentials_path: Path) -> tuple[dict[str, Any] | None, str]:
    """Load Claude's OAuth credential blob. File first (Windows/Linux), then the
    macOS Keychain. Returns (raw, source) where source is 'file'|'keychain'|'none'.

    A file is only preferred when it actually carries an access token. On macOS
    Claude Code keeps the real credentials in the login Keychain and can leave a
    *token-less* ``~/.claude/.credentials.json`` stub behind (migration residue,
    or a file that only holds non-OAuth settings). The old "file first, no
    questions asked" logic accepted that stub, never consulted the Keychain, and
    the usage meter stuck on "—" forever. So: take the file if it has a token;
    otherwise fall through to the Keychain; only then fall back to the (tokenless)
    file so callers still log a consistent "no access token". (M-xplat)
    """
    file_raw: dict[str, Any] | None = None
    try:
        parsed = json.loads(credentials_path.read_text(encoding="utf-8"))
        file_raw = parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError):
        file_raw = None

    if file_raw is not None and _has_access_token(file_raw):
        return file_raw, "file"

    blob = _read_keychain_credentials()
    if blob:
        try:
            kc_raw = json.loads(blob)
        except ValueError:
            kc_raw = None
        if isinstance(kc_raw, dict) and _has_access_token(kc_raw):
            return kc_raw, "keychain"

    if file_raw is not None:
        return file_raw, "file"
    return None, "none"


def fetch_usage(config_dir: Path | None = None) -> UsageData | None:
    credentials_path = (config_dir or Path.home() / ".claude") / ".credentials.json"
    try:
        raw, source = _load_raw_credentials(credentials_path)
        if raw is None:
            _log.error(
                "No Claude credentials: file %s missing and no macOS Keychain entry",
                credentials_path,
            )
            return None
        credentials = _normalize_credentials(raw)
        token = credentials["access_token"]
        if not token:
            _log.error("No access token in Claude credentials (source=%s)", source)
            return None

        if credentials["expires_at"] <= datetime.now(tz=UTC).timestamp() + 300:
            refresh_token = credentials["refresh_token"]
            if not refresh_token:
                _log.error("Token expired and no refresh token is available")
                return None
            if source != "file":
                # Creds came from the macOS Keychain. Refreshing would rotate the
                # token and we'd have to write it back to the Keychain — which
                # risks clobbering the entry Claude Code itself manages. Claude
                # Code keeps the Keychain token fresh, so defer to it and try
                # again next tick rather than touch its credential store.
                _log.warning("Keychain token expired; deferring to Claude Code to refresh")
                return None
            token = _refresh_and_persist(credentials_path, raw, refresh_token)

        data = _request_json(
            _USAGE_URL,
            headers={
                **_HEADERS_BASE,
                "user-agent": _resolve_user_agent(),
                "Authorization": f"Bearer {token}",
            },
        )
        windows = [
            window
            for name in _WINDOW_NAMES
            if (window := _parse_window(name, data.get(name))) is not None
        ]
        return UsageData(
            plan=credentials["plan"],
            windows=windows,
            extra_usage_enabled=bool((data.get("extra_usage") or {}).get("is_enabled")),
            fetched_at=datetime.now(tz=UTC),
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited(_parse_retry_after(exc.headers.get("Retry-After"))) from exc
        _log.error("HTTP %s from usage API", exc.code)
    except urllib.error.URLError as exc:
        _log.error("Network error fetching usage: %s", exc.reason)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _log.error("Unable to fetch Claude usage: %s", exc)
    return None


def _resolve_config_dir(config_dir: Path | None) -> Path:
    """Resolve a config_dir to an absolute canonical Path for use as a dict key."""
    if config_dir is None:
        return (Path.home() / ".claude").resolve()
    return Path(config_dir).resolve()


# ── cross-process shared fetch state ─────────────────────────────────────────
# The usage endpoint is aggressively rate-limited per account (observed
# 2026-07-17: Retry-After up to ~60 min, re-armed by further attempts). One
# machine often runs SEVERAL cockpit instances (prod + dev) that each polled
# independently — every instance honoured only its own in-memory backoff, so
# together they kept the server-side penalty armed forever and the chip froze
# on stale data. The fix: persist {last good payload, fetched_at, backoff_until}
# in a small JSON *inside the polled profile's config dir* — the one location
# every process polling that account already shares (same precedent as
# `takkub-claude-auth.json`). All wall-clock epochs (cross-process; monotonic
# clocks don't compare between processes).
_STATE_FILENAME = "takkub-usage-state.json"


def _state_path(config_dir: Path | None) -> Path:
    return _resolve_config_dir(config_dir) / _STATE_FILENAME


def _serialize_usage(data: UsageData) -> dict[str, Any]:
    return {
        "plan": data.plan,
        "extra_usage_enabled": data.extra_usage_enabled,
        "fetched_at": data.fetched_at.isoformat() if data.fetched_at else None,
        "windows": [
            {
                "name": w.name,
                "utilization": w.utilization,
                "resets_at": w.resets_at.isoformat(),
            }
            for w in data.windows
        ],
    }


def _deserialize_usage(raw: Any) -> UsageData | None:
    if not isinstance(raw, dict):
        return None
    try:
        windows = []
        for w in raw.get("windows") or []:
            util = w.get("utilization")
            windows.append(
                LimitWindow(
                    name=str(w["name"]),
                    utilization=float(util) if util is not None else None,
                    resets_at=datetime.fromisoformat(str(w["resets_at"])),
                )
            )
        fetched_raw = raw.get("fetched_at")
        return UsageData(
            plan=str(raw.get("plan") or "Unknown"),
            windows=windows,
            extra_usage_enabled=bool(raw.get("extra_usage_enabled")),
            fetched_at=datetime.fromisoformat(str(fetched_raw)) if fetched_raw else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_shared_state(config_dir: Path | None) -> dict[str, Any]:
    """Return {"backoff_until": epoch, "fetched_at": epoch, "data": UsageData|None}.

    Best-effort: missing/corrupt file → all-zero state (never raises)."""
    out: dict[str, Any] = {"backoff_until": 0.0, "fetched_at": 0.0, "data": None}
    try:
        raw = json.loads(_state_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    try:
        out["backoff_until"] = float(raw.get("backoff_until") or 0.0)
    except (TypeError, ValueError):
        pass
    try:
        out["fetched_at"] = float(raw.get("fetched_at") or 0.0)
    except (TypeError, ValueError):
        pass
    out["data"] = _deserialize_usage(raw.get("data"))
    return out


def save_shared_state(
    config_dir: Path | None,
    *,
    data: UsageData | None = None,
    backoff_until: float | None = None,
) -> None:
    """Merge-write the shared state file (atomic, best-effort).

    `data` given → record the fresh payload + fetched_at=now and CLEAR any
    backoff (a success proves the penalty lapsed). `backoff_until` given →
    record the deadline, keeping the last good payload for stale display."""
    path = _state_path(config_dir)
    current = load_shared_state(config_dir)
    if data is not None:
        current["data"] = data
        current["fetched_at"] = time.time()
        current["backoff_until"] = 0.0
    if backoff_until is not None:
        current["backoff_until"] = float(backoff_until)
    payload = {
        "backoff_until": current["backoff_until"],
        "fetched_at": current["fetched_at"],
        "data": _serialize_usage(current["data"]) if current["data"] is not None else None,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        _log.debug("could not persist usage state to %s", path, exc_info=True)


def fetch_usage_shared(config_dir: Path | None, max_age_s: float = 300.0) -> UsageData | None:
    """Cross-process-polite `fetch_usage`: reuse another process's recent
    result, honour a persisted backoff, and record any 429 penalty for
    everyone else. One-shot callers (limit_autoresume signal-b confirmation)
    should use this instead of `fetch_usage` so they never re-arm a penalty
    the pollers are already sitting out. May return stale data (or None)
    while backed off — callers treat it as best-effort telemetry."""
    now = time.time()
    state = load_shared_state(config_dir)
    if state["backoff_until"] > now:
        return state["data"]
    if state["data"] is not None and now - state["fetched_at"] <= max_age_s:
        return state["data"]
    try:
        fresh = fetch_usage(config_dir)
    except RateLimited as exc:
        save_shared_state(config_dir, backoff_until=now + max(exc.retry_after or 0.0, 300.0))
        return state["data"]
    if fresh is not None:
        save_shared_state(config_dir, data=fresh)
        return fresh
    return state["data"]


class LimitStore:
    """Shared background poller with per-user (config_dir) ref-counting.

    One daemon thread polls all registered config_dirs on *interval_s*
    cadence.  Switching tabs reads the cache only — never fetches.

    Usage:
        store = LimitStore(on_update=lambda cd, data: signal.emit((cd, data)))
        store.start()
        store.register(config_dir)     # on tab open
        store.get(config_dir)          # on tab switch — cache read, no fetch
        store.unregister(config_dir)   # on tab close
        store.stop()                   # on app quit
    """

    def __init__(
        self,
        interval_s: int = 120,
        on_update: Callable[[Path, UsageData | None], None] | None = None,
        stagger_s: float = 2.0,
    ) -> None:
        self._interval_s = interval_s
        self._on_update = on_update
        self._stagger_s = stagger_s
        self._min_backoff_s = 300.0
        self._lock = threading.Lock()
        self._refs: dict[Path, int] = {}
        self._cache: dict[Path, UsageData | None] = {}
        # Per-key monotonic deadline (time.monotonic()) before which _loop must
        # NOT re-fetch — set when the usage endpoint returns HTTP 429 so we
        # honour its Retry-After instead of hammering through the penalty.
        self._backoff_until: dict[Path, float] = {}
        self._running = False
        self._wake = threading.Event()

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="limit-store-loop").start()

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def register(self, config_dir: Path | None) -> None:
        """Increment ref-count for *config_dir*.

        First registration triggers an immediate background fetch so the UI
        has data quickly without waiting for the next polling interval.
        """
        key = _resolve_config_dir(config_dir)
        with self._lock:
            is_new = key not in self._refs
            self._refs[key] = self._refs.get(key, 0) + 1
            backed_off = time.monotonic() < self._backoff_until.get(key, 0.0)
        if is_new and not backed_off:
            threading.Thread(
                target=self._do_fetch,
                args=(key,),
                daemon=True,
                name=f"limit-fetch-{key.name}",
            ).start()

    def unregister(self, config_dir: Path | None) -> None:
        """Decrement ref-count for *config_dir*.

        When the count reaches 0, remove from the poll set and clear the
        cached data.
        """
        key = _resolve_config_dir(config_dir)
        with self._lock:
            if key not in self._refs:
                return
            self._refs[key] -= 1
            if self._refs[key] <= 0:
                self._refs.pop(key, None)
                self._cache.pop(key, None)

    def get(self, config_dir: Path | None) -> UsageData | None:
        """Return cached data for *config_dir* without triggering any fetch."""
        key = _resolve_config_dir(config_dir)
        with self._lock:
            return self._cache.get(key)

    # ── internal ──────────────────────────────────────────────────

    def _do_fetch(self, key: Path) -> None:
        """Fetch usage for one config_dir, update cache, emit callback.

        On HTTP 429 we honour the server's Retry-After: record a per-key
        backoff deadline so ``_loop`` stops hitting the endpoint until it
        expires. The old code swallowed ``RateLimited`` and kept polling every
        ``interval_s`` straight through the penalty window, which re-armed the
        429 (Retry-After escalated all the way to 3600s) and left the meter
        stuck on "—" forever. We also emit the last known data marked
        ``rate_limited`` so a stale value can show instead of blanking.
        Mirrors the backoff logic in ``Poller._do_poll``.

        Both the backoff and the last good payload are ALSO persisted via the
        shared state file (see module comment above ``_state_path``) so
        concurrent cockpit instances and post-restart runs neither re-arm a
        served penalty nor duplicate a fetch another process just made.
        """
        now = time.time()
        shared = load_shared_state(key)

        # Another process (or a previous run) is sitting out a 429 penalty —
        # honour it instead of re-arming, and surface its stale data.
        if shared["backoff_until"] > now:
            with self._lock:
                if key not in self._refs:
                    return
                self._backoff_until[key] = time.monotonic() + (shared["backoff_until"] - now)
                if shared["data"] is not None and self._cache.get(key) is None:
                    self._cache[key] = shared["data"]
                cached = self._cache.get(key)
                emit_data = (
                    dataclasses.replace(cached, status="rate_limited")
                    if cached is not None
                    else None
                )
            self._emit(key, emit_data)
            return

        # Another process fetched recently enough — reuse instead of a
        # duplicate network hit (N instances collapse to ~1 fetch/interval).
        if shared["data"] is not None and now - shared["fetched_at"] < self._interval_s:
            with self._lock:
                if key not in self._refs:
                    return
                self._cache[key] = shared["data"]
                self._backoff_until.pop(key, None)
            self._emit(key, shared["data"])
            return

        try:
            data = fetch_usage(key)
        except RateLimited as exc:
            backoff = max(exc.retry_after or 0.0, self._min_backoff_s)
            _log.warning("Rate limited for %s; backing off %.0fs", key, backoff)
            save_shared_state(key, backoff_until=now + backoff)
            with self._lock:
                if key not in self._refs:
                    return  # unregistered while fetch was in-flight
                self._backoff_until[key] = time.monotonic() + backoff
                if shared["data"] is not None and self._cache.get(key) is None:
                    self._cache[key] = shared["data"]
                cached = self._cache.get(key)
                emit_data = (
                    dataclasses.replace(cached, status="rate_limited")
                    if cached is not None
                    else None
                )
            self._emit(key, emit_data)
            return
        except Exception:
            _log.exception("Error fetching usage for %s", key)
            data = None

        with self._lock:
            if key not in self._refs:
                return  # unregistered while fetch was in-flight
            if data is not None:
                self._cache[key] = data
                self._backoff_until.pop(key, None)  # success clears any backoff
            emit_data = self._cache.get(key)

        self._emit(key, emit_data)
        # Persist last (after cache + emit): the UI shouldn't wait on disk
        # I/O, and other processes only need the file eventually.
        if data is not None:
            save_shared_state(key, data=data)

    def _emit(self, key: Path, data: UsageData | None) -> None:
        if self._on_update is not None:
            try:
                self._on_update(key, data)
            except Exception:
                _log.exception("Error in LimitStore on_update callback")

    def _loop(self) -> None:
        """Background daemon: sleep *interval_s*, then fetch all registered dirs."""
        while self._running:
            # Wait first — immediate fetches on register() handle "show quickly"
            self._wake.wait(
                timeout=self._interval_s + random.uniform(0, min(self._interval_s * 0.1, 10))
            )
            self._wake.clear()
            if not self._running:
                return
            with self._lock:
                keys = list(self._refs.keys())
            for i, key in enumerate(keys):
                if not self._running:
                    return
                with self._lock:
                    backed_off = time.monotonic() < self._backoff_until.get(key, 0.0)
                if not backed_off:
                    self._do_fetch(key)
                if i < len(keys) - 1:
                    # Small stagger between fetches to avoid burst
                    self._wake.wait(timeout=self._stagger_s)
                    self._wake.clear()


class Poller:
    def __init__(
        self,
        interval_s: int,
        on_update: Callable[[UsageData | None], None],
        on_schedule: Callable[[float], None] | None = None,
        config_dir: Path | None = None,
    ) -> None:
        self._interval_s = interval_s
        self._on_update = on_update
        self._on_schedule = on_schedule
        self._config_dir = config_dir
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._running = False
        self._last_data: UsageData | None = None

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._do_poll, daemon=True).start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def poll_now(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        threading.Thread(target=self._do_poll, daemon=True).start()

    def set_interval(self, seconds: int) -> None:
        with self._lock:
            self._interval_s = seconds
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        threading.Thread(target=self._do_poll, daemon=True).start()

    def _schedule_next(self, delay: float | None = None) -> None:
        if not self._running:
            return
        with self._lock:
            if not self._running:
                return
            actual_delay = (
                self._interval_s + random.uniform(0, min(self._interval_s * 0.2, 10))
                if delay is None
                else delay
            )
            if self._on_schedule is not None:
                self._on_schedule(actual_delay)
            self._timer = threading.Timer(actual_delay, self._do_poll)
            self._timer.daemon = True
            self._timer.start()

    def _emit(self, data: UsageData | None) -> None:
        try:
            self._on_update(data)
        except Exception:
            _log.exception("Error in usage poller callback")

    def _do_poll(self) -> None:
        try:
            data = fetch_usage(self._config_dir)
            if data is not None:
                self._last_data = data
                self._emit(data)
            else:
                self._emit(self._last_data)
            self._schedule_next()
        except RateLimited as exc:
            backoff = max(exc.retry_after or 0.0, 300.0)
            stale = (
                dataclasses.replace(self._last_data, status="rate_limited")
                if self._last_data is not None
                else UsageData(
                    plan="-",
                    windows=[],
                    extra_usage_enabled=False,
                    status="rate_limited",
                )
            )
            self._emit(stale)
            self._schedule_next(delay=backoff)
        except Exception:
            _log.exception("Unhandled error polling Claude usage")
            self._emit(self._last_data)
            self._schedule_next()
