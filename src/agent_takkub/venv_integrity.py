"""On-disk integrity of the cockpit's own Python interpreter (#341/#695/#696).

Stdlib-only leaf: imported by `doctor`, `pane_guard`, `spawn_engine`,
`orchestrator` and `cli_shim`, so it must not import any of them.

Threat model
------------
Every spawned pane runs an autonomous shell in a process tree whose PATH
(until #696) started with the cockpit venv's own ``Scripts/``/``bin/``. Any
tool a pane runs that "writes next to its interpreter" — or any shell quirk
that swaps a write's target and value — lands on the file every pane, the
CLI and the cockpit itself boot from. Seen twice for real (2026-08-22 and
2026-09-22): a 29-byte text file where ``venv/Scripts/python.exe`` used to
be, after which every ``takkub`` call died with empty output, Windows raised
"Unsupported 16-Bit Application" modals, and the idle-reminder loop kept
nudging panes to run a ``takkub done`` that could never succeed.

Three layers, all default-deny, all here or fed from here:

1. **Prevention** — `cockpit_executable_dirs()` is the deny-list
   `pane_guard` applies to every write/move/copy/delete from every role
   (Lead included); `cli_shim` keeps the venv's script dir OFF pane PATH.
2. **Detection** — `find_problems()` reads the interpreter files on disk
   (the running cockpit keeps the healthy image mapped, so it can notice
   corruption that happened under it).
3. **Repair** — `repair()` restores the file from the base interpreter the
   venv was created from (``pyvenv.cfg`` ``home``), keeping the clobbered
   file as evidence next to it.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

# #341/#446: floor + magic-byte check for a venv python that EXISTS but was
# overwritten/truncated by something outside agent-takkub. Mirrors
# npm/scripts/lib.js's pythonExecutableProblem() so the cockpit's own
# `doctor` and the npm launcher agree on what counts as broken. The floor
# only needs to catch truncation (#341's 29-byte case) — a pyenv/python.org
# framework build's `bin/pythonX.Y` is a tiny Mach-O re-exec stub that can be
# well under the old 40KB floor while still being completely runnable
# (#446); magic-byte sniffing is what actually distinguishes it from
# garbage.
MIN_PYTHON_EXE_BYTES = 1024
_ELF_MAGIC = b"\x7f\x45\x4c\x46"
_MACHO_MAGICS = frozenset(
    {
        b"\xfe\xed\xfa\xce",  # MH_MAGIC (32-bit)
        b"\xce\xfa\xed\xfe",  # MH_CIGAM (32-bit, byte-swapped)
        b"\xfe\xed\xfa\xcf",  # MH_MAGIC_64
        b"\xcf\xfa\xed\xfe",  # MH_CIGAM_64 (byte-swapped)
        b"\xca\xfe\xba\xbe",  # FAT_MAGIC (universal/fat binary)
        b"\xbe\xba\xfe\xca",  # FAT_CIGAM (universal/fat binary, byte-swapped)
    }
)

PROBLEM_DESCRIPTIONS = {
    "missing": "the file doesn't exist",
    "too-small": f"the file is smaller than {MIN_PYTHON_EXE_BYTES} bytes — too small to be a real interpreter",
    "bad-magic": "the file's header does not match a known executable format (PE/ELF/Mach-O) or a shebang script",
    "read-error": "the file could not be read to check its header",
}


def python_executable_problem(py_path: Path, *, platform: str | None = None) -> str | None:
    """None when `py_path` looks like a real, runnable interpreter, else a
    short machine-readable reason (a key of `PROBLEM_DESCRIPTIONS`)."""
    platform = platform or sys.platform
    try:
        size = py_path.stat().st_size
    except OSError:
        return "missing"
    if size < MIN_PYTHON_EXE_BYTES:
        return "too-small"
    try:
        with open(py_path, "rb") as f:
            header = f.read(4)
    except OSError:
        return "read-error"
    if platform == "win32":
        # Windows PE executables always start with the 'MZ' DOS-header magic.
        return None if header[:2] == b"MZ" else "bad-magic"
    # POSIX: accept a real ELF or Mach-O binary, or a shebang script — a
    # pyenv shim / venv `python` wrapper is a text file starting with '#!',
    # not a compiled executable, and is just as valid an interpreter path.
    if header[:2] == b"#!":
        return None
    if header == _ELF_MAGIC or header in _MACHO_MAGICS:
        return None
    return "bad-magic"


def interpreter_dir(executable: str | Path | None = None) -> Path:
    """The script directory of the interpreter this process runs from
    (``venv/Scripts`` on Windows, ``venv/bin`` on POSIX). Resolves the
    DIRECTORY, never the file: a POSIX venv's ``bin/python`` is a symlink to
    the base interpreter, and following it would report the base install's
    ``bin/`` instead of the venv's."""
    return Path(executable or sys.executable).parent.resolve()


def venv_root(executable: str | Path | None = None) -> Path | None:
    """The venv root of *executable* (the dir holding ``pyvenv.cfg``), or
    None when it is not a venv interpreter (system python)."""
    d = interpreter_dir(executable)
    for candidate in (d.parent, d):
        if (candidate / "pyvenv.cfg").is_file():
            return candidate
    return None


def interpreter_files(executable: str | Path | None = None) -> list[Path]:
    """Every interpreter file the cockpit/CLI can be launched through —
    all of them must pass `python_executable_problem`."""
    d = interpreter_dir(executable)
    if sys.platform == "win32":
        names = ("python.exe", "pythonw.exe")
    else:
        names = ("python", "python3", f"python{sys.version_info.major}.{sys.version_info.minor}")
    out: list[Path] = []
    for name in names:
        p = d / name
        if p.exists() or p.is_symlink():
            out.append(p)
    return out


