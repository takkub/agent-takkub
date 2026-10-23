"""cocoindex-code (`ccc`) — semantic, local-embedding code search for panes.

Pilot 2026-10-22 (`docs/audit/2026-10-22-cocoindex-code-pilot.md`): on 12
questions phrased WITHOUT the identifiers' own words, `ccc search` found the
right code in the top-3 for 10/12 vs 5/12 for graft's lexical `ask`. The price
is machine-side, not money: a 1.1 GB install, an ~800 MB resident daemon, and
a first index measured in tens of minutes per repo on CPU. So the cockpit
wires it as a SECOND code MCP next to graft (structure: `skeleton`/`callers`;
meaning: `search`) and never indexes anything on its own — a project gets the
MCP only after someone ran `takkub ccc index` for it.

Everything path-shaped lives here so `shared_dev_tools` and the CLI agree:

* `ccc` discovers its project by walking up from the MCP server's cwd and —
  when nothing is found — auto-creates `.cocoindex_code/settings.yml` IN THAT
  DIRECTORY. The cockpit pins the root explicitly (`COCOINDEX_CODE_ROOT_PATH`)
  and only injects the MCP when `index_ready()` says the index exists, so no
  pane can ever trigger a fresh 47-minute index inside a user's repo, and a
  worktree pane searches its project's main checkout instead of seeding a
  brand-new project under `worktrees/`.
* The databases (217 MB for this repo) are relocated under DATA_HOME via
  `COCOINDEX_CODE_DB_PATH_MAPPING` (same lesson as graft_store.py / #146:
  nothing cockpit-generated may land inside a project tree). The one file
  `ccc` still writes into the project is `.cocoindex_code/settings.yml`
  (+ a `.gitignore` line) — accepted for the trial, flagged in the audit note.
* `ccc init` asks for the embedding provider through an interactive prompt
  that crashes under the cockpit's shells (no Win32 console behind Git Bash /
  pythonw): `ensure_global_settings()` writes the local default instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from .config import DATA_HOME, REPO_ROOT

CCC_DB_ROOT_NAME = "cocoindex-db"
SETTINGS_DIR = ".cocoindex_code"
PROJECT_SETTINGS_FILE = "settings.yml"
GLOBAL_SETTINGS_FILE = "global_settings.yml"
INDEX_DB_FILE = "target_sqlite.db"
# What `ccc init` picks for the `[full]` install when stdin is not a TTY —
# local sentence-transformers, no API key (cocoindex_code.cli.DEFAULT_ST_MODEL).
DEFAULT_ST_MODEL = "Snowflake/snowflake-arctic-embed-xs"
_KEY_HEX_LEN = 8
CCC_RUNNER_ARGS = ("-m", "agent_takkub.cli", "ccc", "serve")

GLOBAL_SETTINGS_TEMPLATE = f"""# CocoIndex Code — global settings (written by agent-takkub `takkub ccc index`).
# After editing this file, run `ccc doctor` to verify your configuration.

embedding:
  provider: sentence-transformers
  model: {DEFAULT_ST_MODEL}
  indexing_params: {{}}
  query_params:
    prompt_name: query
