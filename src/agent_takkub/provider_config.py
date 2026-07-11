"""Per-role CLI provider mapping.

The cockpit can spawn teammate panes backed by Claude Code
(`claude.exe`), OpenAI Codex (`codex.CMD`), or Google Antigravity
(`agy`, the `gemini` role's engine). By default every role except
`codex` and `gemini` runs
claude. This module lets the user override the mapping globally —
e.g. "backend always uses codex regardless of project" — by editing
a small JSON file under `~/.takkub/`.

Resolution rules:
- `lead`   → user config wins; default `claude` (issue #101, degraded-mode
             unlock: a codex/agy-backed Lead is now allowed. Default stays
             claude — unlock is opt-in, not a default change. Switching Lead
             off claude loses several claude-specific capabilities (mobile
             mirror, `--resume`, remote-control history/resume, JSONL token
             meter) — see docs/reviews/2026-07-11-101-lead-unlock.md and the
             `supports_*` capability flags on `provider_spec.ProviderSpec`
             that gate each of those call sites instead of crashing.)
- `codex`  → always `codex` (the role's whole point)
- `gemini` → always `gemini` (the role's whole point)
- everything else → user config wins; default `claude`

Config file: `~/.takkub/role-providers.json`. Created on first read
if missing (empty `{}`). Hand-edit to override:

    {"backend": "codex", "qa": "gemini"}

`provider_for("backend")` then returns `"codex"`. Restart cockpit
to pick up changes (no live reload in v1).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from .config import SETTINGS_HOME as _BASE_DIR
from .provider_spec import PROVIDER_REGISTRY

CLAUDE = "claude"
CODEX = "codex"
GEMINI = "gemini"
# Dynamic — derived from the registry (issue #103 Phase 0) instead of a
# hand-maintained frozenset, so a new PROVIDER_REGISTRY entry is
# automatically a valid provider everywhere this constant is consulted.
VALID_PROVIDERS = frozenset(PROVIDER_REGISTRY.keys())

# Roles whose provider is hard-coded — cannot be overridden by config.
# The `codex`/`gemini` roles' whole identity IS that CLI — remapping them
# would be a contradiction (a "codex" pane not running codex). `lead` was
# forced here too until issue #101's degraded-mode unlock; it is now a
# regular (optional) override — see the module docstring's "Resolution
# rules" for what a non-claude Lead loses.
_FORCED_PROVIDER = {
    "codex": CODEX,
    "gemini": GEMINI,
}

# Roles whose CLI is fixed and must not be offered as an override in the UI.
FORCED_ROLES = frozenset(_FORCED_PROVIDER)

# Global mapping — the cross-project default. Kept as a module global so tests
# can monkeypatch ``_CONFIG_PATH``; per-project mappings live under
# ``_BASE_DIR/projects/<slug>/`` (monkeypatch ``_BASE_DIR`` to redirect those).
_CONFIG_PATH = _BASE_DIR / "role-providers.json"


def _project_slug(project: str) -> str:
    """Filesystem-safe folder name for a project (mirrors pipeline_config)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", project) or "default"


def config_path(project: str | None = None) -> Path:
    """Where the per-role provider mapping lives.

    ``project`` → that project's own file under ``~/.takkub/projects/<slug>/``
    so each tab can back the same role with a different CLI without colliding;
    ``None`` → the global file (also the fallback a project inherits until it
    overrides). Function form so tests can monkeypatch ``_CONFIG_PATH``
    (global) or ``_BASE_DIR`` (per-project root).
    """
    if project:
        return _BASE_DIR / "projects" / _project_slug(project) / "role-providers.json"
    return _CONFIG_PATH


