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
import re
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

_BASE_DIR = Path.home() / ".takkub"
# Global file — the cross-project default. Kept as a module global so existing
# tests can monkeypatch ``_PATH``; per-project files live under
# ``_BASE_DIR/projects/<slug>/`` (monkeypatch ``_BASE_DIR`` to redirect those).
_PATH = _BASE_DIR / "pipelines.json"


def _project_slug(project: str) -> str:
    """Filesystem-safe folder name for a project (mirrors shared_dev_tools)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", project) or "default"


def path(project: str | None = None) -> Path:
    """Where pipeline state lives.

    ``project`` → that project's own file under ``~/.takkub/projects/<slug>/``
    so each tab keeps an independent pipeline config (no cross-project
    collision). ``None`` → the global file, which doubles as the default a new
    project inherits on first open. Function form so tests can monkeypatch
    ``_PATH`` (global) or ``_BASE_DIR`` (per-project root).
    """
    if project:
        return _BASE_DIR / "projects" / _project_slug(project) / "pipelines.json"
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
            # Verify flow: DEV impl (parallel, auto-chained) → devops brings the
            # stack up locally on non-clashing ports → QA tests LAST as the
            # single final gate against the running stack. reviewer is a PR-time
            # gate (qa-only mid-cycle), so it's not a default hop here.
            "hops": [
                [_entry("frontend", auto_chain=True), _entry("backend", auto_chain=True)],
                [_entry("devops")],
                [_entry("qa")],
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


def _has_runnable_hop(hops: list[list[dict]]) -> bool:
    """True if at least one hop carries a role — guards built-in overrides from
    wiping a pipeline down to nothing."""
    return any(hop for hop in hops)


def _normalize(raw: object) -> dict:
    """Validate any input into the canonical payload. Never raises.

    Built-ins are seeded from code and placed first. Their **identity**
    (id/name/builtin) is locked — a file claiming a built-in id can rename or
    declassify nothing. Their **hops** are user-overridable, though: a file
    entry with a built-in id replaces that template's hops, which is how
    per-project pipeline edits persist (a non-degenerate override only — an
    all-empty one is ignored so a built-in can't be accidentally emptied).
    Well-formed custom templates follow in file order (deduped by id).
    """
    data = raw if isinstance(raw, dict) else {}

    builtins = {t["id"]: t for t in _builtin_templates()}
    order = list(builtins.keys())  # canonical display order
    customs: list[dict] = []
    seen_ids: set[str] = set(BUILTIN_IDS)
    raw_templates = data.get("templates")
    if isinstance(raw_templates, list):
        for item in raw_templates:
            if not isinstance(item, dict):
                continue
            tid = item.get("id")
            tid = tid.strip() if isinstance(tid, str) else ""
            if tid in BUILTIN_IDS:
                # Override hops only — name/builtin stay canonical.
                if "hops" in item:
                    hops = _norm_hops(item.get("hops"))
                    if _has_runnable_hop(hops):
                        builtins[tid] = {
                            "id": tid,
                            "name": builtins[tid]["name"],
                            "builtin": True,
                            "hops": hops,
                        }
                continue
            tpl = _norm_custom_template(item)
            if tpl is None or tpl["id"] in seen_ids:
                continue
            seen_ids.add(tpl["id"])
            customs.append(tpl)

    templates = [builtins[i] for i in order] + customs

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


def load(project: str | None = None) -> dict:
    """Return the validated pipeline payload. Missing/corrupt → built-in seed.

    For a ``project`` with no per-project file yet, falls back to the global
    file so a fresh tab inherits the user's global defaults until it saves its
    own — after which the two are independent and never collide across tabs.
    """
    p = path(project)
    if project and not p.exists():
        p = path(None)  # inherit global defaults on first open
    if not p.exists():
        return _normalize({})
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _normalize({})
    return _normalize(data)


def save(payload: object, project: str | None = None) -> None:
    """Validate ``payload`` and persist atomically.

    ``project`` targets that project's own file; ``None`` writes the global
    one. Built-in templates whose hops still match the code defaults are NOT
    written (they keep tracking code and the file stays minimal); only *edited*
    built-ins and custom templates are persisted.

    ``payload`` may include unrelated ``providers`` / ``roleProviders`` keys
    (the settings page sends the whole state blob) — ignored here; provider
    enable/disable is owned by :mod:`provider_state` and the per-role CLI map by
    :mod:`provider_config`.
    """
    normalized = _normalize(payload)
    default_hops = {t["id"]: t["hops"] for t in _builtin_templates()}
    normalized["templates"] = [
        t
        for t in normalized["templates"]
        if not (t["id"] in BUILTIN_IDS and t["hops"] == default_hops[t["id"]])
    ]
    p = path(project)
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
