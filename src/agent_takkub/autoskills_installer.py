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

`install()` prefers running `autoskills` against a throwaway staging mirror
of the project (see `_build_staging_mirror`) rather than the real project
tree, so a skill the user did NOT select never touches real disk at all —
only the ones they picked get copied in. If the mirror can't be built for any
reason, it falls back to the original direct in-place approach (full install
in `.claude/skills/`, then delete whatever wasn't selected) — see
`InstallResult.staging_used` and the "Staging vs. direct" note on `install()`
for the trade-off and an important unverified assumption about the former.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
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
    """Result of a real install.

    `written` — skill entry names (relative to `.claude/skills/`) that ended
    up on disk in the real project AND were selected by the user; the only
    ones actually kept.

    `skipped` — names `autoskills` produced that the user did NOT select.
    With the staging path (`staging_used=True`) these never touched the real
    project at all; with the direct fallback they were written for real then
    deleted again.

    `overwritten` — pre-existing `.claude/skills/` entries whose content
    changed because of this install. Two ways this happens: (1) the user
    selected a name that collided with something already on disk (expected —
    they asked for it), or (2) — direct/fallback path only — `autoskills`'
    non-filtered install run touched a pre-existing entry the user did NOT
    select; that case is restored from a pre-run backup and reported here
    too, since restoring successfully means no data was actually lost.

    `overwrite_failed` — pre-existing entries `autoskills` overwrote WITHOUT
    the user selecting them, where restoring the original content also
    failed. Only possible on the direct/fallback path. Non-empty here forces
    `ok=False` — this is real, unrecoverable data loss the caller must
    surface loudly, never swallow.

    `staging_used` — True if this install ran `autoskills` against an
    isolated staging mirror (see `_build_staging_mirror`) rather than the
    real project tree directly.
    """

    ok: bool
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    overwritten: list[str] = field(default_factory=list)
    overwrite_failed: list[str] = field(default_factory=list)
    staging_used: bool = False
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


def _path_escapes(path: Path, skills_real: Path) -> bool:
    """True when `path`'s resolved real location does not live under the
    already-resolved `skills_real` directory (or it can't be resolved at
    all — treated as an escape, not as "safe by default")."""
    try:
        real = path.resolve()
        real.relative_to(skills_real)
    except (OSError, ValueError):
        return True
    return False


def _escaped_entries(skills_dir: Path, names: Iterable[str]) -> set[str]:
    """Names among `names` that escape the resolved `skills_dir` — either
    because the top-level entry itself is a symlink/junction pointing
    outside it, or because a symlink NESTED anywhere inside a newly-written
    directory points outside (e.g. `skill-a/assets/evil -> C:\\Windows`,
    checking only the top-level entry would miss this). Walks each directory
    entry with `followlinks=False` so a symlinked subdirectory is inspected
    as a leaf — its own target checked — rather than followed into. The
    path-escape guard for `install()`."""
    try:
        skills_real = skills_dir.resolve()
    except OSError:
        return set(names)

    escaped: set[str] = set()
    for name in names:
        top = skills_dir / name
        if _path_escapes(top, skills_real):
            escaped.add(name)
            continue
        if top.is_symlink() or not top.is_dir():
            continue
        for root, dirs, files in os.walk(top, followlinks=False):
            root_path = Path(root)
            found = False
            for entry_name in (*dirs, *files):
                if _path_escapes(root_path / entry_name, skills_real):
                    escaped.add(name)
                    found = True
                    break
            if found:
                break
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


# ---------------------------------------------------------------------------
# Overwrite detection/recovery for pre-existing `.claude/skills/` entries —
# used by the direct/fallback install path, where `autoskills` runs against
# the real tree and could silently rewrite something already there under a
# name the user never selected (a same-name diff never shows up as "new").
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return "unreadable"
    return h.hexdigest()


def _entry_signature(path: Path) -> str:
    """Content-based fingerprint of one skill entry (file, dir, or symlink),
    used to tell whether `autoskills` silently rewrote a pre-existing entry.
    Hashes actual file bytes rather than size/mtime so it's immune to
    filesystem timestamp-resolution flakiness (notably coarse on some
    Windows filesystems) and to tools that preserve mtimes on write.
    Symlinks are fingerprinted by their target string, never followed."""
    if path.is_symlink():
        try:
            return f"symlink:{os.readlink(path)}"
        except OSError:
            return "symlink:?"
    if path.is_file():
        return f"file:{_hash_file(path)}"
    if path.is_dir():
        parts: list[str] = []
        for root, _dirs, files in os.walk(path, followlinks=False):
            root_path = Path(root)
            for fname in files:
                fpath = root_path / fname
                rel = fpath.relative_to(path).as_posix()
                if fpath.is_symlink():
                    try:
                        parts.append(f"{rel}:symlink:{os.readlink(fpath)}")
                    except OSError:
                        parts.append(f"{rel}:symlink:?")
                else:
                    parts.append(f"{rel}:{_hash_file(fpath)}")
        h = hashlib.sha256()
        for p in sorted(parts):
            h.update(p.encode("utf-8", "surrogateescape"))
            h.update(b"\0")
        return f"dir:{h.hexdigest()}"
    return "missing"


def _backup_entry(path: Path, backup_root: Path, name: str) -> Path | None:
    """Best-effort copy of one pre-existing skill entry into a temp backup
    dir before `autoskills` runs in-place, so a silent overwrite can be
    restored afterward. Returns None (entry can then only be reported, not
    restored) if the backup copy itself fails."""
    dest = backup_root / name
    try:
        if path.is_symlink():
            target = os.readlink(path)
            os.symlink(target, dest, target_is_directory=path.is_dir())
        elif path.is_dir():
            shutil.copytree(path, dest, symlinks=True)
        elif path.is_file():
            shutil.copy2(path, dest)
        else:
            return None
    except OSError:
        return None
    return dest


def _restore_entry(backup: Path, dest: Path) -> bool:
    """Best-effort restore of one pre-existing skill entry from its pre-run
    backup, after `autoskills` overwrote it. Returns False if the restore
    itself fails partway — the caller must then report `dest`'s name as
    unrestorable rather than assume success."""
    _remove_skill_entry(dest)
    try:
        if backup.is_symlink():
            target = os.readlink(backup)
            os.symlink(target, dest, target_is_directory=backup.is_dir())
        elif backup.is_dir():
            shutil.copytree(backup, dest, symlinks=True)
        else:
            shutil.copy2(backup, dest)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Staging mirror — install() prefers running `autoskills` here instead of the
# real project tree, so an unselected skill never touches real disk at all.
# ---------------------------------------------------------------------------

_STAGING_PREFIX = ".autoskills-staging-"


def _build_staging_mirror(project_root: Path) -> Path | None:
    """Best-effort isolated copy of `project_root` for `install()` to run
    `autoskills` against instead of the real project tree, so a skill the
    user did NOT select never touches real disk at all — the direct/fallback
    path has to write it for real first and delete it again, a window where
    a concurrent reader could pick it up, and where a mid-run crash leaves it
    stranded permanently.

    Lives INSIDE `project_root` (guarantees the same filesystem volume, so
    the mirror can use hardlinks — ~0 extra disk, same technique as this
    codebase's graft staging mirror) under a `.autoskills-staging-<token>/`
    directory that the mirror walk itself excludes. `.git/` and the real
    `.claude/skills/` are also excluded: `.git` because it's irrelevant to
    `autoskills` per this module's docstring, `.claude/skills` because
    hardlinking it would be actively dangerous — if `autoskills` opened one
    of those hardlinked files for an in-place rewrite, it would mutate the
    REAL project's file too (a hardlink shares the underlying bytes; only
    deleting/replacing the directory ENTRY is isolated, not editing content
    through it). Staging gets a fresh empty `.claude/skills/` instead; the
    caller (`_install_via_staging`) diffs newly-written names against the
    REAL project's `.claude/skills/` snapshot taken separately, after the
    CLI run, since the real one was never touched during it.

    UNVERIFIED ASSUMPTION, flagged per audit doc
    (docs/audit/2026-08-13-autoskills-installer.md): this relies on
    `autoskills`' stack detection only reading manifest/config files
    reachable under the project tree — not, say, requiring the literal real
    absolute path, network-probing something project-root-relative, or
    needing `.git` to exist. There is no network access in this environment
    to confirm that against the real CLI. `install()` treats ANY failure to
    build this mirror as non-fatal and falls back to the direct in-place
    path instead (see `InstallResult.staging_used`) — so a wrong assumption
    here degrades to the previous (also imperfect, but previously the only)
    behavior rather than breaking installs outright.

    Returns None (caller falls back to the direct path) only when the
    staging root itself can't be created. Per-file mirror failures inside
    the walk (a locked file, a permission error) are skipped individually
    and never abort the whole mirror — real dev trees routinely have a few
    such files that have nothing to do with stack detection.
    """
    try:
        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(project_root)))
    except OSError:
        return None

    for root, dirs, files in os.walk(project_root, followlinks=False):
        root_path = Path(root)
        try:
            rel_parts = root_path.relative_to(project_root).parts
        except ValueError:
            dirs[:] = []
            continue

        if not rel_parts:
            dirs[:] = [d for d in dirs if d != ".git" and d != staging.name]
        elif rel_parts == (".claude",):
            dirs[:] = [d for d in dirs if d != "skills"]

        dest_dir = staging.joinpath(*rel_parts)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            dirs[:] = []
            continue

        for fname in files:
            src = root_path / fname
            dest = dest_dir / fname
            try:
                if src.is_symlink():
                    target = os.readlink(src)
                    os.symlink(target, dest, target_is_directory=src.is_dir())
                else:
                    os.link(src, dest)
            except OSError:
                try:
                    shutil.copy2(src, dest)
                except OSError:
                    continue  # best-effort: one bad file never aborts the mirror

    try:
        (staging / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return None
    return staging


def _install_via_staging(
    project_root: Path,
    staging: Path,
    cmd: list[str],
    selected: set[str],
    timeout: float,
) -> InstallResult:
    staging_skills = staging / ".claude" / "skills"
    full_cmd = [*cmd, "--yes", "--agent", "claude-code"]
    try:
        proc = _run(full_cmd, staging, timeout)
    except subprocess.TimeoutExpired:
        return InstallResult(
            ok=False, error=f"autoskills install หมดเวลา ({timeout:.0f}s)", staging_used=True
        )
    except OSError as e:
        return InstallResult(ok=False, error=f"เรียก autoskills ไม่สำเร็จ: {e}", staging_used=True)

    raw = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return InstallResult(
            ok=False,
            raw_output=raw,
            error=f"autoskills exited {proc.returncode}",
            staging_used=True,
        )

    staged_names = _skill_entry_names(staging_skills)
    escaped = _escaped_entries(staging_skills, staged_names)
    if escaped:
        return InstallResult(
            ok=False,
            raw_output=raw,
            error=f"autoskills เขียนไฟล์นอก .claude/skills/ — ยกเลิกทั้งหมด: {', '.join(sorted(escaped))}",
            staging_used=True,
        )

    real_skills_dir = project_root / ".claude" / "skills"
    real_before = _skill_entry_names(real_skills_dir)

    written: list[str] = []
    skipped: list[str] = []
    overwritten: list[str] = []
    for name in sorted(staged_names):
        if name not in selected:
            skipped.append(name)  # never copied — never touches the real project at all
            continue
        if name in real_before:
            overwritten.append(name)  # user explicitly chose this name — expected
        dest = real_skills_dir / name
        try:
            real_skills_dir.mkdir(parents=True, exist_ok=True)
            _remove_skill_entry(dest)
            src = staging_skills / name
            if src.is_dir():
                shutil.copytree(src, dest, symlinks=True)
            else:
                shutil.copy2(src, dest)
            written.append(name)
        except OSError as e:
            return InstallResult(
                ok=False,
                written=written,
                skipped=skipped,
                overwritten=overwritten,
                raw_output=raw,
                error=f"คัดลอก {name} เข้าโปรเจกต์ไม่สำเร็จ: {e}",
                staging_used=True,
            )

    return InstallResult(
        ok=True,
        written=written,
        skipped=skipped,
        overwritten=overwritten,
        raw_output=raw,
        staging_used=True,
    )


def _install_direct(
    project_root: Path,
    cmd: list[str],
    selected: set[str],
    timeout: float,
) -> InstallResult:
    """Original in-place install path (no staging isolation available):
    `autoskills` runs directly against `project_root`, writing straight into
    the real `.claude/skills/`. Everything it wrote that the user didn't
    select is deleted again afterward. Every pre-existing entry is
    fingerprinted and backed up before the run so a same-name overwrite can
    be detected and — for names the user did NOT select — restored."""
    skills_dir = project_root / ".claude" / "skills"
    before_names = _skill_entry_names(skills_dir)
    before_signatures = {name: _entry_signature(skills_dir / name) for name in before_names}

    backup_dir: Path | None = None
    backups: dict[str, Path] = {}
    if before_names:
        try:
            backup_dir = Path(tempfile.mkdtemp(prefix="autoskills-backup-"))
        except OSError:
            backup_dir = None
        if backup_dir is not None:
            for name in before_names:
                backed_up = _backup_entry(skills_dir / name, backup_dir, name)
                if backed_up is not None:
                    backups[name] = backed_up

    def _cleanup_backup() -> None:
        if backup_dir is not None:
            shutil.rmtree(backup_dir, ignore_errors=True)

    full_cmd = [*cmd, "--yes", "--agent", "claude-code"]
    try:
        proc = _run(full_cmd, project_root, timeout)
    except subprocess.TimeoutExpired:
        _cleanup_backup()
        return InstallResult(ok=False, error=f"autoskills install หมดเวลา ({timeout:.0f}s)")
    except OSError as e:
        _cleanup_backup()
        return InstallResult(ok=False, error=f"เรียก autoskills ไม่สำเร็จ: {e}")

    raw = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        _cleanup_backup()
        return InstallResult(ok=False, raw_output=raw, error=f"autoskills exited {proc.returncode}")

    after_names = _skill_entry_names(skills_dir)
    new_entries = after_names - before_names

    overwritten: list[str] = []
    overwrite_failed: list[str] = []
    for name in sorted(before_names & after_names):
        if _entry_signature(skills_dir / name) == before_signatures.get(name):
            continue  # untouched
        if name in selected:
            overwritten.append(name)  # user explicitly chose this name — expected
            continue
        backup = backups.get(name)
        if backup is not None and _restore_entry(backup, skills_dir / name):
            overwritten.append(name)
        else:
            overwrite_failed.append(name)
    _cleanup_backup()

    escaped = _escaped_entries(skills_dir, new_entries)
    if escaped:
        for name in new_entries:
            _remove_skill_entry(skills_dir / name)
        error = f"autoskills เขียนไฟล์นอก .claude/skills/ — ยกเลิกทั้งหมด: {', '.join(sorted(escaped))}"
        if overwrite_failed:
            error += f" | เขียนทับ skill เดิมและกู้คืนไม่ได้: {', '.join(overwrite_failed)}"
        return InstallResult(
            ok=False,
            raw_output=raw,
            error=error,
            overwritten=overwritten,
            overwrite_failed=overwrite_failed,
        )

    written: list[str] = []
    skipped: list[str] = []
    for name in sorted(new_entries):
        if name in selected:
            written.append(name)
        else:
            skipped.append(name)
            _remove_skill_entry(skills_dir / name)

    ok = not overwrite_failed
    error = ""
    if overwrite_failed:
        error = f"autoskills เขียนทับ skill เดิมและกู้คืนไม่ได้: {', '.join(overwrite_failed)}"

    return InstallResult(
        ok=ok,
        written=written,
        skipped=skipped,
        overwritten=overwritten,
        overwrite_failed=overwrite_failed,
        raw_output=raw,
        error=error,
    )


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

    `autoskills` documents no per-skill filter flag, so this always runs a
    full non-interactive install (`--yes --agent claude-code`) and then
    reconciles the result down to exactly `selected_names`.

    Staging vs. direct: this function first tries to build an isolated
    staging mirror of `project_root` (`_build_staging_mirror`) and run
    `autoskills` there — an entry the user didn't select then simply never
    gets copied into the real project, full stop. If the mirror can't be
    built, it falls back to the ORIGINAL approach: run `autoskills` directly
    against `project_root`, then delete whatever wasn't selected — meaning
    an unselected skill briefly exists for real on disk before being removed
    again. `InstallResult.staging_used` tells the caller which path ran.
    **The staging path's correctness rests on an unverified assumption**
    (that `autoskills`' stack detection only needs the mirrored file tree,
    not the real absolute project path or anything staging can't replicate)
    — see `_build_staging_mirror`'s docstring and
    docs/audit/2026-08-13-autoskills-installer.md for the full caveat; there
    is no network access in this environment to confirm it against the real
    CLI.

    Every newly-written entry is checked against `.claude/skills/` for a
    path escape (e.g. a symlink pointing outside the project, at any depth
    inside a written directory); if any entry escapes, the entire install is
    rolled back and this returns `ok=False`. On the direct/fallback path, a
    pre-existing entry that `autoskills` silently overwrote is also detected
    and — if the user didn't select that name — restored from a backup taken
    before the run; see `InstallResult.overwritten` / `.overwrite_failed`.
    """
    project_root = Path(project_root)
    selected = {n.strip() for n in selected_names if n and n.strip()}
    if not selected:
        return InstallResult(ok=False, error="ไม่มี skill ที่เลือก")

    cmd = _resolve_autoskills_cmd()
    if cmd is None:
        return InstallResult(ok=False, error=_NO_RUNTIME_ERROR)

    staging = _build_staging_mirror(project_root)
    if staging is not None:
        try:
            return _install_via_staging(project_root, staging, cmd, selected, timeout)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    return _install_direct(project_root, cmd, selected, timeout)
