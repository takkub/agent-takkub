"""MCP Server repository — adapter over :mod:`~...shared_dev_tools` (master
``shared-mcp.json`` CRUD) + :mod:`~...pane_tools_policy` (per-role
allowlist).

UI (``pages/mcp_page.py``) only ever talks to this module; it never imports
``shared_dev_tools``/``pane_tools_policy`` directly (SPEC.md "UI ห้าม import
JSON path ตรง").

Ownership per SPEC.md §MCP Servers:
  - MANAGED: a browser MCP the cockpit force-injects (``playwright``,
    ``chrome-devtools``) — definition read-only, assignment still editable
    from a Role's Access tab.
  - USER: any other server — full CRUD.

Credential handling: ``get()`` always returns a secret-masked config
(`shared_dev_tools.mask_secrets`) — the UI must never render a raw
credential value.
"""

from __future__ import annotations

import hashlib
import json

from ... import pane_tools_policy, shared_dev_tools
from ..commands import CreateMcpCommand, McpConfigDraft, UpdateMcpCommand
from ..models import Capability, DeletePlan, McpDetail, McpSummary, OperationResult, Ownership
from ..transaction import FileTransaction


class McpNotFoundError(KeyError):
    pass


def _ownership(name: str) -> Ownership:
    return Ownership.MANAGED if name in shared_dev_tools._BROWSER_MCP_NAMES else Ownership.USER


def _draft_to_cfg(draft: McpConfigDraft, existing: dict | None = None) -> dict:
    """Merge the form-editable fields onto *existing* (preserving unknown
    keys like ``headers``) — or build a fresh config when there's nothing to
    merge onto (create).

    ``existing`` is always the RAW (unmasked) stored config — `get()` masks
    for display, but the draft's env/args values came from that masked
    display, so any secret the user never touched still equals the masked
    placeholder. `restore_masked_secrets` swaps those back to their real
    value, field-by-field, so an unrelated edit (e.g. only ``command``)
    can't clobber a stored credential with ``••••••••`` (HIGH-1).
    """
    cfg = dict(existing or {})
    cfg["type"] = draft.type
    cfg["command"] = draft.command
    cfg["args"] = [*draft.args]
    cfg["env"] = dict(draft.env)
    if existing:
        cfg = shared_dev_tools.restore_masked_secrets(cfg, existing)
    return cfg


def _explicit_allowlist_roles(name: str) -> tuple[str, ...]:
    policy = pane_tools_policy.load_policy()
    return tuple(
        sorted(role for role, entry in policy.items() if name in (entry.get("mcps") or []))
    )


def _visible_roles(name: str) -> tuple[str, ...]:
    """Every role whose EFFECTIVE mcp set includes *name* — combines the
    built-in per-role default table with any `pane_tools_policy` override,
    the same resolution `pane_tools_policy.effective_mcps` performs at
    spawn time."""
    defaults = shared_dev_tools.default_role_mcp_policy()
    out = []
    for role in sorted(pane_tools_policy.known_roles()):
        effective = pane_tools_policy.effective_mcps(role, default=defaults.get(role))
        if effective is None or name in effective:
            out.append(role)
    return tuple(out)


def _signature(name: str) -> str:
    cfg = shared_dev_tools.list_master_mcps().get(name, {})
    policy_blob = json.dumps(pane_tools_policy.load_policy(), sort_keys=True)
    cfg_blob = json.dumps(cfg, sort_keys=True)
    return hashlib.sha256((cfg_blob + policy_blob).encode("utf-8")).hexdigest()[:16]


def list(query: str = "") -> list[McpSummary]:  # contract name (models.py Repository contract)
    query = (query or "").strip().lower()
    out: list[McpSummary] = []
    for name, cfg in sorted(shared_dev_tools.list_master_mcps().items()):
        if query and query not in name.lower():
            continue
        out.append(
            McpSummary(name=name, command=str(cfg.get("command", "")), ownership=_ownership(name))
        )
    return out


def get(entity_id: str) -> McpDetail:
    cfg = shared_dev_tools.list_master_mcps().get(entity_id)
    if cfg is None:
        raise McpNotFoundError(entity_id)
    return McpDetail(
        name=entity_id,
        config=shared_dev_tools.mask_secrets(cfg),
        ownership=_ownership(entity_id),
        has_secrets=shared_dev_tools._has_secrets(cfg),
        allowed_roles=_visible_roles(entity_id),
        capabilities=capabilities(entity_id),
    )


def capabilities(entity_id: str | None = None) -> Capability:
    if entity_id is None:
        return Capability()
    if entity_id not in shared_dev_tools.list_master_mcps():
        return Capability(
            can_create=True, can_update=False, can_delete=False, reason="ไม่พบ MCP server นี้"
        )
    if _ownership(entity_id) is Ownership.MANAGED:
        return Capability(
            can_create=True,
            can_update=False,
            can_delete=False,
            reason="Managed browser MCP ของ cockpit — definition แก้ไม่ได้ (assignment ยังแก้ได้จากหน้า Role)",
        )
    return Capability(can_create=True, can_update=True, can_delete=True)


