"""Markdown reference verifier — catch stale file/symbol refs after refactors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PathRef:
    text: str
    path: str
    line: int | None
    source: str
    source_line: int


@dataclass
class SymbolRef:
    text: str
    class_name: str | None
    method: str
    source: str
    source_line: int


@dataclass
class VerifyResult:
    ref_text: str
    status: str  # "ok" | "missing" | "line_oob"
    message: str
    source: str
    source_line: int


_CODE_EXTENSIONS = (".py", ".md", ".json", ".yml", ".yaml", ".ts", ".tsx", ".js", ".sh", ".toml")

# Point-in-time, design-process artifacts are excluded by default: they
# reference intended/prototype names (e.g. a planned `settings_dialog.py` or
# `provider_dialog.py`) that legitimately never match final code once a feature
# is built differently or deferred. Verifying them produces only false drift
# that drowns out real drift in *live* docs (architecture, guides, CLAUDE.md,
# README), which stay checked. `docs/reviews/*` was already excluded for the
# same reason (vendored review output); `plans/` and `specs/` are the same
# category — design at a moment, not a contract with the current tree.
# Point-in-time process artifacts: design plans/specs, code reviews, and QA
# reports. They reference names *as of that moment* — prototype files that were
# built differently or deferred (`settings_dialog.py`, `_pty_posix.py`), symbols
# since renamed, test symbols (`TestX.test_y`, in tests/ not src/), and external
# APIs (`QTimer.singleShot`, builtin `sorted()`). Verifying them yields only
# false drift that buries real drift in *live* docs (guides, system-overview,
# ARCHITECTURE.md, CLAUDE.md, README — all still checked). `docs/reviews/*` was
# already excluded for exactly this reason; the rest are the same category.
_DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "docs/reviews/*",
    "docs/review/*",
    "docs/code-review/*",
    "docs/design-review/*",
    "docs/qa/*",
    "docs/qa-reports/*",
    "docs/eval/*",
    "docs/superpowers/plans/*",
    "docs/superpowers/specs/*",
    # Loose top-level artifacts of the same nature (no dedicated dir to glob):
    #  - MACOS_PORT_PLAN.md — plan referencing intended _pty_posix.py/make_backend()
    #    that only exist once the macOS port is built.
    #  - code-review-issue-cli-*.md — a point-in-time code review (next_id() etc.).
    "docs/MACOS_PORT_PLAN.md",
    "docs/code-review-issue-cli-*.md",
)


def strip_code_blocks(md_text: str) -> str:
    """Replace content inside triple-backtick fences with blank lines.

    Preserves line count so error source_line numbers remain accurate.
    If a fence is unmatched (no closing ```), returns the original text unchanged.
    """
    lines = md_text.split("\n")
    result: list[str] = []
    in_fence = False
    temp: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not in_fence and stripped.startswith("```"):
            in_fence = True
            temp = [line]
        elif in_fence and stripped.startswith("```"):
            # Close the fence — replace all interior lines with empty strings
            temp.append(line)
            for j, orig in enumerate(temp):
                if j == 0 or j == len(temp) - 1:
                    result.append(orig)  # keep fence markers (they have no refs)
                else:
                    result.append("")
            temp = []
            in_fence = False
        elif in_fence:
            temp.append(line)
        else:
            result.append(line)

    if in_fence:
        # Unmatched fence — return original unchanged
        return md_text

    return "\n".join(result)


# Matches backtick-quoted paths that contain at least one `/` (directory-qualified)
# e.g. `src/foo.py:42` or `docs/bar.md` — bare filenames without a `/` are skipped.
_PATH_PATTERN = re.compile(
    r"`((?!https?://)([a-zA-Z0-9_.][a-zA-Z0-9_./\-]*/[a-zA-Z0-9_./\-]+(?:"
    + "|".join(re.escape(ext) for ext in _CODE_EXTENSIONS)
    + r"))(?::(\d+))?)`"
)

# Matches `ClassName.method` (method >= 4 chars) or `function_name()` (func >= 4 chars)
_SYMBOL_CLASS_METHOD = re.compile(r"`([A-Z][a-zA-Z0-9_]*)\.([a-z_][a-zA-Z0-9_]{3,})`")
_SYMBOL_FUNCTION = re.compile(r"`([a-z_][a-zA-Z0-9_]{3,})\(\)`")

_SKIP_SYMBOLS = frozenset({"i.e", "e.g", "etc"})


def extract_path_refs(md_text: str, source: str = "") -> list[PathRef]:
    """Extract backtick-quoted file path references from markdown text."""
    refs: list[PathRef] = []
    for lineno, line in enumerate(md_text.splitlines(), start=1):
        for m in _PATH_PATTERN.finditer(line):
            full_text, path, line_str = m.group(1), m.group(2), m.group(3)
            refs.append(
                PathRef(
                    text=f"`{full_text}`",
                    path=path,
                    line=int(line_str) if line_str else None,
                    source=source,
                    source_line=lineno,
                )
            )
    return refs


def extract_symbol_refs(md_text: str, source: str = "") -> list[SymbolRef]:
    """Extract backtick-quoted symbol references (Class.method or func()) from markdown."""
    refs: list[SymbolRef] = []
    for lineno, line in enumerate(md_text.splitlines(), start=1):
        for m in _SYMBOL_CLASS_METHOD.finditer(line):
            cls, method = m.group(1), m.group(2)
            refs.append(
                SymbolRef(
                    text=f"`{cls}.{method}`",
                    class_name=cls,
                    method=method,
                    source=source,
                    source_line=lineno,
                )
            )
        for m in _SYMBOL_FUNCTION.finditer(line):
            func = m.group(1)
            if func in _SKIP_SYMBOLS:
                continue
            refs.append(
                SymbolRef(
                    text=f"`{func}()`",
                    class_name=None,
                    method=func,
                    source=source,
                    source_line=lineno,
                )
            )
    return refs


def verify_path(ref: PathRef, repo_root: Path) -> VerifyResult:
    """Check that a path ref's file exists and, if a line is given, is in range."""
    target = repo_root / ref.path
    if not target.exists():
        return VerifyResult(
            ref_text=ref.text,
            status="missing",
            message=f"file not found: {ref.path}",
            source=ref.source,
            source_line=ref.source_line,
        )
    if ref.line is not None:
        try:
            line_count = sum(1 for _ in target.read_bytes().split(b"\n"))
        except OSError:
            line_count = 0
        if ref.line > line_count:
            return VerifyResult(
                ref_text=ref.text,
                status="line_oob",
                message=f"line {ref.line} > {line_count} lines in {ref.path}",
                source=ref.source,
                source_line=ref.source_line,
            )
    return VerifyResult(
        ref_text=ref.text,
        status="ok",
        message="",
        source=ref.source,
        source_line=ref.source_line,
    )


def verify_symbol(
    ref: SymbolRef,
    repo_root: Path,
    search_dirs: tuple[Path, ...] = (Path("src"),),
) -> VerifyResult:
    """Search for the symbol definition in source files under search_dirs."""
    patterns = [
        re.compile(rf"\bdef\s+{re.escape(ref.method)}\b"),
        re.compile(rf"\bclass\s+{re.escape(ref.method)}\b"),
        re.compile(rf"^{re.escape(ref.method)}\s*=", re.MULTILINE),
    ]
    if ref.class_name:
        patterns.append(re.compile(rf"\bclass\s+{re.escape(ref.class_name)}\b"))

    for search_dir in search_dirs:
        base = repo_root / search_dir
        if not base.exists():
            continue
        for f in base.rglob("*.py"):
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat in patterns:
                if pat.search(content):
                    return VerifyResult(
                        ref_text=ref.text,
                        status="ok",
                        message="",
                        source=ref.source,
                        source_line=ref.source_line,
                    )

    return VerifyResult(
        ref_text=ref.text,
        status="missing",
        message=f"symbol '{ref.method}' not found in {[str(d) for d in search_dirs]}",
        source=ref.source,
        source_line=ref.source_line,
    )


def verify_docs(
    docs_dirs: tuple[Path, ...] = (Path("docs"),),
    extras: tuple[Path, ...] = (Path("CLAUDE.md"), Path("README.md")),
    repo_root: Path = Path("."),
    exclude_globs: tuple[str, ...] | None = None,
    use_default_excludes: bool = True,
) -> list[VerifyResult]:
    """Walk markdown files, extract refs, verify each. Returns all non-ok results.

    exclude_globs: additional glob patterns to skip (matched against relative path).
    use_default_excludes: when True, auto-excludes docs/reviews/*.md (vendored content).
    """
    from pathlib import PurePath

    active_excludes: tuple[str, ...] = exclude_globs if exclude_globs is not None else ()
    if use_default_excludes:
        active_excludes = active_excludes + _DEFAULT_EXCLUDE_GLOBS

    results: list[VerifyResult] = []

    def _is_excluded(md_path: Path) -> bool:
        try:
            rel = md_path.relative_to(repo_root)
        except ValueError:
            rel = Path(str(md_path))
        rel_posix = rel.as_posix()
        for pattern in active_excludes:
            if PurePath(rel_posix).match(pattern):
                return True
            # A `dir/*` exclude means "everything under this dir". PurePath.match's
            # `*` doesn't cross `/` and Python 3.11 has no recursive `**`, so a
            # nested artifact (e.g. docs/code-review/2026-05-29-system/codex.md)
            # would otherwise slip past `docs/code-review/*` and flag stale refs
            # from a point-in-time snapshot. Treat the pattern as a dir prefix too.
            if pattern.endswith("/*"):
                prefix = pattern[:-2]
                if rel_posix == prefix or rel_posix.startswith(prefix + "/"):
                    return True
        return False

    def _process(md_path: Path) -> None:
        if _is_excluded(md_path):
            return
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        text = strip_code_blocks(text)
        rel = str(md_path.relative_to(repo_root)) if md_path.is_absolute() else str(md_path)
        for ref in extract_path_refs(text, source=rel):
            r = verify_path(ref, repo_root)
            if r.status != "ok":
                results.append(r)
        for ref in extract_symbol_refs(text, source=rel):
            r = verify_symbol(ref, repo_root)
            if r.status != "ok":
                results.append(r)

    for docs_dir in docs_dirs:
        base = repo_root / docs_dir
        if not base.exists():
            continue
        for md_file in base.rglob("*.md"):
            _process(md_file)

    for extra in extras:
        p = repo_root / extra
        if p.exists():
            _process(p)

    return results


def format_drift_report(results: list[VerifyResult]) -> str:
    """Markdown table of broken refs; empty → 'No broken refs found.'"""
    broken = [r for r in results if r.status != "ok"]
    if not broken:
        return "No broken refs found."
    lines = [
        "# Docs drift report",
        "",
        "| Source | Line | Ref | Status | Message |",
        "|--------|------|-----|--------|---------|",
    ]
    for r in broken:
        lines.append(f"| {r.source} | {r.source_line} | {r.ref_text} | {r.status} | {r.message} |")
    return "\n".join(lines)
