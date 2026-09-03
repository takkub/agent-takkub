"""Repo tidiness check for the qa-gate (#477, user directive 2026-09-03:
"อยากให้เป็นระบบ ระเบียบเรียบร้อย"). WARN-only by default — flags a test
file that lands somewhere the project's OWN existing tests don't, and a
scratch/debug file that leaked into the diff. Never fails the gate unless the
caller opts into `TAKKUB_QA_TIDY=strict`; `TAKKUB_QA_TIDY=0` disables the
step outright.

Convention is *learned*, not hardcoded to one layout: a project with no
existing tests of a given language has no established convention, so a new
test file in that language is never flagged for placement — there is nothing
for it to conform to yet.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

_GIT_TIMEOUT_S = 15
_BASE_REF_CANDIDATES: tuple[str, ...] = ("main", "master", "origin/main", "origin/master")
_MAX_LISTED = 10

_PY_TEST_RE = re.compile(r"(^|/)test_[^/]+\.py$")
_NODE_TEST_RE = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
_DUNDER_TESTS_DIR_RE = re.compile(r"(^|/)__tests__/")
_TESTS_DIR_RE = re.compile(r"(^|/)tests?/")
_PY_TESTS_DIR_RE = re.compile(r"(^|/)tests/")
_E2E_DIR_RE = re.compile(r"(^|/)(e2e|playwright)(/|$)")

_SCRATCH_BASENAME_RE = re.compile(r"^(debug_|tmp_|scratch)", re.IGNORECASE)
_SCRATCH_EXT_RE = re.compile(r"\.(log|orig|rej|bak)$", re.IGNORECASE)
_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g)$", re.IGNORECASE)
_ALLOWED_IMAGE_DIR_RE = re.compile(r"(^|/)(docs|assets|public|screenshots)/")
_FLOATING_TEST_BASENAMES = {"test.js", "test.ts", "test.py"}
_NEW_ENV_FILE_RE = re.compile(r"(^|/)\.env\.[^/]+$")


def _git(root: Path, *args: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _resolve_merge_base(root: Path) -> str | None:
    for ref in _BASE_REF_CANDIDATES:
        if not _git(root, "rev-parse", "--verify", "--quiet", ref):
            continue
        merge_base = _git(root, "merge-base", "HEAD", ref)
        if merge_base:
            return merge_base[0].strip()
    return None


def _diff_status(root: Path, ref: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in _git(root, "diff", "--name-status", "-M", ref):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        path = parts[-1].strip().replace("\\", "/")
        if path:
            out.append((status, path))
    return out


def new_or_moved_files(root: Path) -> list[str]:
    """Paths added or moved into place in this diff — merge-base-with-main
    when one resolves, else everything different from `HEAD` (working tree +
    staged), plus untracked files. A file only *modified in place* never
    appears here: placement/scratch checks only make sense for something
    landing somewhere NEW."""
    ref = _resolve_merge_base(root) or "HEAD"
    entries: dict[str, str] = {}
    for status, path in _diff_status(root, ref):
        entries[path] = status
    for line in _git(root, "ls-files", "--others", "--exclude-standard"):
        path = line.strip().replace("\\", "/")
        if path:
            entries.setdefault(path, "A")
    return sorted(p for p, s in entries.items() if s in ("A", "R"))


def _tracked_files(root: Path) -> list[str]:
    return [ln.strip().replace("\\", "/") for ln in _git(root, "ls-files") if ln.strip()]


def _is_e2e_path(path: str) -> bool:
    return bool(_E2E_DIR_RE.search(path))


def _py_test_shape(path: str) -> str | None:
    if not _PY_TEST_RE.search(path):
        return None
    return "tests-root" if _PY_TESTS_DIR_RE.search(path) else "colocated"


def _node_test_shape(path: str) -> str | None:
    if not _NODE_TEST_RE.search(path):
        return None
    if _DUNDER_TESTS_DIR_RE.search(path):
        return "__tests__"
    if _TESTS_DIR_RE.search(path):
        return "tests-root"
    return "colocated"


@dataclass
class Convention:
    py_shapes: set[str] = field(default_factory=set)
    node_shapes: set[str] = field(default_factory=set)


def learn_convention(root: Path) -> Convention:
    """The set of test-file "shapes" (co-located / `tests/` root /
    `__tests__/`) this project's OWN already-tracked tests actually use, per
    language — empty when the project has no tests of that language at all,
    which is the signal `_placement_warning` reads as "no convention to
    conform to"."""
    convention = Convention()
    for path in _tracked_files(root):
        if _is_e2e_path(path):
            continue
        shape = _py_test_shape(path)
        if shape:
            convention.py_shapes.add(shape)
            continue
        shape = _node_test_shape(path)
        if shape:
            convention.node_shapes.add(shape)
    return convention


