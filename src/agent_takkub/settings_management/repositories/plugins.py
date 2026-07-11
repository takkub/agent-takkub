"""Plugin repository — adapter over :mod:`~...plugin_installer` (the
``claude plugin`` CLI: ``list --json`` / ``install`` / ``uninstall``) +
:mod:`~...pane_tools_dialog` (governable marketplace discovery) +
:mod:`~...pane_tools_policy` (per-role plugin allowlist).

UI (``pages/plugins_page.py``) only ever talks to this module; it never
imports ``plugin_installer``/``pane_tools_dialog``/``pane_tools_policy``/
``lead_context`` directly (SPEC.md "UI ห้าม import JSON path ตรง" — extended
here to "ห้าม import installer/policy ตรง").

**Assignment granularity.** The plugin identity ``claude plugin`` reports is
``<key>@<marketplace>`` (e.g. ``github@claude-plugins-official``), but
``pane_tools_policy``'s per-role allowlist — and ``lead_context.
_default_plugin_dirs``, which actually resolves what a spawned pane gets —
both operate on **marketplace** names, not individual plugin ids
(``pane_tools_policy._validate_name`` rejects ``@`` outright, and a role's
"plugins" override is a set of marketplace directories to load in full). So
"Allowed roles" on this page is computed and edited at the marketplace level
via :func:`~...pane_tools_dialog.discover_marketplaces` /
``pane_tools_policy.effective_plugins`` — see that function's docstring for
the 2026-07-02 incident this avoids repeating. A plugin whose marketplace
isn't in the cockpit's governable set (``config._SAFE_PLUGINS``) is simply
never injected into any pane regardless of role — ``governable=False``.

**Denylist.** ``lead_context._PANE_PLUGIN_DENYLIST`` (``security-guidance``,
``remember``) names hook-heavy plugin *keys* the cockpit never pushes into a
pane even when their marketplace is otherwise allowed for a role — installed
+ usable in the user's own sessions, just never injected. Surfaced here as
``blocked``/``blocked_reason``; the page must show this and disable
assignment for these entries even where ``governable`` is True.
"""

from __future__ import annotations

import hashlib
import json

from ... import lead_context, pane_tools_dialog, pane_tools_policy, plugin_installer
from ..commands import CreatePluginCommand
from ..models import Capability, DeletePlan, OperationResult, Ownership, PluginDetail, PluginSummary

_BLOCKED_REASONS: dict[str, str] = {
    "security-guidance": (
        "SessionStart hook เพิ่ม ~180s ต่อ pane spawn — cockpit กันไม่ให้ inject เข้า "
        "teammate panes (ยังใช้ในเซสชันของ user เองได้ปกติ)"
    ),
    "remember": (
        "SessionStart hook เขียน memory ทุก PostToolUse — cockpit กันไม่ให้ inject เข้า "
        "teammate panes (ยังใช้ในเซสชันของ user เองได้ปกติ)"
    ),
}


class PluginNotFoundError(KeyError):
    pass


def _blocked_reason(key: str) -> str:
    return _BLOCKED_REASONS.get(key, "")


def _split(entity_id: str) -> tuple[str, str]:
    key, _, marketplace = entity_id.partition("@")
    return key, marketplace


def _installed_entries() -> list[dict]:
    """``claude plugin list --json`` entries, or a best-effort fallback built
    from the raw install registry when the CLI is unavailable (offline dev
    box, sandboxed CI) — enough to populate the list, not enough for
    version/scope/install-path (those come back empty)."""
    data = plugin_installer.list_installed()
    if data is not None:
        return data
    return [{"id": name} for name in pane_tools_dialog.discover_marketplace_plugins()]


def governable_marketplaces() -> frozenset[str]:
    """Marketplace names the cockpit can actually inject into a pane — the
    universe of choices the "Install Plugin" form's marketplace field
    offers. Public (unlike this module's other ``_``-prefixed helpers)
    because the page needs it to populate that field."""
    return frozenset(pane_tools_dialog.discover_marketplaces())


def _visible_roles(marketplace: str) -> tuple[str, ...]:
    """Every role whose EFFECTIVE plugin set includes *marketplace* —
    combines the built-in per-role default table with any
    ``pane_tools_policy`` override, mirroring ``mcps.py``'s
    ``_visible_roles`` (identical shape, plugin defaults instead of MCP
    defaults)."""
    out = []
    for role in sorted(pane_tools_policy.known_roles()):
        default = lead_context._ROLE_PLUGIN_POLICY.get(role, lead_context._TEAMMATE_PLUGINS)
        effective = pane_tools_policy.effective_plugins(role, default)
        if effective is None or marketplace in effective:
            out.append(role)
    return tuple(out)


