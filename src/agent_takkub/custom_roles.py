"""Custom role registry — user-defined roles beyond the built-in cockpit set.

A6 (Role & Skill Manager). Two files persist a custom role:

  ~/.takkub/custom-roles.json   registry: {name: {label, color, column, row}}
  <config.CUSTOM_AGENTS_DIR>/<name>.md   role file (stand-in instructions),
                                          same format as built-in files under
                                          config.AGENTS_DIR

`roles.py` resolves a role by name through its own runtime `_CUSTOM` dict —
this module is what fills that dict, both at cockpit boot
(`load_and_register_all()`) and immediately after a dialog creates a role
(`create_role()`'s caller registers it so it's spawnable without a restart).

Provider-neutral by design: a custom role has no CLI/model field, so it
always spawns on the default (claude) provider like any other role
`provider_config.provider_for()` doesn't special-case — see the A6 design
note for the tradeoff of exposing that as a UI knob.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path

from .config import CUSTOM_AGENTS_DIR, SETTINGS_HOME, validate_name
from .roles import ALL_DEFAULT, Role

_log = logging.getLogger(__name__)

CUSTOM_ROLES_FILE = SETTINGS_HOME / "custom-roles.json"

_RESERVED_NAMES = frozenset(r.name for r in ALL_DEFAULT)
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DEFAULT_COLOR = "#94a3b8"


def validate_role_name(name: str) -> tuple[bool, str]:
    """Return (ok, error_message) — error_message is "" when ok.

    Reuses `config.validate_name`'s `[a-z0-9][a-z0-9_-]{0,63}` charset (the
    same one every path-derived role/project name in the cockpit is held to)
    so a custom role name can never escape `CUSTOM_AGENTS_DIR` /
    `runtime/agents/<role>/` via traversal. `#` is rejected separately since
    it's reserved for shard suffixes (`qa#1`) and `validate_name` only
    validates the shard *form*, not that a bare name lacks one.
    """
    if not name:
        return False, "ชื่อห้ามว่าง"
    if "#" in name:
        return False, "ชื่อห้ามมี '#' (สงวนไว้สำหรับ shard index เช่น qa#1)"
    try:
        normalized = validate_name(name, "role")
    except ValueError:
        return False, "ชื่อต้องเป็น a-z0-9 กับ - _ เท่านั้น (เริ่มด้วยตัวอักษร/ตัวเลข, ยาวไม่เกิน 64)"
    if normalized in _RESERVED_NAMES:
        return False, f"ชื่อ '{normalized}' ชนกับ built-in role อยู่แล้ว"
    return True, ""


def _default_role_template(name: str, label: str) -> str:
    return (
        f"# {label}\n\n"
        f"คุณคือ **{label}** — custom role ที่ user สร้างเองผ่าน Role Manager "
        f"(`--role {name}`). ทำงานเองโดยตรง ห้าม spawn subagent.\n\n"
        "## หน้าที่\n"
        "_(แก้ไฟล์นี้เพื่อกำหนดหน้าที่ + ขอบเขตงานของ role นี้ — "
        f"ไฟล์อยู่ที่ `{(CUSTOM_AGENTS_DIR / f'{name}.md').as_posix()}`)_\n\n"
        "## การรายงานกลับ\n"
        'รายงานกลับด้วย `takkub done "<สรุปงาน>"` เมื่อเสร็จงาน '
        'หรือ `takkub send --to lead "blocked: ..."` ถ้าติด — '
        "ห้ามพิมพ์คำถามค้างไว้เฉยๆ ในจอตัวเอง\n"
    )


def load_custom_roles() -> dict[str, Role]:
    """Load the custom-role registry. Never raises; missing/corrupt -> {}."""
    if not CUSTOM_ROLES_FILE.is_file():
        return {}
    try:
        data = json.loads(CUSTOM_ROLES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.debug("load_custom_roles: could not read %s: %s", CUSTOM_ROLES_FILE, e)
        return {}
    if not isinstance(data, dict):
        return {}
    raw_roles = data.get("roles")
    if not isinstance(raw_roles, dict):
        return {}

    out: dict[str, Role] = {}
    for name, entry in raw_roles.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        ok, _err = validate_role_name(name)
        if not ok:
            _log.debug("load_custom_roles: skipping invalid entry %r", name)
            continue
        label = entry.get("label")
        color = entry.get("color")
        column = entry.get("column")
        row = entry.get("row")
        if not isinstance(label, str) or not label.strip():
            label = name.capitalize()
        if not isinstance(color, str) or not _COLOR_RE.match(color):
            color = _DEFAULT_COLOR
        if not isinstance(column, int) or column not in (1, 2):
            column = 2
        if not isinstance(row, int) or row < 0:
            row = 99
        out[name] = Role(name=name, label=label, color=color, column=column, row=row)
    return out


def save_custom_roles(roles: dict[str, Role]) -> bool:
    """Atomically persist the registry (tmp + replace). Never raises."""
    payload = {
        "version": 1,
        "roles": {
            name: {"label": r.label, "color": r.color, "column": r.column, "row": r.row}
            for name, r in roles.items()
        },
    }
    tmp_path: Path | None = None
    try:
        CUSTOM_ROLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=CUSTOM_ROLES_FILE.parent,
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(payload, tmp, indent=2, ensure_ascii=False)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(CUSTOM_ROLES_FILE)
        return True
    except OSError as e:
        _log.warning("save_custom_roles: could not write %s: %s", CUSTOM_ROLES_FILE, e)
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def list_role_names() -> frozenset[str]:
    """Names of every registered custom role (fresh read, mirrors
    `pane_tools_policy`'s uncached-read-per-call pattern)."""
    return frozenset(load_custom_roles())


def create_role(
    name: str,
    label: str,
    color: str,
    column: int,
    row: int,
    instructions: str | None = None,
) -> tuple[bool, str]:
    """Validate + persist a new custom role: registry entry + role file.

    Returns (ok, message) — message is "" on success, an error string
    otherwise. Does NOT call `roles.register_role()` itself (this module has
    no import-time dependency on mutating the live registry) — the caller
    does that right after a successful create so the role is spawnable in
    this process immediately, without waiting for a restart.
    """
    ok, err = validate_role_name(name)
    if not ok:
        return False, err
    name = name.lower().strip()
    if not isinstance(color, str) or not _COLOR_RE.match(color):
        return False, "สีต้องเป็นรูปแบบ #rrggbb"
    if column not in (1, 2):
        return False, "column ต้องเป็น 1 (dev) หรือ 2 (support)"

    label = (label or "").strip() or name.capitalize()
    content = (
        instructions
        if instructions and instructions.strip()
        else _default_role_template(name, label)
    )

    # Write the role markdown to a temp file FIRST (not committed under its
    # final name yet), then commit the registry, then rename the temp file
    # into place last. If the rename fails, the registry write is rolled
    # back — the old order (registry write, then role-file write) left a
    # partial-commit on role-file failure: a registry entry with no matching
    # file, which then made a retry collide as "already exists".
    role_file = CUSTOM_AGENTS_DIR / f"{name}.md"
    tmp_path: Path | None = None
    try:
        CUSTOM_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=CUSTOM_AGENTS_DIR,
            suffix=".md",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
    except OSError as e:
        return False, f"เขียน role file ไม่สำเร็จ: {e}"

    roles = load_custom_roles()
    previous = roles.get(name)
    roles[name] = Role(name=name, label=label, color=color, column=column, row=row)
    if not save_custom_roles(roles):
        tmp_path.unlink(missing_ok=True)
        return False, "เขียน custom-roles.json ไม่สำเร็จ"

    try:
        tmp_path.replace(role_file)
    except OSError as e:
        if previous is None:
            roles.pop(name, None)
        else:
            roles[name] = previous
        save_custom_roles(roles)
        tmp_path.unlink(missing_ok=True)
        return False, f"เขียน role file ไม่สำเร็จ: {e}"

    return True, ""


def role_file_path(name: str) -> Path:
    """Where `name`'s role .md lives, whether or not it exists yet."""
    return CUSTOM_AGENTS_DIR / f"{name}.md"


def delete_role(name: str) -> bool:
    """Remove a custom role from the registry. The role .md file under
    CUSTOM_AGENTS_DIR is left in place (harmless, keeps a backup/history) —
    caller is responsible for also calling `roles._CUSTOM.pop(name, None)`
    equivalent if it wants the live process to forget the role immediately."""
    roles = load_custom_roles()
    if name not in roles:
        return True
    del roles[name]
    return save_custom_roles(roles)


def load_and_register_all() -> int:
    """Boot-time hook: load the registry and register every entry with
    `roles.register_role` so `roles.by_name()` resolves them. Returns the
    count registered. Never raises — a corrupt registry just means zero
    custom roles for this session, not a failed boot."""
    from . import roles as roles_mod

    try:
        loaded = load_custom_roles()
    except Exception:
        _log.exception("load_and_register_all: unexpected failure loading registry")
        return 0
    for r in loaded.values():
        roles_mod.register_role(r)
    return len(loaded)
