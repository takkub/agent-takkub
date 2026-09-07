"""Team preset — per-project team-size switch (#512).

Most company tasks are a small, scope-clear bug fix, not a full feature — yet
the cockpit defaulted every project to booting frontend+backend+QA for
everything (~67k prompt tokens/pane, measured). A **team preset** is a single
per-project switch (with a per-task override) that composes the settings that
already existed piecemeal:

  * :mod:`pipeline_config` ``rolesEnabled`` (#510 — a role toggled off)
  * :mod:`pipeline_config` ``activeTemplate`` (which hop sequence runs)
  * :mod:`exec_mode` (solo vs multi-instance fan-out — informational field
    here; the module itself is currently forced PARALLEL, see its docstring)
  * whether **Lead** may edit project files itself (``lead_may_implement``,
    enforced by :func:`lead_context.render_lead_settings`)

Preset roster (2026-09-07 Lead scope addition): a preset only sees/toggles
five **positions** — ``frontend`` / ``backend`` / ``mobile`` / ``devops`` +
any project custom role — plus one **checker** slot. ``qa`` and ``critic``
stay separate roles today (#513 will fold them into ``reviewer`` with a
code|e2e|ui mode and drop providers from the role list); until then the
checker slot maps to a real role through the single :data:`CHECKER_ROLES`
table below, so that future collapse only touches this one table, not the
preset model. Provider-panes (codex/gemini/opencode/kimi/cursor) and ``shell``
are NEVER part of a preset's roster — still directly `assign`-able as always,
untouched by preset switches.

File shape (mirrors ``pipeline_config``'s per-project JSON)::

    {"preset": "solo-lead", "custom": null, "override": null}
    {"preset": "custom",
     "custom": {"roles": {"frontend": true, ...}, "checker": "qa",
                "lead_may_implement": false, "template": "feature",
                "exec_mode": "parallel"},
     "override": null}

``override`` is a per-task preset id set by ``takkub assign --role lead
--team <preset>`` (#512 item 4) — it wins over ``preset`` until explicitly
cleared or reassigned. ponytail: no auto-expiry: the override survives until
the next ``--team`` assign, ``set_current``, or ``clear_override`` call —
wiring it to "clears when the task's `done` lands" would need a hook into
`orchestrator.done()`/`close()` this issue didn't scope; simple explicit
clear is enough for a v1.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import SETTINGS_HOME as _BASE_DIR

# ─────────────────────────────────────────────────────────────────────
# Preset roster & built-ins
# ─────────────────────────────────────────────────────────────────────

#: The four "position" roles a preset's roster ever shows/toggles. Custom
#: project roles are appended dynamically (see `_position_roles`) — provider
#: panes (codex/gemini/opencode/kimi/cursor) and `shell` are deliberately
#: excluded, per the 2026-09-07 Lead scope addition.
POSITION_ROLES: tuple[str, ...] = ("frontend", "backend", "mobile", "devops")

#: checker slot value -> the real role it spawns. Single seam for #513 (qa +
#: critic -> reviewer with a mode) to repoint without touching preset logic.
CHECKER_ROLES: dict[str, str] = {"reviewer": "reviewer", "qa": "qa"}

PRESET_IDS: tuple[str, ...] = ("solo-lead", "pair", "full", "custom", "auto")

_LABELS: dict[str, str] = {
    "solo-lead": "ทำเอง",
    "pair": "คู่",
    "full": "ทีมเต็ม",
    "custom": "กำหนดเอง",
    "auto": "อัตโนมัติ",
}

# Built-in presets: {roles: {position: enabled}, checker, lead_may_implement,
# template, exec_mode}. `verify` is derived (checker or "self"), not stored.
BUILTIN_PRESETS: dict[str, dict] = {
    "solo-lead": {
        "roles": dict.fromkeys(POSITION_ROLES, False),
        "checker": None,
        "lead_may_implement": True,
        "template": "quickfix",
        "exec_mode": "solo",
    },
    "pair": {
        "roles": dict.fromkeys(POSITION_ROLES, False),
        "checker": "reviewer",
        "lead_may_implement": True,
        "template": "quickfix",
        "exec_mode": "solo",
    },
    "full": {
        "roles": dict.fromkeys(POSITION_ROLES, True),
        "checker": "qa",
        "lead_may_implement": False,
        "template": "feature",
        "exec_mode": "parallel",
    },
}

_DEFAULT_PRESET = "auto"

#: The 4 presets a quick picker (status-bar chip menu, Settings team-size
#: cards, mobile drawer) offers directly — "custom" needs a roles/checker
#: payload none of those one-tap UIs can carry, so it's reached through
#: Settings' full role toggles instead (see settings_window's own docstring).
QUICK_PRESET_IDS: tuple[str, ...] = ("solo-lead", "pair", "full", "auto")

_DESCRIPTIONS: dict[str, str] = {
    "solo-lead": "Lead อ่าน → แก้ → ทดสอบเอง ไม่ spawn ใคร",
    "pair": "Lead ทำเอง + reviewer 1 คน อ่านอย่างเดียว",
    "full": "แยก role ตาม template, QA ปิดท้ายเสมอ",
    "custom": "เลือกเอง: role ไหนบ้าง · Lead แก้โค้ดได้ไหม · ตรวจด้วยอะไร",
    "auto": "Lead เสนอขนาดจาก scope ของงานแต่ละครั้ง",
}

_PANE_NOTES: dict[str, str] = {
    "solo-lead": "0 pane · ประหยัดสุด",
    "pair": "1 pane",
    "full": "2–5 pane",
    "custom": "แล้วแต่ตั้งค่า",
    "auto": "แล้วแต่งาน",
}


def label(preset_id: str) -> str:
    return _LABELS.get(preset_id, preset_id)


def description(preset_id: str) -> str:
    return _DESCRIPTIONS.get(preset_id, "")


def pane_note(preset_id: str) -> str:
    return _PANE_NOTES.get(preset_id, "")


def verify_mode(cfg: dict) -> str:
    """ "self" | "reviewer" | "qa" — the resolved config's check step."""
    return cfg.get("checker") or "self"


