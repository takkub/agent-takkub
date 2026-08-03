"""Guard: every subprocess.run/Popen/call/check_output/check_call call under
`src/agent_takkub/` must pass `creationflags=` so it doesn't flash a conhost
window when spawned from the pythonw-hosted GUI (issue: "หน้าต่างดำเด้ง").

`creationflags=SUBPROCESS_NO_WINDOW` (or a local platform-gated equivalent,
e.g. `_CREATE_NO_WINDOW` in remote/tunnel.py) is 0 on non-Windows, so it's a
safe no-op to pass unconditionally, including inside POSIX-only branches.

A call site that genuinely needs the real console (e.g. an interactive
editor subprocess) is exempted by putting `# subprocess-console-ok: <reason>`
on the same line as the call, or on the line immediately above it — this
guard fails loudly if a new site is added without either the kwarg or the
exemption comment, so it can't silently regress (14 sites fixed 2026-08-03).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).parent.parent / "src" / "agent_takkub"
_CALL_NAMES = {"run", "Popen", "call", "check_output", "check_call"}
_EXEMPT_MARKER = "subprocess-console-ok:"


def _is_subprocess_call(node: ast.Call) -> str | None:
    """Return the subprocess function name if *node* calls it, else None."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _CALL_NAMES:
        if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
            return func.attr
    elif isinstance(func, ast.Name) and func.id in _CALL_NAMES:
        return func.id
    return None


def _is_exempted(source_lines: list[str], lineno: int) -> bool:
    """Check the call's own line and the line above for the exemption marker."""
    for idx in (lineno - 1, lineno - 2):
        if 0 <= idx < len(source_lines) and _EXEMPT_MARKER in source_lines[idx]:
            return True
    return False


def _find_violations(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _is_subprocess_call(node)
        if name is None:
            continue
        has_creationflags = any(kw.arg == "creationflags" for kw in node.keywords)
        if has_creationflags:
            continue
        if _is_exempted(lines, node.lineno):
            continue
        violations.append(f"{path}:{node.lineno}: subprocess.{name}() missing creationflags=")
    return violations


PY_FILES = sorted(SRC_ROOT.rglob("*.py"))


@pytest.mark.parametrize("py_file", PY_FILES, ids=lambda p: str(p.relative_to(SRC_ROOT)))
def test_subprocess_calls_have_creationflags(py_file: Path) -> None:
    violations = _find_violations(py_file)
    assert not violations, (
        "\n".join(violations) + "\n\nEvery subprocess.run/Popen/call/check_output/check_call needs "
        "creationflags=SUBPROCESS_NO_WINDOW (from ._win_console) so it doesn't "
        "flash a console window when spawned from the pythonw GUI. If this "
        "call genuinely needs the real console, add "
        "`# subprocess-console-ok: <reason>` on the same or preceding line."
    )
