"""Project + runtime config helpers."""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path

_log = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_SHARD_IDX = re.compile(r"^[1-9][0-9]{0,2}$")  # 1–999
DEFAULT_NPM_REGISTRY = "https://registry.npmjs.org/"

# Plugins cockpit wants spawned agents to inherit (skipping claude-obsidian's broken
# SessionStart hook). Each entry is a marketplace name under ~/.claude/plugins/cache/.
_SAFE_PLUGINS: tuple[str, ...] = (
    "superpowers-dev",
    "addy-agent-skills",
    "pordee",
    # Anthropic's official marketplace. The cockpit ships its dev-skill plugins
    # (frontend-design, code-review) into panes; the hook-heavy ones in the same
    # marketplace (remember, security-guidance — SessionStart command hooks that
    # add a 180s agent-SDK setup / per-tool memory writes) are filtered out of
    # pane injection by `lead_context._PANE_PLUGIN_DENYLIST` so they don't slow
    # spawns, while staying user-enabled for the user's own sessions.
    "claude-plugins-official",
    # UI/UX design-intelligence skill (design-system generator + BM25 KB). Its
    # own marketplace; scoped to design roles only via lead_context's
    # _ROLE_PLUGIN_POLICY so backend/devops/qa panes never pay for it.
    "ui-ux-pro-max-skill",
    # claude-obsidian-marketplace is intentionally excluded: the cached 1.4.3
    # build ships a SessionStart prompt-hook that crashed all panes in v0.2.0
    # (ToolUseContext required error). Until a spawn smoke-test under cockpit
    # flags confirms the hook no longer fires, do not add it here.
)


def npm_registry() -> str:
    """Registry used for public npm packages without changing user config."""
    return os.environ.get("TAKKUB_NPM_REGISTRY") or DEFAULT_NPM_REGISTRY


def validate_name(value: str, kind: str) -> str:
    """Normalise and validate a role or project name used as a path component.

    Lowercases and strips whitespace before matching so callers that already
    normalise their input still pass. Raises ValueError for anything that could
    escape the intended runtime subtree (traversal sequences, uppercase-only
    chars, spaces, empty string, …).

    Shard-instance keys like ``"qa#1"`` are accepted: the role part is validated
    with the usual regex and the numeric suffix must be 1–999.  The ``#`` is
    never used as a path separator so it cannot escape the runtime subtree.
    """
    name = (value or "").lower().strip()
    if ".." in name or name.startswith(".") or name.endswith("."):
        raise ValueError(f"invalid {kind}: {value!r}")
    if "#" in name:
        role_part, _, shard_part = name.partition("#")
        if (
            ".." in role_part
            or role_part.startswith(".")
            or role_part.endswith(".")
            or not _SAFE_NAME.fullmatch(role_part)
        ):
            raise ValueError(f"invalid {kind}: {value!r}")
        if not _SAFE_SHARD_IDX.fullmatch(shard_part):
            raise ValueError(f"invalid {kind} shard index: {value!r}")
        return name
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError(f"invalid {kind}: {value!r}")
    return name


