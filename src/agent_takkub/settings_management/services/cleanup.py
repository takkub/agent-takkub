"""Reference discovery + delete-plan computation for custom roles.

A custom role can be referenced from a pipeline template hop (global or any
per-project ``pipelines.json``). SPEC.md's rule: **block delete** when a
template references the role (link to fix references first); everything
else the role touches (policy entries, provider override, instructions
file, registry entry) is a leaf that's safe to cascade-delete and gets
listed as an "effect" in the confirm dialog.
"""

from __future__ import annotations

import hashlib
import json

from ... import custom_roles, pane_tools_policy, provider_config, skill_policy
from ...config import SETTINGS_HOME
from .. import models


def find_role_template_references(role: str) -> tuple[str, ...]:
    """Template names (global + every per-project pipelines.json) whose hops
    reference ``role``. Best-effort: unreadable/corrupt files are skipped —
    mirrors every other policy loader's silent-recovery tolerance."""
    from ... import pipeline_config

    refs: list[str] = []
    paths = [pipeline_config.path(None)]
    projects_dir = SETTINGS_HOME / "projects"
    if projects_dir.is_dir():
        for child in sorted(projects_dir.iterdir()):
            candidate = child / "pipelines.json"
            if candidate.is_file():
                paths.append(candidate)

    for p in paths:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for tmpl in data.get("templates") or ():
            if not isinstance(tmpl, dict):
                continue
            hops = tmpl.get("hops") or ()
            found = any(
                isinstance(hop, list)
                and any(isinstance(cell, dict) and cell.get("role") == role for cell in hop)
                for hop in hops
            )
            if found:
                name = tmpl.get("name") or tmpl.get("id") or "?"
                if name not in refs:
                    refs.append(str(name))
    return tuple(refs)


def _role_signature(name: str) -> str:
    """Opaque token that changes whenever any store this role touches
    changes — used to reject a stale delete confirm (SPEC.md `DeletePlan`)."""
    parts = [
        json.dumps(custom_roles.load_custom_roles().get(name).__dict__, default=str, sort_keys=True)
        if name in custom_roles.load_custom_roles()
        else "",
        json.dumps(pane_tools_policy.load_policy().get(name, {}), sort_keys=True),
        json.dumps(skill_policy.load_policy().get(name, []), sort_keys=True),
        json.dumps(provider_config.load_providers().get(name, ""), sort_keys=True),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def role_delete_plan(name: str) -> models.DeletePlan:
    is_custom = name in custom_roles.load_custom_roles()
    version = _role_signature(name)

    if not is_custom:
        return models.DeletePlan(
            entity_id=name,
            deletable=False,
            version=version,
            blockers=("Built-in role ลบไม่ได้ (definition ล็อกอยู่)",),
        )

    template_refs = find_role_template_references(name)
    effects = [
        "ลบ custom-roles.json registry entry",
        "ลบไฟล์ instructions (.md)",
    ]
    if name in pane_tools_policy.load_policy():
        effects.append("ลบ MCP/Plugin policy entry (revert เป็น defaults)")
    if name in skill_policy.load_policy():
        effects.append("ลบ Skill policy entry")
    if name in provider_config.load_providers():
        effects.append("ลบ provider override")

    if template_refs:
        return models.DeletePlan(
            entity_id=name,
            deletable=False,
            version=version,
            effects=tuple(effects),
            blockers=tuple(f"ใช้อยู่ใน pipeline template: {ref}" for ref in template_refs),
        )

    return models.DeletePlan(
        entity_id=name,
        deletable=True,
        version=version,
        effects=tuple(effects),
    )
