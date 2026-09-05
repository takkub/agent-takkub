"""Cross-provider usage/quota abstraction (#103 follow-up, Wave 2).

One `ProviderUsage` shape for every CLI provider the cockpit can spawn
(claude/codex/gemini/opencode/kimi/cursor), built on top of
`docs/audit/2026-08-13-provider-usage-survey.md` (empirical spike — which
channel exists per provider, proven live or not) and
`docs/design/2026-08-13-provider-usage-abstraction.md` (the data-shape/UX
design). See `docs/audit/2026-08-13-provider-usage-impl.md` for what
actually shipped vs. what stayed `unsupported` and why.

Every `fetch_*_usage` function below is a **blocking, synchronous** call
(subprocess spawn, file read, or an HTTP GET via `limit_status`) — none of
them may run on the Qt main thread. `ProviderUsageStore` is the only piece
meant to be touched from Qt code; it does all fetching on background
threads and only ever hands back already-fetched cache reads.

Design contract (never violate):
- `utilization`/`plan`/`resets_at` are quota-percentage semantics. A
  provider with no quota API (opencode) must NEVER populate `utilization`
  — self-tallied spend/token counts live in the separate `spend` field
  instead, so a UI can never mislabel "tokens I sent" as "quota left".
- Missing data is `None`, never `0` / `0.0` — a fabricated 0% reads as
  "plenty of quota left" (see `limit_status.LimitWindow.utilization`'s own
  contract, which this module mirrors).
- Every adapter catches its own failures and returns `status="error"` (or
  `"unsupported"` when no channel exists) instead of raising — a provider
  outage must never take down whatever polls this module.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import limit_status
from ._win_console import SUBPROCESS_NO_WINDOW

_log = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_LOADING = "loading"
STATUS_UNSUPPORTED = "unsupported"
STATUS_ERROR = "error"

_VALID_STATUSES = frozenset(
    {STATUS_ACTIVE, STATUS_STALE, STATUS_LOADING, STATUS_UNSUPPORTED, STATUS_ERROR}
)

# Every fetch (subprocess spawn or HTTP GET) is bounded by this so one wedged
# provider CLI can never hang a background poll thread forever.
_FETCH_TIMEOUT_S = 10.0

PROVIDER_NAMES: tuple[str, ...] = ("claude", "codex", "gemini", "opencode", "kimi", "cursor")


@dataclass
class ProviderUsage:
    """One provider's current usage/quota snapshot.

    `raw_data` carries provider-specific extras (e.g. Claude's multiple
    limit windows, Codex's spend-control fields) for callers that want more
    than the flattened `utilization`/`plan`/`resets_at` triple — never
    relied on by this module's own status logic.

    `spend` is a DIFFERENT KIND OF NUMBER than `utilization` — self-tallied
    tokens/cost a provider's own CLI has counted locally (currently only
    opencode). It is never a quota fraction and must render as separate UI,
    never blended into a utilization meter.

    `windows` (#204) is the same one-provider snapshot broken into its
    individual rolling quota periods (claude's five_hour/seven_day/
    seven_day_sonnet, codex's primary/secondary), each
    `{"name": str, "utilization": float | None, "resets_at": iso str | None}`.
    `utilization`/`resets_at` above stay the single headline figure (matches
    what the desktop chip already shows — never change their meaning); a
    provider with only one meaningful window, or none, leaves this `None`
    and callers fall back to the headline fields, same as before #204.

    `error` is the short, human-readable line a UI shows by default (Thai,
    one sentence, states the cause and — when there is one — the fix, e.g.
    "codex: login หมดอายุ ... รัน codex login ใหม่"). `detail` (2026-08-31
    #454) carries the untouched raw error text (subprocess stderr, an HTTP
    body, a provider SDK exception) for a collapsed "รายละเอียด" section —
    never the headline, since a raw stack trace or JSON body means nothing
    to a phone user staring at a red usage card.
    """

    provider: str
    status: str
    plan: str | None = None
    utilization: float | None = None
    resets_at: datetime | None = None
    fetched_at: datetime | None = None
    raw_data: dict[str, Any] | None = None
    error: str | None = None
    detail: str | None = None
    spend: dict[str, Any] | None = None
    windows: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"invalid ProviderUsage.status: {self.status!r}")


def _unsupported(provider: str, reason: str) -> ProviderUsage:
    return ProviderUsage(provider=provider, status=STATUS_UNSUPPORTED, error=reason)


def _error(provider: str, reason: str) -> ProviderUsage:
    return ProviderUsage(provider=provider, status=STATUS_ERROR, error=reason)


def usage_to_dict(data: ProviderUsage) -> dict[str, Any]:
    """JSON-safe serialization for the `/api/usage` remote endpoint."""
    return {
        "provider": data.provider,
        "status": data.status,
        "plan": data.plan,
        "utilization": data.utilization,
        "resets_at": data.resets_at.isoformat() if data.resets_at else None,
        "fetched_at": data.fetched_at.isoformat() if data.fetched_at else None,
        "error": data.error,
        "detail": data.detail,
        "spend": data.spend,
        "raw_data": data.raw_data,
        "windows": data.windows,
    }


# ── claude ────────────────────────────────────────────────────────────────

# #203: a fetch that never got the cockpit's real CLAUDE_CONFIG_DIR silently
# fell back to `limit_status._resolve_config_dir(None)` == `~/.claude`, which
# on an installed build is NOT where panes actually run (they get
# `~/.agent-takkub/claude-config` — see `config.default_claude_config_dir`).
# The store then read/wrote a completely different `takkub-usage-state.json`
# than the one panes were keeping fresh, so it could go stale for weeks with
# no signal anything was wrong. `data.status == "rate_limited"` alone can't
# catch that either — a wrong-directory read is neither backed off nor an
# HTTP error, so age-based staleness (below) is the only thing that can.
_CLAUDE_STALE_THRESHOLD_S = 24 * 3600.0


def _resolve_claude_config_dir() -> Path:
    """Which Claude config dir this cockpit instance's usage telemetry should
    come from — mirrors `limit_panel.LimitPanelMixin._init_limit_store`'s own
    resolution for the desktop status chip (the active project tab's
    profile), so the mobile/`/api/usage` number can never disagree with what
    desktop is already showing. Falls back to the cockpit-wide default
    profile when no project tab is active yet (e.g. a poll racing boot).
    """
    from . import config as _config
    from . import user_profile

    active, _ = _config.active_project()
    return user_profile.config_dir_for(active or "")


def fetch_claude_usage(config_dir: Path | None = None) -> ProviderUsage:
    """Wraps `limit_status.fetch_usage_shared()` — NEVER call
    `limit_status.fetch_usage()` directly here (the endpoint 403/429s hard
    with up to ~60min Retry-After; the shared cross-process state/backoff
    file is what survives that — see `usage-endpoint-hardened-shared-backoff`
    project memory). Blocking (one HTTP GET, bounded by
    `limit_status._TIMEOUT_S`) — run off the Qt main thread.

    `config_dir=None` (the normal call from `_FETCHERS`) resolves the real
    cockpit profile dir via `_resolve_claude_config_dir` rather than letting
    `limit_status` silently default to `~/.claude` (#203). Callers that
    already know the dir (tests, or a future per-profile caller) may still
    pass one explicitly.
    """
    if config_dir is None:
        config_dir = _resolve_claude_config_dir()
    try:
        data = limit_status.fetch_usage_shared(config_dir)
    except Exception:
        _log.exception("claude usage fetch failed")
        return _error("claude", "fetch failed")
    if data is None:
        return _error("claude", "not logged in, or no usage data available")
    five_hour = next((w for w in data.windows if w.name == "five_hour"), None)
    age_s = (datetime.now(tz=UTC) - data.fetched_at).total_seconds() if data.fetched_at else None
    if data.status == "rate_limited" or age_s is None or age_s > _CLAUDE_STALE_THRESHOLD_S:
        status = STATUS_STALE
    else:
        status = STATUS_ACTIVE
    return ProviderUsage(
        provider="claude",
        status=status,
        plan=data.plan,
        utilization=five_hour.utilization if five_hour else None,
        resets_at=five_hour.resets_at if five_hour else None,
        fetched_at=data.fetched_at,
        windows=[
            {
                "name": w.name,
                "utilization": w.utilization,
                "resets_at": w.resets_at.isoformat(),
            }
            for w in data.windows
        ],
        raw_data={
            "windows": [
                {
                    "name": w.name,
                    "utilization": w.utilization,
                    "resets_at": w.resets_at.isoformat(),
                }
                for w in data.windows
            ],
            "extra_usage_enabled": data.extra_usage_enabled,
        },
    )


# ── codex ─────────────────────────────────────────────────────────────────
# Verified LIVE against codex-cli 0.146.0 (docs/audit/2026-08-13-provider-
# usage-survey.md follow-up probe): `codex app-server` (stdio JSON-RPC),
# `initialize` handshake, then `account/rateLimits/read` returns a real
# RateLimitSnapshot. The whole app-server surface is still labelled
# `[experimental]` in `codex --help`, so this shape can move under us
# without a deprecation window — every field access below is defensive
# (`.get()`, never raw indexing).


def _codex_executable() -> str | None:
    from .codex_helper import find_codex_executable

    return find_codex_executable()


# #454: a raw codex app-server/RPC error (subprocess stderr framing, an HTTP
# body echoed straight from `chatgpt.com/backend-api/...`, a JSON-RPC error
# object) landed verbatim in `ProviderUsage.error`, which the cockpit/PWA
# usage cards render as their headline line — a phone user saw a wall of
# "401 Unauthorized; content-type=...; body={...}" with no indication that
# the fix is `codex login`. This never re-raises: an unmatched message just
# falls into the generic branch, still short and still carrying `detail`.
_CODEX_AUTH_ERROR_MARKERS = (
    "401",
    "token_invalidated",
    "unauthorized",
    "not logged in",
    "refresh token",
)
_CODEX_DETAIL_MAX_LEN = 300


def classify_codex_error(msg: str) -> tuple[str, str]:
    """Classify a raw codex app-server/RPC error string into `(short, detail)`.

    `short` is one Thai sentence naming the cause and, when known, the fix —
    what a UI shows by default. `detail` is *msg* unchanged except truncated
    to `_CODEX_DETAIL_MAX_LEN` chars, meant for a collapsed "รายละเอียด"
    section, never the headline.
    """
    raw = msg or ""
    detail = raw[:_CODEX_DETAIL_MAX_LEN]
    lowered = raw.lower()
    if any(marker in lowered for marker in _CODEX_AUTH_ERROR_MARKERS):
        short = (
            "codex: login หมดอายุ (token_invalidated) — รัน codex login ใหม่ใน terminal "
            "ที่ CODEX_HOME ชี้ codex-home ของ cockpit"
        )
    elif "did not respond" in lowered or "timeout" in lowered:
        short = "codex: app-server ไม่ตอบสนอง (timeout) — ลองใหม่อีกครั้ง หรือรีสตาร์ท codex CLI"
    elif "could not start" in lowered:
        short = "codex: เปิด app-server ไม่สำเร็จ — เช็คว่าติดตั้ง/PATH ของ codex CLI ถูกต้อง"
    else:
        short_raw = raw.strip() or "ไม่ทราบสาเหตุ"
        if len(short_raw) > 100:
            short_raw = short_raw[:100] + "…"
        short = f"codex: ดึงข้อมูล usage ไม่สำเร็จ — {short_raw}"
    return short, detail


def _codex_error(raw_msg: str) -> ProviderUsage:
    short, detail = classify_codex_error(raw_msg)
    return ProviderUsage(provider="codex", status=STATUS_ERROR, error=short, detail=detail)


def _terminate_quietly(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


def _codex_rpc_roundtrip(proc: subprocess.Popen, timeout: float) -> ProviderUsage:
    responses: dict[int, dict] = {}
    lock = threading.Lock()
    got_message = threading.Event()

    def _reader() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                    with lock:
                        responses[msg["id"]] = msg
                    got_message.set()
        except (OSError, ValueError):
            pass

    threading.Thread(target=_reader, daemon=True).start()

    def _send(req: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()

    def _wait_for(req_id: int, deadline: float) -> dict | None:
        while time.monotonic() < deadline:
            with lock:
                if req_id in responses:
                    return responses.pop(req_id)
            got_message.wait(timeout=0.1)
            got_message.clear()
        return None

    deadline = time.monotonic() + timeout
    _send(
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "takkub-cockpit", "version": "1"}},
        }
    )
    init_resp = _wait_for(1, deadline)
    if init_resp is None:
        return _codex_error("app-server did not respond to initialize")
    if "error" in init_resp:
        return _codex_error(f"initialize error: {init_resp['error']}")

    _send({"id": 2, "method": "account/rateLimits/read", "params": None})
    rl_resp = _wait_for(2, deadline)
    if rl_resp is None:
        return _codex_error("app-server did not respond to account/rateLimits/read")
    if "error" in rl_resp:
        err = rl_resp["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return _codex_error(f"account/rateLimits/read error: {msg}")

    result = rl_resp.get("result")
    rate_limits = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(rate_limits, dict):
        return _codex_error("account/rateLimits/read returned no rateLimits")
    return _parse_codex_rate_limits(rate_limits)


def _parse_codex_rate_window(window: Any) -> dict[str, Any] | None:
    """Parse one `RateLimitWindow` (`{usedPercent, resetsAt, ...}`) into the
    `windows` entry shape. Returns None when *window* carries no usable
    percentage at all, so a genuinely absent window (e.g. no `secondary` on
    this account) never becomes a fabricated 0%-looking row."""
    if not isinstance(window, dict):
        return None
    used_percent = window.get("usedPercent")
    try:
        utilization = float(used_percent) if used_percent is not None else None
    except (TypeError, ValueError):
        utilization = None
    resets_at_epoch = window.get("resetsAt")
    resets_at_iso: str | None = None
    if resets_at_epoch is not None:
        try:
            resets_at_iso = datetime.fromtimestamp(float(resets_at_epoch), tz=UTC).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            resets_at_iso = None
    if utilization is None and resets_at_iso is None:
        return None
    return {"utilization": utilization, "resets_at": resets_at_iso}


def _parse_codex_rate_limits(rate_limits: dict[str, Any]) -> ProviderUsage:
    primary = rate_limits.get("primary")
    primary = primary if isinstance(primary, dict) else {}
    used_percent = primary.get("usedPercent")
    try:
        utilization = float(used_percent) if used_percent is not None else None
    except (TypeError, ValueError):
        utilization = None
    resets_at_epoch = primary.get("resetsAt")
    resets_at: datetime | None = None
    if resets_at_epoch is not None:
        try:
            resets_at = datetime.fromtimestamp(float(resets_at_epoch), tz=UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            resets_at = None
    plan = rate_limits.get("planType")
    # #204: codex's `secondary` (weekly) window was fetched all along
    # (part of `rate_limits`/`raw_data`) but silently dropped on the floor —
    # never shown anywhere. Surface both windows the same way claude's
    # five_hour/seven_day pair already is.
    windows: list[dict[str, Any]] = []
    primary_window = _parse_codex_rate_window(primary)
    if primary_window is not None:
        windows.append({"name": "primary", **primary_window})
    secondary_window = _parse_codex_rate_window(rate_limits.get("secondary"))
    if secondary_window is not None:
        windows.append({"name": "secondary", **secondary_window})
    return ProviderUsage(
        provider="codex",
        status=STATUS_ACTIVE,
        plan=str(plan) if plan else None,
        utilization=utilization,
        resets_at=resets_at,
        fetched_at=datetime.now(tz=UTC),
        raw_data=rate_limits,
        windows=windows or None,
    )


def fetch_codex_usage(
    timeout: float = _FETCH_TIMEOUT_S, config_dir: Path | None = None
) -> ProviderUsage:
    """Spawns a throwaway `codex app-server`, does one JSON-RPC round trip,
    then kills the subprocess — never reuses/pools it. Blocking (subprocess
    I/O bounded by *timeout*) — run off the Qt main thread.

    `config_dir` (epic #309 Phase 3b): when given, scopes the probe to one
    account's isolated `CODEX_HOME` (same var `pane_env.inject_provider_home_env`/
    `config.provider_home_env` already use) instead of whatever CODEX_HOME
    this process inherited — the per-account counterpart of
    `fetch_claude_usage`'s `config_dir` param.

    `config_dir=None` (the normal call from `_FETCHERS`) resolves the
    cockpit's real `CODEX_HOME` via `codex_helper.codex_home()` rather than
    letting the subprocess inherit whatever `CODEX_HOME` this process
    happened to have — #455: the cockpit process itself never gets that env
    var (only `pane_env.inject_provider_home_env` injects it, into spawned
    panes), so an installed build's probe fell through to codex's own
    `~/.codex` default and reported a false "login expired" even though the
    cockpit's isolated codex-home was logged in fine. Mirrors
    `fetch_claude_usage`/`_resolve_claude_config_dir`'s same fix for #203.
    """
    exe = _codex_executable()
    if not exe:
        return _unsupported("codex", "codex binary not found on PATH")
    if config_dir is None:
        from .codex_helper import codex_home

        config_dir = codex_home()
    env = {**os.environ, "CODEX_HOME": str(config_dir)}
    try:
        proc = subprocess.Popen(
            [exe, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except OSError as exc:
        return _codex_error(f"could not start app-server: {exc}")
    try:
        return _codex_rpc_roundtrip(proc, timeout)
    except Exception:
        _log.exception("codex usage fetch failed")
        return _codex_error("app-server RPC failed")
    finally:
        _terminate_quietly(proc)


# ── gemini (agy / Antigravity) ───────────────────────────────────────────
# No CLI subcommand exists (survey confirmed via `agy --help`) — no channel
# was found to actively refresh the Antigravity desktop app's local quota
# cache file (`docs/audit/2026-08-31-gemini-quota-hunt.md`). #456 follow-up
# (2026-09-05, user-approved): a LIVE channel does exist after all — the
# same undocumented `retrieveUserQuota` RPC that audit flagged but didn't
# implement (needs a keyring credential + an undocumented client check —
# see `_fetch_gemini_live_usage` below for exactly what was needed and how
# fragile it is). It is the primary path now; every failure mode (no
# keyring entry, expired token, endpoint/schema change) falls straight
# through to the cache-file read below unchanged, so the card can never go
# fully dark over this — freshness is still NEVER assumed for the fallback
# path: `fetched_at` always rides along and anything older than the
# threshold below is reported `stale`, never `active`.

_GEMINI_STALE_THRESHOLD_S = 24 * 3600.0

# The OS credential store entry `agy`/Antigravity itself uses — confirmed
# empirically (2026-09-05) via `cmdkey /list` showing
# `LegacyGeneric:target=gemini:antigravity`, then reading that credential's
# blob directly (`advapi32!CredReadW`): a UTF-8 JSON string
# `{"auth_method": "...", "token": {"access_token", "token_type",
# "refresh_token", "expiry"}}`. This is the shape zalando/go-keyring's Go
# backends serialize into ONE secret string on every OS — Windows Credential
# Manager and macOS Keychain just store that same string under a different
# native mechanism, so both platform readers below hand the same JSON text
# to the same parser. Never verified against a real login on macOS (this
# dev/audit box is Windows-only) — a naming or format mismatch there just
# means `_read_macos_keychain_secret` returns None and the live path is
# skipped, same as any other miss.
_GEMINI_KEYRING_SERVICE = "gemini"
_GEMINI_KEYRING_ACCOUNT = "antigravity"

# `POST /v1internal:retrieveUserQuota` on the same host `agy` already calls
# for chat (`daily-cloudcode-pa.googleapis.com`) — RPC name, REST path and
# the request's one field (`project`, matched against the cache file's own
# `projectId`) were extracted from `agy.EXE`'s embedded proto descriptor
# strings (`RetrieveUserQuotaRequest`/`RetrieveUserQuotaResponse`,
# `buckets`/`remainingFraction`/`resetTime`/`modelId` — same field names the
# cache JSON already uses). Empirically verified live 2026-09-05.
_GEMINI_QUOTA_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"

# UNDOCUMENTED, found by trial only: the plain default User-Agent (Python's
# urllib, or a generic "google-api-go-client/..." string) gets a MISLEADING
# `403 SUBSCRIPTION_REQUIRED` from this endpoint even on a fully licensed,
# correctly-authenticated account — the server appears to gate this specific
# RPC on the caller identifying itself as an Antigravity client. Any
# User-Agent containing "antigravity" was sufficient in testing; kept as
# short/plain as it can be rather than spoofing a specific product/version
# string that was never observed. This has NO public contract and could
# stop working on any Google-side change with no changelog to watch — that
# is exactly why every caller of `_fetch_gemini_live_usage` treats a failure
# here as "no live data this poll", never an error surfaced to the UI.
_GEMINI_QUOTA_USER_AGENT = "antigravity"

# Skip the live attempt entirely once the access token is this close to (or
# past) its own `expiry` — it would just 401, so don't spend a network round
# trip finding that out.
_GEMINI_TOKEN_EXPIRY_MARGIN_S = 30.0

# The Antigravity DESKTOP APP is the only writer of this quota cache — `agy`,
# the CLI this cockpit actually spawns, never refreshes it (observed on the
# dev box: a cache file 5 months old while agy itself ran daily). So a stale
# gemini snapshot does NOT mean "the next poll will catch up", it means
# "nothing will ever update this until the app is opened". The generic stale
# wording in `usage_meter` implies a refresh is on its way, which is exactly
# backwards here, so the adapter ships its own hint alongside the snapshot.
GEMINI_STALE_HINT = (
    "agy CLI ไม่มี usage API — ตัวเลขนี้คือ cache ของแอป Antigravity ครั้งล่าสุด "
    "ต้องเปิดแอป Antigravity ถึงจะอัปเดต (pane gemini ที่รันอยู่ไม่ทำให้ค่านี้ใหม่ขึ้น)"
)


def _gemini_stale_hint(fetched_at: datetime | None) -> str:
    """`GEMINI_STALE_HINT` plus the cache file's own date, so the UI shows
    *how* stale the number is instead of just "stale" (#456 audit follow-up).
    """
    if fetched_at is None:
        return GEMINI_STALE_HINT
    return f"{GEMINI_STALE_HINT} (cache {fetched_at.date().isoformat()})"


def _antigravity_authorized_cache_dirs() -> list[Path]:
    """Candidate cache directories where Antigravity / Gemini quota files may be written."""
    home = Path.home()
    return [
        home / ".antigravity_cockpit" / "cache" / "quota_api_v1_plugin" / "authorized",
        home / ".antigravity_cockpit" / "cache" / "quota" / "authorized",
        home / ".gemini" / "cache" / "quota_api_v1_plugin" / "authorized",
        home / ".gemini" / "antigravity-cli" / "cache" / "quota_api_v1_plugin" / "authorized",
        home / ".antigravity" / "cache" / "quota_api_v1_plugin" / "authorized",
        home / ".antigravity-ide" / "cache" / "quota_api_v1_plugin" / "authorized",
    ]


def _antigravity_authorized_cache_dir() -> Path:
    return Path.home() / ".antigravity_cockpit" / "cache" / "quota_api_v1_plugin" / "authorized"


def _read_windows_credential_secret(target: str) -> str | None:
    """Raw secret of a Windows Credential Manager *Generic* credential, via
    `advapi32!CredReadW` directly through ctypes — stdlib only, no pywin32/
    keyring dependency needed. Returns None for "not found" and for any
    read/decode failure; never raises."""
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    cred_type_generic = 1
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
        ]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
    except OSError:
        return None
    cred_ptr = ctypes.POINTER(_CREDENTIAL)()
    if not advapi32.CredReadW(target, cred_type_generic, 0, ctypes.byref(cred_ptr)):
        return None
    try:
        cred = cred_ptr.contents
        size = cred.CredentialBlobSize
        if size <= 0 or not cred.CredentialBlob:
            return None
        addr = ctypes.cast(cred.CredentialBlob, ctypes.c_void_p).value
        return ctypes.string_at(addr, size).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    finally:
        advapi32.CredFree(cred_ptr)


def _read_macos_keychain_secret(service: str, account: str) -> str | None:
    """Raw secret of a macOS Keychain *generic password* item via the
    built-in `security` CLI (no new dependency) — the Darwin counterpart of
    `_read_windows_credential_secret`. Never verified against a real
    Antigravity login (audit/dev machine is Windows-only) — any failure
    here (binary missing, item not found, non-zero exit) is just another
    "no live credential" miss, same as everywhere else in this path."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    secret = result.stdout.strip()
    return secret or None


def _read_gemini_keyring_secret() -> str | None:
    if sys.platform == "win32":
        return _read_windows_credential_secret(
            f"{_GEMINI_KEYRING_SERVICE}:{_GEMINI_KEYRING_ACCOUNT}"
        )
    if sys.platform == "darwin":
        return _read_macos_keychain_secret(_GEMINI_KEYRING_SERVICE, _GEMINI_KEYRING_ACCOUNT)
    return None


def _parse_gemini_keyring_token(secret: str) -> tuple[str, datetime | None] | None:
    """`(access_token, expiry)` out of the keyring secret's JSON, or None
    when it's unparseable or missing the field this needs. Never raises."""
    try:
        data = json.loads(secret)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    if not isinstance(token, dict):
        return None
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    expiry: datetime | None = None
    expiry_raw = token.get("expiry")
    if isinstance(expiry_raw, str):
        try:
            expiry = datetime.fromisoformat(expiry_raw)
        except ValueError:
            expiry = None
    return access_token, expiry


def _gemini_access_token() -> str | None:
    """A live, unexpired access token from the OS keyring, or None when
    there's no credential, it's unparseable, or it's expired (or about to
    be) — any of which means "skip the live attempt", not an error."""
    secret = _read_gemini_keyring_secret()
    if secret is None:
        return None
    parsed = _parse_gemini_keyring_token(secret)
    if parsed is None:
        return None
    access_token, expiry = parsed
    if expiry is not None:
        now = datetime.now(tz=expiry.tzinfo or UTC)
        if (expiry - now).total_seconds() <= _GEMINI_TOKEN_EXPIRY_MARGIN_S:
            return None
    return access_token


def _fetch_gemini_live_buckets(
    access_token: str, project_id: str, timeout: float = _FETCH_TIMEOUT_S
) -> list[dict[str, Any]] | None:
    """One `retrieveUserQuota` RPC round trip. Blocking, bounded by
    *timeout*. Returns the response's `buckets` list, or None on ANY
    failure (network, non-200, malformed response) — never raises. The
    Authorization header is the only thing carrying the token; no failure
    path below ever puts *access_token* into a returned value, a log line,
    or an exception message."""
    body = json.dumps({"project": project_id}).encode("utf-8")
    request = urllib.request.Request(
        _GEMINI_QUOTA_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": _GEMINI_QUOTA_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    buckets = raw.get("buckets")
    return buckets if isinstance(buckets, list) else None


def _gemini_usage_from_live_buckets(
    buckets: list[dict[str, Any]], email: str | None
) -> ProviderUsage | None:
    """Same worst-case-model aggregation as the cache-file path below, just
    over the live RPC's `buckets` shape instead of the cache's `models`
    shape. Returns None when no bucket carries a usable fraction (treated
    by the caller as "live fetch didn't pan out" — falls back to cache)."""
    best_fraction: float | None = None
    resets_at: datetime | None = None
    windows: list[dict[str, Any]] = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        fraction = bucket.get("remainingFraction")
        if fraction is None:
            continue
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            continue
        model_id = bucket.get("modelId")
        m_utilization = max(0.0, min(100.0, (1.0 - fraction) * 100.0))
        reset_raw = bucket.get("resetTime")
        m_resets_at: datetime | None = None
        if isinstance(reset_raw, str):
            try:
                m_resets_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
            except ValueError:
                m_resets_at = None
        windows.append(
            {
                "name": str(model_id) if model_id else "?",
                "utilization": m_utilization,
                "resets_at": m_resets_at.isoformat() if m_resets_at else None,
            }
        )
        if best_fraction is None or fraction < best_fraction:
            best_fraction = fraction
            resets_at = m_resets_at
    if best_fraction is None:
        return None
    return ProviderUsage(
        provider="gemini",
        status=STATUS_ACTIVE,
        utilization=max(0.0, min(100.0, (1.0 - best_fraction) * 100.0)),
        resets_at=resets_at,
        fetched_at=datetime.now(tz=UTC),
        raw_data={"email": email, "model_count": len(windows), "source": "live"},
        windows=windows or None,
    )


def _fetch_gemini_live_usage(project_id: Any, email: Any) -> ProviderUsage | None:
    """Best-effort live attempt: keyring credential -> RPC -> aggregate.
    Returns None the moment any step doesn't pan out (no/expired
    credential, RPC failure, empty/unusable response) so the caller falls
    through to the cache-file path unchanged — this is ONLY ever a bonus
    freshness path, never the sole source of a number. Wrapped in a
    catch-all (unlike the individual helpers, which already never raise on
    their own) so an unforeseen failure here degrades the same way instead
    of ever reaching a caller that isn't `fetch_provider_usage`'s own
    catch-all — matches `fetch_codex_usage`'s same belt-and-suspenders
    pattern for its RPC round trip."""
    if not isinstance(project_id, str) or not project_id:
        return None
    try:
        access_token = _gemini_access_token()
        if access_token is None:
            return None
        buckets = _fetch_gemini_live_buckets(access_token, project_id)
        if not buckets:
            return None
        return _gemini_usage_from_live_buckets(buckets, email if isinstance(email, str) else None)
    except Exception:
        _log.exception("gemini live quota fetch failed")
        return None


def fetch_gemini_usage() -> ProviderUsage:
    """Tries a live `retrieveUserQuota` RPC first (`_fetch_gemini_live_usage`
    — needs a keyring credential, see the module comment above); on any miss
    falls back to reading the newest `quota_api_v1_plugin/authorized/*.json`
    cache file, same as before #456. Blocking (a bounded HTTP POST, or local
    file I/O only) — run off the Qt main thread like every other adapter
    here.
    """
    primary_dir = _antigravity_authorized_cache_dir()
    # Only widen discovery when the primary is the real default location.
    # A redirected primary (tests, or a future per-account config_dir) must
    # stay scoped — a "startswith(home)" guard is not enough on Windows
    # where pytest's tmp_path itself lives under the user's home.
    if (
        primary_dir
        != Path.home() / ".antigravity_cockpit" / "cache" / "quota_api_v1_plugin" / "authorized"
    ):
        dirs_to_check = [primary_dir]
    else:
        dirs_to_check = [primary_dir] + [
            d for d in _antigravity_authorized_cache_dirs() if d != primary_dir
        ]
    candidates: list[Path] = []
    for cache_dir in dirs_to_check:
        try:
            if cache_dir.is_dir():
                candidates.extend(cache_dir.glob("*.json"))
        except OSError:
            pass

    if not candidates:
        return _unsupported("gemini", "no Antigravity quota cache file found (not logged in?)")

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    try:
        raw = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _error("gemini", "could not read Antigravity quota cache file")
    if not isinstance(raw, dict):
        return _error("gemini", "malformed Antigravity quota cache file")

    live_usage = _fetch_gemini_live_usage(raw.get("projectId"), raw.get("email"))
    if live_usage is not None:
        return live_usage

    fetched_at: datetime | None = None
    updated_at_ms = raw.get("updatedAt")
    if updated_at_ms is not None:
        try:
            fetched_at = datetime.fromtimestamp(float(updated_at_ms) / 1000.0, tz=UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            fetched_at = None

    payload = raw.get("payload")
    models = payload.get("models") if isinstance(payload, dict) else None
    models = models if isinstance(models, dict) else {}

    # Aggregate to the worst-case (lowest remaining fraction) tracked model —
    # a single meter is what a status-bar chip needs; per-model detail rides
    # along in raw_data and windows for anything that wants more.
    best_fraction: float | None = None
    resets_at: datetime | None = None
    windows: list[dict[str, Any]] = []
    for model_name, info in models.items():
        if not isinstance(info, dict):
            continue
        quota = info.get("quotaInfo")
        if not isinstance(quota, dict):
            continue
        fraction = quota.get("remainingFraction")
        if fraction is None:
            continue
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            continue
        m_utilization = max(0.0, min(100.0, (1.0 - fraction) * 100.0))
        m_reset_raw = quota.get("resetTime")
        m_resets_at_iso = None
        if isinstance(m_reset_raw, str):
            try:
                m_resets_at_iso = datetime.fromisoformat(
                    m_reset_raw.replace("Z", "+00:00")
                ).isoformat()
            except ValueError:
                m_resets_at_iso = None
        display_name = info.get("displayName") or model_name
        windows.append(
            {
                "name": str(display_name),
                "utilization": m_utilization,
                "resets_at": m_resets_at_iso,
            }
        )
        if best_fraction is None or fraction < best_fraction:
            best_fraction = fraction
            reset_raw = quota.get("resetTime")
            resets_at = None
            if isinstance(reset_raw, str):
                try:
                    resets_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                except ValueError:
                    resets_at = None

    now = datetime.now(tz=UTC)
    age_s = (now - fetched_at).total_seconds() if fetched_at else None
    is_stale = age_s is None or age_s > _GEMINI_STALE_THRESHOLD_S
    is_expired = resets_at is not None and resets_at < now

    if is_stale or is_expired:
        status = STATUS_STALE
        # If the quota window expired (resets_at in past) or data is ancient (>7d),
        # utilization must be None rather than a misleading stale 0% (which renders as 100% left).
        if is_expired or (age_s is not None and age_s > 7 * 86400):
            utilization = None
            resets_at = None
            windows_out = None
        else:
            utilization = (
                None
                if best_fraction is None
                else max(0.0, min(100.0, (1.0 - best_fraction) * 100.0))
            )
            windows_out = windows or None
    else:
        status = STATUS_ACTIVE
        utilization = (
            None if best_fraction is None else max(0.0, min(100.0, (1.0 - best_fraction) * 100.0))
        )
        windows_out = windows or None

    return ProviderUsage(
        provider="gemini",
        status=status,
        utilization=utilization,
        resets_at=resets_at,
        fetched_at=fetched_at,
        raw_data={"email": raw.get("email"), "model_count": len(models)},
        windows=windows_out,
        error=_gemini_stale_hint(fetched_at) if status == STATUS_STALE else None,
    )


# ── opencode ──────────────────────────────────────────────────────────────
# opencode has NO quota/rate-limit channel of any kind (survey: `message.data`
# is an opaque per-response JSON blob; the only account-related table holds
# just active-account-id pointers). `opencode stats` is the documented,
# stable CLI surface but renders text-table only (no --json on `stats`
# itself) — `opencode db "<SELECT ...>" --format json` gives real JSON, at
# the cost of depending on the sqlite schema `stats` itself relies on, so
# every field read here is defensive. This NEVER populates `utilization`.

_OPENCODE_STATS_QUERY = (
    "SELECT "
    "SUM(json_extract(data,'$.cost')) AS cost, "
    "SUM(json_extract(data,'$.tokens.input')) AS input_tokens, "
    "SUM(json_extract(data,'$.tokens.output')) AS output_tokens, "
    "SUM(json_extract(data,'$.tokens.cache.read')) AS cache_read_tokens, "
    "SUM(json_extract(data,'$.tokens.cache.write')) AS cache_write_tokens, "
    "COUNT(*) AS message_count "
    "FROM message WHERE json_extract(data,'$.role')='assistant'"
)


def _opencode_executable() -> str | None:
    for name in ("opencode", "opencode.cmd", "opencode.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def fetch_opencode_usage(timeout: float = _FETCH_TIMEOUT_S) -> ProviderUsage:
    """Sums opencode's own self-tallied per-message cost/token fields via a
    read-only `opencode db` query. Blocking (subprocess, bounded by
    *timeout*) — run off the Qt main thread. Result always lands in `spend`,
    never `utilization` — see module docstring.

    #455 follow-up: same isolation gap as codex — an installed build's
    cockpit process never inherits `XDG_DATA_HOME`/`XDG_CONFIG_HOME` pointed
    at DATA_HOME (only `pane_env.inject_provider_home_env` injects those,
    into spawned panes), so the spawned `opencode` binary would otherwise
    resolve its OS-default `opencode.db` instead of the cockpit's isolated
    one (see `opencode_helper.opencode_db_path`'s own isolation-first
    resolution). Passing `config.provider_home_env("opencode")` here keeps
    this probe reading the same database the pane writes.
    """
    exe = _opencode_executable()
    if not exe:
        return _unsupported("opencode", "opencode binary not found on PATH")
    from . import config

    home_env = config.provider_home_env("opencode")
    env = {**os.environ, **home_env} if home_env else None
    try:
        result = subprocess.run(
            [exe, "db", _OPENCODE_STATS_QUERY, "--format", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _error("opencode", f"opencode db query failed: {exc}")
    if result.returncode != 0:
        return _error("opencode", "opencode db query failed")
    try:
        rows = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return _error("opencode", "opencode db returned unparsable output")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return _error("opencode", "opencode db returned no data")
    row = rows[0]
    spend = {
        "cost_usd": row.get("cost") or 0,
        "input_tokens": row.get("input_tokens") or 0,
        "output_tokens": row.get("output_tokens") or 0,
        "cache_read_tokens": row.get("cache_read_tokens") or 0,
        "cache_write_tokens": row.get("cache_write_tokens") or 0,
        "message_count": row.get("message_count") or 0,
    }
    return ProviderUsage(
        provider="opencode",
        status=STATUS_ACTIVE,
        fetched_at=datetime.now(tz=UTC),
        spend=spend,
    )


# ── kimi ──────────────────────────────────────────────────────────────────
# Survey found no channel at all: no CLI subcommand (`kimi --help`'s full
# list has nothing usage-shaped) and no local state file carries anything
# beyond a plain OAuth token blob. Statically unsupported — nothing to
# probe at runtime.


def fetch_kimi_usage() -> ProviderUsage:
    return _unsupported("kimi", "no usage/quota channel found in kimi CLI")


# ── cursor ────────────────────────────────────────────────────────────────
# Survey could not test this provider at all — `cursor-agent`/`agent` was
# not installed on the spike machine. Distinguish "installed but no known
# channel yet" from "not installed here" in the reason text so the UI can
# say something more honest than a blanket "unsupported".


def fetch_cursor_usage() -> ProviderUsage:
    installed = any(
        shutil.which(name) for name in ("cursor-agent", "cursor-agent.exe", "agent", "agent.exe")
    )
    if installed:
        return _unsupported("cursor", "cursor CLI usage/quota channel not yet verified (#103)")
    return _unsupported("cursor", "cursor CLI not installed")


# ── dispatch + background store ──────────────────────────────────────────

_FETCHERS: dict[str, Callable[[], ProviderUsage]] = {
    "claude": fetch_claude_usage,
    "codex": fetch_codex_usage,
    "gemini": fetch_gemini_usage,
    "opencode": fetch_opencode_usage,
    "kimi": fetch_kimi_usage,
    "cursor": fetch_cursor_usage,
}


# Providers with a real per-account isolation knob (config.py's
# `_PROVIDER_HOME_SUBDIRS` / user_profile's CLAUDE_CONFIG_DIR) that
# `fetch_provider_usage`'s `config_dir` param can actually scope a probe to.
# Every other provider has no such knob yet (config.PROVIDER_ISOLATION_GAPS)
# so `config_dir` is silently ignored for them rather than guessed at.
_CONFIG_DIR_AWARE_FETCHERS: dict[str, Callable[..., ProviderUsage]] = {
    "claude": fetch_claude_usage,
    "codex": fetch_codex_usage,
}


def fetch_provider_usage(provider: str, config_dir: Path | None = None) -> ProviderUsage:
    """Single-provider fetch with a catch-all safety net. Blocking — run off
    the Qt main thread (same contract as the individual `fetch_*` functions
    above).

    `config_dir` (epic #309 Phase 3b): scopes the probe to one account's
    isolated config/home dir for the providers that support it
    (`_CONFIG_DIR_AWARE_FETCHERS`) — ignored (provider-level fetch, same as
    before) for every other provider.
    """
    if config_dir is not None:
        aware_fetcher = _CONFIG_DIR_AWARE_FETCHERS.get(provider)
        if aware_fetcher is not None:
            try:
                return aware_fetcher(config_dir=config_dir)
            except Exception:
                _log.exception("unexpected error fetching usage for %s", provider)
                return _error(provider, "unexpected error")
    fetcher = _FETCHERS.get(provider)
    if fetcher is None:
        return _unsupported(provider, f"unknown provider: {provider}")
    try:
        return fetcher()
    except Exception:
        _log.exception("unexpected error fetching usage for %s", provider)
        return _error(provider, "unexpected error")


class ProviderUsageStore:
    """Background poller for all six providers, cached for readers like the
    `/api/usage` remote endpoint to consume WITHOUT ever triggering a live
    fetch themselves (design doc §4 — a phone poll must never become an
    extra rate-limit hit). One daemon thread does the fetching; every other
    method here only touches the in-memory cache under a lock.

    A provider whose most recent fetch came back `unsupported` is never
    re-polled — that verdict is static per machine (binary present or not,
    channel exists or not), so re-fetching it every interval would be pure
    waste.
    """

    def __init__(
        self,
        interval_s: int = 300,
        on_update: Callable[[str, ProviderUsage], None] | None = None,
    ) -> None:
        self._interval_s = interval_s
        self._on_update = on_update
        self._lock = threading.Lock()
        self._cache: dict[str, ProviderUsage] = {}
        # Account-scoped cache, keyed by (provider, str(config_dir)) — kept
        # separate from `_cache` so `get_all()`/the background poll loop
        # (both provider-level, unchanged since before Phase 3b) never see
        # an account-scoped entry mixed in.
        self._account_cache: dict[tuple[str, str], ProviderUsage] = {}
        self._running = False
        self._wake = threading.Event()

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="provider-usage-loop").start()

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def get(self, provider: str, config_dir: Path | str | None = None) -> ProviderUsage | None:
        if config_dir is None:
            with self._lock:
                return self._cache.get(provider)
        with self._lock:
            return self._account_cache.get((provider, str(config_dir)))

    def get_all(self) -> dict[str, ProviderUsage]:
        with self._lock:
            return dict(self._cache)

    def refresh_now(self, provider: str, config_dir: Path | str | None = None) -> None:
        """Fire a background fetch for one provider (or, with *config_dir*,
        one ACCOUNT — epic #309 Phase 3b) outside the regular interval (e.g.
        right after an AI generation completes — design doc §4). Non-
        blocking: returns immediately, cache updates asynchronously.
        """
        # 1-arg call when config_dir is None (unchanged from before Phase
        # 3b) — a caller-supplied `_fetch_one` stand-in (tests) that only
        # accepts `provider` keeps working; only the new per-account path
        # needs the extra arg.
        args = (provider,) if config_dir is None else (provider, config_dir)
        threading.Thread(
            target=self._fetch_one,
            args=args,
            daemon=True,
            name=f"usage-fetch-{provider}",
        ).start()

    # ── internal ──────────────────────────────────────────────────

    def _fetch_one(self, provider: str, config_dir: Path | str | None = None) -> None:
        if config_dir is None:
            with self._lock:
                if provider not in self._cache:
                    self._cache[provider] = ProviderUsage(provider=provider, status=STATUS_LOADING)
                    self._emit(provider, self._cache[provider])
            data = fetch_provider_usage(provider)
            with self._lock:
                self._cache[provider] = data
            self._emit(provider, data)
            return
        key = (provider, str(config_dir))
        with self._lock:
            if key not in self._account_cache:
                self._account_cache[key] = ProviderUsage(provider=provider, status=STATUS_LOADING)
        data = fetch_provider_usage(provider, config_dir=Path(config_dir))
        with self._lock:
            self._account_cache[key] = data
        self._emit(provider, data)

    def _emit(self, provider: str, data: ProviderUsage) -> None:
        if self._on_update is not None:
            try:
                self._on_update(provider, data)
            except Exception:
                _log.exception("Error in ProviderUsageStore on_update callback")

    def _loop(self) -> None:
        for provider in PROVIDER_NAMES:
            if not self._running:
                return
            self._fetch_one(provider)
        while self._running:
            self._wake.wait(timeout=self._interval_s)
            self._wake.clear()
            if not self._running:
                return
            for provider in PROVIDER_NAMES:
                if not self._running:
                    return
                with self._lock:
                    cached = self._cache.get(provider)
                if cached is not None and cached.status == STATUS_UNSUPPORTED:
                    continue
                self._fetch_one(provider)


_store_lock = threading.Lock()
_store: ProviderUsageStore | None = None


def get_store() -> ProviderUsageStore:
    """Process-wide singleton. First call starts the background poll thread
    — it never blocks on a fetch itself, so callers (like the remote HTTP
    handler) can call this inline and still get an immediate reply built
    from whatever is already cached (`loading` for anything not fetched
    yet)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = ProviderUsageStore()
            _store.start()
        return _store
