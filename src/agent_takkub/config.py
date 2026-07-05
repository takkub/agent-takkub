"""Project + runtime config helpers."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SAFE_SHARD_IDX = re.compile(r"^[1-9][0-9]{0,2}$")  # 1–999

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
    if "#" in name:
        role_part, _, shard_part = name.partition("#")
        if not _SAFE_NAME.fullmatch(role_part):
            raise ValueError(f"invalid {kind}: {value!r}")
        if not _SAFE_SHARD_IDX.fullmatch(shard_part):
            raise ValueError(f"invalid {kind} shard index: {value!r}")
        return name
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError(f"invalid {kind}: {value!r}")
    return name


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write *data* to *path* via a temp file so a crash mid-write never
    leaves a partial/corrupt JSON file behind."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


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
RUNTIME_DIR = DATA_HOME / "runtime"
PORT_FILE = RUNTIME_DIR / "port"
EVENTS_LOG = RUNTIME_DIR / "events.log"


def ensure_runtime() -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return RUNTIME_DIR


def load_projects() -> dict:
    if not PROJECTS_JSON.exists():
        return {"active": None, "projects": {}}
    try:
        return json.loads(PROJECTS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        import logging

        logging.getLogger(__name__).warning(
            "projects.json contains invalid JSON — falling back to empty project list"
        )
        return {"active": None, "projects": {}}


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

    # 1. explicit lead path key
    lead_key = proj.get("lead")
    if isinstance(lead_key, str) and lead_key in paths:
        return paths[lead_key]

    # 2. common parent of all paths
    try:
        common = os.path.commonpath([str(p) for p in paths.values()])
        if common and Path(common).is_dir() and Path(common).parent != Path(common):
            return common
    except ValueError:
        pass

    # 3. first listed path
    return next(iter(paths.values()))


def set_active_project(name: str) -> bool:
    """Write a new active project name back to projects.json. Returns True if
    the name was valid (existed in `projects`), False otherwise."""
    data = load_projects()
    if name not in data.get("projects", {}):
        return False
    data["active"] = name
    _write_json_atomic(PROJECTS_JSON, data)
    return True


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


def agent_role_dir(role: str) -> Path:
    """Per-role staging dir under runtime/agents/<role>/.

    A copy of `.claude/agents/<role>.md` is materialised here as CLAUDE.md so
    claude reads the specialist role definition before any task arrives. A
    central dev-server-hygiene block (`_DEV_SERVER_HYGIENE`) is appended to every
    role so the `next build && next start` rule applies cockpit-wide from one
    place.
    """
    role = validate_name(role, "role")
    base = (RUNTIME_DIR / "agents").resolve()
    d = (RUNTIME_DIR / "agents" / role).resolve()
    if d != base and base not in d.parents:
        raise ValueError(f"role path escapes runtime: {role!r}")
    d.mkdir(parents=True, exist_ok=True)
    src = AGENTS_DIR / f"{role}.md"
    if src.exists():
        # strip frontmatter (between leading --- and the next ---)
        text = src.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                text = text[end + 4 :].lstrip()
        (d / "CLAUDE.md").write_text(
            text.rstrip() + _DEV_SERVER_HYGIENE + _NON_INTERACTIVE_HYGIENE,
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
