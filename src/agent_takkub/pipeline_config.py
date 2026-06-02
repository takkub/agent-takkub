"""Pipeline template store — ``~/.takkub/pipelines.json``.

Owns the user's named dev-pipeline templates (an ordered list of *hops*, each
hop a set of roles that run in **parallel**; hops run in **sequence**) plus the
per-role enable/disable map and the active-template pointer.

It deliberately does **not** own provider (codex/gemini) enable/disable — that
lives in :mod:`provider_state` (``disabled-providers.json``) so the status-bar
toggle and the Pipeline-Settings dialog share one source of truth. The dialog
bridge (:mod:`pipeline_dialog`) composes the two on load and splits them on save.

File shape (matches the settings page's JS ``STATE`` / ``TEMPLATES`` 1:1)::

    {
      "templates": [
        {"id": "feature", "name": "Feature (UI+API)", "builtin": true,
         "hops": [[{"role": "frontend", "cwd": "",
                    "requiresCommit": false, "autoChain": true}, ...], ...]}
      ],
      "rolesEnabled": {"frontend": true, "backend": true, ...},
      "activeTemplate": "feature"
    }

Graceful by design: a missing file, corrupt JSON, or any structural surprise
normalizes back to the built-in seed — :func:`load` never raises and always
returns a fresh, fully-validated payload. The three built-in templates are
re-asserted from code on every load/save, so they can't be renamed, deleted, or
drift, and changing a definition here propagates to every user on next load.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .roles import DEFAULT_TEAMMATES

# Roles selectable in a pipeline = every default teammate. Lead is the locked
# human/coordinator seat and is intentionally excluded from pipelines.
VALID_ROLES: tuple[str, ...] = tuple(r.name for r in DEFAULT_TEAMMATES)
_VALID_ROLE_SET: frozenset[str] = frozenset(VALID_ROLES)

# Built-in template ids — immutable in the UI (no rename/delete; Duplicate to
# fork). Re-asserted from code on every load/save so they can't drift.
BUILTIN_IDS: frozenset[str] = frozenset({"feature", "design", "quickfix"})

_PATH = Path.home() / ".takkub" / "pipelines.json"


def path() -> Path:
    """Where state lives. Function form so tests can monkeypatch ``_PATH``."""
    return _PATH


# ─────────────────────────────────────────────────────────────────────
# Built-in templates: the three real cockpit pipelines
# ─────────────────────────────────────────────────────────────────────


def _entry(
    role: str,
    cwd: str = "",
    requires_commit: bool = False,
    auto_chain: bool = False,
) -> dict:
    """One hop cell: a role plus its per-role flags (camelCase keys for JS)."""
    return {
        "role": role,
        "cwd": cwd,
        "requiresCommit": requires_commit,
        "autoChain": auto_chain,
    }


def _builtin_templates() -> list[dict]:
    """Fresh canonical copies of the immutable built-in templates."""
    return [
        {
            "id": "feature",
            "name": "Feature (UI+API)",
            "builtin": True,
            "hops": [
                [_entry("frontend", auto_chain=True), _entry("backend", auto_chain=True)],
                [_entry("qa"), _entry("reviewer")],
            ],
        },
        {
            "id": "design",
            "name": "Design Review",
            "builtin": True,
            "hops": [
                [_entry("qa")],
                [_entry("critic"), _entry("gemini")],
                [_entry("frontend")],
            ],
        },
        {
            "id": "quickfix",
            "name": "Quick fix",
            "builtin": True,
            "hops": [
                [_entry("backend", requires_commit=True)],
                [_entry("qa")],
            ],
        },
    ]


# ─────────────────────────────────────────────────────────────────────
# Normalization (the single validation path used by both load and save)
# ─────────────────────────────────────────────────────────────────────


def _norm_entry(raw: object) -> dict | None:
    """Coerce one hop cell; drop it (None) if the role is unknown/missing."""
    if not isinstance(raw, dict):
        return None
    role = raw.get("role")
    if not isinstance(role, str) or role not in _VALID_ROLE_SET:
        return None
    cwd = raw.get("cwd", "")
    return {
        "role": role,
        "cwd": cwd if isinstance(cwd, str) else "",
        "requiresCommit": bool(raw.get("requiresCommit", False)),
        "autoChain": bool(raw.get("autoChain", False)),
    }


def _norm_hop(raw: object) -> list[dict]:
    """Coerce one hop. A hop is a *set* of roles — dedup by role (keep first)."""
    if not isinstance(raw, list):
        return []
    hop: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        ent = _norm_entry(item)
        if ent is None or ent["role"] in seen:
            continue
        seen.add(ent["role"])
        hop.append(ent)
    return hop


def _norm_hops(raw: object) -> list[list[dict]]:
    if not isinstance(raw, list):
        return []
    return [_norm_hop(h) for h in raw]


def _norm_custom_template(raw: object) -> dict | None:
    """Coerce a custom template; drop it if malformed or claims a built-in id."""
    if not isinstance(raw, dict):
        return None
    tid = raw.get("id")
    if not isinstance(tid, str) or not tid.strip():
        return None
    tid = tid.strip()
    if tid in BUILTIN_IDS:
        return None  # built-ins come from code, never from the file
    name = raw.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else tid
    return {"id": tid, "name": name, "builtin": False, "hops": _norm_hops(raw.get("hops"))}


def _normalize(raw: object) -> dict:
    """Validate any input into the canonical payload. Never raises.

    Canonical built-ins are placed first (re-asserted from code), then the
    file's well-formed custom templates in their original order (deduped by id,
    built-in-id collisions dropped).
    """
    data = raw if isinstance(raw, dict) else {}

    templates = _builtin_templates()
    seen_ids: set[str] = set(BUILTIN_IDS)
    raw_templates = data.get("templates")
    if isinstance(raw_templates, list):
        for item in raw_templates:
            tpl = _norm_custom_template(item)
            if tpl is None or tpl["id"] in seen_ids:
                continue
            seen_ids.add(tpl["id"])
            templates.append(tpl)

    raw_roles = data.get("rolesEnabled")
    raw_roles = raw_roles if isinstance(raw_roles, dict) else {}
    roles_enabled = {role: bool(raw_roles.get(role, True)) for role in VALID_ROLES}

    active = data.get("activeTemplate")
    ids = {t["id"] for t in templates}
    if not isinstance(active, str) or active not in ids:
        active = templates[0]["id"]

    return {"templates": templates, "rolesEnabled": roles_enabled, "activeTemplate": active}


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def seed() -> dict:
    """The default payload (built-ins, all roles enabled, active = feature)."""
    return _normalize({})


def load() -> dict:
    """Return the validated pipeline payload. Missing/corrupt → built-in seed."""
    p = path()
    if not p.exists():
        return _normalize({})
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _normalize({})
    return _normalize(data)


def save(payload: object) -> None:
    """Validate ``payload`` and persist atomically (built-ins re-asserted).

    ``payload`` may include an unrelated ``providers`` key (the settings page
    sends the whole state blob) — it is ignored here; provider enable/disable is
    owned by :mod:`provider_state`.
    """
    normalized = _normalize(payload)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


# ─────────────────────────────────────────────────────────────────────
# Provider <-> page bridge helpers (pure; unit-tested without Qt)
#
# The settings page expresses providers as ``true = native CLI on``, while
# provider_state stores ``true = disabled``. These two helpers translate at the
# boundary so the dialog bridge stays a thin shell.
# ─────────────────────────────────────────────────────────────────────


def with_providers(payload: dict, disabled: Iterable[str], togglable: Iterable[str]) -> dict:
    """Return ``payload`` plus a ``providers`` map for the page.

    ``providers[name] = True`` means *enabled* (native CLI on) — i.e. the
    provider is NOT in the ``disabled`` set.
    """
    disabled_set = set(disabled)
    out = dict(payload)
    out["providers"] = {name: name not in disabled_set for name in sorted(togglable)}
    return out


def provider_disabled_targets(payload: object, togglable: Iterable[str]) -> dict[str, bool]:
    """Read a saved page payload → ``{provider: desired_disabled}`` (inverted).

    Only includes togglable providers actually present in the payload's
    ``providers`` map. ``desired_disabled = not enabled``.
    """
    providers = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(providers, dict):
        return {}
    return {name: not bool(providers[name]) for name in togglable if name in providers}
