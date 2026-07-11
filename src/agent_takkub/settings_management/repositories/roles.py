"""Role repository — adapter over ``roles`` + ``custom_roles`` +
:mod:`~..services.relationships` (provider/skills/MCP/plugins).

UI (``pages/roles_page.py``) only ever talks to :class:`RoleRepository`; it
never imports ``custom_roles``/``provider_config``/``pane_tools_policy``/
``skill_policy`` directly (SPEC.md "UI ห้าม import JSON path ตรง").
"""

from __future__ import annotations

from ... import custom_roles
from ... import roles as roles_mod
from ..commands import CreateRoleCommand, UpdateRoleCommand
from ..models import Capability, DeletePlan, OperationResult, Ownership, RoleDetail, RoleSummary
from ..services import cleanup, relationships, validation


class RoleNotFoundError(KeyError):
    pass


def _ownership(name: str) -> Ownership:
    return (
        Ownership.BUILT_IN if roles_mod.by_name(name) in roles_mod.ALL_DEFAULT else Ownership.CUSTOM
    )


def list(query: str = "") -> list[RoleSummary]:  # contract name (models.py Repository contract)
    query = (query or "").strip().lower()
    out: list[RoleSummary] = []
    for name in roles_mod.all_role_names():
        role = roles_mod.by_name(name)
        if role is None:
            continue
        if query and query not in name.lower() and query not in role.label.lower():
            continue
        out.append(
            RoleSummary(
                name=role.name,
                label=role.label,
                color=role.color,
                ownership=_ownership(role.name),
                column=role.column,
                row=role.row,
            )
        )
    return out


def get(entity_id: str) -> RoleDetail:
    role = roles_mod.by_name(entity_id)
    if role is None:
        raise RoleNotFoundError(entity_id)

    ownership = _ownership(role.name)
    instructions = ""
    instructions_path = ""
    if ownership is Ownership.CUSTOM:
        path = custom_roles.role_file_path(role.name)
        instructions_path = str(path)
        if path.is_file():
            try:
                instructions = path.read_text(encoding="utf-8")
            except OSError:
                instructions = ""

    return RoleDetail(
        name=role.name,
        label=role.label,
        color=role.color,
        ownership=ownership,
        column=role.column,
        row=role.row,
        instructions=instructions,
        instructions_path=instructions_path,
        access=relationships.get_role_access(role.name),
        capabilities=capabilities(role.name),
    )


def capabilities(entity_id: str | None = None) -> Capability:
    if entity_id is None:
        return Capability()
    role = roles_mod.by_name(entity_id)
    if role is None:
        return Capability(can_create=True, can_update=False, can_delete=False, reason="ไม่พบ role นี้")
    if _ownership(role.name) is Ownership.BUILT_IN:
        return Capability(
            can_create=True,
            can_update=True,  # Access tab (provider/skills/MCP/plugins) stays editable
            can_delete=False,
            reason="Built-in role — แก้ได้เฉพาะ Access (provider/skills/MCP/plugins); ลบไม่ได้",
        )
    return Capability(can_create=True, can_update=True, can_delete=True)


def create(command: CreateRoleCommand) -> OperationResult:
    ok, err = validation.validate_role_name(command.name)
    if not ok:
        return OperationResult(ok=False, message=err)
    if not validation.validate_color(command.general.color):
        return OperationResult(ok=False, message="สีต้องเป็นรูปแบบ #rrggbb")

    ok, err = custom_roles.create_role(
        command.name,
        command.general.label,
        command.general.color,
        command.general.column,
        command.general.row,
        instructions=command.general.instructions,
    )
    if not ok:
        return OperationResult(ok=False, message=err)

    role = roles_mod.Role(
        name=command.name,
        label=(command.general.label or "").strip() or command.name.capitalize(),
        color=command.general.color,
        column=command.general.column,
        row=command.general.row,
    )
    roles_mod.register_role(role)

    access_result = relationships.write_access(command.name, command.access)
    if not access_result.ok:
        # Role file/registry already committed; surface the access failure
        # but don't roll back role creation itself — the role exists and is
        # editable, just without the requested access yet (matches
        # create_role's own "commit what succeeded" philosophy).
        return OperationResult(
            ok=True,
            message=f"สร้าง role สำเร็จ แต่ตั้งค่า access ไม่สำเร็จ: {access_result.message}",
            entity_id=command.name,
        )
    return OperationResult(ok=True, entity_id=command.name)


def update(entity_id: str, command: UpdateRoleCommand) -> OperationResult:
    role = roles_mod.by_name(entity_id)
    if role is None:
        return OperationResult(ok=False, message="ไม่พบ role นี้")

    if _ownership(entity_id) is Ownership.CUSTOM:
        if not validation.validate_color(command.general.color):
            return OperationResult(ok=False, message="สีต้องเป็นรูปแบบ #rrggbb")

        current = custom_roles.load_custom_roles()
        current[entity_id] = roles_mod.Role(
            name=entity_id,
            label=(command.general.label or "").strip() or entity_id.capitalize(),
            color=command.general.color,
            column=command.general.column,
            row=command.general.row,
        )
        if not custom_roles.save_custom_roles(current):
            return OperationResult(ok=False, message="เขียน custom-roles.json ไม่สำเร็จ")
        roles_mod.register_role(current[entity_id])

        path = custom_roles.role_file_path(entity_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(command.general.instructions, encoding="utf-8")
        except OSError as e:
            return OperationResult(ok=False, message=f"เขียน role file ไม่สำเร็จ: {e}")

    result = relationships.write_access(entity_id, command.access)
    if not result.ok:
        return result
    return OperationResult(ok=True, entity_id=entity_id)


def delete_plan(entity_id: str) -> DeletePlan:
    return cleanup.role_delete_plan(entity_id)


def delete(entity_id: str, confirmed_plan_version: str) -> OperationResult:
    plan = cleanup.role_delete_plan(entity_id)
    if not plan.deletable:
        return OperationResult(
            ok=False,
            message="ลบไม่ได้: " + "; ".join(plan.blockers) if plan.blockers else "ลบไม่ได้",
            entity_id=entity_id,
        )
    if plan.version != confirmed_plan_version:
        return OperationResult(
            ok=False,
            message="ข้อมูลเปลี่ยนไปตั้งแต่เปิด confirm — โหลดใหม่แล้วลองอีกครั้ง",
            entity_id=entity_id,
        )

    if not custom_roles.delete_role(entity_id):
        return OperationResult(ok=False, message="ลบ role ไม่สำเร็จ", entity_id=entity_id)
    roles_mod.unregister_role(entity_id)

    from ...pane_tools_policy import reset_role as _reset_tools_policy
    from ...provider_config import save_role_overrides as _save_provider_overrides
    from ...skill_policy import load_policy as _load_skill_policy
    from ...skill_policy import save_policy as _save_skill_policy

    _reset_tools_policy(entity_id)
    _save_provider_overrides({}, scope=[entity_id])
    skills = _load_skill_policy()
    if entity_id in skills:
        del skills[entity_id]
        _save_skill_policy(skills)

    return OperationResult(ok=True, entity_id=entity_id)
