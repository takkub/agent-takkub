"""Optional design tool integrations (#365 phase 7, epic #309 Capability
Hub) — `docs/plans/workspace-1.2.0-design/08_DESIGN_TOOL_INTEGRATIONS.md`:
"All optional through Capability Hub; never hard dependencies."

Storybook detection is REAL (no credential, no network call — a filesystem
scan of the project's own configured roots) and is the preferred source of
truth for what a component actually looks like, ahead of any external
reference (`07_DESIGN_DIRECTOR_WORKFLOW.md`'s anti-AI-cliché checklist: a
design that copies a real, already-reviewed component beats one invented
from a generic reference).

21st.dev/Figma/Penpot are registry-only stubs (task scope: "ไม่ต้อง
implement client จริง — แค่ registry entry + policy gate + doc/stub"):
`OPTIONAL_DESIGN_MCPS` names what the cockpit KNOWS about, not a working
`npx`/API client. Enabling one for real still goes through the two
mechanisms that already exist and are NOT bypassed here:

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
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from agent_takkub import pane_tools_policy

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
