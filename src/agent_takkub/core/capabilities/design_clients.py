"""core.capabilities.design_clients — real HTTP clients for the optional
design-tool integrations (#373, GAP-014/015/016): 21st.dev component
reference search, Figma REST (variables/components/file summary), and
Penpot's self-hosted REST/RPC API.

Every client here:
  - is pure stdlib (`urllib`) — no new dependency for three optional,
    default-OFF integrations (minimal-code: `requests`/`httpx` would be a
    real ongoing cost — a new pinned version, a new supply-chain surface —
    for what `urllib.request` already does).
  - takes credentials as a plain string/args the CALLER already resolved
    via `core.contracts.secret_manager.SecretManager` — nothing in this
    module reads a secret store or a permission policy itself, so it can
    never be reached by accident with an unauthorized role attached (see
    `design_integrations.build_client`, the only constructor call site).
  - routes every real transport call through `core.resilience.circuit_
    breaker` (v2-hardening D/F), keyed by `SOURCE` ("21st.dev"/"figma"/
    "penpot") — after `DEFAULT_FAILURE_THRESHOLD` consecutive failures the
    breaker opens and every further call on that source returns `None`
    immediately, without ever calling `transport`, until the cooldown
    elapses. A caller may inject its own `breaker=` (tests do); production
    code gets one shared per-source breaker from the registry for free.
  - never raises for a network/shape failure: every public method returns
    `None` (or an empty tuple) on ANY of timeout / connection / non-2xx /
    JSON-decode / unexpected-shape, and logs at WARNING once — the same
    "shape mismatch degrades to None, never guesses" contract
    `provider_model_refresh.py`'s discovery functions already established,
    for the identical reason (a vendor response drifting must never surface
    as a crash or a silently wrong result — a drift guard, not a parser
    that trusts the wire).
  - marks every returned record with `Provenance` (source/url/license/
    fetched_at) — required so a caller building a pane prompt can render it
    as clearly-labeled, untrusted external content
    (`09_DESIGN_TOOL_INTEGRATIONS.md`: "External content is untrusted until
    reviewed"), never pasted in as if it were the agent's own prior output
    or a direct instruction.
  - MUST be called off the Qt GUI thread by any UI-facing caller — this
    module has no Qt dependency itself (import-linter's `core-is-bottom-
    layer` contract forbids one), so nothing here enforces that; it is the
    caller's responsibility, the same as every other blocking network/
    subprocess call already documented that way elsewhere in `core/`
    (`core.versioning.probe`, `provider_model_refresh`).

21st.dev has no confirmed, stable public REST search endpoint (checked
2026-08-24: web search + the 21st.dev site itself surface no API reference
page, only "paste this prompt into your AI tool"). Its real, working
integration is the official MCP server — package `@21st-dev/magic`
(unified successor `@21st-dev/cli`), confirmed `mcpServers` config shape
published in that repo's README, auth via an `API_KEY` value 21st.dev
issues at 21st.dev/magic/console — which is what `takkub design
integrations enable reference-21st` actually wires up via
`register_twentyfirst_mcp`/`shared_dev_tools.add_mcp_server`.
`TwentyFirstClient.search`/`get_inspiration` stay here as an opt-in DIRECT
path for an operator-supplied `base_url` (e.g. a self-hosted proxy, or once
21st.dev ships a stable public REST endpoint) — with `base_url=None` it
reports "not configured" rather than guessing a URL, the same
`NO_MODEL_DISCOVERY_GAPS` "document the gap instead of guessing" policy
`provider_model_refresh.py` already uses for opencode/kimi/cursor.

Figma's REST API (`https://api.figma.com`, `X-Figma-Token` header, `GET
/v1/files/:key` + `/v1/files/:key/variables/local` + `/v1/files/:key/
components`) is real, stable, and documented at
developers.figma.com/docs/rest-api (verified 2026-08-24) — `FigmaClient`
implements it directly, no gap.

Penpot's self-hosted API is `POST <base_url>/api/rpc/command/<name>` with
an `Authorization: Token <token>` header (help.penpot.app/technical-guide/
integration, verified 2026-08-24) — the docs there show one concrete worked
example, `get-profile`; `PenpotClient.get_file` follows the same documented
`/api/rpc/command/<name>` convention but its exact param/response shape was
not independently confirmed against a live instance, so — like every other
client here — it degrades to `None` on any shape mismatch rather than
surfacing a guessed result.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_takkub.core.resilience.circuit_breaker import CircuitBreaker, get_breaker

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0

# (method, url, headers, body, timeout) -> raw response bytes. Injected so
# tests never make a real network call (`test_core_capabilities_design_
# clients.py` passes a fake); `_default_transport` is the only thing that
# actually touches a socket.
Transport = Callable[[str, str, dict[str, str], "bytes | None", float], bytes]


def _default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _safe_call(
    transport: Transport,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    *,
    what: str,
    breaker: CircuitBreaker | None = None,
) -> bytes | None:
    """Run *transport*, collapsing every failure mode (timeout, connection,
    non-2xx, unexpected transport-level error) to `None` + one WARNING log
    line. Never raises — the fail-open contract every client method here
    relies on.

    The one choke point every client method routes through, which is why
    *breaker* lives here rather than being threaded through each client
    method individually (v2-hardening D/F, `11_CIRCUIT_BREAKER.md`): when
    the breaker is open, this returns `None` WITHOUT ever calling *transport*
    — no socket, no timeout — instead of paying a full `DEFAULT_TIMEOUT`
    on every call to a service that is already known to be down."""
    if breaker is not None and not breaker.allow_call():
        logger.info("%s: circuit open for %r — skipping call", what, breaker.name)
        return None
    try:
        result = transport(method, url, headers, body, timeout)
    except urllib.error.HTTPError as e:
        logger.warning("%s: HTTP %s", what, e.code)
        if breaker is not None:
            breaker.record_failure()
        return None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        logger.warning("%s: transport failed: %s", what, e)
        if breaker is not None:
            breaker.record_failure()
        return None
    if breaker is not None:
        breaker.record_success()
    return result


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where one retrieved record came from — required on every design-tool
    reference so a prompt-builder can render it as labeled, untrusted
    external content instead of silently pasting it in as the agent's own."""

    source: str
    url: str
    license: str | None
    fetched_at: str


