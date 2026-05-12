"""Project + runtime config helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

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
    return json.loads(PROJECTS_JSON.read_text(encoding="utf-8"))


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
    "qa": ("web", "api"),
    "reviewer": ("api", "web"),
}


def default_cwd_for_role(role_name: str) -> str | None:
    """Return the path from the active project that best matches the role,
    falling back to the project's first listed path. None if no project."""
    _, proj = active_project()
    paths = proj.get("paths", {})
    if not paths:
        return None
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


def set_active_project(name: str) -> bool:
    """Write a new active project name back to projects.json. Returns True if
    the name was valid (existed in `projects`), False otherwise."""
    data = load_projects()
    if name not in data.get("projects", {}):
        return False
    data["active"] = name
    PROJECTS_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def agent_role_dir(role: str) -> Path:
    """Per-role staging dir under runtime/agents/<role>/.

    A copy of `.claude/agents/<role>.md` is materialised here as CLAUDE.md so
    claude reads the specialist role definition before any task arrives.
    """
    d = RUNTIME_DIR / "agents" / role
    d.mkdir(parents=True, exist_ok=True)
    src = AGENTS_DIR / f"{role}.md"
    if src.exists():
        # strip frontmatter (between leading --- and the next ---)
        text = src.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                text = text[end + 4 :].lstrip()
        (d / "CLAUDE.md").write_text(text, encoding="utf-8")
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