def _placement_warning(path: str, convention: Convention) -> str | None:
    if _is_e2e_path(path):
        return None
    shape = _py_test_shape(path)
    if shape is not None:
        if shape in convention.py_shapes or not convention.py_shapes:
            return None
        allowed = "/".join(sorted(convention.py_shapes))
        return f"placement: {path} — โปรเจคใช้ {allowed} สำหรับ python test"
    shape = _node_test_shape(path)
    if shape is not None:
        if shape in convention.node_shapes or not convention.node_shapes:
            return None
        allowed = "/".join(sorted(convention.node_shapes))
        return f"placement: {path} — โปรเจคใช้ {allowed} สำหรับ test file"
    return None


def _scratch_reason(path: str) -> str | None:
    basename = path.rsplit("/", 1)[-1]
    if _SCRATCH_BASENAME_RE.match(basename):
        return "ชื่อไฟล์ scratch/debug"
    if "/" not in path and basename in _FLOATING_TEST_BASENAMES:
        return "test file ลอยที่ root"
    if _SCRATCH_EXT_RE.search(basename):
        return "scratch artifact (.log/.orig/.rej/.bak)"
    if _IMAGE_EXT_RE.search(basename) and not _ALLOWED_IMAGE_DIR_RE.search(path):
        return "รูปภาพนอกโฟลเดอร์ docs/assets/public/screenshots"
    if _NEW_ENV_FILE_RE.search(path):
        return ".env.* ไฟล์ใหม่"
    return None


def find_tidy_warnings(root: Path) -> list[str]:
    """One string per offending new/moved file — a placement mismatch takes
    priority over a scratch-name match on the same file (report once, not
    twice)."""
    files = new_or_moved_files(root)
    if not files:
        return []
    convention = learn_convention(root)
    warnings: list[str] = []
    for path in files:
        placement = _placement_warning(path, convention)
        if placement:
            warnings.append(placement)
            continue
        scratch = _scratch_reason(path)
        if scratch:
            warnings.append(f"scratch: {path} ({scratch})")
    return warnings


@dataclass
class TidyFinding:
    ok: bool
    skipped: bool
    warn: bool
    detail: str


def check_tidy(root: Path, env: dict | None = None) -> TidyFinding:
    mode = (env if env is not None else os.environ).get("TAKKUB_QA_TIDY", "").strip().lower()
    if mode in ("0", "off", "false"):
        return TidyFinding(True, True, False, "skipped (TAKKUB_QA_TIDY=0)")
    strict = mode == "strict"

    warnings = find_tidy_warnings(root)
    if not warnings:
        return TidyFinding(True, False, False, "สะอาด — ไม่มีไฟล์เทส/scratch ผิดที่ในดิฟ")

    shown = warnings[:_MAX_LISTED]
    detail = "; ".join(shown)
    extra = len(warnings) - len(shown)
    if extra > 0:
        detail += f" (+{extra} เพิ่มเติม)"
    if strict:
        return TidyFinding(False, False, False, f"TAKKUB_QA_TIDY=strict: {detail}")
    return TidyFinding(True, False, True, detail)