def load_providers(project: str | None = None) -> dict[str, str]:
    """Return the role→provider mapping for ``project`` (or global when None).

    A ``project`` with no per-project file falls back to the global mapping, so
    a fresh tab inherits global overrides until it saves its own. Only the
    global file is auto-created on first read (so the user has one to discover);
    per-project files are written lazily on first save. Invalid JSON or non-dict
    content is treated as empty (silent recovery — never blocks spawn)."""
    if project:
        p = config_path(project)
        if not p.exists():
            return load_providers(None)  # inherit global defaults
    else:
        p = config_path(None)
        if not p.exists():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}\n", encoding="utf-8")
            except OSError:
                return {}
            return {}
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Sanitize: drop entries with unknown providers so a typo in the
    # JSON doesn't silently route a role to nothing.
    return {
        str(role).lower(): str(provider).lower()
        for role, provider in data.items()
        if str(provider).lower() in VALID_PROVIDERS
    }


def save_providers(mapping: dict[str, str], project: str | None = None) -> None:
    """Write the mapping back to disk (per-project when ``project`` given, else
    global). Best-effort: raises only if the target dir is unwritable (very
    rare). Caller passes the full desired mapping — partial updates aren't
    supported."""
    path = config_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {
        str(role).lower(): str(provider).lower()
        for role, provider in mapping.items()
        if str(provider).lower() in VALID_PROVIDERS
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def role_provider_map(roles: Iterable[str], project: str | None = None) -> dict[str, str]:
    """Return ``{role: provider_for(role)}`` for the given roles (scoped to
    ``project`` when given).

    Used to seed the Pipeline-Settings page's per-role CLI dropdowns with the
    currently-configured mapping (forced roles resolve to their fixed CLI).
    """
    return {r: provider_for(r, project) for r in roles}


def save_role_overrides(
    mapping: dict[str, str],
    project: str | None = None,
    *,
    scope: Iterable[str] | None = None,
) -> None:
    """Persist only real overrides from a page payload (per-project when
    ``project`` given, else global).

    Drops forced roles (lead/codex/gemini — their CLI is fixed) and claude
    defaults (claude is the implicit default, storing it adds noise), then
    writes the result via :func:`save_providers`. Mirrors the old
    RoleProviderDialog save behavior so the file stays minimal.

    ``scope`` — when given, this call only owns overrides for roles in
    ``scope``: any pre-existing override for a role OUTSIDE ``scope`` (e.g. a
    custom role a UI page doesn't render a control for) is preserved instead
    of being silently dropped. Omit (default ``None``) to keep the historic
    full-replace behavior, where ``mapping`` is the complete desired mapping
    and anything missing from it is deleted.
    """
    overrides: dict[str, str] = {}
    if scope is not None:
        scope_set = {str(r).lower().strip() for r in scope}
        overrides = {r: p for r, p in load_providers(project).items() if r not in scope_set}
    for role, provider in (mapping or {}).items():
        r = str(role).lower().strip()
        p = str(provider).lower().strip()
        if r in FORCED_ROLES or p == CLAUDE or p not in VALID_PROVIDERS:
            continue
        overrides[r] = p
    save_providers(overrides, project)


def provider_for(role: str, project: str | None = None) -> str:
    """Resolve which CLI backs the given role.

    Returns one of `"claude"`, `"codex"`, or `"gemini"`. Forced for `lead`,
    `codex`, and `gemini`; consulted from the per-project (or global)
    role-providers mapping for everything else; defaults to `"claude"` when the
    role isn't in the config.
    """
    key = (role or "").lower().strip()
    if key in _FORCED_PROVIDER:
        return _FORCED_PROVIDER[key]
    mapping = load_providers(project)
    return mapping.get(key, CLAUDE)


def _provider_available(provider: str) -> bool:
    """True iff `provider` can actually run right now.

    Two ways a codex/gemini provider becomes unusable:
      1. Toggled off in the cockpit status bar (`disabled-providers.json`).
      2. Its CLI isn't installed (binary not on PATH).

    `claude` is always considered available (it's the cockpit's baseline;
    if claude itself is missing the spawn fails far louder elsewhere).
    Imports are lazy so this stays a thin per-role config module with no
    hard dependency on provider_state / the CLI helpers at import time.

    Discovery goes through the registered spec's ``custom_discovery_fn``
    (issue #103 Phase 0) instead of a hand-written per-provider branch —
    each wrapper (``provider_spec._discover_codex`` etc.) still does its
    ``from .codex_helper import find_codex_executable`` lazily *inside* the
    call, so a test that monkeypatches ``codex_helper.find_codex_executable``
    keeps working exactly as before.
    """
    if provider == CLAUDE:
        return True
    # (1) user-intent toggle
    try:
        from .provider_state import is_disabled

        if is_disabled(provider):
            return False
    except Exception:
        pass
    # (2) CLI actually installed
    try:
        spec = PROVIDER_REGISTRY.get(provider)
        if spec is not None and spec.custom_discovery_fn is not None:
            return spec.custom_discovery_fn() is not None
    except Exception:
        return False
    return True


def effective_provider_for(role: str, project: str | None = None) -> str:
    """Resolve which CLI will *actually* back the role this spawn.

    Like `provider_for()` but degrades a codex/gemini role to `claude`
    when that provider is unavailable — toggled off OR not installed.
    The role keeps its identity (a "gemini" pane is still a "gemini"
    pane); only the engine behind it changes. This is the "Claude รับ
    ตำแหน่งแทน" substitution: an assigned codex/gemini slot never fails
    or refuses — Claude fills it instead.

    `provider_for()` answers "which CLI is *configured* for this role"
    (static identity); this answers "which CLI is *usable* right now"
    (runtime). Spawn-time decisions should use this one.
    """
    desired = provider_for(role, project)
    if desired == CLAUDE:
        return CLAUDE
    return desired if _provider_available(desired) else CLAUDE


# Capability labels surfaced to the user when Lead is degraded off claude
# (issue #101). Keyed to the `ProviderSpec.supports_*` flag that gates the
# affected call site, so a future provider that gains one of these
# capabilities just flips its flag and drops off this list automatically —
# no hand-maintained enable list to forget to update.
_LEAD_CAPABILITY_LABELS: tuple[tuple[str, str], ...] = (
    ("supports_mirror", "mobile mirror (มือถือ mirror หน้าจอ Lead)"),
    ("supports_resume", "session resume (--resume · มือถือปุ่ม Resume)"),
    ("supports_remote_history", "remote-control history (มือถือดูประวัติแชท Lead ย้อนหลัง)"),
    ("supports_token_meter", "token/limit meter (usage แถบสถานะ อิง JSONL transcript)"),
    ("supports_hooks", "SessionStart hook (session-report auto session-uuid tracking)"),
)


def lead_capability_gap(project: str | None = None) -> tuple[str, list[str]] | None:
    """Return `(provider, [missing feature labels])` when Lead is currently
    backed by something other than claude, or `None` when it's claude (no
    gap).

    Used by the Settings UI (capability-warning badge on the Lead row) and
    the remote API (mobile mirror/history/resume responses) to tell the
    user WHY a claude-only feature is unavailable instead of silently doing
    nothing — issue #101 requires visible degradation, never a silent break.
    """
    from .provider_spec import PROVIDER_REGISTRY

    provider = effective_provider_for("lead", project)
    if provider == CLAUDE:
        return None
    spec = PROVIDER_REGISTRY.get(provider)
    if spec is None:
        return provider, [label for _, label in _LEAD_CAPABILITY_LABELS]
    missing = [label for flag, label in _LEAD_CAPABILITY_LABELS if not getattr(spec, flag, False)]
    return provider, missing


def lead_missing_capability(flag: str, project: str | None = None) -> str | None:
    """Return the current Lead provider's name if it lacks `flag` (a
    `ProviderSpec.supports_*` attribute name), else `None` (claude, or a
    provider that actually has the flag).

    Precise sibling to `lead_capability_gap` for call sites that only care
    about ONE capability — e.g. `remote/api.py`'s `resume_lead` gates
    specifically on `"supports_resume"` rather than blocking on ANY gap
    (a future provider could have mirror but not resume, or vice versa).
    """
    from .provider_spec import PROVIDER_REGISTRY

    provider = effective_provider_for("lead", project)
    if provider == CLAUDE:
        return None
    spec = PROVIDER_REGISTRY.get(provider)
    if spec is None or not getattr(spec, flag, False):
        return provider
    return None
