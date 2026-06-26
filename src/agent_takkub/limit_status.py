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
    utilization: float
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
    return LimitWindow(
        name=name,
        utilization=float(value.get("utilization") or 0.0),
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


def _load_raw_credentials(credentials_path: Path) -> tuple[dict[str, Any] | None, str]:
    """Load Claude's OAuth credential blob. File first (Windows/Linux), then the
    macOS Keychain. Returns (raw, source) where source is 'file'|'keychain'|'none'."""
    try:
        return json.loads(credentials_path.read_text(encoding="utf-8")), "file"
    except (OSError, ValueError):
        pass
    blob = _read_keychain_credentials()
    if blob:
        try:
            return json.loads(blob), "keychain"
        except ValueError:
            pass
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
        self._lock = threading.Lock()
        self._refs: dict[Path, int] = {}
        self._cache: dict[Path, UsageData | None] = {}
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
        if is_new:
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
        """Fetch usage for one config_dir, update cache, emit callback."""
        try:
            data = fetch_usage(key)
        except RateLimited:
            _log.warning("Rate limited for %s, retaining cached data", key)
            data = None
        except Exception:
            _log.exception("Error fetching usage for %s", key)
            data = None

        with self._lock:
            if key not in self._refs:
                return  # unregistered while fetch was in-flight
            if data is not None:
                self._cache[key] = data
            emit_data = self._cache.get(key)

        if self._on_update is not None:
            try:
                self._on_update(key, emit_data)
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