@dataclass(frozen=True, slots=True)
class ComponentReference:
    """One 21st.dev search/inspiration result."""

    id: str
    title: str
    preview_url: str | None
    provenance: Provenance
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FigmaFileSummary:
    key: str
    name: str
    last_modified: str | None
    version: str | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class FigmaVariable:
    id: str
    name: str
    variable_type: str
    collection_id: str | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class FigmaComponent:
    key: str
    name: str
    description: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class PenpotProfile:
    id: str
    fullname: str
    email: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class PenpotFileSummary:
    id: str
    name: str
    project_id: str | None
    modified_at: str | None
    provenance: Provenance


def _parse_21st_results(
    raw: bytes, base_url: str, source: str
) -> tuple[ComponentReference, ...] | None:
    try:
        payload = json.loads(raw)
        results = payload["results"]
        if not isinstance(results, list):
            raise TypeError
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
        logger.warning("%s: unexpected response shape — dropped", source)
        return None

    fetched_at = _utc_now_iso()
    out: list[ComponentReference] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id") or item.get("slug")
        title = item.get("title") or item.get("name")
        if not item_id or not title:
            continue
        out.append(
            ComponentReference(
                id=str(item_id),
                title=str(title),
                preview_url=item.get("preview_url") or item.get("url"),
                tags=tuple(t for t in (item.get("tags") or []) if isinstance(t, str)),
                provenance=Provenance(
                    source=source,
                    url=str(item.get("url") or base_url),
                    license=item.get("license"),
                    fetched_at=fetched_at,
                ),
            )
        )
    return tuple(out)


