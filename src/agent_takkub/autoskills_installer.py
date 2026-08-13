"""Bridge to the `autoskills` CLI (https://www.autoskills.sh) — scans a
project's `package.json`/config to guess its tech stack, then fetches
matching skills from the `skills.sh` registry as `.claude/skills/<name>/`
files.

Two-step, confirm-before-write API by design: a skill that lands under
`.claude/skills/` is a prompt every pane in the project auto-loads, so
installing one is equivalent to importing external content straight into the
team's shared context. `preview()` only ever runs the CLI's `--dry-run` mode
(guaranteed no writes) so the UI can show the user what would land; `install()`
is the ONLY function that writes files, and callers must gate it behind an
explicit user confirmation of which skills to keep — never call it
speculatively or automatically.

Both `preview()` and `install()` are synchronous, blocking calls (they shell
out and wait, bounded by `timeout`) — call them from a worker thread, never
the Qt main thread. The UI layer is responsible for that threading; nothing
here spawns one.

Resolution order for the CLI itself: a directly-installed `autoskills` (or
`autoskills.cmd` on Windows) binary on PATH wins; otherwise falls back to
`npx --yes autoskills@latest`, resolving `npx`/`npx.cmd` via `shutil.which`
(never a hardcoded path) so this works unmodified on both Windows and macOS.
When neither is found, both functions return a readable Thai error instead of
raising or hanging.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

PREVIEW_TIMEOUT_DEFAULT = 60.0
INSTALL_TIMEOUT_DEFAULT = 120.0

_NO_RUNTIME_ERROR = "ไม่พบ autoskills และไม่พบ npx บนเครื่องนี้ — ติดตั้ง Node.js ก่อน (npx มาพร้อม Node.js)"


@dataclass(frozen=True)
class SkillCandidate:
    """One skill `autoskills` proposes to install. `source` is its registry
    URL when the CLI's output included one; "" when it didn't (best-effort
    text parse — see `_parse_preview_output`)."""

    name: str
    source: str = ""


@dataclass(frozen=True)
class PreviewResult:
    """Result of a `--dry-run` pass. `raw_output` is kept verbatim (stdout +
    stderr) even on a successful parse, since `stack`/`skills` are a
    best-effort text parse of an external CLI's output — the UI can fall back
    to showing `raw_output` if the parsed fields look wrong or empty."""

    ok: bool
    stack: list[str] = field(default_factory=list)
    skills: list[SkillCandidate] = field(default_factory=list)
    raw_output: str = ""
    error: str = ""


@dataclass(frozen=True)
class InstallResult:
    """Result of a real install. `written` are the skill directory/file names
    (relative to `.claude/skills/`) that autoskills wrote AND the user
    selected — the only ones actually kept on disk. `skipped` are names
    autoskills wrote that the user did NOT select; those are deleted again
    before this returns (see `install()`)."""

    ok: bool
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    raw_output: str = ""
    error: str = ""


def _resolve_autoskills_cmd() -> list[str] | None:
    """The argv prefix to invoke `autoskills`, or None when neither a direct
    install nor `npx` is available. Checks the Windows `.cmd` shim name
    explicitly before the bare name (same pattern as `verify.detect_stack`'s
    npm/npx resolution) since `subprocess.run(shell=False)` cannot reliably
    launch a bare `.cmd` name via `CreateProcess` on Windows."""
    direct = shutil.which("autoskills.cmd") or shutil.which("autoskills")
    if direct:
        return [direct]
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if npx:
        return [npx, "--yes", "autoskills@latest"]
    return None


def _run(cmd: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run one autoskills invocation: no console window, no inherited stdin
    (so a surprise interactive prompt fails fast instead of hanging forever),
    bounded by `timeout`. `npm_config_yes`/`GIT_TERMINAL_PROMPT` cover the
    same two blocking-prompt classes `pane_env._apply_non_interactive_env`
    guards panes against, applied here too since `npx` may need to fetch the
    package on first run."""
    env = dict(os.environ)
    env.setdefault("npm_config_yes", "true")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        errors="replace",
        creationflags=SUBPROCESS_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        env=env,
    )