"""


def ccc_cli_path() -> str | None:
    """Absolute path of the `ccc` launcher, or None when not installed."""
    return shutil.which("ccc")


def ccc_server_path() -> str | None:
    """Absolute path of the `cocoindex-code` launcher (same package, second
    console script). The MCP is started through THIS one, not `ccc mcp`:
    `ccc mcp` only ever walks up from its cwd for `.cocoindex_code/` and
    exits "Not in an initialized project directory" otherwise (a worktree
    pane), while `cocoindex-code serve` falls back to
    `COCOINDEX_CODE_ROOT_PATH` — the pin `env_for_root` sets — verified
    2026-10-22 by driving both over stdio from a non-project cwd."""
    return shutil.which("cocoindex-code")


def _norm(path: Path) -> str:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser().absolute()
    text = str(resolved)
    return text.lower() if os.name == "nt" else text


def project_root_for_pane(cwd: str | Path, project: str | None) -> Path | None:
    """The checkout a pane at *cwd* should search.

    A worktree pane (cwd under `worktree_root(project)`) gets its project's
    lead checkout: the index is built once per project, never per worktree,
    and pointing `ccc` at the worktree would make it seed a new project there.
    """
    from .config import lead_cwd
    from .worktree_manager import worktree_root

    target = Path(cwd).expanduser()
    try:
        resolved = target.resolve()
    except OSError:
        return None
    if project:
        try:
            wt_root = worktree_root(project).resolve()
        except OSError:
            wt_root = None
        if wt_root is not None and (resolved == wt_root or wt_root in resolved.parents):
            main = lead_cwd(project)
            if not main:
                return None
            try:
                return Path(main).expanduser().resolve()
            except OSError:
                return None
    return resolved


def _store_base() -> Path:
    """DATA_HOME — except on a dev checkout where DATA_HOME == REPO_ROOT and
    the cockpit's own repo is also a configured project: a DATA_HOME-relative
    store would then sit INSIDE that project tree (graft_store.py found the
    same trap 2026-08-05), so that one case falls back to `~/.agent-takkub`."""
    if DATA_HOME == REPO_ROOT:
        return Path.home() / ".agent-takkub"
    return DATA_HOME


def db_dir(root: Path) -> Path:
    """Where *root*'s databases live: <store base>/cocoindex-db/<name>-<hash8>.

    Name + hash (not name alone) so two projects called `web` under different
    parents never share an index.
    """
    key = hashlib.sha256(_norm(root).encode("utf-8")).hexdigest()[:_KEY_HEX_LEN]
    return _store_base() / CCC_DB_ROOT_NAME / f"{root.name}-{key}"


# #901/#904: the daemon `cocoindex-code serve` spawns embeds on CPU through
# torch/sentence-transformers, whose thread pools default to one thread per
# core — measured 2026-10-23 at 7-10 cores for as long as a refresh ran, on a
# machine the team was also building on. Two threads keep a warm refresh in
# seconds (6 s for this repo) while leaving the machine to the panes. The
# daemon inherits these from whichever `serve`/`index` starts it, so every
# cockpit entry point must pass them. A value the user exported wins.
EMBED_THREAD_CAP = "2"
_THREAD_CAP_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "TORCH_NUM_THREADS",
)


def env_for_root(root: Path) -> dict[str, str]:
    """Environment that makes `ccc` (CLI or MCP) work on *root* without
    touching anything the cockpit does not own."""
    env = {
        "COCOINDEX_CODE_ROOT_PATH": str(root),
        "COCOINDEX_CODE_DB_PATH_MAPPING": f"{root}={db_dir(root)}",
        "COCOINDEX_DISABLE_USAGE_TRACKING": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    for name in _THREAD_CAP_VARS:
        env[name] = os.environ.get(name) or EMBED_THREAD_CAP
    return env


def project_settings_path(root: Path) -> Path:
    return root / SETTINGS_DIR / PROJECT_SETTINGS_FILE


def index_ready(root: Path) -> bool:
    """True once `takkub ccc index` (or a manual `ccc index` with the same
    mapping) has produced a searchable index for *root*."""
    return project_settings_path(root).is_file() and (db_dir(root) / INDEX_DB_FILE).is_file()


def global_settings_path() -> Path:
    override = os.environ.get("COCOINDEX_CODE_DIR")
    base = Path(override) if override else Path.home() / SETTINGS_DIR
    return base / GLOBAL_SETTINGS_FILE


def ensure_global_settings() -> Path:
    """Write the local-embedding default when the global settings are missing."""
    path = global_settings_path()
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(GLOBAL_SETTINGS_TEMPLATE, encoding="utf-8")
    return path


def runner_mcp_config() -> dict[str, object]:
    """Provider-neutral MCP entry for providers without session argv support.

    The runner resolves the active pane's ``TAKKUB_PROJECT`` at launch, rather
    than storing one project's path or database mapping in a user-level MCP
    configuration.  This makes the same safe entry usable by agy, OpenCode,
    and Cursor across projects and worktrees.
    """
    env: dict[str, str] = {}
    if DATA_HOME == REPO_ROOT:
        # A source checkout is not guaranteed to have an editable install
        # (for example while uv cannot replace a live winpty executable).
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return {"command": _console_python(), "args": list(CCC_RUNNER_ARGS), "env": env}


def _console_python() -> str:
    """The interpreter an MCP host can talk stdio to: the installed cockpit
    runs under `pythonw.exe`, whose console-less launch is the wrong thing to
    hand a provider as a stdio server; its `python.exe` sits next to it."""
    exe = Path(sys.executable)
    if exe.stem.lower() == "pythonw":
        sibling = exe.with_name("python" + exe.suffix)
        if sibling.is_file():
            return str(sibling)
    return sys.executable


def pane_mcp_config(root: Path) -> dict[str, object]:
    """The `ccc` MCP entry a pane of *root* gets, for every provider (#904).

    Always the cockpit runner rather than the `cocoindex-code` launcher
    itself: the runner starts the launcher at below-normal priority (and the
    daemon it spawns inherits that on Windows and POSIX alike), which a
    provider spawning the launcher directly cannot do.
    """
    runner = runner_mcp_config()
    return {
        **runner,
        "args": [*runner["args"], "--cwd", str(root)],  # type: ignore[misc]
        "env": {**runner["env"], **env_for_root(root)},  # type: ignore[dict-item]
    }


def ensure_cursor_mcp(path: Path | None = None) -> tuple[bool, str]:
    """Merge the generic runner into Cursor's documented user MCP config.

    Never replace unrelated user servers and fail closed on malformed config
    or an existing non-cockpit ``ccc`` definition.
    """
    config_path = path or Path.home() / ".cursor" / "mcp.json"
    data: dict[str, object] = {}
    if config_path.is_file():
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, f"Cursor MCP config is unreadable: {config_path}"
        if not isinstance(parsed, dict):
            return False, f"Cursor MCP config is not an object: {config_path}"
        data = parsed
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    if not isinstance(servers, dict):
        return False, f"Cursor MCP config has invalid mcpServers: {config_path}"
    expected = runner_mcp_config()
    existing = servers.get("ccc")
    if existing is not None and existing != expected:
        return False, "Cursor already has a non-cockpit MCP named 'ccc'; left unchanged"
    if existing == expected:
        return True, "Cursor ccc MCP already present"
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        servers["ccc"] = expected
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return False, f"could not write Cursor MCP config: {exc}"
    return True, f"added Cursor ccc MCP: {config_path}"
