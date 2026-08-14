"""Guard: every subprocess.run/Popen/call/check_output/check_call call under
`src/agent_takkub/` that opens in text mode (`text=True` or
`universal_newlines=True`) must pass an explicit `encoding=` *and*
`errors=` (#205).

Root cause: `text=True` without `encoding=` makes Python decode the child
process's stdout/stderr with `locale.getpreferredencoding()` — the OS
codepage, not UTF-8. On a Thai-locale Windows machine that's `cp874`; the
instant an external CLI (git/npm/claude/codex/gemini/cloudflared/...)
writes a byte `cp874` can't map, the reader thread dies with
`UnicodeDecodeError: 'charmap' codec can't decode byte ...` and takes the
whole subprocess call down with it — proven live on a real user machine
(cockpit v1.0.60, pid 19952, `subprocess.py:1597 _readerthread`).

`encoding=` alone isn't enough either: with the default `errors="strict"`,
a single byte the *declared* encoding can't map still raises the same way.
Output from an external CLI is not something this codebase controls, so
every text-mode call must also pass `errors="replace"` — never `"strict"`
(silently replacing an unmappable byte is strictly better than crashing
the caller).

Companion to `test_subprocess_no_window_guard.py` — same call-site
discovery, same exemption-comment escape hatch, different invariant.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ._subprocess_call_ast import collect_subprocess_aliases, is_subprocess_call

SRC_ROOT = Path(__file__).parent.parent / "src" / "agent_takkub"
_TEXT_MODE_KWARGS = {"text", "universal_newlines"}
_EXEMPT_MARKER = "subprocess-encoding-ok:"


def _is_true_literal(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value is True


def _is_text_mode(node: ast.Call) -> bool:
    return any(kw.arg in _TEXT_MODE_KWARGS and _is_true_literal(kw.value) for kw in node.keywords)


def _str_literal(value: ast.expr) -> str | None:
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


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
    module_aliases, func_aliases = collect_subprocess_aliases(tree)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = is_subprocess_call(node, module_aliases, func_aliases)
        if name is None or not _is_text_mode(node):
            continue
        if _is_exempted(lines, node.lineno):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        missing = [kw for kw in ("encoding", "errors") if kw not in kwargs]
        if missing:
            violations.append(
                f"{path}:{node.lineno}: subprocess.{name}() opens text mode but is "
                f'missing {", ".join(missing)}= (needs encoding="utf-8", errors="replace")'
            )
            continue
        encoding_value = _str_literal(kwargs["encoding"])
        if encoding_value != "utf-8":
            violations.append(
                f"{path}:{node.lineno}: subprocess.{name}() encoding= must be literal "
                f'"utf-8", found {encoding_value!r}'
            )
        errors_value = _str_literal(kwargs["errors"])
        if errors_value != "replace":
            violations.append(
                f"{path}:{node.lineno}: subprocess.{name}() errors= must be literal "
                f'"replace" (found {errors_value!r}) — "strict" (the default) still '
                "raises on any byte the declared encoding can't map; if a different "
                "value is truly required, add `# subprocess-encoding-ok: <reason>`"
            )
    return violations


PY_FILES = sorted(SRC_ROOT.rglob("*.py"))


@pytest.mark.parametrize("py_file", PY_FILES, ids=lambda p: str(p.relative_to(SRC_ROOT)))
def test_subprocess_text_mode_calls_have_encoding(py_file: Path) -> None:
    violations = _find_violations(py_file)
    assert not violations, (
        "\n".join(violations) + "\n\nEvery subprocess call with text=True/universal_newlines=True "
        'must also pass encoding="utf-8", errors="replace" — otherwise decoding '
        "falls back to the OS locale codepage and can crash on any byte that "
        "codepage can't map (#205). If this call genuinely can't take those kwargs, "
        "add `# subprocess-encoding-ok: <reason>` on the same or preceding line."
    )


def test_guard_catches_aliased_import_missing_encoding(tmp_path: Path) -> None:
    """#238: `import subprocess as X` must not blind the guard."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        "import subprocess as _subprocess\n\n"
        "def f():\n"
        "    return _subprocess.run(['echo'], capture_output=True, text=True)\n",
        encoding="utf-8",
    )
    violations = _find_violations(offender)
    assert len(violations) == 1
    assert "missing encoding, errors=" in violations[0]


def test_guard_catches_aliased_function_import_missing_encoding(tmp_path: Path) -> None:
    """#238: `from subprocess import run as _run` must not blind the guard."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        "from subprocess import run as _run\n\n"
        "def f():\n"
        "    return _run(['echo'], capture_output=True, text=True)\n",
        encoding="utf-8",
    )
    violations = _find_violations(offender)
    assert len(violations) == 1
    assert "missing encoding, errors=" in violations[0]
