"""Test-DB reachability preflight for the qa-gate (#529).

A DB-backed suite prints one connection-refused failure per test file when
the test DB container is missing or never started — dozens to hundreds of
failures, all reading exactly like a code regression, none of them saying
"infra", wasting the time it takes a human to notice the pattern by hand
(real incident: ~115 failures from one missing docker container). A cheap
TCP preflight against the URL the suite is about to use turns that into one
fail-fast line before the suite even starts.

Opt-in by env shape only: no DB URL env var set (most projects, and every
run of this repo's own Python-only test suite) means nothing to check —
this must never turn "no DB configured" into a spurious FAIL.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

# Priority order: a project that sets both a generic and a test-specific URL
# almost always means "use the test-specific one for the test run".
_DB_URL_ENV_VARS: tuple[str, ...] = (
    "TEST_DATABASE_URL",
    "DATABASE_URL",
    "POSTGRES_URL",
    "MYSQL_URL",
    "DB_URL",
)

_DEFAULT_PORTS: dict[str, int] = {
    "postgres": 5432,
    "postgresql": 5432,
    "mysql": 3306,
    "mysql2": 3306,
    "redis": 6379,
    "mongodb": 27017,
    "mongodb+srv": 27017,
}

_CONNECT_TIMEOUT_S = 3.0


@dataclass
class DbPreflightFinding:
    ok: bool
    skipped: bool
    detail: str


def _redact(url: str) -> str:
    """Never let a raw DB password reach gate output/log."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
        return url.replace(parts.netloc, netloc, 1)
    return url


def _find_db_url(env: dict) -> tuple[str, str] | None:
    """(env_var_name, url) for the first DB URL-like var set in *env*, else
    `None` — a project that never configures one has nothing for this
    preflight to check."""
    for name in _DB_URL_ENV_VARS:
        value = env.get(name)
        if value:
            return name, value
    return None


def check_test_db_reachable(env: dict) -> DbPreflightFinding | None:
    """`None` = nothing to check (no DB URL env var set). Otherwise a
    TCP-connect preflight against that URL's host:port: `ok=False` fails
    fast with one clear message instead of letting a whole DB-backed suite
    run and print N connection-refused failures that read like a code
    regression (#529)."""
    found = _find_db_url(env)
    if found is None:
        return None
    var_name, url = found

    host: str | None
    port: int | None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or _DEFAULT_PORTS.get((parts.scheme or "").lower())
    except ValueError:
        host = None
        port = None
    if not host or not port:
        return DbPreflightFinding(
            True,
            True,
            f"{var_name} ไม่สามารถแยก host/port ได้ ({_redact(url)}) — ข้าม preflight",
        )

    try:
        with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_S):
            pass
    except OSError as e:
        return DbPreflightFinding(
            False,
            False,
            f"test DB {host}:{port} unreachable ({var_name}, {type(e).__name__}: {e}) — "
            "start it before re-running qa-gate (e.g. `docker compose up -d`, or this "
            "project's own test-db-up/migrate scripts) — not a code regression, see #529",
        )
    return DbPreflightFinding(True, False, f"test DB {host}:{port} reachable ({var_name})")
