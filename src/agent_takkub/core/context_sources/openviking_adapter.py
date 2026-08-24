"""OpenViking sidecar adapter (issue #372, `14_OPENVIKING_INTEGRATION.md`).

HTTP client ONLY — OpenViking (volcengine/OpenViking, AGPL) is never
vendored into this repo (MIT). Every real endpoint used here is confirmed
against the upstream API docs (github.com/volcengine/OpenViking/blob/main/
docs/en/api/{06-retrieval,07-system,02-resources}.md), pinned to the
handful judged most stable:

- ``GET /health`` — no auth, ``{status, healthy, version, auth_mode}``.
  Chosen over ``GET /ready`` (which reports internal subsystem checks this
  adapter has no use for) and over the authenticated ``/api/v1/system/
  status`` (extra auth round-trip just to answer "is it up").
- ``POST /api/v1/search/find`` — plain vector-similarity search with no
  session/intent state. Chosen over ``/api/v1/search/search`` ("intelligent
  retrieval with session context") deliberately: this adapter has no
  server-side session to hand back, and `/find`'s request shape is a strict
  subset of `/search`'s much larger surface — fewer fields this adapter's
  schema-drift guard has to reason about.
- ``POST /api/v1/resources`` — add-resource ingest, used only by `takkub ov
  index` (see ``indexing.py``), never by the context-retrieval hot path.
  Returns the sidecar's own ``result.root_uri`` (issue #377) so the caller
  can key its local registry off the SAME identifier `/search/find` will
  later echo back on a hit — the request's own ``to=`` is advisory only.

Auth: ``X-API-Key: <key>`` header (upstream docs also allow a Bearer token;
this adapter only implements the one header form).

Every call is fail-open (plan §0-style rule, mirrored from `core.brain.
facade`): a network error, timeout, non-2xx status, or a response shape
this module doesn't recognise (schema drift) returns an empty/None result
and logs at DEBUG — this module NEVER raises into a caller. Must never run
on the Qt GUI thread — same requirement `core.brain.facade.
build_context_for_assign`'s docstring already places on ITS caller, which
is where every code path into this adapter originates.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

_ENV_ENABLED = "TAKKUB_OPENVIKING_ENABLED"
_ENV_MODE = "TAKKUB_OPENVIKING_MODE"
_ENV_URL = "TAKKUB_OPENVIKING_URL"
_ENV_API_KEY = "TAKKUB_OPENVIKING_API_KEY"

_DEFAULT_URL = "http://127.0.0.1:1933"
_TIMEOUT_S = 4.0
_INDEX_TIMEOUT_S = 15.0

MODES: tuple[str, ...] = ("shadow", "read", "hybrid")
_DEFAULT_MODE = "shadow"

# Tested major.minor prefixes ("Pin tested OpenViking release/capabilities",
# rollout doc). A version outside this set is NOT hard-blocked — fail-open
# beats refusing a sidecar that likely still works after a point release —
# it only flips `HealthStatus.known_version` to False so `doctor` can flag
# a capability-drift WARN.
_KNOWN_VERSION_PREFIXES = ("0.1.", "0.2.", "0.3.", "0.4.", "0.5.")


def enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "0") == "1"


def mode() -> str:
    raw = (os.environ.get(_ENV_MODE) or _DEFAULT_MODE).strip().lower()
    return raw if raw in MODES else _DEFAULT_MODE


def base_url() -> str:
    return (os.environ.get(_ENV_URL) or _DEFAULT_URL).rstrip("/")


def api_key() -> str | None:
    """``TAKKUB_OPENVIKING_API_KEY`` wins; otherwise a file the existing
    `core.secrets` mechanism's own `FileSecretBackend` shape can read (the
    whole file's stripped text IS the key — same convention that backend
    already uses for every provider credential file), so a key can be
    dropped at ``DATA_HOME/openviking/api_key`` without ever going in an
    env var or a tracked config file. Never raises: an unreadable/missing
    file is just "no key configured"."""
    direct = os.environ.get(_ENV_API_KEY)
    if direct:
        return direct
    try:
        from agent_takkub import config
        from agent_takkub.core.secrets.backends.file_backend import FileSecretBackend

        backend = FileSecretBackend(config.DATA_HOME / "openviking" / "api_key")
        value = backend.get("default")
        return value.strip() if value else None
    except Exception:
        return None


@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    healthy: bool
    version: str | None
    known_version: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    uri: str
    text: str
    score: float
    category: str


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = api_key()
    if key:
        headers["X-API-Key"] = key
    return headers


def _request(method: str, path: str, payload: dict | None, timeout: float) -> dict | None:
    """One HTTP round-trip, stdlib-only (matches this codebase's existing
    convention — `limit_status.py`/`claude_update.py` — no new HTTP
    dependency). Returns the parsed JSON body, or ``None`` on ANY failure:
    connection refused, DNS, timeout, non-2xx, or a body that isn't valid
    JSON. Never raises."""
    url = f"{base_url()}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        if not body:
            return {}
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        _log.debug("openviking adapter %s %s failed: %s", method, path, exc)
        return None


def health(*, timeout: float = _TIMEOUT_S) -> HealthStatus:
    """``GET /health`` — safe to call even when `enabled()` is False (e.g.
    from `takkub doctor` to explain WHY nothing works), but every other
    function in this module still gates on `enabled()` itself."""
    resp = _request("GET", "/health", None, timeout)
    if resp is None:
        return HealthStatus(
            ok=False, healthy=False, version=None, known_version=False, error="unreachable"
        )
    version = resp.get("version")
    version = version if isinstance(version, str) else None
    healthy = bool(resp.get("healthy"))
    known = version is not None and version.startswith(_KNOWN_VERSION_PREFIXES)
    return HealthStatus(ok=True, healthy=healthy, version=version, known_version=known)


def search_resources(query: str, *, limit: int = 8, timeout: float = _TIMEOUT_S) -> list[SearchHit]:
    """``POST /api/v1/search/find`` scoped to ``context_type=resource`` —
    OpenViking's memories/skills are out of scope here on purpose (`13_
    GRAFT_FINAL_ROLE.md`/`11_BRAIN_CONVERSATION_BOUNDARY.md`: operational
    memory stays Takkub Brain, OpenViking only ever supplies curated
    resources into this codebase's Context Builder — "OpenViking never
    directly injects into panes", `16_CONTEXT_MERGE_POLICY.md`)."""
    if not enabled() or not (query or "").strip():
        return []
    resp = _request(
        "POST",
        "/api/v1/search/find",
        {
            "query": query,
            "context_type": "resource",
            "node_limit": limit,
            "include_provenance": True,
        },
        timeout,
    )
    if not isinstance(resp, dict):
        return []
    result = resp.get("result")
    if not isinstance(result, dict):
        _log.debug("openviking search/find: unexpected response shape (schema drift?)")
        return []
    raw_hits = result.get("resources")
    if not isinstance(raw_hits, list):
        return []
    hits: list[SearchHit] = []
    for item in raw_hits:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        text = item.get("overview") or item.get("abstract") or ""
        if not isinstance(uri, str) or not uri or not isinstance(text, str) or not text:
            continue
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        hits.append(
            SearchHit(
                uri=uri, text=text, score=score, category=str(item.get("category") or "resource")
            )
        )
        if len(hits) >= limit:
            break
    return hits


def add_resource(
    path: Path, *, to: str | None = None, timeout: float = _INDEX_TIMEOUT_S
) -> str | None:
    """``POST /api/v1/resources`` — best-effort ingest of one local file
    into the sidecar's own knowledge base. Only `indexing.index_vault`
    calls this; never part of the context-retrieval hot path (it's a
    slower, write-side call — OpenViking may chunk/embed synchronously).

    Returns the ``result.root_uri`` the sidecar actually assigned this
    resource (the SAME identifier a later ``/search/find`` hit's ``uri``
    will carry — see `docs/en/concepts/04-viking-uri.md`), or ``None`` on
    any failure. ``to`` is a request, not a guarantee (upstream docs: when
    omitted the server auto-resolves ``root_uri`` itself, and nothing in
    the spec promises it is honoured when present either) — callers MUST
    key off the returned value, never off ``to``, to stay correlated with
    what `/search/find` will actually echo back later (issue #377)."""
    if not enabled():
        return None
    payload: dict = {"path": str(path), "wait": True}
    if to:
        payload["to"] = to
    resp = _request("POST", "/api/v1/resources", payload, timeout)
    if not isinstance(resp, dict):
        return None
    result = resp.get("result")
    if not isinstance(result, dict) or result.get("status") != "success":
        return None
    root_uri = result.get("root_uri")
    return root_uri if isinstance(root_uri, str) and root_uri else None


__all__ = [
    "MODES",
    "HealthStatus",
    "SearchHit",
    "add_resource",
    "api_key",
    "base_url",
    "enabled",
    "health",
    "mode",
    "search_resources",
]
