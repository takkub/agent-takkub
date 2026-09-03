"""Per-role CLI provider mapping.

The cockpit can spawn teammate panes backed by any CLI registered in
``provider_spec.PROVIDER_REGISTRY``. By default every role except the forced
provider-identity roles runs claude. This module lets the user override the mapping globally —
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
- `opencode` → always `opencode` (the role's whole point)
- `kimi`   → always `kimi` (the role's whole point)
- `cursor` → always `cursor` (the role's whole point)
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
import time
from collections.abc import Iterable
from pathlib import Path

from .config import SETTINGS_HOME as _BASE_DIR
from .provider_spec import PROVIDER_REGISTRY

CLAUDE = "claude"
CODEX = "codex"
GEMINI = "gemini"
OPENCODE = "opencode"
KIMI = "kimi"
CURSOR = "cursor"
# Dynamic — derived from the registry (issue #103 Phase 0) instead of a
# hand-maintained frozenset, so a new PROVIDER_REGISTRY entry is
# automatically a valid provider everywhere this constant is consulted.
VALID_PROVIDERS = frozenset(PROVIDER_REGISTRY.keys())

# Roles whose provider is hard-coded — cannot be overridden by config.
# Provider-named roles' whole identity IS that CLI — remapping them would be
# a contradiction (a "codex" pane not running codex). `lead` was
# forced here too until issue #101's degraded-mode unlock; it is now a
# regular (optional) override — see the module docstring's "Resolution
# rules" for what a non-claude Lead loses.
_FORCED_PROVIDER = {
    "codex": CODEX,
    "gemini": GEMINI,
    "opencode": OPENCODE,
    "kimi": KIMI,
    "cursor": CURSOR,
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


def _sanitize_providers(data: dict) -> dict[str, str]:
    # Drop entries with unknown providers so a typo in the JSON doesn't
    # silently route a role to nothing.
    return {
        str(role).lower(): str(provider).lower()
        for role, provider in data.items()
        if str(provider).lower() in VALID_PROVIDERS
    }


def load_providers(project: str | None = None) -> dict[str, str]:
    """Return the role→provider mapping for ``project`` (or global when None).

    A ``project`` with no per-project file falls back to the global mapping, so
    a fresh tab inherits global overrides until it saves its own. Only the
    global file is auto-created on first read (so the user has one to discover);
    per-project files are written lazily on first save. Invalid JSON or non-dict
    content is treated as empty (silent recovery — never blocks spawn).

    ``TAKKUB_V2_AUTHORITY`` (#362 Phase 10 wave 2, default off): when on and
    the dual-written ``v2/`` routing mirror exists, sanitizes THAT scope
    (global or this project's entry) instead of the V1 file — same
    sanitizer either way. A project with no v2 entry AT ALL falls back to
    the V2 global (mirrors V1's "no per-project file -> inherit global").
    A project WITH an entry (even ``{}``) resolves from it directly, no
    inheritance — matching V1, because `dual_write_routing`'s caller
    (`save_providers`, and the migration ladder's `RoleAgentMigrationStep`)
    only ever write a project entry for a project whose per-project V1
    file actually exists (#480); a project with no per-project file simply
    gets no key in the mirror, same as V1's own existence check.
    """
    from .core.storage.v2_authority import read_routing, v2_authority_enabled

    if v2_authority_enabled():
        v2_routing = read_routing()
        if v2_routing is not None:
            if project and project in v2_routing["projects"]:
                return _sanitize_providers(v2_routing["projects"][project])
            return _sanitize_providers(v2_routing["global"])

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
    return _sanitize_providers(data)


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

    from . import config as _config
    from .core.storage.dual_write import dual_write_routing
    from .core.storage.legacy_reader import read_json

    dual_write_routing(
        read_json(config_path(None)),
        {
            name: read_json(config_path(name))
            for name in _config.list_project_names()
            if config_path(name).exists()
        },
    )


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

    Returns one of `"claude"`, `"codex"`, `"gemini"`, etc. Consulted from the
    per-project (or global) role-providers mapping for everything else;
    defaults to `"claude"` when the role isn't in the config.
    """
    key = re.sub(r"#\d+$", "", (role or "").lower().strip())
    if key in _FORCED_PROVIDER:
        return _FORCED_PROVIDER[key]
    mapping = load_providers(project)
    if key in mapping:
        return mapping[key]
    return _provider_from_role_models(key)


def _provider_from_role_models(key: str) -> str:
    """Fallback for a role that `role-providers.json` says nothing about:
    honour the provider recorded alongside its model in `role-models.json`
    before defaulting to claude (#338).

    Both files are written by the same Settings page, and `role-models.json`
    stores `{"frontend": {"provider": "codex", "model": ..., "effort": ...}}`
    — a shape that reads as "run frontend on codex". It was never consulted
    here, so a team that set up model diversity entirely through the model
    picker (leaving `role-providers.json` empty, which is its shipped state)
    silently ran EVERY role on claude. Measured on a real cockpit: six roles
    configured across codex/gemini/claude, all six resolving to claude, with
    no event saying so.

    Only a role with no explicit mapping reaches here, so this never
    overrides a provider the user chose in Providers & Roles — it fills in
    the case that used to be a silent claude default. Imported lazily to keep
    this module's import graph flat (same reason `_provider_available` does).
    """
    try:
        from . import role_models

        entry = role_models.raw_model_for(key)
    except Exception:
        return CLAUDE
    if not entry:
        return CLAUDE
    provider = (entry[0] or "").strip().lower()
    return provider if provider in PROVIDER_REGISTRY else CLAUDE


# `_provider_available`'s CLI-installed check (`custom_discovery_fn`) walks
# PATH doing a real filesystem probe per call (`shutil.which` under the
# hood) — cheap in isolation, but this is called every ~5s watchdog tick for
# every "working" codex/gemini pane across every open project
# (`Orchestrator._check_stuck_tool_panes`), fully uncached, directly on the
# Qt main thread. Confirmed root cause of recurring `main_thread_stall`
# events (0.7-2s+ each) via captured main-thread stack traces landing here
# through `_check_idle_teammates` → `_check_stuck_tool_panes` →
# `effective_provider_for` (2026-08-22 investigation). A short TTL cache is
# enough for THIS probe only — it only needs to be fresher than "the next
# watchdog tick", not real-time; a CLI (un)install takes effect within one
# TTL window instead of instantly, which is an acceptable trade for cutting
# a per-pane-per-tick disk probe down to once per TTL.
#
# The `is_disabled()` toggle check below is deliberately OUTSIDE this cache
# (#343 regression from the first cut of this fix): Settings → Providers &
# Roles' "→ Claude" substitute badge (`settings_window._sync_role_provider_badge`)
# calls `_provider_available()` synchronously on every combo change and
# expects the disable toggle to be reflected immediately, not up to
# `_PROVIDER_AVAILABLE_TTL_S` seconds late. `is_disabled()` is a small JSON-file
# read, not a PATH walk, so it doesn't need the cache the CLI probe does.
_PROVIDER_AVAILABLE_TTL_S = 15.0
_provider_cli_installed_cache: dict[str, tuple[float, bool]] = {}


def reset_provider_available_cache() -> None:
    """Clear the CLI-installed TTL cache. Test-only hook so one test's
    monkeypatched discovery result can't leak into the next via the
    module-global cache (production code relies on the TTL expiring on its
    own and never needs to call this)."""
    _provider_cli_installed_cache.clear()


def _provider_available(provider: str) -> bool:
    """True iff `provider` can actually run right now.

    Two ways a codex/gemini provider becomes unusable:
      1. Toggled off in Settings → Providers & Roles (`disabled-providers.json`).
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

    Only the CLI-installed probe is cached (per provider, for
    `_PROVIDER_AVAILABLE_TTL_S`) — see the module comment above this
    function for why the disable-toggle check below is deliberately not.
    """
    if provider == CLAUDE:
        return True

    # (1) user-intent toggle — always read live, never cached.
    try:
        from .provider_state import is_disabled

        if is_disabled(provider):
            return False
    except Exception:
        pass

    # (2) CLI actually installed — this probe IS TTL-cached.
    now = time.monotonic()
    cached = _provider_cli_installed_cache.get(provider)
    if cached is not None and now - cached[0] < _PROVIDER_AVAILABLE_TTL_S:
        return cached[1]

    result = _provider_cli_installed_uncached(provider)
    _provider_cli_installed_cache[provider] = (now, result)
    return result


def _provider_cli_installed_uncached(provider: str) -> bool:
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
    # Pane shards (for example ``codex#2``) share their base role's provider.
    role = re.sub(r"#\d+$", "", role or "")
    desired = provider_for(role, project)
    if desired == CLAUDE:
        return CLAUDE
    return desired if _provider_available(desired) else CLAUDE


# ── model-id family patterns (issue #127) ───────────────────────────────────
# Recognizable naming conventions for each provider's own model ids. Used to
# catch a --model override that is unambiguously from the WRONG provider (for
# example a claude-* id assigned to a role that maps to gemini/agy) instead of
# letting it through to spawn, where the CLI just warns "model ... is not
# recognized, using default" and silently falls back — the exact #127 report.
#
# Providers that route to many third-party backends by design (opencode's
# `-m provider/model` across 75+ integrations, cursor's multi-backend model
# picker) intentionally have NO entry here: almost any id can legitimately be
# valid for them, so they are never blocked or warned about by family.
_MODEL_ID_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    CLAUDE: (
        re.compile(r"^claude[-_]", re.IGNORECASE),
        re.compile(r"^(opus|sonnet|haiku|fable)([-_]|$)", re.IGNORECASE),
    ),
    CODEX: (
        re.compile(r"^gpt[-_]", re.IGNORECASE),
        re.compile(r"^o[1-9][a-z0-9-]*$", re.IGNORECASE),
        re.compile(r"^codex", re.IGNORECASE),
    ),
    GEMINI: (re.compile(r"^gemini[-_]", re.IGNORECASE),),
    KIMI: (
        re.compile(r"^k2([.\-_]|$)", re.IGNORECASE),
        re.compile(r"^kimi[-_]", re.IGNORECASE),
        re.compile(r"^moonshot", re.IGNORECASE),
    ),
}


def _model_family(model: str) -> str | None:
    """Return the provider whose naming pattern ``model`` matches, else None.

    ``None`` means either the id is genuinely new/unrecognized, or it belongs
    to a router provider with no pattern table — both are non-findings here,
    not a family match.
    """
    for provider, patterns in _MODEL_ID_PATTERNS.items():
        if any(p.match(model) for p in patterns):
            return provider
    return None


def assign_provider_override_error(
    provider: str | None,
) -> str | None:
    """Return a user-facing error when ``--provider`` cannot be honored.

    Validates a Lead-issued PER-ASSIGN escape hatch (issue #270): when a
    role's configured provider is stuck (boot-stalled CLI, broken MCP
    config, auth wall, ...) and no auto-degrade has fired yet, Lead can
    force this ONE assign's fresh spawn onto a different, working CLI
    without hand-editing role-providers.json (which needs a cockpit
    restart to take effect — see the module docstring). Only fires for a
    genuinely unusable target: an unknown provider name, or a known one
    that's disabled/not installed right now (that override would just fail
    the exact same way). Deliberately does NOT special-case forced-identity
    roles (codex/gemini/...) — the same pane-scoped substitution already
    happens automatically via ``PaneState.provider_override`` when their
    own CLI is unavailable (see :func:`effective_provider_for`); this just
    lets Lead trigger the same substitution manually and earlier.
    """
    normalized = str(provider or "").strip().lower()
    if not normalized:
        return None
    if normalized not in VALID_PROVIDERS:
        return (
            f"--provider '{normalized}' is not a known provider "
            f"(valid: {', '.join(sorted(VALID_PROVIDERS))})"
        )
    if not _provider_available(normalized):
        return (
            f"--provider '{normalized}' is not available right now (disabled in "
            "Settings → Providers & Roles, or its CLI isn't installed) — this "
            "override would just fail the same way; fix that first or pick a "
            "different provider"
        )
    return None


def assign_model_override_error(
    role: str,
    model: str | None,
    project: str | None = None,
    provider_override: str | None = None,
) -> str | None:
    """Return a user-facing error when ``--model`` cannot reach this role.

    Validation is intentionally based on the provider that will *actually*
    spawn, including provider substitution.  This keeps a per-assign model id
    from being silently ignored by a current or future provider that has no
    :class:`ProviderSpec` ``model_flag``, and (issue #127) blocks a model id
    that unambiguously belongs to a DIFFERENT provider's naming scheme (for
    example a claude-* id sent to a role that maps to gemini/agy). A model id
    that simply isn't recognized by any known pattern is NOT blocked here —
    see :func:`assign_model_override_warning` for that softer case, since a
    provider can ship a brand-new model id at any time.

    ``provider_override`` — issue #270: when the SAME assign also carries a
    validated ``--provider``, that provider is what will actually spawn, not
    the role's normal config/availability resolution. Pass it through so
    ``--model claude-opus-5 --provider claude`` on a codex-mapped role is
    validated against claude (and succeeds) instead of against codex (and
    wrongly blocked as a cross-provider id).
    """
    normalized = str(model or "").strip()
    if not normalized:
        return None

    from .provider_spec import PROVIDER_REGISTRY

    provider = (provider_override or "").strip().lower() or effective_provider_for(role, project)
    spec = PROVIDER_REGISTRY.get(provider)
    if spec is None:
        return (
            f"--model cannot be used for role '{role}': "
            f"effective provider '{provider}' is not registered"
        )
    if spec.model_flag is None:
        return (
            f"--model is not supported by provider '{provider}' "
            f"(role '{role}' has no ProviderSpec.model_flag)"
        )
    family = _model_family(normalized)
    if family is not None and family != provider and provider in _MODEL_ID_PATTERNS:
        display = spec.display_name or provider.capitalize()
        # #270: when the caller didn't already pass a --provider override,
        # point at the actual escape hatch instead of leaving Lead stuck —
        # the old message only said what was wrong, never what to do about it.
        hint = (
            ""
            if provider_override
            else f" · add --provider {family} to the same assign to force this"
        )
        return (
            f"--model '{normalized}' looks like a {family} model id, but role "
            f"'{role}' → provider '{provider}' ({display}); use a {provider} "
            f"model id instead of a {family} one (role→provider mapping: "
            f"'{role}' → '{provider}'){hint}"
        )
    return None


def assign_model_override_warning(
    role: str,
    model: str | None,
    project: str | None = None,
    provider_override: str | None = None,
) -> str | None:
    """Return a non-blocking heads-up when ``--model`` isn't a recognized id
    for the effective provider, but also isn't confidently from a different
    one (that case is :func:`assign_model_override_error`'s hard block, not
    this warning).

    Only fires for providers with a KNOWN naming scheme in
    ``_MODEL_ID_PATTERNS`` — router providers (opencode/cursor) accept model
    ids from many backends by design and are never warned about here.

    ``provider_override`` — see :func:`assign_model_override_error`'s
    docstring (issue #270); same "this is what will actually spawn" reasoning.
    """
    normalized = str(model or "").strip()
    if not normalized:
        return None

    from .provider_spec import PROVIDER_REGISTRY

    provider = (provider_override or "").strip().lower() or effective_provider_for(role, project)
    spec = PROVIDER_REGISTRY.get(provider)
    if spec is None or spec.model_flag is None:
        return None
    if provider not in _MODEL_ID_PATTERNS:
        return None
    family = _model_family(normalized)
    if family == provider:
        return None
    if family is not None:
        # Belongs to a different provider's naming scheme — that's
        # assign_model_override_error's hard block, not this softer warning.
        return None
    return (
        f"--model '{normalized}' does not match any known {provider} model id "
        f"pattern (role '{role}' → provider '{provider}'); continuing — if this "
        f"is a new model that's fine, but if it was meant for another provider "
        f"the CLI may silently fall back to its own default instead of using it"
    )


def assign_effort_override_error(
    role: str,
    effort: str | None,
    project: str | None = None,
    provider_override: str | None = None,
) -> str | None:
    """Return a user-facing error when ``--effort`` (issue #323) cannot reach
    this role.

    Validated against the provider that will *actually* spawn (including a
    ``--provider`` override on the same assign — same "what actually spawns"
    reasoning as :func:`assign_model_override_error`, issue #270).

    A provider with no ``ProviderSpec.effort_flag`` at all (opencode/kimi/
    cursor today — #103 gap, no CLI knob to inject one) is NOT an error here:
    issue #323's own acceptance criteria requires it degrade silently,
    exactly like :func:`spawn_engine._append_provider_effort` already no-ops
    for such a provider. Only a level string the provider's CLI does NOT
    accept is blocked, mirroring the model id family mismatch that
    :func:`assign_model_override_error` blocks. gemini/agy gained a real
    ``--effort`` knob (#125 fixed upstream in agy 1.1.10 — see
    ``provider_spec.gemini_spec``'s own comment) and is validated the same
    way claude/codex are.
    """
    normalized = str(effort or "").strip().lower()
    if not normalized:
        return None

    from .provider_spec import PROVIDER_REGISTRY

    provider = (provider_override or "").strip().lower() or effective_provider_for(role, project)
    spec = PROVIDER_REGISTRY.get(provider)
    if spec is None:
        return (
            f"--effort cannot be used for role '{role}': "
            f"effective provider '{provider}' is not registered"
        )
    if spec.effort_flag is None:
        return None
    if spec.effort_levels and normalized not in spec.effort_levels:
        display = spec.display_name or provider.capitalize()
        return (
            f"--effort '{normalized}' is not accepted by provider '{provider}' "
            f"({display}); valid levels: {', '.join(spec.effort_levels)}"
        )
    return None


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


def lead_capability_gap_for_provider(provider: str) -> list[str]:
    """Missing Lead-only capability labels if Lead were backed by `provider`.

    Pure function of a provider name — no role/project lookup, no disk
    read — so callers can warn reactively on an unsaved selection (e.g. the
    Roles-page Access tab's provider combo while the user is still editing
    a draft) without touching provider-overrides.json. `lead_capability_gap`
    below is the disk-backed sibling that resolves the role's *current*
    provider first, then delegates here.
    """
    from .provider_spec import PROVIDER_REGISTRY

    if provider == CLAUDE:
        return []
    spec = PROVIDER_REGISTRY.get(provider)
    if spec is None:
        return [label for _, label in _LEAD_CAPABILITY_LABELS]
    return [label for flag, label in _LEAD_CAPABILITY_LABELS if not getattr(spec, flag, False)]


def lead_capability_gap(project: str | None = None) -> tuple[str, list[str]] | None:
    """Return `(provider, [missing feature labels])` when Lead is currently
    backed by something other than claude, or `None` when it's claude (no
    gap).

    Used by the Settings UI (capability-warning badge on the Lead row) and
    the remote API (mobile mirror/history/resume responses) to tell the
    user WHY a claude-only feature is unavailable instead of silently doing
    nothing — issue #101 requires visible degradation, never a silent break.
    """
    provider = effective_provider_for("lead", project)
    if provider == CLAUDE:
        return None
    return provider, lead_capability_gap_for_provider(provider)


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