def _write_json_atomic(path: Path, data: dict) -> bool:
    """Write *data* to *path* via a temp file so a crash mid-write never
    leaves a partial/corrupt JSON file behind. Return whether it persisted."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    # Windows can transiently reject replacement while an AV scanner or
    # another reader still has the destination open.  A bounded retry absorbs
    # that race without letting PermissionError escape a Qt save slot.
    for attempt, delay in enumerate((0.0, 0.02, 0.05, 0.1)):
        if delay:
            time.sleep(delay)
        try:
            tmp.replace(path)
        except PermissionError:
            if attempt < 3:
                continue
            _log.exception("could not replace %s after retries; keeping existing config", path)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        else:
            if sys.platform != "win32":
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            return True


REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_data_home() -> Path:
    """Where mutable *user data* (projects.json, runtime/) lives.

    Split from REPO_ROOT so a pip/npm install never writes user data inside
    ``site-packages`` (a pip upgrade could sweep it, and users can't find it):

    * ``AGENT_TAKKUB_HOME`` set  → honour it verbatim (tests, sandbox installs).
    * dev checkout (pyproject.toml + src/ next to REPO_ROOT) → REPO_ROOT, so a
      from-source run behaves EXACTLY as before (dev + existing tests untouched).
    * installed (config.py sits in …/venv/…/site-packages) → derive the isolated
      home from the ``venv`` ancestor (honours a custom home chosen at install
      time); fall back to the documented default ``~/.agent-takkub``.

    Structural, app-shipped paths (bin/, .claude/agents/, CLAUDE.md, git remote)
    stay on REPO_ROOT — only user data moves.
    """
    env = os.environ.get("AGENT_TAKKUB_HOME")
    if env:
        return Path(env)
    if (REPO_ROOT / "pyproject.toml").is_file() and (REPO_ROOT / "src").is_dir():
        return REPO_ROOT
    for parent in Path(__file__).resolve().parents:
        if parent.name == "venv":
            return parent.parent
    return Path.home() / ".agent-takkub"


DATA_HOME = _resolve_data_home()


def _resolve_settings_home() -> Path:
    """Where user-level cockpit *settings* live (user-profiles.json,
    exec-mode.json, pane-tools.json, disabled-providers.json, plan.json,
    projects/<slug>/ per-project files, cache/).

    Dev checkouts keep the historical ``~/.takkub`` (shared across checkouts
    on purpose). Installed builds keep settings inside DATA_HOME
    (``~/.agent-takkub``) so an installed cockpit NEVER shares mutable state
    with a dev checkout on the same machine — field report: the installed
    copy listed the dev machine's user profiles and couldn't switch them.
    """
    if DATA_HOME == REPO_ROOT:
        return Path.home() / ".takkub"
    return DATA_HOME


SETTINGS_HOME = _resolve_settings_home()


def is_installed_package() -> bool:
    """True when the cockpit runs from an installed wheel
    (…/site-packages/agent_takkub/…), not a dev source checkout (…/src/…).

    Steers the self-update UX: an installed build updates via its package
    manager (``npm update -g agent-takkub``), never by converting its
    site-packages folder into a git checkout of the (private) upstream repo.
    """
    return "site-packages" in Path(__file__).resolve().parts


def default_claude_config_dir() -> Path:
    """Default ``CLAUDE_CONFIG_DIR`` for *this cockpit instance's* implicit
    default Claude profile (the one a project uses when it hasn't picked a
    named profile — see ``user_profile.py``).

    * dev checkout (``DATA_HOME == REPO_ROOT``) → ``~/.claude``, unchanged.
    * installed build → isolated under ``DATA_HOME`` (e.g.
      ``~/.agent-takkub/claude-config``) so a prod cockpit never shares
      session history/auth/plugins with a dev checkout's ``~/.claude``
      running on the same machine (see
      docs/audit/2026-07-05-isolation-plan-crosscheck-codex.md, finding C5).

    Computed fresh on every call (never cached) so tests can monkeypatch
    ``Path.home`` / ``config.DATA_HOME`` and see it reflected immediately.
    """
    if DATA_HOME == REPO_ROOT:
        return Path.home() / ".claude"
    return DATA_HOME / "claude-config"


# ── Non-claude provider homes (user directive 2026-08-19) ─────────────────
#
# "ทุกอย่างที่รันบน cockpit ที่เป็น prod ให้เก็บใน DATA_HOME ให้หมด."
#
# claude has had this since the C5 isolation audit (``default_claude_config_dir``
# above → ``CLAUDE_CONFIG_DIR``); every other provider was still writing to its
# own OS-wide home, so an installed cockpit shared codex/opencode sessions and
# auth with a dev checkout and with the user's own hand-run CLI. This is the
# single source of truth for where an installed build puts each one.
#
# Read by BOTH sides on purpose:
#   * spawn  — `pane_env.inject_provider_home_env` exports these into the pane.
#   * mirror — `codex_helper.codex_sessions_root` / `opencode_helper` resolve
#     transcripts through the same function.
# Splitting those two would point the Remote mirror at a directory no pane
# writes to — silent-blank-phone, the exact class of bug this release fixes.
#
# Dev checkouts (``DATA_HOME == REPO_ROOT``) are deliberately excluded: a
# from-source run keeps using the plain provider defaults, unchanged.
_PROVIDER_HOME_SUBDIRS: dict[str, dict[str, str]] = {
    # `CODEX_HOME` is Codex's own documented knob (74 references in the
    # 0.147 binary) — sessions/, auth.json and config.toml all move with it.
    "codex": {"CODEX_HOME": "codex-home"},
    # OpenCode has no single home var: it reads the XDG pair (verified in the
    # shipped binary), storing `opencode.db` under XDG_DATA_HOME/opencode and
    # its config under XDG_CONFIG_HOME/opencode. Both are scoped to the
    # OpenCode pane only — never exported cockpit-wide, or every other XDG
    # tool in a pane (gh, uv, …) would silently lose its config too.
    "opencode": {
        "XDG_DATA_HOME": "opencode-home/data",
        "XDG_CONFIG_HOME": "opencode-home/config",
    },
}

# Providers with NO isolation knob, kept explicit so the gap is visible
# instead of looking like an oversight (#103). Surfaced by `takkub doctor`.
PROVIDER_ISOLATION_GAPS: dict[str, str] = {
    "gemini": (
        "gemini-cli joins the constant `.gemini` onto os.homedir() and exposes no "
        "directory env var — isolating it would need a full HOME override"
    ),
    "kimi": "no documented home/config env var found in the shipped kimi binary",
    "cursor": "cursor-agent home layout not yet verified on this machine",
}


def provider_home_env(provider: str) -> dict[str, str]:
    """Env overrides that move *provider*'s state into DATA_HOME.

    Empty for dev checkouts, for claude (handled by ``CLAUDE_CONFIG_DIR``),
    and for any provider with no isolation knob (``PROVIDER_ISOLATION_GAPS``).
    Never creates directories — the provider CLI makes its own, and a
    read-side caller must be able to ask "where would it be?" without
    causing a write.
    """
    if DATA_HOME == REPO_ROOT:
        return {}
    subdirs = _PROVIDER_HOME_SUBDIRS.get(str(provider or "").strip().lower())
    if not subdirs:
        return {}
    return {var: str(DATA_HOME / sub) for var, sub in subdirs.items()}


def provider_home_dir(provider: str, var: str) -> Path | None:
    """The isolated directory *provider* uses for *var*, or None when this
    build/provider has no isolated home (caller falls back to the OS default).
    """
    value = provider_home_env(provider).get(var)
    return Path(value) if value else None


def instance_display_version() -> str:
    """Best-effort version string for the window title / audit breadcrumb.

    Installed builds resolve via ``importlib.metadata`` (the package really
    is installed under that name). Dev checkouts read ``pyproject.toml``
    directly since a from-source run has no installed distribution record
    to query (mirrors the version-chip fallback in ``update_panel.py``).
    """
    if DATA_HOME == REPO_ROOT:
        try:
            text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
            m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if m:
                return m.group(1)
        except OSError:
            pass
        return "0.0.0"
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("agent-takkub")
    except Exception:
        return "?"


def instance_identity_label() -> str:
    """Short identity tag distinguishing this running instance — shown in the
    window title so an installed cockpit and a dev checkout (or two different
    dev checkouts) open side by side are never mistaken for each other.

    A dev checkout still needs the ``dev · <repo>`` tag since two checkouts
    can run side by side with the same version. An installed build only
    needs its version — the literal word "prod" added no information the
    version number didn't already carry.

    The ``<repo>`` name prefers ``TAKKUB_PROJECT`` (the orchestrator-issued,
    registry-backed project namespace stamped into every cockpit-spawned
    pane's env — Lead and teammates alike, see ``spawn_engine.py``) over
    ``REPO_ROOT.name``. A teammate pane spawned with ``--cwd`` pointing at a
    worktree checkout still carries the *same* instance's canonical project
    name via that env var, whereas ``REPO_ROOT`` is only this particular CLI
    invocation's own on-disk resolution and isn't guaranteed to match (#185:
    a worktree-scoped pane showed the worktree's basename here instead of
    the actual project name, making Lead think it only controlled part of
    the fleet). Manual terminal invocations have no ``TAKKUB_PROJECT`` set
    and keep falling back to ``REPO_ROOT.name`` unchanged."""
    if DATA_HOME == REPO_ROOT:
        name = os.environ.get("TAKKUB_PROJECT") or REPO_ROOT.name
        return f"dev · {name}"
    return f"v{instance_display_version()}"


def instance_window_title() -> str:
    """Full ``agent-takkub …`` window-title identity segment.

    Dev checkouts keep the bracketed tag (``agent-takkub [dev · repo]``) so
    it reads as a qualifier; an installed build shows the version bare
    (``agent-takkub v1.0.14``) since there's nothing to disambiguate it from."""
    label = instance_identity_label()
    if DATA_HOME == REPO_ROOT:
        return f"agent-takkub [{label}]"
    return f"agent-takkub {label}"


def _resolve_assets_root() -> Path:
    """Where app-shipped, read-only assets live: the cockpit ``CLAUDE.md``
    and ``.claude/agents/*.md`` role files that get materialised into every
    Lead/teammate spawn prompt.

    * dev checkout (``DATA_HOME == REPO_ROOT``) → ``REPO_ROOT`` unchanged —
      dev behaviour must not shift at all.
    * installed (pip/npm wheel) → ``REPO_ROOT`` resolves into an empty venv
      ancestor (``…/venv/Lib``) that ships none of the repo's structural
      files, so assets are shipped inside the package itself instead, at
      ``agent_takkub/_assets/`` (see ``package_data`` in pyproject.toml).
    """
    if DATA_HOME == REPO_ROOT:
        return REPO_ROOT
    return Path(__file__).resolve().parent / "_assets"


ASSETS_ROOT = _resolve_assets_root()


def _resolve_cli_bin_dir() -> Path:
    """Where the ``takkub`` CLI binary that spawned panes should PATH-prefer
    lives — so a pane always dials back into *this* running cockpit instead
    of a different ``takkub`` checkout that happens to sit earlier on the
    user's PATH (code-version skew).

    * dev checkout → ``REPO_ROOT/bin`` (``bin/takkub`` + ``bin/takkub.cmd``),
      unchanged.
    * installed → the venv's own console-script directory (``Scripts/`` on
      Windows, ``bin/`` on macOS/Linux), derived from ``sys.executable`` so
      it always tracks whichever venv this process is actually running in.
    """
    if DATA_HOME == REPO_ROOT:
        return REPO_ROOT / "bin"
    return Path(sys.executable).resolve().parent


CLI_BIN_DIR = _resolve_cli_bin_dir()

PROJECTS_JSON = DATA_HOME / "projects.json"
AGENTS_DIR = ASSETS_ROOT / ".claude" / "agents"


def _resolve_skills_dir() -> Path:
    """Where the shipped skill bundle physically lives (Phase 5a, epic
    #309 Capability Hub — a skill store is provider-neutral, not
    claude-only, so it moved out from under ``.claude/``; see
    ``docs/v2/REUSE_VS_REWRITE_MATRIX.md`` §3 "Skill store path").

    * new layout → ``ASSETS_ROOT/capabilities/skills`` — the real,
      git-tracked storage. ``.claude/skills`` is kept only as a per-skill
      junction/symlink SURFACE so claude's own Skill tool (which
      auto-discovers ``.claude/skills`` from cwd) and every existing
      `skill_scan.scan_skills` caller — none of which changed — keep
      finding the exact same skills at the path they already look at.
      See `core.capabilities.skill_store.ensure_shipped_skill_surface`.
    * legacy fallback → ``ASSETS_ROOT/.claude/skills`` when the new dir
      doesn't exist yet (an older installed build that shipped before
      this migration) — never a hard break.
    """
    new_dir = ASSETS_ROOT / "capabilities" / "skills"
    if new_dir.is_dir():
        return new_dir
    return ASSETS_ROOT / ".claude" / "skills"


# Default (built-in) skill bundle shipped alongside the role files — same
# read path as AGENTS_DIR (dev checkout: repo root; installed: staged wheel
# data under _assets/, see setup.py's _stage_assets). Reference material for
# the New Role / Skill Catalog pickers (`skill_scan.scan_skills`); unlike
# AGENTS_DIR nothing at spawn time depends on it being present.
SKILLS_DIR = _resolve_skills_dir()
# Writable counterpart to AGENTS_DIR for user-created custom roles (A6). AGENTS_DIR
# sits under ASSETS_ROOT, which is READ-ONLY app-shipped assets on an installed
# build (see `_resolve_assets_root`) — a custom role's .md file can never be
# written there. SETTINGS_HOME is always a per-user, writable dir on both dev
# and installed builds, so custom role files live at SETTINGS_HOME/agents/
# instead. `agent_role_dir()` checks AGENTS_DIR first, then this dir.
CUSTOM_AGENTS_DIR = SETTINGS_HOME / "agents"
RUNTIME_DIR = DATA_HOME / "runtime"
PORT_FILE = RUNTIME_DIR / "port"
EVENTS_LOG = RUNTIME_DIR / "events.log"
# Central home for custom *skills* a project's New-Skill form creates —
# the writable counterpart of the read-only shipped `SKILLS_DIR` bundle,
# mirroring how CUSTOM_AGENTS_DIR is the writable counterpart of AGENTS_DIR
# for custom roles. Skills MUST still be discoverable at `<project>/.claude/
# skills/<name>` (claude auto-discovers from cwd; codex/agy walk up), so a
# per-skill junction (win) / symlink (mac) links the project path back here.
# Keeping the real files central means the New-Skill button never dirties the
# user's repo (`git status` stays clean, nothing to accidentally commit).
PROJECT_SKILLS_HOME = DATA_HOME / "project-skills"
# Central home for LLM-authored docs (design-review / reviews / guides /
# system-overview) a pane produces per the CLAUDE.md routing. Panes read the
# effective path from the `TAKKUB_DOCS_DIR` env var (`pane_env`), so the
# instruction wording points at `$TAKKUB_DOCS_DIR/...` instead of a repo-
# relative `docs/...` that would land inside the user's project.
DOCS_DIR = RUNTIME_DIR / "docs"


def ensure_runtime() -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return RUNTIME_DIR


def project_skills_dir(project_ns: str) -> Path:
    """Central storage dir for one project's custom skills:
    ``PROJECT_SKILLS_HOME/<project_ns>``.

    Each skill lives at ``<this>/<name>/SKILL.md``; the project side gets a
    junction/symlink ``<project>/.claude/skills/<name>`` pointing here so
    every CLI still discovers it from cwd. ``project_ns`` is validated as a
    path component (same rule as roles/projects) so a crafted name can't
    escape ``PROJECT_SKILLS_HOME``.
    """
    ns = validate_name(project_ns, "project")
    base = PROJECT_SKILLS_HOME.resolve()
    d = (PROJECT_SKILLS_HOME / ns).resolve()
    if d != base and base not in d.parents:
        raise ValueError(f"project-skills path escapes home: {project_ns!r}")
    return d


def project_docs_dir(project_ns: str) -> Path:
    """Central per-project docs dir: ``RUNTIME_DIR/docs/<project_ns>``.

    Stamped into every pane's env as ``TAKKUB_DOCS_DIR`` (see
    ``pane_env._apply_artifacts_dir``) so design-review/reviews/guides/
    system-overview markdown+html land out of the user's repo. Same
    traversal-safe validation as ``project_skills_dir``.
    """
    ns = validate_name(project_ns, "project")
    base = DOCS_DIR.resolve()
    d = (DOCS_DIR / ns).resolve()
    if d != base and base not in d.parents:
        raise ValueError(f"docs path escapes home: {project_ns!r}")
    return d


def load_projects() -> dict:
    empty = {"active": None, "projects": {}}
    if not PROJECTS_JSON.exists():
        return empty
    try:
        data = json.loads(PROJECTS_JSON.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _log.warning("could not read projects.json (%r) — falling back to empty project list", exc)
        return empty
    if not isinstance(data, dict):
        _log.warning("projects.json root is not an object — falling back to empty project list")
        return empty
    return data


def active_project() -> tuple[str | None, dict]:
    """Return (project_name, project_dict) for the active project, or (None, {})."""
    data = load_projects()
    name = data.get("active")
    if not name:
        return None, {}
    proj = data.get("projects", {}).get(name, {})
    return name, proj


def list_project_names() -> list[str]:
    """All known project names from projects.json."""
    return list(load_projects().get("projects", {}).keys())


def project_folder_exists(name: str | None) -> bool:
    """True if the project's Lead working directory still exists on disk.

    A project can stay listed in projects.json after its folder is deleted
    out from under the cockpit. Spawning Lead into that missing cwd hangs the
    ConPTY backend (see `_pty_backend.spawn_pty`), so boot/tab-restore use this
    to skip dead projects instead of spawning into a vanished directory.
    Unknown name or a project with no configured paths → False.
    """
    if not name:
        return False
    cwd = lead_cwd(name)
    return bool(cwd) and Path(cwd).is_dir()


# preferred path key per role (first match wins; falls back to first path)
_ROLE_PATH_PREFS: dict[str, tuple[str, ...]] = {
    "frontend": ("web", "client", "frontend"),
    "backend": ("api", "server", "backend"),
    "mobile": ("mobile", "app", "web"),
    "devops": ("api", "infra", "ci", "ops"),
    "designer": ("web", "client", "design"),
    "critic": ("web", "client", "design"),
    "qa": ("web", "api"),
    "reviewer": ("api", "web"),
}


def _project_dict(project: str | None) -> dict:
    """Resolve a project name to its dict from projects.json. Falls
    back to the active project when `project` is None — centralises
    the "by-name vs. active" pattern so callers below can take an
    optional project arg and stay project-scoped under multi-tab
    workflows (a spawn coming from Lead in tab A shouldn't read
    tab B's paths just because tab B is the focused tab).
    """
    if project:
        data = load_projects()
        return data.get("projects", {}).get(project, {}) or {}
    _, proj = active_project()
    return proj


def default_cwd_for_role(role_name: str, project: str | None = None) -> str | None:
    """Return the path from `project` (default: active project) that
    best matches the role, falling back to that project's first
    listed path. None if no project is resolved.

    Passing `project` explicitly is what makes multi-tab workflows
    safe: when Lead in tab A calls `takkub assign --role frontend`,
    the orchestrator passes its project namespace here so the cwd
    comes from tab A's paths even if tab B is focused.
    """
    proj = _project_dict(project)
    paths = proj.get("paths", {})
    if not paths:
        return None
    # per-project role→path-key override: lets a single project route a
    # role to a non-default folder (e.g. tak-game devops→deployment)
    # WITHOUT touching the global `_ROLE_PATH_PREFS` shared by every other
    # project. Only projects that declare `role_paths` are affected.
    override_key = (proj.get("role_paths") or {}).get(role_name)
    if override_key and override_key in paths:
        return paths[override_key]
    for k in _ROLE_PATH_PREFS.get(role_name, ()):
        if k in paths:
            return paths[k]
    return next(iter(paths.values()))


def preset_roles_for_active() -> list[str]:
    """List of role names to auto-spawn on cockpit startup, from the active
    project's `presets` field in projects.json. Empty if not configured."""
    _, proj = active_project()
    raw = proj.get("presets") or []
    return [str(x).strip().lower() for x in raw if str(x).strip()]


def lead_cwd(project: str | None = None) -> str | None:
    """Where Lead should spawn.

    Priority:
      1. The project's `lead` path key (explicit pick), e.g.
         `"lead": "web"` reuses paths.web.
      2. The shared parent of all configured project paths (`app/` for
         `app-web` + `app-api`), if that parent exists on disk.
      3. The project's first listed path (often `web`).
    Returns None if no project is resolved.

    Passing `project` explicitly is what makes multi-tab workflows
    safe: each tab's Lead respawn picks up its own paths even when
    a different tab is focused.
    """
    import os

    proj = _project_dict(project)
    paths = proj.get("paths") or {}
    if not paths:
        return None

    def _absolutize(p: str) -> str:
        # L6: a raw configured path can be relative (e.g. a hand-edited
        # projects.json entry, or `os.path.commonpath()` below collapsing to
        # a relative prefix). Handing a relative string straight to the
        # native spawn call resolves it against the *cockpit process's* cwd,
        # not the project — Lead silently lands in the wrong directory.
        # `expanduser().resolve()` absolutizes it at the source so every
        # caller of `lead_cwd()` gets a real, unambiguous path.
        try:
            return str(Path(p).expanduser().resolve())
        except OSError:
            return p

    # 1. explicit lead path key
    lead_key = proj.get("lead")
    if isinstance(lead_key, str) and lead_key in paths:
        return _absolutize(paths[lead_key])

    # 2. common parent of all paths
    try:
        common = os.path.commonpath([str(p) for p in paths.values()])
        if common and Path(common).is_dir() and Path(common).parent != Path(common):
            return _absolutize(common)
    except ValueError:
        pass

    # 3. first listed path
    return _absolutize(next(iter(paths.values())))


def set_active_project(name: str) -> bool:
    """Write a new active project name back to projects.json. Returns True if
    the name was valid and persisted, False otherwise."""
    data = load_projects()
    if name not in data.get("projects", {}):
        return False
    data["active"] = name
    return _write_json_atomic(PROJECTS_JSON, data)


def clear_active_project() -> None:
    """Clear `active` in projects.json (e.g. the last open tab was closed).

    `active_project()` already treats a missing/empty `active` as "no active
    project" (`if not name: return None, {}`), so writing `None` here is safe
    for old readers too — they'll just see no active project instead of a
    stale name pointing at a project with no open tab."""
    data = load_projects()
    data["active"] = None
    _write_json_atomic(PROJECTS_JSON, data)


def get_open_tabs() -> list[str]:
    """Project names of every tab the user wants restored on next launch.

    Backwards-compatible reader: if `open_tabs` isn't in projects.json
    (file pre-dates the multi-tab refactor) we synthesise it from
    `active` so old configs open the active project as a single tab.
    Orphaned names (project no longer exists in `projects`) are filtered
    out silently — the cockpit logs a status-bar warning at startup.
    """
    data = load_projects()
    raw = data.get("open_tabs")
    known = set(data.get("projects", {}).keys())
    if isinstance(raw, list):
        return [n for n in raw if isinstance(n, str) and n in known]
    active = data.get("active")
    if isinstance(active, str) and active in known:
        return [active]
    return []


def set_open_tabs(names: list[str]) -> None:
    """Persist the current tab order. Dedupes while preserving first-seen
    order so a stray double-add doesn't corrupt the saved list."""
    data = load_projects()
    seen: set[str] = set()
    cleaned: list[str] = []
    known = set(data.get("projects", {}).keys())
    for n in names:
        if not isinstance(n, str):
            continue
        if n in seen or n not in known:
            continue
        seen.add(n)
        cleaned.append(n)
    data["open_tabs"] = cleaned
    _write_json_atomic(PROJECTS_JSON, data)


# Central dev-server hygiene appended to EVERY role's materialised CLAUDE.md so
# the rule applies to all roles in all projects from one place. Born from a
# `next dev` postcss-worker leak that piled up to 3170 node procs / 18 GB: HMR
# dev servers fork a worker per compile and leak them, and a force-closed pane
# can orphan the whole tree. `next build && next start` has no per-compile worker
# churn, so it's the default for verify/smoke runs.
_DEV_SERVER_HYGIENE = (
    "\n\n## รัน web/dev server (กฎกลาง — ทุกโปรเจค)\n"
    "- **เพื่อ verify / smoke test หน้าเว็บ Next: ใช้ `next build && next start` "
    "ไม่ใช่ `next dev`.** `next dev` (HMR) fork postcss/jest-worker subprocess "
    "ต่อ compile แล้ว leak — เคยพอกถึง ~3170 node proc / 18 GB. "
    "`next build && next start` ไม่มี worker churn นั้น.\n"
    "- `next dev` ใช้เฉพาะตอน **iterative UI dev ที่ต้องการ HMR จริงๆ** เท่านั้น "
    "และต้อง background (`&` + redirect หรือ `nohup`) + **ปิด server เมื่อเสร็จงาน** "
    "อย่าทิ้งค้าง.\n"
    "- หลักเดียวกันกับ dev server อื่น (vite, `nest --watch`, `pnpm dev`): "
    "background เสมอ ห้าม foreground + ปิดเมื่อจบ.\n"
)


# Central non-interactive shell hygiene appended to EVERY role's CLAUDE.md
# so commands inside panes never block on y/N or credential prompts.
# Born from issue #52 where npx's 'Ok to proceed? (y)' or a git credential
# prompt caused the pane to hang permanently with the watchdog unable to
# distinguish it from "idle, forgot takkub done".
_NON_INTERACTIVE_HYGIENE = (
    "\n\n## รัน shell แบบ non-interactive (กฎกลาง — ทุกโปรเจค)\n"
    "- **ห้ามรันคำสั่งที่รอ y/N จาก user**: "
    "`npx` → ใช้ `npx --yes <pkg>`, "
    "`npm install` → `npm ci` หรือ `npm install --yes`, "
    "`git` → ต้อง cache credential ก่อน (`GIT_TERMINAL_PROMPT=0` inject อัตโนมัติ → git fail แทน prompt)\n"
    "- **อย่าใช้ `npx <pkg>` ตรงๆ** ถ้า pkg ยังไม่ติดตั้ง — "
    "npx จะถาม 'Ok to proceed? (y)' แล้วปิดกั้น pane ถาวร; "
    "ใช้ test runner ของโปรเจค (`pytest`, `jest`, `vitest`, `pnpm test`) แทนเสมอ\n"
    "- คำสั่งที่รอ 'Press any key', 'Are you sure', 'Overwrite?' → "
    "ต้องผ่าน flag `--force` / `--yes` / `--no-interaction` "
    "หรือ pipe `yes |` ก่อนเรียก\n"
)


# Token-discipline hygiene appended to EVERY role's CLAUDE.md (token-reduction
# wave 4, 2026-08-13). Unlike the boot-time trims in the same wave (which cut
# what gets INJECTED at spawn), this shapes behaviour for the rest of the
# session: redundant reads / speculative tool calls / large raw output pulled
# into the main context all get re-charged as cache_read on every later turn,
# the same mechanism BIG_FILE_GUARD documents for whole-file reads. Kept to a
# few lines on purpose — a discipline reminder that itself bloats the prompt
# defeats the point. Inspired by the "skip redundant reads / kill speculative
# calls / route large output away from context" pattern from the
# russelleNVy/three-man-team project (see docs/audit/2026-08-13-token-
# reduction-wave4.md).
_TOKEN_DISCIPLINE_HYGIENE = (
    "\n\n## วินัยโทเคน (กฎกลาง — ทุก session)\n"
    "- **ก่อน Read ไฟล์ที่อ่านไปแล้วในเทิร์นนี้/ก่อนหน้า** — ใช้เนื้อหาที่มีอยู่ใน "
    "context แทน อย่าอ่านซ้ำ (เว้นแต่ไฟล์ถูกแก้ไประหว่างนั้นจริงๆ)\n"
    "- **อย่าเรียก tool แบบเดา** ('เผื่อมีข้อมูล') — เรียกเมื่อมีเหตุผลจากงานจริงเท่านั้น "
    "แต่ละ tool call ที่ไม่จำเป็นคือ context ที่ค้างและถูกชาร์จซ้ำทุก turn ถัดไป\n"
    "- **ผลลัพธ์ก้อนใหญ่** (log เต็ม, dump, ผลสแกนทั้งไดเรกทอรี) **→ เขียนลงไฟล์แล้วสรุปสั้นๆ "
    "กลับมาในคำตอบ** อย่าดึงก้อนดิบเข้า context หลักถ้าไม่จำเป็นต้องอ่านทั้งหมด\n"
)


# Role capability declaration (#250): single source of truth for "which
# roles are PROVEN to never need guard X", read by both this module
# (_DEV_SERVER_HYGIENE, below) and spawn_engine.py (STALE_FILE_GUARD, added
# to the SAME staged CLAUDE.md later in the same spawn — see that module's
# `_appendix` build next to `agent_role_dir(base_role)`). Deliberately
# fail-safe: only role names actually verified — by reading their
# `.claude/agents/<role>.md` (2026-08-15 audit, not guessed from the role
# name) — to have no way of needing a guard are listed here. Every other
# name, including a registered role this table hasn't been taught about yet
# or an A6 custom role (never appears in either set, `CUSTOM_AGENTS_DIR`),
# falls through to "needs it". Do NOT add a role to either set without first
# reading its role file: `critic`/`designer`/`qa` all drive a browser
# against a live app and may have to bring it up themselves, so they stay
# OUT of `_NO_DEV_SERVER_ROLES` even though they don't run `next dev`
# directly — cutting the guard there would resurrect the worst failure mode
# it exists to prevent (a dev server left running foreground, wedging an
# unattended pane overnight).
_NO_DEV_SERVER_ROLES: frozenset[str] = frozenset(
    {
        "reviewer",  # Read/Bash only — reviews code, never runs/builds the app
        "gemini",  # claude-substitute: Read/Bash only, planning/second-opinion
        "docs",  # writes docs, never runs/builds the app
        "analyst",  # writes specs, never runs/builds the app
        "security",  # writes findings, never runs/builds the app
        "codex",  # claude-substitute: refactor/cross-check, no dev-server step
        "opencode",  # claude-substitute: same shape as codex
        "kimi",  # claude-substitute: same shape as codex
        "cursor",  # claude-substitute: same shape as codex
    }
)

# Roles whose role file grants no Write/Edit tool at all (Read/Bash only),
# so the "File has been modified since read" race STALE_FILE_GUARD warns
# about can never fire for them — there is no Edit/Write call to race.
_NO_FILE_EDIT_ROLES: frozenset[str] = frozenset({"reviewer", "gemini"})


def role_needs_dev_server_guard(role: str) -> bool:
    """True unless *role* is proven (`_NO_DEV_SERVER_ROLES`) to never run or
    build a dev server. Fail-safe: an unrecognised or custom role name is
    always assumed to need the guard."""
    return role not in _NO_DEV_SERVER_ROLES


def role_needs_stale_file_guard(role: str) -> bool:
    """True unless *role* is proven (`_NO_FILE_EDIT_ROLES`) to hold no
    Edit/Write tool at all. Fail-safe: an unrecognised or custom role name
    is always assumed to need the guard."""
    return role not in _NO_FILE_EDIT_ROLES


def agent_role_dir(role: str) -> Path:
    """Per-role staging dir under runtime/agents/<role>/.

    A copy of `.claude/agents/<role>.md` is materialised here as CLAUDE.md so
    claude reads the specialist role definition before any task arrives. A
    central dev-server-hygiene block (`_DEV_SERVER_HYGIENE`) is appended to
    every role PROVEN to need it (`role_needs_dev_server_guard`, #250) so the
    `next build && next start` rule applies cockpit-wide from one place.
    """
    role = validate_name(role, "role")
    base = (RUNTIME_DIR / "agents").resolve()
    d = (RUNTIME_DIR / "agents" / role).resolve()
    if d != base and base not in d.parents:
        raise ValueError(f"role path escapes runtime: {role!r}")
    d.mkdir(parents=True, exist_ok=True)
    src = AGENTS_DIR / f"{role}.md"
    if not src.exists():
        # Not a built-in role file — check the writable custom-role dir
        # (A6: user-created roles via the Role Manager dialog).
        custom_src = CUSTOM_AGENTS_DIR / f"{role}.md"
        if custom_src.exists():
            src = custom_src
    if src.exists():
        # strip frontmatter (between leading --- and the next ---)
        text = src.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                text = text[end + 4 :].lstrip()
        (d / "CLAUDE.md").write_text(
            text.rstrip()
            + (_DEV_SERVER_HYGIENE if role_needs_dev_server_guard(role) else "")
            + _NON_INTERACTIVE_HYGIENE
            + _TOKEN_DISCIPLINE_HYGIENE,
            encoding="utf-8",
        )
    return d


def _get_port_file() -> Path:
    """Return the effective port file path (TAKKUB_PORT_FILE overrides default)."""
    override = os.environ.get("TAKKUB_PORT_FILE", "").strip()
    return Path(override) if override else PORT_FILE


def write_port(port: int) -> None:
    ensure_runtime()
    _get_port_file().write_text(str(port), encoding="utf-8")


def read_port() -> int | None:
    p = _get_port_file()
    if p.exists():
        try:
            return int(p.read_text(encoding="utf-8").strip())
        except ValueError:
            return None
    return None


def find_claude_executable() -> str:
    """Locate the claude CLI executable.

    On Windows we prefer the real `claude.exe` (in node_modules/.../bin) over
    `claude.cmd` because the .cmd wrapper spawns a visible cmd.exe console
    window when invoked through pywinpty/ConPTY.

    Order:
      1. CLAUDE_EXE env var
      2. node_modules `.../claude-code/bin/claude.exe` resolved via PATH
      3. PATH lookup for claude.exe (rare standalone binary)
      4. PATH lookup for claude.cmd (fallback, will show console)
      5. nvm4w default install path
    """
    env = os.environ.get("CLAUDE_EXE")
    if env and Path(env).exists():
        return env

    from shutil import which

    # Try to resolve via the .cmd's installed directory to find the real .exe
    cmd_path = which("claude.cmd") or which("claude")
    if cmd_path:
        cmd_dir = Path(cmd_path).resolve().parent
        candidates = [
            cmd_dir / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
        ]
        for cand in candidates:
            if cand.exists():
                return str(cand)

    # standalone claude.exe in PATH
    direct = which("claude.exe")
    if direct:
        return direct

    # last resort: the .cmd wrapper (will trigger cmd.exe console window)
    if cmd_path:
        return cmd_path

    # Platform-specific default install locations (when not on PATH).
    # Field incident 2026-07-04: a Node update dropped %APPDATA%\npm from the
    # user PATH — claude vanished and every pane spawn failed. These probes must
    # cover every npm-prefix layout we've seen so the cockpit survives a broken
    # PATH; _heal_process_path() then repairs PATH for this process + children.
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        roots = [
            Path("C:/nvm4w/nodejs"),  # nvm4w
            Path(appdata) / "npm",  # classic npm prefix
            Path("C:/Program Files/nodejs"),  # system node
        ]
        for root in roots:
            exe = root / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            if exe.exists():
                return _heal_process_path(str(exe))
        for root in roots:
            cmd = root / "claude.cmd"
            if cmd.exists():
                return _heal_process_path(str(cmd))
    else:
        # macOS / Linux: common npm-global and Homebrew bin locations, plus the
        # native installer's ~/.local/bin and ~/.claude/local fallbacks.
        for cand in (
            Path.home() / ".local" / "bin" / "claude",
            Path.home() / ".claude" / "local" / "claude",
            Path.home() / "bin" / "claude",
            Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/bin/claude"),
        ):
            if cand.exists():
                return _heal_process_path(str(cand))

    raise RuntimeError(
        "Could not locate claude CLI. Install Claude Code or set CLAUDE_EXE env var."
    )


def _node_version_key(bin_dir: str) -> tuple[int, int, int]:
    """Version-aware sort key for an nvm-style ``.../vX.Y.Z/bin`` path.

    Plain lexicographic sort puts ``v9.x`` after ``v20.x`` (``'9' > '2'``),
    silently picking an older Node major. Parses the version out of the
    parent directory name and sorts numerically instead.
    """
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", os.path.basename(os.path.dirname(bin_dir)))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _gui_binary_dirs() -> list[str]:
    """Platform-specific directories to probe for CLI binaries (node/npm/
    codex/agy/...) that a process launched without an inherited shell PATH
    (e.g. a GUI app opened from Finder/Dock/Launchpad on macOS) would
    otherwise miss. Entries containing ``*`` are glob patterns expanded by
    the caller.
    """
    if sys.platform == "darwin":
        return [
            "/opt/homebrew/bin",  # Apple Silicon Homebrew
            "/usr/local/bin",  # Intel Homebrew
            os.path.expanduser("~/.nvm/versions/node/*/bin"),
            os.path.expanduser("~/.fnm/current/bin"),
            os.path.expanduser("~/.n/bin"),
            os.path.expanduser("~/.local/bin"),
            os.path.expanduser("~/.asdf/shims"),
            os.path.expanduser("~/.volta/bin"),
            "/usr/bin",
        ]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        localappdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        return [
            os.path.join(appdata, "npm"),
            os.path.join(program_files, "nodejs"),
            os.path.join(program_files_x86, "nodejs"),
            os.path.join(localappdata, "Volta", "bin"),
            os.path.join(localappdata, "fnm_multishells", "*"),
        ]
    # linux: not an officially supported desktop target, but CI runs
    # ubuntu-latest — this must never raise there.
    return [
        os.path.expanduser("~/.nvm/versions/node/*/bin"),
        os.path.expanduser("~/.local/bin"),
        "/usr/local/bin",
        "/usr/bin",
    ]


def _expand_gui_binary_dirs() -> list[str]:
    """Expand `_gui_binary_dirs()` globs (newest Node version first) and
    drop directories that don't exist."""
    expanded: list[str] = []
    for p in _gui_binary_dirs():
        if "*" in p:
            expanded.extend(sorted(glob.glob(p), key=_node_version_key, reverse=True))
        else:
            expanded.append(p)
    return [p for p in expanded if p and os.path.isdir(p)]


_gui_path_ensured = False


def ensure_gui_path() -> None:
    """Best-effort: prepend `_gui_binary_dirs()` entries that exist and
    aren't already on PATH to this process's `os.environ["PATH"]`.

    A process launched without an inherited shell PATH (typically a macOS
    GUI app opened from Finder/Dock/Launchpad) otherwise can't discover
    `codex`/`agy`/`npm`/etc. at boot. On Windows/Linux the GUI environment
    usually already has a working PATH, so this is close to a no-op there —
    it still fills in gaps (e.g. a missing `%APPDATA%\\npm`) without erroring.

    Memoized: the actual PATH scan runs at most once per process. Never
    raises — PATH discovery is best-effort and must not block boot.
    """
    global _gui_path_ensured
    if _gui_path_ensured:
        return
    _gui_path_ensured = True
    try:
        current = os.environ.get("PATH", "").split(os.pathsep)
        missing = [p for p in _expand_gui_binary_dirs() if p not in current]
        if missing:
            os.environ["PATH"] = os.pathsep.join(missing) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        _log.debug("ensure_gui_path failed", exc_info=True)


def find_npm() -> str | None:
    """Locate the npm executable, falling back to `_gui_binary_dirs()` when
    `npm` isn't already on PATH (e.g. a GUI-launched process on macOS).

    Mutates `os.environ["PATH"]` by design when a fallback directory hits —
    so the `npm` subprocess this returns can itself locate `node`. Returns
    `None` (leaving PATH untouched) when nothing is found.
    """
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm:
        return npm

    names = ["npm.cmd", "npm.exe", "npm"] if sys.platform == "win32" else ["npm"]
    for d in _expand_gui_binary_dirs():
        for name in names:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate) and (
                sys.platform == "win32" or os.access(candidate, os.X_OK)
            ):
                if d not in os.environ.get("PATH", "").split(os.pathsep):
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                return candidate
    return None


def _heal_process_path(resolved: str) -> str:
    """Self-heal: *resolved* was found by probing (claude is NOT on PATH).

    Prepend its directory to this process's PATH so every child that invokes
    plain ``claude`` (pane spawns, ``claude plugin install`` subprocesses,
    teammate shells) works for the rest of the session even though the user's
    persistent PATH is broken. `takkub doctor --fix` repairs the persistent
    PATH; this keeps the cockpit alive in the meantime. Returns *resolved*
    unchanged so callers can use it inline.
    """
    from shutil import which

    if not (which("claude") or which("claude.cmd")):
        shim_dir = str(Path(resolved).parent)
        os.environ["PATH"] = shim_dir + os.pathsep + os.environ.get("PATH", "")
    return resolved
