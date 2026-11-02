"""Hand a non-claude pane its instructions without writing into the repo (#961).

`AGENTS.md` at the project root is read by every CLI opened in that folder,
including one the user starts by hand while a pane is live, and it is the
trace the issue owner wants gone. Providers with a per-process channel get
the same text through that channel instead:

- codex: ``-c developer_instructions='''...'''`` (a session-only developer
  message; verified on codex-cli 0.164 with 15k chars of multi-line Thai,
  quotes and backticks through ``list2cmdline``).
- opencode: ``OPENCODE_CONFIG_CONTENT={"instructions": [<file>]}`` (merged
  over the user's own config for this process only; verified on 1.19.4).

The file behind each channel lives under ``RUNTIME_DIR/pane-instructions``,
never in the project. Providers without a verified channel (agy, cursor)
keep ``ProviderSpec.instruction_channel = None`` and still use the
cwd ``AGENTS.md`` while the pane is open — a gap stated in provider_spec.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

CODEX_DEVELOPER_INSTRUCTIONS = "codex_developer_instructions"
OPENCODE_CONFIG_INSTRUCTIONS = "opencode_config_instructions"
CHANNELS = (CODEX_DEVELOPER_INSTRUCTIONS, OPENCODE_CONFIG_INSTRUCTIONS)

# Windows caps a whole command line at 32767 UTF-16 units and the codex argv
# also carries MCP overrides, model, resume, etc. Past this budget the text
# goes to a file and the inline value only points at it.
_CODEX_INLINE_BUDGET = 24_000


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", part or "_").strip("._") or "_"


def instructions_file(project: str, role: str) -> Path:
    """Where one pane's instruction text is written (outside every repo)."""
    from . import config

    return config.RUNTIME_DIR / "pane-instructions" / _safe(project) / f"{_safe(role)}.md"


def _codex_value(text: str, path: Path) -> tuple[str, str]:
    """``(developer_instructions=<toml>, mode)`` — the whole text as a TOML
    multi-line literal when it fits, else a pointer to *path*."""
    if "'''" not in text:
        arg = f"developer_instructions='''{text}'''"
        if len(subprocess.list2cmdline([arg])) <= _CODEX_INLINE_BUDGET:
            return arg, "inline"
    pointer = (
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


def deliver(
    channel: str, text: str, project: str, role: str, env: dict[str, str]
) -> tuple[list[str], str, str]:
    """Route *text* through *channel*. Returns ``(argv_extra, path, mode)``;
    the opencode env var is set on *env* in place. Raises OSError when the
    backing file cannot be written, ValueError for an unknown channel."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown instruction channel {channel!r}")
    path = instructions_file(project, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if channel == CODEX_DEVELOPER_INSTRUCTIONS:
        value, mode = _codex_value(text, path)
        return ["-c", value], str(path), mode
    env["OPENCODE_CONFIG_CONTENT"] = _merge_opencode_config(
        env.get("OPENCODE_CONFIG_CONTENT"), path
    )
    return [], str(path), "config_content"
