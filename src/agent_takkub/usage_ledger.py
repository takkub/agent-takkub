"""Usage ledger — append-only token/quota history per provider→account→model
(issue #507, user directive 2026-09-07: "เอาแบบที่เสียจริงๆ" — every number
must come from the provider itself, never estimated from message length or
split proportionally across models).

Layout under ``config.RUNTIME_DIR/usage/`` (dev checkout: ``<repo>/runtime/``,
installed build: ``DATA_HOME/runtime/`` — same convention as every other
report in this codebase, e.g. ``runtime/docs_drift.md``, qa-gate reports):

    usage/<provider>/<account>/YYYY-MM.jsonl        turn-level rows (raw)
    usage/<provider>/<account>/quota-YYYY-MM.jsonl  quota-% samples (raw)
    usage/<provider>/<account>/daily.json           per-day/model rollup
    usage/<provider>/<account>/_import_cursor.json  import progress (perf only)

Only the CURRENT month's raw ``YYYY-MM.jsonl``/``quota-YYYY-MM.jsonl`` are
kept — ``rollup_daily`` folds every raw row into ``daily.json`` (which is
never pruned) before deleting anything older, so nothing is lost.

Two-tier idempotency for the importers:
  * ``_import_cursor.json`` skips a source file entirely when its
    (size, mtime) hasn't changed since the last run — this is what keeps
    ``takkub usage`` cheap to call on every invocation against ~5-6k
    session files (a pure `stat()` per unchanged file).
  * ``record_turn``'s per-(provider, account, month) dedup-by-request-id
    guard is the actual correctness guarantee (a changed file is always
    re-scanned in full) — a request id already present in this month's
    ledger is skipped, so a rerun (or a file that grew) never double-counts.

Every provider adapter here is best-effort and never raises: a corrupt line,
a missing file, or an unreadable database degrades to "skip this row/file",
never a crash — same failure policy as ``token_meter.py``/``provider_usage.py``.

Countability is a static, documented fact per provider (never a runtime
guess) — see ``TURN_COUNTABLE``/``QUOTA_COUNTABLE`` below, sourced from the
schemas already confirmed (or explicitly not confirmed) in ``token_meter.py``
and ``provider_usage.py``.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from datetime import date as _date
from pathlib import Path
from typing import Any

TURN_FIELDS: tuple[str, ...] = ("input", "cache_creation", "cache_read", "output")

# ── countability registry (never guess — #507) ───────────────────────────
# claude: transcript JSONL confirmed (token_meter.read_last_usage). codex:
# rollout `token_count` events confirmed live (codex_helper.read_codex_token_usage,
# verified against codex-cli 0.151.0) — model comes from the `turn_context`
# event's own `model` field (also confirmed live), not a guess. opencode:
# message.data.tokens + modelID confirmed against a real opencode.db
# (opencode_helper.read_opencode_token_usage docstring). gemini/kimi/cursor:
# no confirmed per-turn schema (kimi's is explicitly flagged "provisional,
# never verified against a real line" in kimi_helper.py; cursor's schema has
# never been captured at all; gemini's transcript carries no usage field —
# token_meter's own _GEMINI_UNSUPPORTED_REASON).
TURN_COUNTABLE: dict[str, bool] = {
    "claude": True,
    "codex": True,
    "opencode": True,
    "gemini": False,
    "kimi": False,
    "cursor": False,
}
TURN_UNCOUNTABLE_REASON: dict[str, str] = {
    "gemini": (
        "agy/Antigravity transcript ไม่มี token/usage field เลย — ยืนยันแล้วใน "
        "token_meter._GEMINI_UNSUPPORTED_REASON"
    ),
    "kimi": (
        "kimi wire.jsonl usage schema ยังไม่เคยเจอ StatusUpdate จริงมายืนยัน — "
        "provisional ตาม kimi_helper.read_kimi_token_usage เอง ห้ามเดาต่อ"
    ),
    "cursor": (
        "cursor-agent ยังไม่เคยจับ transcript schema ได้เลย — token_meter._CURSOR_UNSUPPORTED_REASON"
    ),
}

# quota-% countability mirrors provider_usage.py's own contract: a provider
# whose adapter never populates `utilization` (opencode: self-tallied
# `spend` only) or has no channel at all (kimi/cursor) must never get a
# fabricated quota row here either.
QUOTA_COUNTABLE: dict[str, bool] = {
    "claude": True,
    "codex": True,
    "gemini": True,
    "opencode": False,
    "kimi": False,
    "cursor": False,
}
QUOTA_UNCOUNTABLE_REASON: dict[str, str] = {
    "opencode": "opencode ไม่มี quota API — มีแต่ self-tallied spend (provider_usage.fetch_opencode_usage)",
    "kimi": "kimi ไม่มี usage/quota channel เลย (provider_usage.fetch_kimi_usage)",
    "cursor": "cursor CLI usage/quota channel ยังไม่ verified (provider_usage.fetch_cursor_usage)",
}

_DEFAULT_ACCOUNT = "default"
# #507 spec: "เก็บ raw ไว้เดือนเดียว" — only the CURRENT month's raw turn/quota
# files stay on disk; everything older lives solely in daily.json (already
# folded in by `rollup_daily` before a file older than this is deleted).
_RETENTION_MONTHS = 0


def countability_report() -> dict[str, dict[str, tuple[bool, str | None]]]:
    """Snapshot of every provider's turn/quota countability + reason, for a
    CLI/done-report to print verbatim (never re-derive this by hand)."""
    return {
        "turn": {p: (ok, TURN_UNCOUNTABLE_REASON.get(p)) for p, ok in TURN_COUNTABLE.items()},
        "quota": {p: (ok, QUOTA_UNCOUNTABLE_REASON.get(p)) for p, ok in QUOTA_COUNTABLE.items()},
    }


# ── paths ─────────────────────────────────────────────────────────────────


def usage_root() -> Path:
    """Where the ledger lives. `TAKKUB_USAGE_LEDGER_DIR` overrides the
    default `config.RUNTIME_DIR/usage` — lets a read-only cross-check
    against another machine's transcripts (e.g. prod) write its ledger to
    a scratch dir instead of this process's own DATA_HOME, and lets a user
    who moved machines or wants one combined ledger point it anywhere.
    Combine with `usage import --source <dir>` (which controls where
    TRANSCRIPTS are read from) — this only controls where the LEDGER is
    WRITTEN; importers never write into a `--source` dir.
    """
    import os

    override = os.environ.get("TAKKUB_USAGE_LEDGER_DIR", "").strip()
    if override:
        return Path(override)
    from . import config

    return config.RUNTIME_DIR / "usage"


def account_dir(provider: str, account: str) -> Path:
    return usage_root() / provider / (account or _DEFAULT_ACCOUNT)


def _turn_file(provider: str, account: str, month: str) -> Path:
    return account_dir(provider, account) / f"{month}.jsonl"


def _quota_file(provider: str, account: str, month: str) -> Path:
    return account_dir(provider, account) / f"quota-{month}.jsonl"


def _daily_file(provider: str, account: str) -> Path:
    return account_dir(provider, account) / "daily.json"


def _cursor_file(provider: str, account: str) -> Path:
    return account_dir(provider, account) / "_import_cursor.json"


def _month_of(ts_iso: str) -> str:
    return ts_iso[:7]


# ── low-level jsonl I/O ─────────────────────────────────────────────────────


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    return out


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _existing_request_ids(provider: str, account: str, month: str) -> set[str]:
    rows = _read_jsonl(_turn_file(provider, account, month))
    return {r["request_id"] for r in rows if r.get("request_id")}


# ── recording ────────────────────────────────────────────────────────────


def record_turn(
    provider: str,
    account: str,
    ts_iso: str,
    request_id: str,
    model: str,
    usage: dict[str, Any],
    *,
    seen_cache: dict[tuple[str, str, str], set[str]] | None = None,
) -> bool:
    """Append one turn row unless `request_id` was already recorded for this
    (provider, account, month) — returns True iff a new row was appended.

    `seen_cache` (shared across many calls in one import run) avoids
    re-reading a month's already-written request ids off disk for every
    single row; a bare call with no cache still works, just slower.
    """
    if not request_id or not ts_iso:
        return False
    month = _month_of(ts_iso)
    key = (provider, account, month)
    cache = seen_cache if seen_cache is not None else {}
    if key not in cache:
        cache[key] = _existing_request_ids(provider, account, month)
    seen = cache[key]
    if request_id in seen:
        return False
    row = {
        "ts": ts_iso,
        "request_id": request_id,
        "model": model or "unknown",
        **{field: int(usage.get(field) or 0) for field in TURN_FIELDS},
    }
    _append_jsonl(_turn_file(provider, account, month), row)
    seen.add(request_id)
    return True


def record_quota_sample(
    provider: str,
    account: str,
    ts_iso: str,
    window: str,
    utilization: float | None,
    resets_at_iso: str | None,
) -> bool:
    """Append one quota-% sample. A `None` utilization is never fabricated
    into a data point (mirrors provider_usage.py's own "missing is None,
    never 0" contract) — returns False without writing anything. Also
    skips a sample that exactly repeats the immediately-preceding one for
    the same window (same ts) — guards double-recording when both the
    provider-level and an account-scoped fetch land in the same tick.
    """
    if utilization is None or not ts_iso or not window:
        return False
    path = _quota_file(provider, account, _month_of(ts_iso))
    rows = _read_jsonl(path)
    if rows:
        last = rows[-1]
        if last.get("window") == window and last.get("ts") == ts_iso:
            return False
    _append_jsonl(
        path,
        {"ts": ts_iso, "window": window, "utilization": utilization, "resets_at": resets_at_iso},
    )
    return True


# ── import cursor (perf only — never the correctness guard) ────────────────


def _load_cursor(provider: str, account: str) -> dict:
    path = _cursor_file(provider, account)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cursor(provider: str, account: str, cursor: dict) -> None:
    path = _cursor_file(provider, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cursor), encoding="utf-8")


def _file_unchanged(cursor: dict, key: str, st) -> bool:
    prev = cursor.get(key)
    return bool(prev and prev.get("size") == st.st_size and prev.get("mtime") == st.st_mtime)


def _mark_file_scanned(cursor: dict, key: str, st) -> None:
    cursor[key] = {"size": st.st_size, "mtime": st.st_mtime}


# ── claude importer ──────────────────────────────────────────────────────


def _resolve_claude_config_dir(entry_config_dir: str) -> Path:
    if entry_config_dir:
        return Path(entry_config_dir)
    from . import config

    return config.default_claude_config_dir()


def _import_claude_file(
    path: Path,
    account: str,
    cursor: dict,
    stats: dict,
    seen_cache: dict[tuple[str, str, str], set[str]],
) -> None:
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return
    if _file_unchanged(cursor, key, st):
        stats["skipped_files"] += 1
        return
    stats["scanned_files"] += 1
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if j.get("type") != "assistant":
                    continue
                msg = j.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                ts = j.get("timestamp")
                if not isinstance(ts, str):
                    continue
                request_id = j.get("requestId") or msg.get("id")
                if not request_id:
                    continue
                fields = {
                    "input": usage.get("input_tokens"),
                    "cache_creation": usage.get("cache_creation_input_tokens"),
                    "cache_read": usage.get("cache_read_input_tokens"),
                    "output": usage.get("output_tokens"),
                }
                if record_turn(
                    "claude",
                    account,
                    ts,
                    str(request_id),
                    msg.get("model") or "unknown",
                    fields,
                    seen_cache=seen_cache,
                ):
                    stats["new_turns"] += 1
    except OSError:
        return
    _mark_file_scanned(cursor, key, st)


def import_claude(profiles: list[dict] | None = None) -> dict:
    """Walk every Claude profile's `projects/**/*.jsonl` and record every
    assistant turn's real usage block. Idempotent — see module docstring.

    A "shared-session" profile (`user_profile.provision_shared_profile`)
    junctions/symlinks its `projects/` dir straight into another profile's
    — same physical transcripts, different account name. Resolving each
    profile's `projects_dir` and skipping one already seen this run is
    what keeps that from being counted twice under two account labels
    (caught on a real machine: a profile named "office" sharing sessions
    with "default" doubled every total until this guard was added).
    """
    if profiles is None:
        from . import user_profile

        profiles = user_profile.profiles_for_provider("claude")

    stats = {"scanned_files": 0, "skipped_files": 0, "new_turns": 0}
    seen_cache: dict[tuple[str, str, str], set[str]] = {}
    seen_projects_dirs: set[Path] = set()
    for profile in profiles:
        account = profile.get("name") or _DEFAULT_ACCOUNT
        config_dir = _resolve_claude_config_dir(profile.get("config_dir") or "")
        projects_dir = config_dir / "projects"
        if not projects_dir.is_dir():
            continue
        try:
            resolved = projects_dir.resolve()
        except OSError:
            resolved = projects_dir
        if resolved in seen_projects_dirs:
            continue
        seen_projects_dirs.add(resolved)
        cursor = _load_cursor("claude", account)
        for jsonl_path in projects_dir.glob("*/*.jsonl"):
            _import_claude_file(jsonl_path, account, cursor, stats, seen_cache)
        _save_cursor("claude", account, cursor)
    return stats


# ── codex importer ───────────────────────────────────────────────────────


def _import_codex_file(
    path: Path,
    account: str,
    cursor: dict,
    stats: dict,
    seen_cache: dict[tuple[str, str, str], set[str]],
) -> None:
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return
    if _file_unchanged(cursor, key, st):
        stats["skipped_files"] += 1
        return
    stats["scanned_files"] += 1
    stem = path.stem
    # Codex's `token_count` events never carry a model name — the model is
    # tracked from the most recent `turn_context` event's own `model` field
    # (confirmed live), same "last known state, forward-scan" pattern
    # token_meter._scan_lines_for_usage already uses for claude's model.
    current_model: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = j.get("payload")
                if j.get("type") == "turn_context" and isinstance(payload, dict):
                    model = payload.get("model")
                    if isinstance(model, str) and model:
                        current_model = model
                    continue
                if j.get("type") != "event_msg" or not isinstance(payload, dict):
                    continue
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                last = info.get("last_token_usage")
                if not isinstance(last, dict):
                    continue
                ts = j.get("timestamp")
                ordinal = j.get("ordinal")
                if not isinstance(ts, str) or ordinal is None:
                    continue
                fields = {
                    "input": last.get("input_tokens"),
                    "cache_creation": last.get("cache_write_input_tokens"),
                    "cache_read": last.get("cached_input_tokens"),
                    "output": last.get("output_tokens"),
                }
                request_id = f"{stem}:{ordinal}"
                if record_turn(
                    "codex",
                    account,
                    ts,
                    request_id,
                    current_model or "codex",
                    fields,
                    seen_cache=seen_cache,
                ):
                    stats["new_turns"] += 1
    except OSError:
        return
    _mark_file_scanned(cursor, key, st)


def import_codex(profiles: list[dict] | None = None) -> dict:
    """Walk every Codex profile's `sessions/`+`archived_sessions/` rollout
    JSONLs and record every `token_count` event's real per-turn usage.
    """
    from .codex_helper import codex_home

    if profiles is None:
        from . import user_profile

        profiles = user_profile.profiles_for_provider("codex")

    stats = {"scanned_files": 0, "skipped_files": 0, "new_turns": 0}
    seen_cache: dict[tuple[str, str, str], set[str]] = {}
    for profile in profiles:
        account = profile.get("name") or _DEFAULT_ACCOUNT
        raw_dir = profile.get("config_dir") or ""
        home = Path(raw_dir) if raw_dir else codex_home()
        cursor = _load_cursor("codex", account)
        for base in (home / "sessions", home / "archived_sessions"):
            if not base.is_dir():
                continue
            for path in base.rglob("rollout-*.jsonl"):
                _import_codex_file(path, account, cursor, stats, seen_cache)
        _save_cursor("codex", account, cursor)
    return stats


# ── opencode importer ────────────────────────────────────────────────────


def import_opencode(db_path: Path | None = None) -> dict:
    """Read every historical assistant message's real `tokens`+`modelID`
    straight out of `opencode.db` (confirmed schema — see
    `opencode_helper.read_opencode_token_usage`'s own docstring). Single
    "default" account: opencode has no per-account profile switching yet.

    `db_path` overrides the normal isolation-first resolution (`import_all`'s
    `source=` passes an explicit db file for a read-only cross-machine
    check) — the importer only ever reads it, never writes.
    """
    from . import opencode_helper

    stats = {"scanned_files": 0, "skipped_files": 0, "new_turns": 0}
    if db_path is None:
        db_path = opencode_helper.opencode_db_path()
    if db_path is None or not db_path.is_file():
        return stats

    account = _DEFAULT_ACCOUNT
    cursor = _load_cursor("opencode", account)
    key = str(db_path)
    try:
        st = db_path.stat()
    except OSError:
        return stats
    if _file_unchanged(cursor, key, st):
        stats["skipped_files"] += 1
        return stats
    stats["scanned_files"] += 1

    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, time_created, data FROM message "
                "WHERE json_extract(data,'$.role')='assistant'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return stats

    seen_cache: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError, sqlite3.Error):
            continue
        if not isinstance(data, dict):
            continue
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            continue
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        try:
            ts_iso = datetime.fromtimestamp(int(row["time_created"]) / 1000.0, tz=UTC).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        fields = {
            "input": tokens.get("input"),
            "cache_creation": cache.get("write"),
            "cache_read": cache.get("read"),
            "output": tokens.get("output"),
        }
        model = str(data.get("modelID") or "unknown")
        if record_turn(
            "opencode", account, ts_iso, str(row["id"]), model, fields, seen_cache=seen_cache
        ):
            stats["new_turns"] += 1

    _mark_file_scanned(cursor, key, st)
    _save_cursor("opencode", account, cursor)
    return stats


_IMPORTERS: dict[str, Any] = {
    "claude": import_claude,
    "codex": import_codex,
    "opencode": import_opencode,
}


def import_all(provider: str | None = None, *, source: str | None = None) -> dict[str, dict]:
    """Run every turn-countable provider's importer, or just one. A provider
    with no importer (not turn-countable) reports its reason instead of
    crashing — see `TURN_UNCOUNTABLE_REASON`.

    `source` (requires `provider`) reads transcripts from that directory
    instead of the normal profile-registry resolution — e.g. pointing
    read-only at another machine's `claude-config` to cross-check a count
    without ever writing anything there (combine with
    `TAKKUB_USAGE_LEDGER_DIR` to also keep the ledger itself out of this
    process's own DATA_HOME).
    """
    if provider:
        if provider not in _IMPORTERS:
            return {provider: {"error": TURN_UNCOUNTABLE_REASON.get(provider, "unknown provider")}}
        if source:
            if provider == "opencode":
                return {provider: import_opencode(Path(source))}
            return {
                provider: _IMPORTERS[provider]([{"name": _DEFAULT_ACCOUNT, "config_dir": source}])
            }
        return {provider: _IMPORTERS[provider]()}
    return {name: fn() for name, fn in _IMPORTERS.items()}


# ── rollup ───────────────────────────────────────────────────────────────


def _month_files(provider: str, account: str) -> list[Path]:
    d = account_dir(provider, account)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl"))


def _month_n_ago(n: int) -> str:
    now = datetime.now(tz=UTC)
    year, month = now.year, now.month - n
    while month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}"


def _load_daily(provider: str, account: str) -> dict:
    path = _daily_file(provider, account)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def rollup_daily(provider: str, account: str, *, retention_months: int = _RETENTION_MONTHS) -> dict:
    """Recompute `daily.json`'s per-day/model totals from every raw turn
    file still on disk (full recompute, not incremental merge — safe
    because `record_turn` already guarantees no duplicate rows in the raw
    files), then prune raw month files older than the retention window.
    Cheap enough to call on every `takkub usage` read: usually only the
    current month's file has any rows.
    """
    daily = _load_daily(provider, account)
    month_files = _month_files(provider, account)

    fresh: dict[str, dict[str, dict[str, int]]] = {}
    for path in month_files:
        for row in _read_jsonl(path):
            ts = row.get("ts")
            if not isinstance(ts, str) or len(ts) < 10:
                continue
            date_str = ts[:10]
            model = row.get("model") or "unknown"
            bucket = fresh.setdefault(date_str, {}).setdefault(
                model,
                {"turns": 0, "input": 0, "cache_creation": 0, "cache_read": 0, "output": 0},
            )
            bucket["turns"] += 1
            for field in TURN_FIELDS:
                bucket[field] += int(row.get(field) or 0)

    daily.update(fresh)
    daily_path = _daily_file(provider, account)
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_text(json.dumps(daily, sort_keys=True), encoding="utf-8")

    cutoff = _month_n_ago(retention_months)
    for path in month_files:
        if path.stem < cutoff:
            try:
                path.unlink()
            except OSError:
                pass

    return daily


# ── query / report ───────────────────────────────────────────────────────


def _all_accounts(provider: str | None = None) -> list[tuple[str, str]]:
    root = usage_root()
    if not root.is_dir():
        return []
    providers = [provider] if provider else sorted(p.name for p in root.iterdir() if p.is_dir())
    out: list[tuple[str, str]] = []
    for prov in providers:
        pdir = root / prov
        if not pdir.is_dir():
            continue
        out.extend((prov, adir.name) for adir in sorted(pdir.iterdir()) if adir.is_dir())
    return out


def daily_series(provider: str | None = None, *, days: int = 14) -> list[tuple[str, int]]:
    """`(date, total_tokens)` pairs for the last `days` days — the trend
    sparkline's data source. Summed across every account/model for
    `provider` (or every turn-countable provider when None). Always rolls
    up first, same as `query_usage`, so a fresh turn shows up without a
    separate explicit rollup step.
    """
    end_date = datetime.now(tz=UTC).date()
    dates = [_date.fromordinal(end_date.toordinal() - i) for i in range(days - 1, -1, -1)]
    totals: dict[str, int] = {d.isoformat(): 0 for d in dates}

    for prov, account in _all_accounts(provider):
        if not TURN_COUNTABLE.get(prov, False):
            continue
        rollup_daily(prov, account)
        daily = _load_daily(prov, account)
        for date_str, models in daily.items():
            if date_str not in totals or not isinstance(models, dict):
                continue
            for agg in models.values():
                if isinstance(agg, dict):
                    totals[date_str] += sum(int(agg.get(f) or 0) for f in TURN_FIELDS)

    return sorted(totals.items())


def _date_in_range(date_str: str, start: _date, end: _date) -> bool:
    try:
        d = _date.fromisoformat(date_str)
    except ValueError:
        return False
    return start <= d <= end


def _rtk_gain_summary() -> str | None:
    """First line of `rtk gain`'s own stdout, or None when rtk isn't on
    PATH / doesn't run / prints nothing — never mixed into the real (item
    1) totals, per #507's explicit "ห้ามเอาไปบวก/ลบกับข้อ 1"."""
    import shutil

    exe = shutil.which("rtk") or shutil.which("rtk.exe") or shutil.which("rtk.cmd")
    if not exe:
        return None
    try:
        from ._win_console import SUBPROCESS_NO_WINDOW

        result = subprocess.run(
            [exe, "gain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    return lines[0] if lines else None


def _query_quota(
    accounts: list[tuple[str, str]], start: _date, end: _date, month: str | None
) -> list[dict]:
    out: list[dict] = []
    reported_uncountable: set[str] = set()
    months = {month} if month else {start.strftime("%Y-%m"), end.strftime("%Y-%m")}
    for prov, account in accounts:
        if not QUOTA_COUNTABLE.get(prov, False):
            if prov not in reported_uncountable:
                out.append(
                    {
                        "provider": prov,
                        "account": account,
                        "window": None,
                        "delta_pct": None,
                        "samples": 0,
                        "reason": QUOTA_UNCOUNTABLE_REASON.get(prov, "นับไม่ได้"),
                    }
                )
                reported_uncountable.add(prov)
            continue
        by_window: dict[str, list[dict]] = {}
        for m in months:
            for sample in _read_jsonl(_quota_file(prov, account, m)):
                ts = sample.get("ts")
                window = sample.get("window")
                if not isinstance(ts, str) or not isinstance(window, str):
                    continue
                date_str = ts[:10]
                in_range = (
                    date_str.startswith(month) if month else _date_in_range(date_str, start, end)
                )
                if not in_range:
                    continue
                by_window.setdefault(window, []).append(sample)
        for window, items in sorted(by_window.items()):
            items.sort(key=lambda r: r.get("ts", ""))
            delta = 0.0
            prev: float | None = None
            counted = 0
            for item in items:
                u = item.get("utilization")
                if not isinstance(u, (int, float)):
                    continue
                counted += 1
                if prev is not None and u > prev:
                    delta += u - prev
                prev = u
            out.append(
                {
                    "provider": prov,
                    "account": account,
                    "window": window,
                    "delta_pct": round(delta, 2) if counted else None,
                    "samples": counted,
                }
            )
    return out


def query_usage(
    *, days: int | None = None, month: str | None = None, provider: str | None = None
) -> dict:
    """Aggregate the ledger for `takkub usage`/the Settings Usage page.
    Always re-rolls each account's `daily.json` first — see `rollup_daily`
    — so a number is never stale just because nobody explicitly ran
    `takkub usage import` since the last recorded sample.
    """
    accounts = _all_accounts(provider)
    end_date = datetime.now(tz=UTC).date()
    n = days if days and days > 0 else 7
    start_date = _date.fromordinal(end_date.toordinal() - (n - 1))

    rows: list[dict] = []
    uncountable: list[dict] = []
    reported_uncountable: set[str] = set()

    for prov, account in accounts:
        rollup_daily(prov, account)
        daily = _load_daily(prov, account)
        if not TURN_COUNTABLE.get(prov, False):
            if prov not in reported_uncountable:
                uncountable.append(
                    {"provider": prov, "reason": TURN_UNCOUNTABLE_REASON.get(prov, "นับไม่ได้")}
                )
                reported_uncountable.add(prov)
            continue
        totals: dict[str, dict[str, int]] = {}
        for date_str, models in daily.items():
            in_range = (
                date_str.startswith(month)
                if month
                else _date_in_range(date_str, start_date, end_date)
            )
            if not in_range or not isinstance(models, dict):
                continue
            for model, agg in models.items():
                if not isinstance(agg, dict):
                    continue
                bucket = totals.setdefault(
                    model,
                    {"turns": 0, "input": 0, "cache_creation": 0, "cache_read": 0, "output": 0},
                )
                bucket["turns"] += int(agg.get("turns") or 0)
                for field in TURN_FIELDS:
                    bucket[field] += int(agg.get(field) or 0)
        for model, agg in sorted(totals.items()):
            if agg["turns"] == 0:
                continue
            total = agg["input"] + agg["cache_creation"] + agg["cache_read"] + agg["output"]
            rows.append(
                {"provider": prov, "account": account, "model": model, "total": total, **agg}
            )

    return {
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "month": month,
        "rows": rows,
        "uncountable": uncountable,
        "quota": _query_quota(accounts, start_date, end_date, month),
        "rtk_gain": _rtk_gain_summary(),
    }


def format_usage_table(result: dict) -> str:
    """Plain-text render of `query_usage`'s output for the CLI."""
    lines: list[str] = []
    label = (
        f"month {result['month']}"
        if result.get("month")
        else f"{result['start']} .. {result['end']}"
    )
    lines.append(f"Usage report ({label})")
    lines.append("")

    rows = result.get("rows") or []
    if rows:
        lines.append(
            f"{'provider':<10} {'account':<12} {'model':<20} {'turns':>7} "
            f"{'input':>10} {'cache_wr':>10} {'cache_rd':>10} {'output':>10} {'total':>12}"
        )
        for r in rows:
            lines.append(
                f"{r['provider']:<10} {r['account']:<12} {r['model']:<20} {r['turns']:>7} "
                f"{r['input']:>10} {r['cache_creation']:>10} {r['cache_read']:>10} "
                f"{r['output']:>10} {r['total']:>12}"
            )
    else:
        lines.append("(no countable turn data in range)")
    for u in result.get("uncountable") or []:
        lines.append(f"{u['provider']:<10} — นับไม่ได้ ({u['reason']})")

    lines.append("")
    lines.append("Quota % หักในช่วง (ผลรวมส่วนที่เพิ่มขึ้นระหว่างตัวอย่าง, reset ไม่นับลบ):")
    quota = result.get("quota") or []
    if quota:
        for q in quota:
            if q.get("window") is None:
                lines.append(f"  {q['provider']:<10} — นับไม่ได้ ({q.get('reason')})")
            else:
                delta = q.get("delta_pct")
                delta_s = "—" if delta is None else f"{delta:.1f}%"
                lines.append(
                    f"  {q['provider']:<10} {q['account']:<12} {q['window']:<18} "
                    f"{delta_s:>8} ({q.get('samples', 0)} samples)"
                )
    else:
        lines.append("  (no quota samples in range)")

    lines.append("")
    rtk = result.get("rtk_gain")
    lines.append(f"rtk saved (ตามที่ rtk รายงาน — ไม่รวมกับตัวเลขข้างบน): {rtk or '—'}")
    return "\n".join(lines)