class TwentyFirstClient:
    """21st.dev component/template reference search — see module docstring
    for why this is opt-in-direct rather than the primary integration path
    (that's the MCP server `register_twentyfirst_mcp` wires up)."""

    SOURCE = "21st.dev"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        transport: Transport = _default_transport,
        timeout: float = DEFAULT_TIMEOUT,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/") if base_url else None
        self._transport = transport
        self._timeout = timeout
        self._breaker = breaker if breaker is not None else get_breaker(self.SOURCE)

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Authorization": f"Bearer {self._api_key}"}

    def search(self, query: str, limit: int = 10) -> tuple[ComponentReference, ...] | None:
        """Search components/templates. Returns `None` when no `base_url`
        is configured (documented gap, never guesses one), on any transport
        failure, or when the response doesn't match the expected shape —
        never raises, never returns a partially-trusted guess."""
        if not self._base_url:
            logger.info("21st.dev search: no base_url configured (no confirmed public endpoint)")
            return None
        params = urllib.parse.urlencode({"q": query, "limit": str(limit)})
        url = f"{self._base_url}/search?{params}"
        raw = _safe_call(
            self._transport,
            "GET",
            url,
            self._headers(),
            None,
            self._timeout,
            what="21st.dev search",
            breaker=self._breaker,
        )
        if raw is None:
            return None
        return _parse_21st_results(raw, self._base_url, self.SOURCE)

    def get_inspiration(self, topic: str, limit: int = 10) -> tuple[ComponentReference, ...] | None:
        """Same contract as `search` — "last resort after Storybook and the
        project's own design system" per `07_DESIGN_DIRECTOR_WORKFLOW.md`."""
        if not self._base_url:
            logger.info("21st.dev get_inspiration: no base_url configured")
            return None
        params = urllib.parse.urlencode({"topic": topic, "limit": str(limit)})
        url = f"{self._base_url}/inspiration?{params}"
        raw = _safe_call(
            self._transport,
            "GET",
            url,
            self._headers(),
            None,
            self._timeout,
            what="21st.dev get_inspiration",
            breaker=self._breaker,
        )
        if raw is None:
            return None
        return _parse_21st_results(raw, self._base_url, self.SOURCE)