def find_problems(executable: str | Path | None = None) -> list[tuple[Path, str]]:
    """``[(path, problem), ...]`` for every interpreter file that fails the
    static check. Empty when everything looks runnable."""
    out: list[tuple[Path, str]] = []
    for p in interpreter_files(executable):
        problem = python_executable_problem(p)
        if problem is not None:
            out.append((p, problem))
    return out


def cockpit_executable_dirs(
    *, executable: str | Path | None = None, extra: tuple[Path, ...] | list[Path] = ()
) -> frozenset[Path]:
    """Directories no pane may write into, ever: the venv this cockpit runs
    from (its whole tree — site-packages included, #202), the base
    interpreter it was created from, the dev checkout's ``.venv`` when this
    is a source checkout, plus *extra* (the pane shim dir, see `cli_shim`).
    Resolved so the guard can compare against resolved targets."""
    dirs: set[Path] = set()
    root = venv_root(executable)
    if root is not None:
        dirs.add(root.resolve())
    else:
        dirs.add(interpreter_dir(executable))
    try:
        dirs.add(Path(sys.base_prefix).resolve())
    except OSError:
        pass
    repo_root = Path(__file__).resolve().parents[2]
    if "site-packages" not in repo_root.parts:
        dev_venv = repo_root / ".venv"
        if dev_venv.is_dir():
            dirs.add(dev_venv.resolve())
    for e in extra:
        try:
            dirs.add(Path(e).resolve())
        except OSError:
            continue
    return frozenset(dirs)


def is_inside_any(target: Path, dirs: frozenset[Path] | set[Path]) -> Path | None:
    """The first dir of *dirs* that contains (or equals) *target*, else None.
    Case-insensitive on Windows, same as the rest of the guard."""
    norm_t = target.as_posix().rstrip("/")
    fold = sys.platform == "win32"
    if fold:
        norm_t = norm_t.lower()
    for d in dirs:
        norm_d = d.as_posix().rstrip("/")
        if fold:
            norm_d = norm_d.lower()
        if norm_t == norm_d or norm_t.startswith(norm_d + "/"):
            return d
    return None


def _read_pyvenv_home(root: Path) -> Path | None:
    try:
        for line in (root / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            key, sep, val = line.partition("=")
            if sep and key.strip().lower() == "home":
                return Path(val.strip())
    except OSError:
        return None
    return None


def repair_sources(path: Path) -> list[Path]:
    """Candidate files to restore *path* from, best first — none of them is
    guaranteed to exist. Windows: the venv launcher stub CPython copies into
    every venv (``<home>/Lib/venv/scripts/nt/<name>``), then the base
    interpreter itself (also runnable inside a venv, it reads ``pyvenv.cfg``).
    POSIX: the base interpreter the venv symlinks to."""
    root = venv_root(path) or path.parent.parent
    home = _read_pyvenv_home(root)
    if home is None:
        return []
    name = path.name
    if sys.platform == "win32":
        return [home / "Lib" / "venv" / "scripts" / "nt" / name, home / name]
    candidates = [home / name]
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for alt in (ver, "python3", "python"):
        if (home / alt) not in candidates:
            candidates.append(home / alt)
    return candidates


def repair_hint(path: Path) -> str:
    srcs = repair_sources(path)
    # Plain words only: `cli_shim` embeds this in a cmd.exe `if (...)` block,
    # where `<`, `>` or `)` in an echo line break the parse (CI, 2026-09-22).
    src = (
        str(srcs[0]) if srcs else "the base python this venv was created from, per pyvenv.cfg home"
    )
    if sys.platform == "win32":
        return (
            f"Rename-Item '{path}' '{path.name}.clobbered.bak'; Copy-Item '{src}' '{path}' "
            "— or `npm install -g agent-takkub --force` to reprovision the venv"
        )
    return (
        f"mv '{path}' '{path}.clobbered.bak' && ln -s '{src}' '{path}' "
        "— or `npm install -g agent-takkub --force` to reprovision the venv"
    )


def repair(path: Path) -> tuple[bool, str]:
    """Restore a clobbered interpreter file in place. Never raises.

    The broken file is moved aside as ``<name>.clobbered-<ts>.bak`` (kept as
    evidence, and because a rename succeeds where an in-place overwrite of a
    locked file would not — #341). Returns ``(ok, message)``."""
    problem = python_executable_problem(path)
    if problem is None:
        return True, f"{path} already healthy"
    src = next((s for s in repair_sources(path) if python_executable_problem(s) is None), None)
    if src is None:
        return False, f"no healthy source to restore {path} from — {repair_hint(path)}"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.clobbered-{stamp}.bak")
    if path.exists() or path.is_symlink():
        try:
            os.replace(path, backup)
        except OSError:
            try:
                path.unlink()
            except OSError as exc:
                return False, f"cannot move {path} aside ({exc}) — {repair_hint(path)}"
    try:
        if sys.platform == "win32":
            shutil.copy2(src, path)
        else:
            try:
                os.symlink(src, path)
            except OSError:
                shutil.copy2(src, path)
                os.chmod(
                    path, 0o700
                )  # owner-only: per-user cockpit venv, nothing else runs it (CodeQL #57)
    except OSError as exc:
        return False, f"restore {path} from {src} failed ({exc}) — {repair_hint(path)}"
    after = python_executable_problem(path)
    if after is not None:
        return False, f"{path} still {after} after restore from {src}"
    return True, f"restored {path} from {src} (broken copy kept as {backup.name})"