def _signature(entry: dict) -> str:
    blob = json.dumps(entry, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _to_summary(entry: dict) -> PluginSummary:
    entity_id = str(entry.get("id", ""))
    key, marketplace = _split(entity_id)
    return PluginSummary(
        id=entity_id,
        key=key,
        marketplace=marketplace,
        version=str(entry.get("version", "")),
        enabled=bool(entry.get("enabled", False)),
        ownership=Ownership.EXTERNAL,
        blocked=key in lead_context._PANE_PLUGIN_DENYLIST,
        blocked_reason=_blocked_reason(key),
    )


def list(query: str = "") -> list[PluginSummary]:  # contract name (models.py Repository contract)
    query = (query or "").strip().lower()
    out = [_to_summary(e) for e in _installed_entries()]
    if query:
        out = [p for p in out if query in p.id.lower()]
    return sorted(out, key=lambda p: p.id)


def get(entity_id: str) -> PluginDetail:
    entry = next((e for e in _installed_entries() if e.get("id") == entity_id), None)
    if entry is None:
        raise PluginNotFoundError(entity_id)
    key, marketplace = _split(entity_id)
    governable = marketplace in governable_marketplaces()
    blocked = key in lead_context._PANE_PLUGIN_DENYLIST
    return PluginDetail(
        id=entity_id,
        key=key,
        marketplace=marketplace,
        version=str(entry.get("version", "")),
        enabled=bool(entry.get("enabled", False)),
        scope=str(entry.get("scope", "")),
        install_path=str(entry.get("installPath", "")),
        installed_at=str(entry.get("installedAt", "")),
        ownership=Ownership.EXTERNAL,
        blocked=blocked,
        blocked_reason=_blocked_reason(key),
        governable=governable,
        # Denylisted -> assignment disabled regardless of marketplace
        # governance (SPEC.md §Plugins "assignment disabled").
        allowed_roles=_visible_roles(marketplace) if governable and not blocked else (),
        capabilities=capabilities(entity_id),
    )


def capabilities(entity_id: str | None = None) -> Capability:
    if entity_id is None:
        return Capability(
            can_create=True,
            can_update=False,
            can_delete=False,
            reason="แก้ definition ไม่ได้ — identity/version มาจาก marketplace ภายนอก (assignment แก้จากหน้า Role)",
        )
    key, _marketplace = _split(entity_id)
    if key in lead_context._PANE_PLUGIN_DENYLIST:
        return Capability(
            can_create=True,
            can_update=False,
            can_delete=True,
            reason=f"BLOCKED BY COCKPIT — {_blocked_reason(key)}",
        )
    return Capability(
        can_create=True,
        can_update=False,
        can_delete=True,
        reason="แก้ definition ไม่ได้ — identity/version มาจาก marketplace ภายนอก (assignment แก้จากหน้า Role)",
    )


def create(command: CreatePluginCommand) -> OperationResult:
    key = (command.key or "").strip().lower()
    if not key:
        return OperationResult(ok=False, message="Plugin key ห้ามว่าง")
    marketplace = (command.marketplace or "").strip().lower()
    plugin_id = f"{key}@{marketplace}" if marketplace else key
    if marketplace and any(e.get("id") == plugin_id for e in _installed_entries()):
        return OperationResult(
            ok=False, message=f"Plugin '{plugin_id}' ติดตั้งอยู่แล้ว", entity_id=plugin_id
        )

    ok, msg = plugin_installer.install_by_id(plugin_id)
    if not ok:
        return OperationResult(ok=False, message=msg)

    resolved = plugin_id if marketplace else _resolve_installed_id(key)
    return OperationResult(ok=True, entity_id=resolved or plugin_id)


def _resolve_installed_id(key: str) -> str | None:
    """After an install with no explicit marketplace, find the id ``claude``
    actually installed under (re-reads the live registry — the CLI resolves
    the marketplace itself, this module never guesses it)."""
    for entry in _installed_entries():
        entry_key, _marketplace = _split(str(entry.get("id", "")))
        if entry_key == key:
            return str(entry["id"])
    return None


def delete_plan(entity_id: str) -> DeletePlan:
    entry = next((e for e in _installed_entries() if e.get("id") == entity_id), None)
    if entry is None:
        return DeletePlan(
            entity_id=entity_id, deletable=False, version="", blockers=("ไม่พบ plugin นี้",)
        )
    key, _marketplace = _split(entity_id)
    effects = [
        f"เรียก `claude plugin uninstall {entity_id}`",
        "ลบไฟล์ plugin ออกจาก plugins/cache ของเครื่องนี้",
    ]
    if key in lead_context._PANE_PLUGIN_DENYLIST:
        effects.append("plugin นี้ blocked จาก pane อยู่แล้ว — ไม่กระทบ teammate panes")
    return DeletePlan(
        entity_id=entity_id, deletable=True, version=_signature(entry), effects=tuple(effects)
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

    ok, msg = plugin_installer.uninstall_plugin(entity_id)
    if not ok:
        return OperationResult(ok=False, message=msg, entity_id=entity_id)
    return OperationResult(ok=True, entity_id=entity_id)
