"""#574 round9: a bare `except ...: pass` anywhere in the migration ladder
or the boot-flow modules that drive it is a silently-swallowed error — the
504-round4-faults.py acceptance harness's `no_swallowed_errors` check only
ever inspects `promote_v1.py` itself; this locks in the same invariant
across the whole surface the round9 acceptance review named
(`core/migration/*` + `boot_flow*.py` + `auto_migrate_boot.py`) so a future
change can't reintroduce one anywhere else in that surface undetected."""

from __future__ import annotations

import ast
from pathlib import Path

import agent_takkub.auto_migrate_boot as auto_migrate_boot
import agent_takkub.boot_flow as boot_flow
import agent_takkub.boot_flow_terminal as boot_flow_terminal
from agent_takkub.core.migration import promote_v1

_MODULES = [
    auto_migrate_boot,
    boot_flow,
    boot_flow_terminal,
    promote_v1,
]


def _bare_except_pass_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Pass)
    ]


def test_no_bare_except_pass_in_migration_and_boot_flow_modules() -> None:
    found = {mod.__name__: _bare_except_pass_lines(Path(mod.__file__)) for mod in _MODULES}
    offenders = {name: lines for name, lines in found.items() if lines}
    assert not offenders, f"bare 'except: pass' remaining: {offenders}"


def test_no_bare_except_pass_anywhere_under_core_migration_package() -> None:
    root = Path(promote_v1.__file__).parent
    offenders = {
        str(f.relative_to(root)): lines
        for f in sorted(root.glob("*.py"))
        if (lines := _bare_except_pass_lines(f))
    }
    assert not offenders, f"bare 'except: pass' remaining: {offenders}"
