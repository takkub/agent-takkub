"""Lead bash write-intent audit log (Gap A — Phase 1).

Detects write-y shell commands and appends a JSONL record to
``runtime/lead_bash_audit.log``.  Phase 1 is **audit-only** — no blocking.

To exercise live, wire ``audit_lead_bash`` as a Claude Code PreToolUse hook
in ``.claude/settings.json``:

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Bash",
            "hooks": [
              {
                "type": "command",
                "command": "python -c \\"
                  "import sys, json; "
                  "from agent_takkub.lead_bash_audit import audit_lead_bash; "
                  "inp = json.load(sys.stdin); "
                  "audit_lead_bash(inp.get('command',''), cwd=inp.get('cwd',''))\\"
              }
            ]
          }
        ]
      }
    }

Phase 2 (not implemented): deny the command when write intent targets
a path inside BLOCKED_DIRS and return exit-code 2 to abort the tool.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .config import RUNTIME_DIR

# Patterns that indicate the command might write to the file system.
# Ordered by specificity; first match wins.
_PATTERNS: list[tuple[str, str]] = [
    # PowerShell write cmdlets (case-insensitive via re.IGNORECASE)
    (r"\bSet-Content\b", "powershell-write"),
    (r"\bOut-File\b", "powershell-write"),
    (r"\bAdd-Content\b", "powershell-write"),
    # Python open(..., 'w' / 'a' / 'x' / 'wb' / 'ab')
    (r"""open\s*\([^)]*['"][waxWAX][b]?['"]\s*[,)]""", "python-write"),
    # git apply patches
    (r"\bgit\s+apply\b", "git-apply"),
    # sed in-place
    (r"\bsed\s+(-i|--in-place)\b", "sed-inplace"),
    # Shell redirect operators > and >> (false positives accepted in Phase 1)
    (r">>?", "shell-redirect"),
]

_COMPILED = [(re.compile(pat, re.IGNORECASE), reason) for pat, reason in _PATTERNS]

_DEFAULT_LOG = RUNTIME_DIR / "lead_bash_audit.log"


def detect_write_intent(cmd: str) -> str | None:
    """Return the reason string if *cmd* looks like it might write to disk.

    Returns ``None`` for read-only commands.  False positives are acceptable
    in Phase 1 — the goal is observability, not correctness.
    """
    for pattern, reason in _COMPILED:
        if pattern.search(cmd):
            return reason
    return None


def audit_lead_bash(
    cmd: str,
    *,
    cwd: str = "",
    log_path: Path | None = None,
) -> None:
    """Append a JSONL audit record if *cmd* has write intent.

    Args:
        cmd:       The bash command string to inspect.
        cwd:       Working directory of the command (for context).
        log_path:  Override the default log path (used by tests).
    """
    reason = detect_write_intent(cmd)
    if reason is None:
        return

    target = log_path if log_path is not None else _DEFAULT_LOG
    target.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "cwd": cwd,
        "cmd": cmd[:500],
        "reason": reason,
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