def _regen_variants_or_raise() -> None:
    """Checked MCP role-variant regen (HIGH-4) — call inside a
    `FileTransaction` that also snapshots `shared_dev_tools.role_variant_paths()`
    so a partial variant write rolls back together with master/policy
    instead of leaving them disagreeing."""
    ok, failed = shared_dev_tools.regen_role_variants_checked()
    if not ok:
        raise RuntimeError(f"regenerate MCP role variant ไม่สำเร็จ: {', '.join(failed)}")


def create(command: CreateMcpCommand) -> OperationResult:
    name = (command.name or "").strip().lower()
    if not name:
        return OperationResult(ok=False, message="ชื่อห้ามว่าง")
    if name in shared_dev_tools.list_master_mcps():
        return OperationResult(ok=False, message=f"MCP server '{name}' มีอยู่แล้ว")
    cfg = _draft_to_cfg(command.config)
    paths = [shared_dev_tools.SHARED_MCP_FILE, *shared_dev_tools.role_variant_paths()]
    try:
        with FileTransaction(paths):
            if not shared_dev_tools.add_mcp_server(name, cfg, force=True):
                raise RuntimeError("สร้าง MCP server ไม่สำเร็จ — ชื่อไม่ถูกต้อง หรือชนกับ managed MCP")
            _regen_variants_or_raise()
    except (RuntimeError, OSError) as e:
        return OperationResult(ok=False, message=str(e))
    return OperationResult(ok=True, entity_id=name)


def update(entity_id: str, command: UpdateMcpCommand) -> OperationResult:
    existing = shared_dev_tools.list_master_mcps().get(entity_id)
    if existing is None:
        return OperationResult(ok=False, message="ไม่พบ MCP server นี้", entity_id=entity_id)
    if _ownership(entity_id) is Ownership.MANAGED:
        return OperationResult(
            ok=False, message="Managed MCP server แก้ definition ไม่ได้", entity_id=entity_id
        )
    cfg = _draft_to_cfg(command.config, existing)
    paths = [shared_dev_tools.SHARED_MCP_FILE, *shared_dev_tools.role_variant_paths()]
    try:
        with FileTransaction(paths):
            if not shared_dev_tools.add_mcp_server(entity_id, cfg, force=True):
                raise RuntimeError("แก้ MCP server ไม่สำเร็จ")
            _regen_variants_or_raise()
    except (RuntimeError, OSError) as e:
        return OperationResult(ok=False, message=str(e), entity_id=entity_id)
    return OperationResult(ok=True, entity_id=entity_id)


def delete_plan(entity_id: str) -> DeletePlan:
    if entity_id not in shared_dev_tools.list_master_mcps():
        return DeletePlan(
            entity_id=entity_id, deletable=False, version="", blockers=("ไม่พบ MCP server นี้",)
        )
    if _ownership(entity_id) is Ownership.MANAGED:
        return DeletePlan(
            entity_id=entity_id,
            deletable=False,
            version=_signature(entity_id),
            blockers=("Managed browser MCP ลบไม่ได้",),
        )
    affected = _explicit_allowlist_roles(entity_id)
    effects = ["ลบ master entry จาก shared-mcp.json", "regenerate role variant files"]
    if affected:
        effects.append(f"ลบ policy reference จาก role: {', '.join(affected)}")
    return DeletePlan(
        entity_id=entity_id, deletable=True, version=_signature(entity_id), effects=tuple(effects)
    )


def delete(entity_id: str, confirmed_plan_version: str) -> OperationResult:
    plan = delete_plan(entity_id)
    if not plan.deletable:
        return OperationResult(
            ok=False,
            message="; ".join(plan.blockers) if plan.blockers else "ลบไม่ได้",
            entity_id=entity_id,
        )
    if plan.version != confirmed_plan_version:
        return OperationResult(
            ok=False,
            message="ข้อมูลเปลี่ยนไปตั้งแต่เปิด confirm — โหลดใหม่แล้วลองอีกครั้ง",
            entity_id=entity_id,
        )

    paths = [
        shared_dev_tools.SHARED_MCP_FILE,
        pane_tools_policy.PANE_TOOLS_POLICY_FILE,
        *shared_dev_tools.role_variant_paths(),
    ]
    try:
        with FileTransaction(paths):
            if not shared_dev_tools.remove_mcp_server(entity_id):
                raise RuntimeError("ลบ MCP server ไม่สำเร็จ")
            policy = pane_tools_policy.load_policy()
            changed = False
            for entry in policy.values():
                mcps = entry.get("mcps") or []
                if entity_id in mcps:
                    entry["mcps"] = [n for n in mcps if n != entity_id]
                    changed = True
            if changed and not pane_tools_policy.save_policy(policy):
                raise RuntimeError("ลบ policy reference ไม่สำเร็จ")
            _regen_variants_or_raise()
    except (RuntimeError, OSError) as e:
        return OperationResult(ok=False, message=str(e), entity_id=entity_id)

    return OperationResult(ok=True, entity_id=entity_id)
