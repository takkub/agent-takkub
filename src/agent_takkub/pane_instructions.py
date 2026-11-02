"""Hand a non-claude pane its instructions without writing into the repo (#961).

`AGENTS.md` at the project root is read by every CLI opened in that folder,
including one the user starts by hand while a pane is live, and it is the
trace the issue owner wants gone. Every non-claude provider now gets the
text through a channel that only the pane's own process sees:

- codex: ``-c developer_instructions='''...'''`` (a session-only developer
  message; verified on codex-cli 0.164 with 15k chars of multi-line Thai,
  quotes and backticks through ``list2cmdline``).
- opencode: ``OPENCODE_CONFIG_CONTENT={"instructions": [<file>]}`` (merged
  over the user's own config for this process only; verified on 1.19.4).
- agy: ``--agent <name>`` with ``~/.gemini/config/agents/<name>/agent.md``
  (a custom agent is only active when selected by name, keeps the default
  agent's tools; verified on agy 1.4.12 TUI). Removed when the pane closes.
  agy has no per-process plugin/rules switch: ``"disabled": true`` in a
  plugin manifest is ignored and ``--settings`` rejects ``enabledPlugins``.
- cursor: the text rides the first task paste (``first_task_paste``) — the
  CLI has no verified flag, and this path needs none.

The backing file for each pane lives under ``RUNTIME_DIR/pane-instructions``,
never in the project.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

CODEX_DEVELOPER_INSTRUCTIONS = "codex_developer_instructions"
OPENCODE_CONFIG_INSTRUCTIONS = "opencode_config_instructions"
AGY_CUSTOM_AGENT = "agy_custom_agent"
FIRST_TASK_PASTE = "first_task_paste"
CHANNELS = (
    CODEX_DEVELOPER_INSTRUCTIONS,
    OPENCODE_CONFIG_INSTRUCTIONS,
    AGY_CUSTOM_AGENT,
    FIRST_TASK_PASTE,
)

# Windows caps a whole command line at 32767 UTF-16 units and the codex argv
# also carries MCP overrides, model, resume, etc. Past this budget the text
# goes to a file and the inline value only points at it.
_CODEX_INLINE_BUDGET = 24_000

# Written into every agy agent.md we create; only dirs carrying it are removed.
_AGY_AGENT_TAG = "agent-takkub cockpit pane (removed when the pane closes)"


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", part or "_").strip("._") or "_"


def instructions_file(project: str, role: str) -> Path:
    """Where one pane's instruction text is written (outside every repo)."""
    from . import config

    return config.RUNTIME_DIR / "pane-instructions" / _safe(project) / f"{_safe(role)}.md"


def agy_agents_root() -> Path:
    return Path.home() / ".gemini" / "config" / "agents"


def agy_agent_name(project: str, role: str) -> str:
    """Per-pane agent name. Carries a hash of this cockpit's DATA_HOME: dev
    and prod share ``~/.gemini``, and one cockpit's cleanup must never
    remove the other's live agent."""
    from . import config

    home = hashlib.sha1(str(config.DATA_HOME).encode("utf-8")).hexdigest()[:6]
    return f"takkub-{home}-{_safe(project)}-{_safe(role)}".lower().replace(".", "-")


def remove_agy_agent(project: str, role: str) -> bool:
    """Delete this pane's agy custom agent (ours only). Never raises."""
    target = agy_agents_root() / agy_agent_name(project, role)
    try:
        md = target / "agent.md"
        if (
            not md.is_file()
            or _AGY_AGENT_TAG not in md.read_text(encoding="utf-8", errors="replace")[:400]
        ):
            return False
        shutil.rmtree(target)
        return True
    except OSError:
        return False


def _codex_value(text: str, path: Path, marker: str = "") -> tuple[str, str]:
    """``(developer_instructions=<toml>, mode)`` — the whole text as a TOML
    multi-line literal when it fits, else a pointer to *path*."""
    if "'''" not in text:
        arg = f"developer_instructions='''{text}'''"
        if len(subprocess.list2cmdline([arg])) <= _CODEX_INLINE_BUDGET:
            return arg, "inline"
    pointer = (marker + "\n" if marker else "") + (
        "You are running inside an agent-takkub cockpit pane. Your operating "
        f"instructions are in {path} . Before anything else, read that whole file "
        "with a shell command and follow it for the rest of this session."
    )
    return "developer_instructions=" + json.dumps(pointer, ensure_ascii=False), "file_pointer"


def _merge_opencode_config(raw: str | None, path: Path) -> str:
    cfg: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            cfg = parsed
    existing = cfg.get("instructions")
    items = [str(i) for i in existing] if isinstance(existing, list) else []
    if str(path) not in items:
        items.append(str(path))
    cfg["instructions"] = items
    return json.dumps(cfg, ensure_ascii=False)


def _write_agy_agent(name: str, text: str) -> None:
    target = agy_agents_root() / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "agent.md").write_text(
        f"---\nname: {name}\ndescription: {_AGY_AGENT_TAG}\n---\n\n{text}",
        encoding="utf-8",
    )


def session_marker(nonce: str) -> str:
    """Line that tags a codex pane's developer message (#964). Codex records
    the developer message near the top of its rollout, so the rollout that
    contains this line is exactly this pane's session."""
    return f"<!-- takkub-pane-session: {nonce} -->"


def deliver(
    channel: str,
    text: str,
    project: str,
    role: str,
    env: dict[str, str],
    marker: str = "",
) -> tuple[list[str], str, str]:
    """Route *text* through *channel*. Returns ``(argv_extra, path, mode)``;
    the opencode env var is set on *env* in place. *marker* (codex) is put on
    the first line of the inline value so it lands in the rollout. For
    ``first_task_paste`` the caller carries the text in the first paste.
    Raises OSError when a file cannot be written, ValueError for an unknown
    channel."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown instruction channel {channel!r}")
    if marker and channel == CODEX_DEVELOPER_INSTRUCTIONS:
        text = f"{marker}\n{text}"
    path = instructions_file(project, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if channel == CODEX_DEVELOPER_INSTRUCTIONS:
        value, mode = _codex_value(text, path, marker)
        return ["-c", value], str(path), mode
    if channel == OPENCODE_CONFIG_INSTRUCTIONS:
        env["OPENCODE_CONFIG_CONTENT"] = _merge_opencode_config(
            env.get("OPENCODE_CONFIG_CONTENT"), path
        )
        return [], str(path), "config_content"
    if channel == AGY_CUSTOM_AGENT:
        name = agy_agent_name(project, role)
        _write_agy_agent(name, text)
        return ["--agent", name], str(path), "custom_agent"
    return [], str(path), "first_task_paste"
