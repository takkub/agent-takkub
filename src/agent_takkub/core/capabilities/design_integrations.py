"""Optional design tool integrations (#365 phase 7 real Storybook detection;
#373 real 21st.dev/Figma/Penpot clients, GAP-014/015/016, epic #309
Capability Hub) — `docs/plans/workspace-1.2.0-design/
08_DESIGN_TOOL_INTEGRATIONS.md` and `docs/plans/workspace-master-upgrade-
2026-08-24/09_DESIGN_TOOL_INTEGRATIONS.md`: "All optional through
Capability Hub; never hard dependencies."

Storybook detection is REAL (no credential, no network call — a filesystem
scan of the project's own configured roots) and is the preferred source of
truth for what a component actually looks like, ahead of any external
reference (`07_DESIGN_DIRECTOR_WORKFLOW.md`'s anti-AI-cliché checklist: a
design that copies a real, already-reviewed component beats one invented
from a generic reference).

21st.dev/Figma/Penpot are real clients as of #373 (`design_clients.py` +
`build_client` below) — `OPTIONAL_DESIGN_MCPS` still names what the cockpit
KNOWS about (id/label/doc hint/secret ref hint), independent of whether a
live client can be built for it right now. Enabling one for real still goes
through the two mechanisms that already existed pre-#373 and are NOT
bypassed here:

  - `pane_tools_policy` (Layer 1 of `PermissionEngine`) — a role gets one of
    these names only via an explicit `takkub mcp allow --role <role> <name>`
    override; `effective_mcps()` returns the built-in default (which never
    includes these names, per `09_KNOWLEDGE_BOUNDARIES.md`'s "Capability Hub
    owns skills/MCP/plugins/permissions" — nothing else may grant one
    silently) for every role that hasn't been given one — default OFF.
  - `shared_dev_tools.add_mcp_server` — the actual server config (command/
    credential) is supplied by the operator, not guessed here; a
    credential-bearing config is refused unless `--force`, and no value is
    ever hardcoded into this module. A real client's env should resolve its
    token through `core.contracts.secret_manager.SecretManager` (by
    `secret_ref`, never a literal), matching every other credential path in
    the cockpit — this module only carries that expectation as a hint
    string, not a resolved secret.

#373 makes that last sentence literal: `build_client` below is the ONLY
place a real `design_clients.TwentyFirstClient`/`FigmaClient`/`PenpotClient`
gets constructed. It re-checks BOTH gates every call — `PermissionEngine.
mcp_allowed` (default deny, per-role opt-in) and a stored
`core.secrets.manager.SecretManager` credential — so a role that was never
granted an integration, or one that was granted it but never configured a
credential, can never obtain a live client through this module. Nothing
else in the codebase constructs those classes directly; `design_clients.py`
itself has no idea what a "role" or a permission is.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

from agent_takkub import pane_tools_policy

from .audit import log_capability_event
from .design_clients import FigmaClient, PenpotClient, TwentyFirstClient
from .permission_engine import PermissionEngine

_log = logging.getLogger(__name__)

_DEFAULT_STORYBOOK_PORT = 6006
_PORT_FLAG_RE = re.compile(r"(?:-p|--port)[= ](\d{2,5})")


@dataclass(frozen=True, slots=True)
class StorybookStatus:
    """Result of scanning one project's configured roots for Storybook.
    `preview_url` is the URL a Designer pane opens via `takkub preview
    open-url` once Storybook is actually running — this module never starts
    it or probes whether it is live (that would be a network call at
    detection time, which the task explicitly rules out)."""

    detected: bool
    root: str | None = None
    script_name: str | None = None
    port: int = _DEFAULT_STORYBOOK_PORT
    preview_url: str | None = None


def _read_package_scripts(root: Path) -> dict[str, str]:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {k: v for k, v in scripts.items() if isinstance(k, str) and isinstance(v, str)}


def _find_storybook_script(scripts: dict[str, str]) -> tuple[str, str] | None:
    """First `package.json` script whose name or command names storybook —
    `("storybook", "storybook dev -p 6007")`-shaped. `None` if none match."""
    for name, cmd in scripts.items():
        if "storybook" in name.lower() or "storybook" in cmd.lower():
            return name, cmd
    return None


def detect_storybook(roots: list[Path]) -> StorybookStatus:
    """First configured root (in order) that looks like it has Storybook set
    up: a `.storybook/` config dir, or a `package.json` script naming it.
    Detection only — never runs anything, never touches the network."""
    for root in roots:
        root = Path(root)
        config_dir = root / ".storybook"
        script = _find_storybook_script(_read_package_scripts(root))
        if not config_dir.is_dir() and script is None:
            continue
        script_name = script[0] if script else None
        port_match = _PORT_FLAG_RE.search(script[1]) if script else None
        port = int(port_match.group(1)) if port_match else _DEFAULT_STORYBOOK_PORT
        return StorybookStatus(
            detected=True,
            root=str(root),
            script_name=script_name,
            port=port,
            preview_url=f"http://localhost:{port}",
        )
    return StorybookStatus(detected=False)


@dataclass(frozen=True, slots=True)
class OptionalDesignMcp:
    """One known-but-not-wired-up design MCP (`08_DESIGN_TOOL_INTEGRATIONS.md`).
    `enabled_for_role` reflects `pane_tools_policy` only — see module
    docstring for why nothing here can flip it on by itself."""

    id: str
    label: str
    doc_hint: str
    secret_ref_hint: str
    enabled_for_role: bool = False


# Ordered per `07_DESIGN_DIRECTOR_WORKFLOW.md`'s "retrieve references" step,
# LAST resort after Storybook (real components) and the project's own design
# system/tokens — see designer.md's priority-order section, which restates
# this same order for the role prompt itself.
OPTIONAL_DESIGN_MCPS: tuple[OptionalDesignMcp, ...] = (
    OptionalDesignMcp(
        id="reference-21st",
        label="21st.dev component reference",
        doc_hint="inspiration/component reference only — never a source of truth over a real "
        "Storybook story or the project's own design system",
        secret_ref_hint="secret://reference-21st/<account-id>",
    ),
    OptionalDesignMcp(
        id="figma",
        label="Figma design/tokens/components",
        doc_hint="approved design files only — pull tokens/components, not raw pixel copies",
        secret_ref_hint="secret://figma/<account-id>",
    ),
    OptionalDesignMcp(
        id="penpot",
        label="Penpot (self-hosted, open) design source",
        doc_hint="open/self-hosted alternative to Figma — same approved-source rule applies",
        secret_ref_hint="secret://penpot/<account-id>",
    ),
)


def optional_design_mcp_status(role: str) -> tuple[OptionalDesignMcp, ...]:
    """`OPTIONAL_DESIGN_MCPS` with `enabled_for_role` filled in from
    `pane_tools_policy.effective_mcps(role)` — the SAME allowlist
    `PermissionEngine.mcp_allowed` and the real `--mcp-config`/`-c
    mcp_servers.*` spawn-time injection already read, so this can never
    disagree with what a pane actually gets (no separate opt-in flag,
    no bypass)."""
    allowed = pane_tools_policy.effective_mcps(role) or frozenset()
    return tuple(replace(m, enabled_for_role=m.id in allowed) for m in OPTIONAL_DESIGN_MCPS)


@dataclass(frozen=True, slots=True)
class DesignIntegrationsSnapshot:
    storybook: StorybookStatus
    optional_mcps: tuple[OptionalDesignMcp, ...]


def resolve_design_integrations(role: str, roots: list[Path]) -> DesignIntegrationsSnapshot:
    """Everything `role` gets for #365 phase 7, in one call — mirrors
    `CapabilityRegistry.snapshot`'s "one call instead of N policy imports"
    shape for this one slice."""
    return DesignIntegrationsSnapshot(
        storybook=detect_storybook(roots),
        optional_mcps=optional_design_mcp_status(role),
    )


# ---------------------------------------------------------------------------
# #373 — real clients (GAP-014/015/016)
# ---------------------------------------------------------------------------


class IntegrationError(RuntimeError):
    """Base for `build_client` resolution failures — never a raw network
    exception (nothing in this section makes a network call itself; that
    only happens once the caller invokes a method on the returned
    client)."""


class IntegrationDeniedError(IntegrationError):
    """PermissionEngine layer 1 (`pane_tools_policy`) has not granted
    *role* this integration id — default deny, the same gate every other
    MCP grant in the cockpit goes through."""


class IntegrationNotConfiguredError(IntegrationError):
    """Granted by policy but no credential is stored yet (or, for penpot,
    the stored credential is missing its required `base_url`) —
    `takkub design integrations enable --role <role> --token <token>
    [--base-url <url>] <id>` sets it via `SecretManager`."""


def _parse_json_secret(raw: str) -> dict | None:
    """Best-effort JSON decode of a stored secret blob (`reference-21st`/
    `penpot` store `{token/api_key, base_url}` as JSON text — see
    `core.secrets.manager`'s module docstring). `None` on anything that
    isn't a JSON object, never raises — a malformed stored blob must read
    as "not configured", not crash `build_client`."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def integration_config_status(
    integration_id: str, *, secret_manager: object | None = None
) -> tuple[bool, str]:
    """Whether *integration_id* has a stored credential (`SecretManager`
    status FOUND) — configuration only, never a live network probe (same
    "no network call" contract `detect_storybook` already holds for this
    whole module; `doctor`/`takkub design integrations status` both call
    this, never a client method, to stay network-free)."""
    from agent_takkub.core.secrets.backends import BackendStatus
    from agent_takkub.core.secrets.manager import SecretManager

    manager = secret_manager or SecretManager()
    try:
        status = manager.status(f"secret://{integration_id}/default")
    except Exception as e:  # pragma: no cover — SecretManager.status never raises today
        _log.debug("integration_config_status(%r): %s", integration_id, e)
        return False, "status check failed"
    if status == BackendStatus.FOUND:
        return True, "credential configured"
    if status == BackendStatus.MISSING:
        return False, "no credential stored"
    return False, "no secret backend registered for this integration"