# ─────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────


def _project_slug(project: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", project) or "default"


def path(project: str | None) -> Path:
    """Per-project file, ``projects/<slug>/team_preset.json`` (V2 layout: one
    domain, one place — no top-level loose file). ``None``/blank project
    falls back to a ``default`` slug rather than a bare top-level file."""
    slug = _project_slug(project) if project else "default"
    return _BASE_DIR / "projects" / slug / "team_preset.json"


def _custom_role_names(project: str | None) -> frozenset[str]:
    from .roles import custom_roles

    return frozenset(r.name for r in custom_roles())


def _position_roles(project: str | None) -> tuple[str, ...]:
    """POSITION_ROLES plus this project's registered custom roles."""
    return POSITION_ROLES + tuple(sorted(_custom_role_names(project)))


def _governed_roles(project: str | None) -> frozenset[str]:
    """Every role a preset can gate: positions + custom roles + the two
    checker-mappable roles. Everything else (providers, shell, critic) is
    never governed by a preset."""
    return frozenset(_position_roles(project)) | frozenset(CHECKER_ROLES.values())


def _normalize_custom(raw: object, project: str | None) -> dict:
    positions = _position_roles(project)
    raw = raw if isinstance(raw, dict) else {}
    raw_roles = raw.get("roles")
    raw_roles = raw_roles if isinstance(raw_roles, dict) else {}
    roles = {p: bool(raw_roles.get(p, False)) for p in positions}
    checker = raw.get("checker")
    checker = checker if checker in CHECKER_ROLES else None
    template = raw.get("template")
    template = template if isinstance(template, str) and template.strip() else "feature"
    exec_mode = raw.get("exec_mode") if raw.get("exec_mode") in ("solo", "parallel") else "parallel"
    return {
        "roles": roles,
        "checker": checker,
        "lead_may_implement": bool(raw.get("lead_may_implement", False)),
        "template": template,
        "exec_mode": exec_mode,
    }


def _load_raw(project: str | None) -> dict:
    p = path(project)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_raw(data: dict, project: str | None) -> None:
    p = path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)