# ---------------------------------------------------------------------------
# Best-effort output parsing (`autoskills` has no --json flag documented) —
# raw_output is always preserved alongside the parse so a wrong/empty parse
# never hides the real CLI output from the user.
# ---------------------------------------------------------------------------

_STACK_INLINE_RE = re.compile(r"(?im)^\s*(?:detected\s+)?stack\s*:\s*(.+)$")
_HEADER_RE = re.compile(r"(?im)^[ \t]*([A-Za-z][A-Za-z0-9 /_-]*)\s*:\s*$")
_BULLET_RE = re.compile(r"(?m)^[ \t]*[-*•]\s*(.+?)\s*$")
_URL_RE = re.compile(r"https?://[^\s()]+")


def _sections(raw: str) -> dict[str, str]:
    """Split `raw` into header->body blocks keyed by lower-cased header text,
    where a header is a standalone line ending in ':' (no bullet prefix).
    Output with no such headers yields {}."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            current = m.group(1).strip().lower()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


def _parse_bullet_skill(text: str) -> SkillCandidate | None:
    text = text.strip()
    urls = _URL_RE.findall(text)
    src = urls[0].rstrip(".,;:") if urls else ""
    name = _URL_RE.sub("", text).strip(" -:()\t")
    return SkillCandidate(name=name, source=src) if name else None


def _parse_preview_output(raw: str) -> tuple[list[str], list[SkillCandidate]]:
    stack: list[str] = []
    m = _STACK_INLINE_RE.search(raw)
    if m:
        stack = [p.strip() for p in re.split(r"[,/]", m.group(1)) if p.strip()]
    sections = _sections(raw)
    if not stack:
        for key, body in sections.items():
            if "stack" in key:
                stack = [b.strip() for b in _BULLET_RE.findall(body)]
                if stack:
                    break

    skills: list[SkillCandidate] = []
    for key, body in sections.items():
        if "skill" in key:
            for bullet in _BULLET_RE.findall(body):
                cand = _parse_bullet_skill(bullet)
                if cand:
                    skills.append(cand)
            if skills:
                break
    if not skills:
        # Fallback: a bulleted line carrying a URL anywhere in the output is
        # very likely a skill entry (source-linked); stack lines never are.
        for bullet in _BULLET_RE.findall(raw):
            urls = _URL_RE.findall(bullet)
            if not urls:
                continue
            cand = _parse_bullet_skill(bullet)
            if cand:
                skills.append(cand)
    return stack, skills


def preview(project_root: str | Path, timeout: float = PREVIEW_TIMEOUT_DEFAULT) -> PreviewResult:
    """Dry-run `autoskills` against `project_root` and parse what it WOULD
    install. Writes nothing — runs the CLI with `--dry-run --agent
    claude-code` only. Safe to call repeatedly/speculatively since it never
    touches disk. Call from a worker thread; blocks up to `timeout` seconds.
    """
    project_root = Path(project_root)
    cmd = _resolve_autoskills_cmd()
    if cmd is None:
        return PreviewResult(ok=False, error=_NO_RUNTIME_ERROR)

    full_cmd = [*cmd, "--dry-run", "--agent", "claude-code"]
    try:
        proc = _run(full_cmd, project_root, timeout)
    except subprocess.TimeoutExpired:
        return PreviewResult(ok=False, error=f"autoskills preview หมดเวลา ({timeout:.0f}s)")
    except OSError as e:
        return PreviewResult(ok=False, error=f"เรียก autoskills ไม่สำเร็จ: {e}")

    raw = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return PreviewResult(ok=False, raw_output=raw, error=f"autoskills exited {proc.returncode}")

    stack, skills = _parse_preview_output(raw)
    return PreviewResult(ok=True, stack=stack, skills=skills, raw_output=raw)


def _skill_entry_names(skills_dir: Path) -> set[str]:
    if not skills_dir.is_dir():
        return set()
    try:
        return {p.name for p in skills_dir.iterdir()}
    except OSError:
        return set()


def _escaped_entries(skills_dir: Path, names: Iterable[str]) -> set[str]:
    """Names among `names` whose resolved real path does NOT live under the
    resolved `skills_dir` — e.g. a symlink/junction autoskills wrote that
    points outside the project. The path-escape guard for `install()`."""
    try:
        skills_real = skills_dir.resolve()
    except OSError:
        return set(names)
    escaped: set[str] = set()
    for name in names:
        try:
            real = (skills_dir / name).resolve()
            real.relative_to(skills_real)
        except (OSError, ValueError):
            escaped.add(name)
    return escaped


def _remove_skill_entry(path: Path) -> None:
    """Best-effort removal of one skill entry autoskills wrote (dir, file, or
    symlink); never raises. A symlink/junction is unlinked, not followed."""
    try:
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def install(
    project_root: str | Path,
    selected_names: Iterable[str],
    timeout: float = INSTALL_TIMEOUT_DEFAULT,
) -> InstallResult:
    """Install ONLY the skills in `selected_names` into
    `<project_root>/.claude/skills/`.

    MUST be called only after the user has explicitly confirmed a selection
    in the UI — this is the one function in this module that writes files.
    Never call speculatively, automatically, or as a side effect of
    `preview()`. Call from a worker thread; blocks up to `timeout` seconds.

    `autoskills` documents no per-skill filter flag, so this runs a full
    non-interactive install (`--yes --agent claude-code`), diffs
    `.claude/skills/` before/after to see everything the CLI actually wrote,
    then deletes any newly-written entry NOT in `selected_names` — so the net
    effect on disk matches exactly the user's selection. Every newly-written
    entry is also checked against `.claude/skills/` for a path escape (e.g. a
    symlink pointing outside the project); if any entry escapes, ALL new
    entries are rolled back and this returns `ok=False`.
    """
    project_root = Path(project_root)
    selected = {n.strip() for n in selected_names if n and n.strip()}
    if not selected:
        return InstallResult(ok=False, error="ไม่มี skill ที่เลือก")

    cmd = _resolve_autoskills_cmd()
    if cmd is None:
        return InstallResult(ok=False, error=_NO_RUNTIME_ERROR)

    skills_dir = project_root / ".claude" / "skills"
    before = _skill_entry_names(skills_dir)

    full_cmd = [*cmd, "--yes", "--agent", "claude-code"]
    try:
        proc = _run(full_cmd, project_root, timeout)
    except subprocess.TimeoutExpired:
        return InstallResult(ok=False, error=f"autoskills install หมดเวลา ({timeout:.0f}s)")
    except OSError as e:
        return InstallResult(ok=False, error=f"เรียก autoskills ไม่สำเร็จ: {e}")

    raw = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return InstallResult(ok=False, raw_output=raw, error=f"autoskills exited {proc.returncode}")

    new_entries = _skill_entry_names(skills_dir) - before

    escaped = _escaped_entries(skills_dir, new_entries)
    if escaped:
        for name in new_entries:
            _remove_skill_entry(skills_dir / name)
        return InstallResult(
            ok=False,
            raw_output=raw,
            error=f"autoskills เขียนไฟล์นอก .claude/skills/ — ยกเลิกทั้งหมด: {', '.join(sorted(escaped))}",
        )

    written: list[str] = []
    skipped: list[str] = []
    for name in sorted(new_entries):
        if name in selected:
            written.append(name)
        else:
            skipped.append(name)
            _remove_skill_entry(skills_dir / name)

    return InstallResult(ok=True, written=written, skipped=skipped, raw_output=raw)
