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
  6. (default) push + create the GitHub Release with the rolled changelog
     section as its notes, so the release shows up on the Releases page

Step 6 is on by default (`do_github_release=True`; CLI `--no-github-release`
to skip). Without it, a `git push --follow-tags` puts the tag on GitHub but
the Releases page stays empty — the gap that left v0.4.0–v0.5.1 unpublished.
`--no-github-release` reverts to the old "commit + tag only, push left to the
user" behaviour.

The string transforms are pure (unit-tested); only `release()`,
`create_github_release()` touch the filesystem / git / gh.
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


def extract_release_notes(text: str, version: str) -> str:
    """Return the body of the `## [vX.Y.Z]` changelog section (heading
    excluded), up to the next `## ` version heading. Empty string if the
    version's section isn't found. Used as the GitHub Release notes.

    Matches both `## [v0.5.1] - date` and the older un-prefixed `## [0.3.8]`.
    """
    pat = re.compile(rf"(?m)^## \[v?{re.escape(version)}\][^\n]*$")
    m = pat.search(text)
    if not m:
        return ""
    after = text[m.end() :]
    nxt = re.search(r"(?m)^## ", after)
    body = after[: nxt.start()] if nxt else after
    return body.strip()


def create_github_release(
    repo_root: str | pathlib.Path,
    tag: str,
    title: str,
    notes: str,
    *,
    push: bool = True,
) -> tuple[bool, str]:
    """Push (so the tag reaches the remote) then `gh release create`.

    Returns (ok, url-or-error). Best-effort: a missing `gh`, no remote, or a
    network error returns (False, reason) WITHOUT raising — the local commit +
    tag from `release()` already succeeded, so a publish hiccup must not look
    like a failed release. Notes go through a temp file (not an argv string) so
    multi-line Thai content can't hit quoting / length limits.
    """
    import shutil

    repo_root = pathlib.Path(repo_root)
    if not shutil.which("gh"):
        return False, "gh CLI not found — run `gh release create` manually once installed"
    if push:
        try:
            subprocess.run(
                ["git", "-C", str(repo_root), "push", "--follow-tags"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or "git push failed").strip().splitlines()
            return False, f"git push failed: {tail[-1] if tail else 'unknown'}"
    notes_file = repo_root / "runtime" / f"relnotes-{tag}.md"
    try:
        notes_file.parent.mkdir(parents=True, exist_ok=True)
        notes_file.write_text(notes or title, encoding="utf-8")
        proc = subprocess.run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--verify-tag",
                "--title",
                title,
                "--notes-file",
                str(notes_file),
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
    except OSError as e:
        return False, f"gh release create failed: {e}"
    finally:
        try:
            notes_file.unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        tail = (proc.stderr or "gh release create failed").strip().splitlines()
        return False, tail[-1] if tail else "gh release create failed"
    out = (proc.stdout or "").strip().splitlines()
    return True, (out[-1] if out else "")


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
    do_github_release: bool = True,
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
        "github_released": False,
        "github_url": "",
        "github_error": "",
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

    # Publish the GitHub Release (push + gh release create) so the changelog
    # shows on the Releases page. Needs a real commit+tag to push, so it's
    # gated on both. Best-effort: a publish failure is recorded, not raised —
    # the local release already happened.
    if do_github_release and do_commit and do_tag:
        notes = extract_release_notes(new_cl, new_version)
        ok, msg = create_github_release(repo_root, tag, tag, notes)
        if ok:
            summary["github_released"] = True
            summary["github_url"] = msg
        else:
            summary["github_error"] = msg
    return summary