def current_preset_id(project: str | None) -> str:
    """The project's standing preset id — NOT resolving a task override.
    Missing/corrupt file -> the default (``auto``)."""
    raw = _load_raw(project)
    preset = raw.get("preset")
    return preset if preset in PRESET_IDS else _DEFAULT_PRESET


def active_override(project: str | None) -> str | None:
    """The per-task override in effect, if any (see module docstring)."""
    raw = _load_raw(project)
    override = raw.get("override")
    return override if override in PRESET_IDS else None


def _resolve(preset_id: str, project: str | None, raw: dict) -> dict:
    if preset_id in BUILTIN_PRESETS:
        cfg = {**BUILTIN_PRESETS[preset_id], "roles": dict(BUILTIN_PRESETS[preset_id]["roles"])}
        # custom roles ride along disabled-by-default under a built-in preset
        for extra in _custom_role_names(project):
            cfg["roles"].setdefault(extra, False)
        cfg["preset"] = preset_id
        return cfg
    if preset_id == "custom":
        cfg = _normalize_custom(raw.get("custom"), project)
        cfg["preset"] = "custom"
        return cfg
    # "auto": no fixed roster — advisory only (see routing_planner.suggest_team_size).
    return {
        "preset": "auto",
        "roles": dict.fromkeys(_position_roles(project), True),
        "checker": "qa",
        "lead_may_implement": False,
        "template": "feature",
        "exec_mode": "parallel",
    }


def current(project: str | None = None) -> dict:
    """The EFFECTIVE preset config for this project right now: the active
    task override if one is set, else the project's standing preset.

    Returned dict: ``{"preset", "roles", "checker", "lead_may_implement",
    "template", "exec_mode"}``. ``roles`` covers positions + custom roles
    only — never providers/shell/qa/reviewer/critic.
    """
    raw = _load_raw(project)
    effective_id = raw.get("override") if raw.get("override") in PRESET_IDS else None
    effective_id = effective_id or current_preset_id(project)
    return _resolve(effective_id, project, raw)


def resolve(preset_id: str, project: str | None = None) -> dict:
    """The config `preset_id` WOULD produce for `project`, ignoring any
    active per-task override — unlike `current()`. UI surfaces that are
    about to WRITE the project's standing preset (Settings' team-size cards,
    the status-bar chip's quick menu) preview each choice through this, not
    `current()`, so they never read a preview through an override they
    aren't touching. Raises ValueError on an unknown id."""
    preset_id = str(preset_id).strip()
    if preset_id not in PRESET_IDS:
        raise ValueError(f"unknown team preset: {preset_id!r}")
    return _resolve(preset_id, project, _load_raw(project))


def set_current(preset_id: str, project: str | None = None, *, custom: dict | None = None) -> dict:
    """Persist ``preset_id`` as this project's standing preset. Raises
    ValueError on an unknown id. ``custom`` is required (and normalized) when
    ``preset_id == "custom"``; ignored otherwise. Does NOT touch an existing
    per-task ``override`` — clear it explicitly via `clear_override`."""
    preset_id = str(preset_id).strip()
    if preset_id not in PRESET_IDS:
        raise ValueError(f"unknown team preset: {preset_id!r}")
    raw = _load_raw(project)
    raw["preset"] = preset_id
    raw["custom"] = (
        _normalize_custom(custom, project) if preset_id == "custom" else raw.get("custom")
    )
    _save_raw(raw, project)
    return current(project)


def set_override(preset_id: str, project: str | None = None) -> dict:
    """Set a per-task override (#512 item 4, `takkub assign --role lead
    --team <preset>`). Raises ValueError on an unknown id (``custom`` is not
    a valid override target — it needs a roles/checker payload the one-line
    CLI flag can't carry)."""
    preset_id = str(preset_id).strip()
    if preset_id not in PRESET_IDS or preset_id == "custom":
        raise ValueError(f"unknown team preset override: {preset_id!r}")
    raw = _load_raw(project)
    raw["override"] = preset_id
    _save_raw(raw, project)
    return current(project)