def build_client(
    integration_id: str,
    role: str,
    *,
    secret_manager: object | None = None,
    permission_engine: PermissionEngine | None = None,
) -> TwentyFirstClient | FigmaClient | PenpotClient:
    """Construct the real client for *integration_id*, enforcing BOTH gates
    every time — see module docstring. Raises `ValueError` (unknown id),
    `IntegrationDeniedError`, or `IntegrationNotConfiguredError`; never a
    raw network exception.

    This is the only place any of `design_clients`' three classes gets
    constructed — callers that need a live client MUST come through here,
    never import `design_clients` and build one directly, or the
    permission/credential checks are skipped entirely."""
    from agent_takkub.core.secrets.backends import SecretUnavailableError
    from agent_takkub.core.secrets.manager import SecretManager

    ids = {m.id for m in OPTIONAL_DESIGN_MCPS}
    if integration_id not in ids:
        raise ValueError(f"unknown design integration {integration_id!r}")

    engine = permission_engine or PermissionEngine()
    allowed = engine.mcp_allowed(role)
    if allowed is None or integration_id not in allowed:
        log_capability_event("capability.design_integration_denied", who=role, tool=integration_id)
        raise IntegrationDeniedError(
            f"{integration_id!r} is not enabled for role {role!r} — "
            f"'takkub design integrations enable --role {role} --token <token> {integration_id}'"
        )

    manager = secret_manager or SecretManager()
    try:
        raw = manager.get_secret(f"secret://{integration_id}/default")
    except SecretUnavailableError as e:
        raise IntegrationNotConfiguredError(
            f"{integration_id!r} has no stored credential — "
            f"'takkub design integrations enable --role {role} --token <token> {integration_id}'"
        ) from e

    if integration_id == "reference-21st":
        cfg = _parse_json_secret(raw) or {}
        api_key = cfg.get("api_key") or raw.strip()
        return TwentyFirstClient(api_key=api_key, base_url=cfg.get("base_url"))

    if integration_id == "figma":
        return FigmaClient(token=raw.strip())

    # integration_id == "penpot" (the only remaining member of `ids`)
    cfg = _parse_json_secret(raw) or {}
    base_url, token = cfg.get("base_url"), cfg.get("token")
    if not base_url or not token:
        raise IntegrationNotConfiguredError(
            "penpot requires both a token and a base_url — "
            f"'takkub design integrations enable --role {role} --token <token> "
            f"--base-url <url> penpot'"
        )
    return PenpotClient(base_url=base_url, token=token)


