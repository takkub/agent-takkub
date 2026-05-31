"""takkub release — one-shot version bump + changelog roll + git tag.

Historically the 14 SemVer tags (v0.1.0 … v0.3.9) were cut by hand and the
CHANGELOG's `## [vNEXT]` section was never rolled into a dated version
heading. This automates the whole ceremony:

  1. read the current version from pyproject.toml
  2. bump it (major/minor/patch) or take an explicit --version
  3. rewrite pyproject's `version = "..."`
  4. roll CHANGELOG: rename `## [vNEXT]` → `## [vX.Y.Z] - <date>` and drop a
     fresh empty `## [vNEXT]` back on top
  5. git commit (pyproject.toml + CHANGELOG.md only) + annotated tag vX.Y.Z

Pushing is left to the user (`git push --follow-tags`) — consistent with the
cockpit's never-auto-push rule.

The string transforms are pure (unit-tested); only `release()` touches the
filesystem and git.
"""

from __future__ import annotations

import datetime
import pathlib
import re
import subprocess

_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_VNEXT = "## [vNEXT]"


def bump_version(current: str, part: str) -> str:
    """Bump a SemVer 'X.Y.Z' by part ∈ {major, minor, patch}."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", current.strip())
    if not m:
        raise ValueError(f"not a SemVer X.Y.Z version: {current!r}")
    major, minor, patch = (int(x) for x in m.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump part: {part!r} (want major/minor/patch)")


def read_pyproject_version(text: str) -> str:
    m = _VERSION_RE.search(text)
    if not m:
        raise ValueError('no `version = "..."` line in pyproject.toml')
    return m.group(1)


def set_pyproject_version(text: str, version: str) -> str:
    new, n = _VERSION_RE.subn(f'version = "{version}"', text, count=1)
    if n == 0:
        raise ValueError('no `version = "..."` line to update in pyproject.toml')
    return new


def roll_changelog(text: str, version: str, date: str) -> str:
    """Rename the current `## [vNEXT]` section heading to the released
    version + date, and insert a fresh empty `## [vNEXT]` above it so the
    next cycle has somewhere to write. Raises if there's no vNEXT heading."""
    if _VNEXT not in text:
        raise ValueError(f"no '{_VNEXT}' section in CHANGELOG.md — nothing to roll")
    replacement = f"{_VNEXT}\n\n## [v{version}] - {date}"
    return text.replace(_VNEXT, replacement, 1)


def changelog_has_entries(text: str) -> bool:
    """True if the `## [vNEXT]` section has any non-blank content before the
    next `## ` version heading. Guards against cutting a contentless release
    (version bumped + tagged but the changelog says nothing changed)."""
    idx = text.find(_VNEXT)
    if idx == -1:
        return False
    after = text[idx + len(_VNEXT) :]
    m = re.search(r"\n## ", after)  # next version heading (### sub-headings don't match)
    body = after[: m.start()] if m else after
    return bool(body.strip())


def _semver_tuple(v: str) -> tuple[int, int, int]:
    a, b, c = (int(x) for x in v.split("."))
    return (a, b, c)


def _git(repo_root: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _tag_exists(repo_root: pathlib.Path, tag: str) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "tag", "-l", tag],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return tag in out.stdout.split()


def release(
    repo_root: str | pathlib.Path,
    part: str = "patch",
    explicit_version: str | None = None,
    do_commit: bool = True,
    do_tag: bool = True,
    dry_run: bool = False,
    allow_empty: bool = False,
    today: str | None = None,
) -> dict:
    """Run the release ceremony. Returns a summary dict. With dry_run=True
    nothing on disk or in git is touched — but all correctness guards still
    run, so `--dry-run` doubles as a preflight check.

    Guards (raise ValueError, abort before any write):
      - explicit --version must be X.Y.Z and strictly newer than current
      - `## [vNEXT]` must have entries (unless allow_empty) — no contentless release
      - the git tag must not already exist
    """
    repo_root = pathlib.Path(repo_root)
    pyproject = repo_root / "pyproject.toml"
    changelog = repo_root / "CHANGELOG.md"

    pp_text = pyproject.read_text(encoding="utf-8")
    current = read_pyproject_version(pp_text)

    if explicit_version is not None:
        ver = explicit_version.strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+", ver):
            raise ValueError(f"--version must be SemVer X.Y.Z, got {explicit_version!r}")
        if _semver_tuple(ver) <= _semver_tuple(current):
            raise ValueError(f"{ver} is not newer than the current version {current}")
        new_version = ver
    else:
        new_version = bump_version(current, part)

    date = today or datetime.date.today().isoformat()
    tag = f"v{new_version}"

    cl_text = changelog.read_text(encoding="utf-8")
    if not allow_empty and not changelog_has_entries(cl_text):
        raise ValueError(
            "## [vNEXT] has no changelog entries — document what changed first, "
            "or pass --allow-empty to release anyway"
        )
    if do_tag and _tag_exists(repo_root, tag):
        raise ValueError(f"git tag {tag} already exists — pick a different version")

    # compute both transforms up front so a changelog error aborts before we
    # write a half-done pyproject
    new_pp = set_pyproject_version(pp_text, new_version)
    new_cl = roll_changelog(cl_text, new_version, date)

    summary = {
        "current": current,
        "new_version": new_version,
        "tag": tag,
        "date": date,
        "dry_run": dry_run,
        "committed": False,
        "tagged": False,
    }
    if dry_run:
        return summary

    pyproject.write_text(new_pp, encoding="utf-8")
    changelog.write_text(new_cl, encoding="utf-8")

    if do_commit:
        _git(repo_root, "add", "pyproject.toml", "CHANGELOG.md")
        _git(repo_root, "commit", "-m", f"chore(release): {tag}")
        summary["committed"] = True
    if do_tag:
        _git(repo_root, "tag", "-a", tag, "-m", tag)
        summary["tagged"] = True
    return summary