def clear_override(project: str | None = None) -> dict:
    raw = _load_raw(project)
    raw["override"] = None
    _save_raw(raw, project)
    return current(project)


def note_manual_roles_change(new_roles_enabled: dict, project: str | None = None) -> bool:
    """Call after Settings -> Providers & Roles saves `rolesEnabled` by hand.

    If the project is on a FIXED preset (not already "custom") and the saved
    roles for any position the preset governs no longer match what that
    preset would set, flip the project to "custom" carrying over the other
    resolved fields (lead_may_implement/checker/template/exec_mode) plus the
    freshly-saved roles — so a hand toggle doesn't silently get overwritten
    on the next preset-driven action, and doesn't get silently exempt from
    the "toggle by hand -> custom" rule (#512 acceptance).

    Returns True iff the project flipped to custom.
    """
    preset_id = current_preset_id(project)
    if preset_id in ("custom", "auto"):
        return False
    cfg = _resolve(preset_id, project, _load_raw(project))
    positions = _position_roles(project)
    drifted = any(
        bool(new_roles_enabled.get(p, False)) != cfg["roles"].get(p, False) for p in positions
    )
    checker_role = CHECKER_ROLES.get(cfg["checker"]) if cfg["checker"] else None
    if checker_role is not None:
        # A preset's checker is ON in rolesEnabled; hand-disabling it drifts too.
        drifted = drifted or not bool(new_roles_enabled.get(checker_role, True))
    if not drifted:
        return False
    custom_roles_payload = {
        p: bool(new_roles_enabled.get(p, cfg["roles"].get(p, False))) for p in positions
    }
    set_current(
        "custom",
        project,
        custom={
            "roles": custom_roles_payload,
            "checker": cfg["checker"],
            "lead_may_implement": cfg["lead_may_implement"],
            "template": cfg["template"],
            "exec_mode": cfg["exec_mode"],
        },
    )
    return True


# ─────────────────────────────────────────────────────────────────────
# Enforcement (mirrors pipeline_config.is_role_enabled's role as the single
# choke-point helper every enforcement surface calls — #510 pattern)
# ─────────────────────────────────────────────────────────────────────


def can_spawn(role: str, project: str | None = None) -> tuple[bool, str]:
    """True unless *role* is a preset-governed position/checker role that the
    project's EFFECTIVE preset (standing or task-override) doesn't include.

    Roles outside the preset roster (providers, `shell`, `critic`, `qa`/
    `reviewer` when neither is the active checker's OTHER option... no —
    see below) always pass through untouched. ``lead`` always passes (it's
    the coordinator, not something Lead "spawns").  ``"auto"`` never
    restricts (advisory-only, #512 item 5) — nothing to enforce until a task
    override pins a concrete preset.
    """
    base = role.split("#", 1)[0].strip().lower()
    if base == "lead":
        return True, ""
    cfg = current(project)
    if cfg["preset"] == "auto":
        return True, ""
    if base not in _governed_roles(project):
        return True, ""
    if base in CHECKER_ROLES.values():
        allowed = cfg["checker"] == base or (
            # a checker role also registered as a position (custom preset
            # choosing e.g. "reviewer" as a coding position too) stays allowed
            base in cfg["roles"] and cfg["roles"][base]
        )
    else:
        allowed = bool(cfg["roles"].get(base, False))
    if allowed:
        return True, ""
    return False, (
        f"role {base} ไม่เปิดใน team preset '{label(cfg['preset'])}' ของโปรเจคนี้ — "
        f"เปลี่ยน preset ที่ Settings หรือ `takkub assign --role lead --team full` "
        "override เฉพาะงานนี้"
    )


def lead_may_implement(project: str | None = None) -> bool:
    return bool(current(project).get("lead_may_implement", False))
