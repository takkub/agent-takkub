"""Skill repository — adapter over :mod:`~...skill_scan` (writable SKILL.md
CRUD) + :mod:`~...skill_policy` (role assignment) + :mod:`~...skill_audit`
(role-doc references).

UI (``pages/skills_page.py``) only ever talks to this module; it never
imports ``skill_scan``/``skill_policy``/``skill_audit`` directly (SPEC.md
"UI ห้าม import JSON path ตรง").

Ownership per SPEC.md §Skills:
  - PROJECT: file under the active project's writable ``.claude/skills`` —
    full CRUD.
  - SHIPPED: bundled with the cockpit itself (``config.REPO_ROOT`` /
    ``config.ASSETS_ROOT``) — read-only, offers "Duplicate to project".
  - EXTERNAL: discovered from a root the cockpit doesn't own — read-only.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ... import skill_audit, skill_scan
from ... import skill_policy as skill_policy_mod
from ..commands import CreateSkillCommand, UpdateSkillCommand
from ..models import Capability, DeletePlan, OperationResult, Ownership, SkillDetail, SkillSummary
from ..transaction import FileTransaction


class SkillNotFoundError(KeyError):
    pass


def _project_roots() -> list[Path]:
    return [Path.cwd()]


def _shipped_roots() -> list[Path]:
    from ... import config

    roots = [config.REPO_ROOT]
    if config.ASSETS_ROOT != config.REPO_ROOT:
        roots.append(config.ASSETS_ROOT)
    return roots


def _all_roots() -> list[Path]:
    return _project_roots() + _shipped_roots()


def _ownership_for(path: Path) -> Ownership:
    if skill_scan.is_writable_skill(path, _project_roots()):
        return Ownership.PROJECT
    if skill_scan.is_writable_skill(path, _shipped_roots()):
        return Ownership.SHIPPED
    return Ownership.EXTERNAL


def _find(entity_id: str) -> skill_scan.SkillInfo | None:
    for skill in skill_scan.scan_skills(_all_roots()):
        if skill.name == entity_id:
            return skill
    return None


def _assigned_roles(skill_name: str) -> tuple[str, ...]:
    """Roles that reference *skill_name* — either explicitly via the Skill
    Matrix (`skill_policy`) or by naming it in their instructions doc
    (mirrors legacy `settings_window._roles_referencing_skill`'s
    word-boundary regex)."""
    pattern = re.compile(rf"\b{re.escape(skill_name)}\b", re.IGNORECASE)
    docs = skill_audit.load_all_role_docs()
    doc_refs = {role for role, doc in docs.items() if pattern.search(doc)}
    policy_refs = {
        role
        for role in skill_policy_mod.skill_matrix_roles()
        if skill_name in skill_policy_mod.effective_skills(role)
    }
    return tuple(sorted(doc_refs | policy_refs))


def _signature(skill: skill_scan.SkillInfo) -> str:
    try:
        content = skill.path.read_bytes()
    except OSError:
        content = b""
    policy_blob = json.dumps(skill_policy_mod.load_policy(), sort_keys=True).encode("utf-8")
    return hashlib.sha256(content + policy_blob).hexdigest()[:16]


def assignable_names() -> list[str]:
    """Every skill name eligible for a Role's Access-tab checklist — same
    roots the Skills page lists from (`_all_roots()`: project + shipped),
    not just the project root. A Role's Access tab that instead imports
    `skill_scan` directly with a narrower root set (``Path.cwd()`` only)
    can't assign a shipped skill this repository shows elsewhere in the
    same UI (LOW-2)."""
    return sorted(s.name for s in skill_scan.scan_skills(_all_roots()))


def list(query: str = "") -> list[SkillSummary]:  # contract name (models.py Repository contract)
    query = (query or "").strip().lower()
    out: list[SkillSummary] = []
    for skill in skill_scan.scan_skills(_all_roots()):
        if query and query not in skill.name.lower() and query not in skill.description.lower():
            continue
        out.append(
            SkillSummary(
                name=skill.name,
                description=skill.description,
                ownership=_ownership_for(skill.path),
            )
        )
    return out


def get(entity_id: str) -> SkillDetail:
    skill = _find(entity_id)
    if skill is None:
        raise SkillNotFoundError(entity_id)
    _fm, body = skill_scan.read_skill(skill.path)
    return SkillDetail(
        name=skill.name,
        description=skill.description,
        instructions=body,
        path=str(skill.path),
        ownership=_ownership_for(skill.path),
        assigned_roles=_assigned_roles(skill.name),
        capabilities=capabilities(skill.name),
    )


def capabilities(entity_id: str | None = None) -> Capability:
    if entity_id is None:
        return Capability()
    skill = _find(entity_id)
    if skill is None:
        return Capability(
            can_create=True, can_update=False, can_delete=False, reason="ไม่พบ skill นี้"
        )
    ownership = _ownership_for(skill.path)
    if ownership is Ownership.PROJECT:
        return Capability(can_create=True, can_update=True, can_delete=True)
    if ownership is Ownership.SHIPPED:
        return Capability(
            can_create=True,
            can_update=False,
            can_delete=False,
            reason="Shipped skill (bundled กับ cockpit) — read-only, ใช้ Duplicate to project เพื่อแก้",
        )
    return Capability(
        can_create=True,
        can_update=False,
        can_delete=False,
        reason="External skill (root ที่ cockpit เขียนไม่ได้) — read-only",
    )


def create(command: CreateSkillCommand) -> OperationResult:
    roots = _project_roots()
    existing = {s.name for s in skill_scan.scan_skills(_all_roots())}
    ok, err = skill_scan.create_skill(
        roots[0], command.name, command.description, command.instructions, existing=existing
    )
    if not ok:
        return OperationResult(ok=False, message=err)
    return OperationResult(ok=True, entity_id=command.name.strip().lower())


def update(entity_id: str, command: UpdateSkillCommand) -> OperationResult:
    skill = _find(entity_id)
    if skill is None:
        return OperationResult(ok=False, message="ไม่พบ skill นี้")
    if _ownership_for(skill.path) is not Ownership.PROJECT:
        return OperationResult(ok=False, message="Skill นี้เป็น read-only แก้ไม่ได้", entity_id=entity_id)
    ok, err = skill_scan.update_skill(skill.path, command.description, command.instructions)
    if not ok:
        return OperationResult(ok=False, message=err, entity_id=entity_id)
    return OperationResult(ok=True, entity_id=entity_id)


def duplicate_to_project(entity_id: str) -> OperationResult:
    """SHIPPED/EXTERNAL skill's "Duplicate to project" action — copies the
    skill's current description+instructions into a new PROJECT skill of the
    same name, which then shadows the read-only source (`scan_skills` dedups
    by name, first root — the project root — wins)."""
    skill = _find(entity_id)
    if skill is None:
        return OperationResult(ok=False, message="ไม่พบ skill นี้")
    roots = _project_roots()
    if not roots:
        return OperationResult(ok=False, message="ไม่มี project ให้ duplicate ไปลง")
    _fm, body = skill_scan.read_skill(skill.path)
    existing = {s.name for s in skill_scan.scan_skills(_all_roots())} - {entity_id}
    ok, err = skill_scan.create_skill(
        roots[0], entity_id, skill.description, body, existing=existing
    )
    if not ok:
        return OperationResult(ok=False, message=err)
    return OperationResult(ok=True, entity_id=entity_id)


def delete_plan(entity_id: str) -> DeletePlan:
    skill = _find(entity_id)
    if skill is None:
        return DeletePlan(
            entity_id=entity_id, deletable=False, version="", blockers=("ไม่พบ skill นี้",)
        )
    if _ownership_for(skill.path) is not Ownership.PROJECT:
        return DeletePlan(
            entity_id=entity_id,
            deletable=False,
            version=_signature(skill),
            blockers=("Skill นี้เป็น read-only — ลบไม่ได้",),
        )

    assigned = _assigned_roles(entity_id)
    effects = [f"ลบไฟล์ {skill.path}"]
    if assigned:
        effects.append(f"ล้าง skill-policy reference จาก role: {', '.join(assigned)}")
    return DeletePlan(
        entity_id=entity_id, deletable=True, version=_signature(skill), effects=tuple(effects)
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

    skill = _find(entity_id)
    if skill is None:
        return OperationResult(ok=False, message="ไม่พบ skill นี้", entity_id=entity_id)

    # `skill.path` is the SKILL.md file — FileTransaction snapshots/restores
    # that one file if the block raises (HIGH-3). `delete_skill` on a nested
    # layout removes the whole skill folder (scripts/references alongside
    # SKILL.md); only the SKILL.md content itself is restorable this way —
    # a documented FileTransaction limitation (files, not directory trees),
    # matching the review's exact ask (restore the skill *file*).
    paths = [skill.path, skill_policy_mod.SKILL_POLICY_FILE]
    try:
        with FileTransaction(paths):
            if not skill_scan.delete_skill(skill.path):
                raise RuntimeError("ลบ skill ไม่สำเร็จ")
            policy = skill_policy_mod.load_policy()
            changed = False
            for role, names in policy.items():
                if entity_id in names:
                    policy[role] = [n for n in names if n != entity_id]
                    changed = True
            if changed and not skill_policy_mod.save_policy(policy):
                raise RuntimeError("ลบ skill-policy reference ไม่สำเร็จ")
    except (RuntimeError, OSError) as e:
        return OperationResult(ok=False, message=str(e), entity_id=entity_id)

    return OperationResult(ok=True, entity_id=entity_id)
