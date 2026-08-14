"""Shared AST helpers for the subprocess call-site guards (#238).

`_is_subprocess_call()` used to hardcode the module name `subprocess`, so a
call written through an alias (`import subprocess as _subprocess` then
`_subprocess.run(...)`) was invisible to every guard that copied it. This
module resolves the *actual* alias(es) bound to `subprocess` (and any
`from subprocess import run as _run` function aliases) from each file's own
AST instead of assuming a literal name.
"""

from __future__ import annotations

import ast

CALL_NAMES = {"run", "Popen", "call", "check_output", "check_call"}


def collect_subprocess_aliases(tree: ast.Module) -> tuple[set[str], dict[str, str]]:
    """Return (module_aliases, func_aliases) bound to `subprocess` in *tree*.

    `module_aliases` always contains the literal `"subprocess"` plus any name
    bound via `import subprocess as X`. `func_aliases` maps a local name to
    the real subprocess function it refers to, from
    `from subprocess import run as _run` (or unaliased `from subprocess
    import run`, mapping `run` -> `run`).
    """
    module_aliases = {"subprocess"}
    func_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_aliases.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    if alias.name in CALL_NAMES:
                        func_aliases[alias.asname or alias.name] = alias.name
    return module_aliases, func_aliases


def is_subprocess_call(
    node: ast.Call, module_aliases: set[str], func_aliases: dict[str, str]
) -> str | None:
    """Return the subprocess function name if *node* calls it, else None."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in CALL_NAMES:
        if isinstance(func.value, ast.Name) and func.value.id in module_aliases:
            return func.attr
    elif isinstance(func, ast.Name):
        if func.id in func_aliases:
            return func_aliases[func.id]
        if func.id in CALL_NAMES:
            return func.id
    return None
