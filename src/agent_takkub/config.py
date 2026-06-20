"""Project + runtime config helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SAFE_SHARD_IDX = re.compile(r"^[1-9][0-9]{0,2}$")  # 1–999


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
PROJECTS_JSON = REPO_ROOT / "projects.json"
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
RUNTIME_DIR = REPO_ROOT / "runtime"
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
      2. The shared parent of all configured project paths (`pms/` for
         `pms-web` + `pms-api`), if that parent exists on disk.
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


def write_port(port: int) -> None:
    ensure_runtime()
    PORT_FILE.write_text(str(port), encoding="utf-8")


def read_port() -> int | None:
    if PORT_FILE.exists():
        try:
            return int(PORT_FILE.read_text(encoding="utf-8").strip())
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

    # nvm4w default
    nvm_exe = Path("C:/nvm4w/nodejs/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
    if nvm_exe.exists():
        return str(nvm_exe)
    nvm_cmd = Path("C:/nvm4w/nodejs/claude.cmd")
    if nvm_cmd.exists():
        return str(nvm_cmd)

    raise RuntimeError(
        "Could not locate claude CLI. Install Claude Code or set CLAUDE_EXE env var."
    )