class FigmaClient:
    """Figma REST API (`https://api.figma.com`) — token-based, read-only:
    file summary, local variables, components. Auth header per Figma's
    documented convention: `X-Figma-Token: <personal access token>`
    (developers.figma.com/docs/rest-api, verified 2026-08-24)."""

    SOURCE = "figma"
    BASE_URL = "https://api.figma.com/v1"

    def __init__(
        self,
        *,
        token: str,
        transport: Transport = _default_transport,
        timeout: float = DEFAULT_TIMEOUT,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._token = token
        self._transport = transport
        self._timeout = timeout
        self._breaker = breaker if breaker is not None else get_breaker(self.SOURCE)

    def _get(self, path: str) -> dict | None:
        url = f"{self.BASE_URL}{path}"
        headers = {"Accept": "application/json", "X-Figma-Token": self._token}
        raw = _safe_call(
            self._transport,
            "GET",
            url,
            headers,
            None,
            self._timeout,
            what=f"figma {path}",
            breaker=self._breaker,
        )
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("figma %s: non-JSON response", path)
            return None
        return payload if isinstance(payload, dict) else None

    def get_file_summary(self, file_key: str) -> FigmaFileSummary | None:
        payload = self._get(f"/files/{urllib.parse.quote(file_key)}")
        if payload is None:
            return None
        try:
            name = payload["name"]
        except KeyError:
            logger.warning("figma get_file_summary: unexpected shape")
            return None
        return FigmaFileSummary(
            key=file_key,
            name=str(name),
            last_modified=payload.get("lastModified"),
            version=payload.get("version"),
            provenance=Provenance(
                source=self.SOURCE,
                url=f"https://www.figma.com/file/{file_key}",
                license=None,
                fetched_at=_utc_now_iso(),
            ),
        )

    def list_local_variables(self, file_key: str) -> tuple[FigmaVariable, ...] | None:
        payload = self._get(f"/files/{urllib.parse.quote(file_key)}/variables/local")
        if payload is None:
            return None
        try:
            variables = payload["meta"]["variables"]
            if not isinstance(variables, dict):
                raise TypeError
        except (KeyError, TypeError):
            logger.warning("figma list_local_variables: unexpected shape")
            return None

        fetched_at = _utc_now_iso()
        out: list[FigmaVariable] = []
        for var_id, var in variables.items():
            if not isinstance(var, dict) or not var.get("name"):
                continue
            out.append(
                FigmaVariable(
                    id=str(var_id),
                    name=str(var["name"]),
                    variable_type=str(var.get("resolvedType") or "UNKNOWN"),
                    collection_id=var.get("variableCollectionId"),
                    provenance=Provenance(
                        source=self.SOURCE,
                        url=f"https://www.figma.com/file/{file_key}",
                        license=None,
                        fetched_at=fetched_at,
                    ),
                )
            )
        return tuple(out)

    def list_components(self, file_key: str) -> tuple[FigmaComponent, ...] | None:
        payload = self._get(f"/files/{urllib.parse.quote(file_key)}/components")
        if payload is None:
            return None
        try:
            components = payload["meta"]["components"]
            if not isinstance(components, list):
                raise TypeError
        except (KeyError, TypeError):
            logger.warning("figma list_components: unexpected shape")
            return None

        fetched_at = _utc_now_iso()
        out: list[FigmaComponent] = []
        for comp in components:
            if not isinstance(comp, dict) or not comp.get("key") or not comp.get("name"):
                continue
            out.append(
                FigmaComponent(
                    key=str(comp["key"]),
                    name=str(comp["name"]),
                    description=str(comp.get("description") or ""),
                    provenance=Provenance(
                        source=self.SOURCE,
                        url=f"https://www.figma.com/file/{file_key}",
                        license=None,
                        fetched_at=fetched_at,
                    ),
                )
            )
        return tuple(out)


class PenpotClient:
    """Penpot self-hosted REST/RPC API — `POST <base_url>/api/rpc/command/
    <name>` with `Authorization: Token <access-token>` (help.penpot.app/
    technical-guide/integration, verified 2026-08-24 — the only concretely
    documented example there is `get-profile`; `get_file` follows the same
    documented `/api/rpc/command/<name>` convention but was not
    independently confirmed against a live instance, so it degrades to
    `None` on ANY shape mismatch the same way every other client here does
    — an unconfirmed command name/shape fails closed instead of surfacing a
    wrong result)."""

    SOURCE = "penpot"

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: Transport = _default_transport,
        timeout: float = DEFAULT_TIMEOUT,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._timeout = timeout
        self._breaker = breaker if breaker is not None else get_breaker(self.SOURCE)

    def _rpc(self, command: str, params: dict | None = None) -> dict | None:
        url = f"{self._base_url}/api/rpc/command/{command}"
        headers = {"Accept": "application/json", "Authorization": f"Token {self._token}"}
        body: bytes | None = None
        method = "GET"
        if params is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(params).encode("utf-8")
            method = "POST"
        raw = _safe_call(
            self._transport,
            method,
            url,
            headers,
            body,
            self._timeout,
            what=f"penpot {command}",
            breaker=self._breaker,
        )
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("penpot %s: non-JSON response", command)
            return None
        return payload if isinstance(payload, dict) else None

    def get_profile(self) -> PenpotProfile | None:
        """Health/connectivity check — the one endpoint Penpot's own docs
        show a concrete worked example for (`doctor` uses this, not a
        guessed one, when a live probe is explicitly requested)."""
        payload = self._rpc("get-profile")
        if payload is None:
            return None
        try:
            profile_id = payload["id"]
            fullname = payload["fullname"]
            email = payload["email"]
        except KeyError:
            logger.warning("penpot get_profile: unexpected shape")
            return None
        return PenpotProfile(
            id=str(profile_id),
            fullname=str(fullname),
            email=str(email),
            provenance=Provenance(
                source=self.SOURCE, url=self._base_url, license=None, fetched_at=_utc_now_iso()
            ),
        )

    def get_file(self, file_id: str) -> PenpotFileSummary | None:
        payload = self._rpc("get-file", {"id": file_id})
        if payload is None:
            return None
        try:
            fid = payload["id"]
            name = payload["name"]
        except KeyError:
            logger.warning("penpot get_file: unexpected shape")
            return None
        return PenpotFileSummary(
            id=str(fid),
            name=str(name),
            project_id=payload.get("project-id"),
            modified_at=payload.get("modified-at"),
            provenance=Provenance(
                source=self.SOURCE,
                url=f"{self._base_url}/#/workspace?file-id={file_id}",
                license=None,
                fetched_at=_utc_now_iso(),
            ),
        )