def register_twentyfirst_mcp(force: bool = True) -> bool:
    """Register 21st.dev's official MCP server (package `@21st-dev/magic`,
    unified successor `@21st-dev/cli`) in the master `shared-mcp.json` via
    `shared_dev_tools.add_mcp_server` — the confirmed `mcpServers` config
    shape published in that project's README (verified 2026-08-24), with
    the literal API key replaced by a `${TWENTY_FIRST_API_KEY}` reference
    (never a literal token written to a shared file — the SAME `${VAR}`
    placeholder convention `shared_dev_tools._has_secrets` already
    recognizes and requires `force=True` to write past, exactly like every
    other credential-bearing MCP entry in the cockpit).

    Still gated: a role only gets to actually launch this server via the
    normal `pane_tools_policy` MCP allowlist (`takkub design integrations
    enable --role <role> reference-21st`) — this function only makes the
    server config exist, it does not grant anyone access to it.

    Returns `False` (never raises) on any validation/I/O failure — same
    contract as `add_mcp_server` itself."""
    from agent_takkub import shared_dev_tools as sdt

    cfg = {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@21st-dev/magic@latest", "API_KEY=${TWENTY_FIRST_API_KEY}"],
    }
    return sdt.add_mcp_server("reference-21st", cfg, force=force)
