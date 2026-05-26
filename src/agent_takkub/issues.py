"""Issue tracker for agent-takkub cockpit — GitHub Issues backend.

All operations delegate to the `gh` CLI. Repo is auto-detected from the
project's working directory via `gh repo view`, so `takkub issue new` filed
from an unirecon pane goes to the unirecon repo, not agent-takkub's.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_SEVERITY_VALUES = ("low", "med", "high")

# Label colour map used when auto-creating missing labels.
_LABEL_COLORS: dict[str, str] = {
    "severity:high": "#d73a4a",
    "severity:med": "#fbca04",
    "severity:low": "#fef2c0",
}
_ROLE_LABEL_COLOR = "#c5def5"
_ROLES = (
    "frontend",
    "backend",
    "mobile",
    "devops",
    "qa",
    "reviewer",
    "critic",
    "codex",
    "gemini",
)


# ── gh helpers ────────────────────────────────────────────────────────────────


def _gh(*args: str, cwd: str | Path | None = None, input_text: str | None = None) -> str:
    """Run gh CLI, return stdout. Raises RuntimeError on non-zero exit."""
    _require_gh()
    cmd = ["gh", *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd) if cwd else None,
        input=input_text,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh exited {result.returncode}")
    return result.stdout.strip()


def _require_gh() -> None:
    import shutil

    if not shutil.which("gh"):
        raise RuntimeError(
            "gh CLI not found — install from https://cli.github.com/ and authenticate with 'gh auth login'"
        )


def _detect_repo(cwd: str | Path | None = None) -> str:
    """Return 'owner/repo' for the git remote in cwd. Raises RuntimeError if not GitHub."""
    try:
        repo = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner", cwd=cwd)
    except RuntimeError as exc:
        msg = str(exc)
        if (
            "not a git repository" in msg.lower()
            or "no git remote" in msg.lower()
            or "Could not resolve" in msg.lower()
        ):
            raise RuntimeError(
                f"no GitHub remote found in {cwd or '.'}. "
                "Create a repo with 'gh repo create' or set a remote with 'git remote add origin <url>'"
            ) from exc
        raise RuntimeError(f"cannot detect GitHub repo: {msg}") from exc
    if not repo:
        raise RuntimeError(f"directory {cwd or '.'} has no GitHub remote")
    return repo


def _ensure_label(label: str, color: str, repo: str, cwd: str | Path | None = None) -> None:
    """Create label if it doesn't exist; ignore 'already exists' error."""
    try:
        _gh("label", "create", label, "--color", color.lstrip("#"), "--repo", repo, cwd=cwd)
    except RuntimeError as exc:
        if "already exists" in str(exc).lower():
            return
        raise


def _ensure_labels(labels: list[str], repo: str, cwd: str | Path | None = None) -> None:
    """Ensure all needed labels exist in the repo."""
    for label in labels:
        if label in _LABEL_COLORS:
            color = _LABEL_COLORS[label]
        elif label.startswith("role:"):
            color = _ROLE_LABEL_COLOR
        elif label.startswith("noticed-in:"):
            color = "#e4e669"
        else:
            color = "#ededed"
        _ensure_label(label, color, repo, cwd=cwd)


# ── public API ────────────────────────────────────────────────────────────────


def new_issue(
    title: str,
    body: str,
    *,
    severity: str = "med",
    noticed_in: str | None = None,
    role: str | None = None,
    tags: list[str] | None = None,
    cwd: str | Path | None = None,
) -> tuple[int, str]:
    """Create a GitHub issue. Returns (number, url)."""
    if not title.strip():
        raise ValueError("title must not be empty")
    if severity not in _SEVERITY_VALUES:
        raise ValueError(f"severity must be one of {_SEVERITY_VALUES}, got {severity!r}")

    repo = _detect_repo(cwd)

    labels: list[str] = [f"severity:{severity}"]
    if role:
        labels.append(f"role:{role}")
    if noticed_in:
        labels.append(f"noticed-in:{noticed_in}")
    if tags:
        labels.extend(tags)

    _ensure_labels(labels, repo, cwd=cwd)

    gh_args = ["issue", "create", "--repo", repo, "--title", title, "--body", body or ""]
    for lbl in labels:
        gh_args += ["--label", lbl]

    out = _gh(*gh_args, cwd=cwd)
    # gh returns the URL as last line
    url = out.splitlines()[-1] if out else ""
    # Extract number from URL: .../issues/123
    number = 0
    if url:
        try:
            number = int(url.rstrip("/").rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            pass
    return number, url


def list_issues(
    *,
    filter_open: bool = False,
    filter_closed: bool = False,
    noticed_in: str | None = None,
    role: str | None = None,
    severity: str | None = None,
    cwd: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return list of issue dicts from GitHub matching filters."""
    repo = _detect_repo(cwd)

    # Determine state
    if filter_open and not filter_closed:
        state = "open"
    elif filter_closed and not filter_open:
        state = "closed"
    else:
        state = "all"

    gh_args = [
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--json",
        "number,title,state,labels,url,createdAt,closedAt",
        "--limit",
        "200",
    ]

    if severity:
        gh_args += ["--label", f"severity:{severity}"]
    if role:
        gh_args += ["--label", f"role:{role}"]
    if noticed_in:
        gh_args += ["--label", f"noticed-in:{noticed_in}"]

    out = _gh(*gh_args, cwd=cwd)
    if not out:
        return []

    raw = json.loads(out)
    results = []
    for item in raw:
        label_names = [lb["name"] for lb in item.get("labels", [])]
        sev = next((lb.split(":")[-1] for lb in label_names if lb.startswith("severity:")), "")
        r = next((lb.split(":")[-1] for lb in label_names if lb.startswith("role:")), "")
        ni = next(
            (lb.split("noticed-in:", 1)[-1] for lb in label_names if lb.startswith("noticed-in:")),
            "",
        )
        extra_tags = [
            lb
            for lb in label_names
            if not lb.startswith("severity:")
            and not lb.startswith("role:")
            and not lb.startswith("noticed-in:")
        ]
        results.append(
            {
                "number": item["number"],
                "title": item["title"],
                "status": item["state"].lower(),
                "severity": sev,
                "role": r,
                "noticed_in": ni,
                "tags": extra_tags,
                "url": item["url"],
                "created_at": item.get("createdAt", ""),
                "closed_at": item.get("closedAt") or "",
            }
        )
    return results


def close_issue(
    issue_id: str,
    *,
    note: str = "",
    cwd: str | Path | None = None,
) -> str:
    """Close a GitHub issue by number. Returns the issue URL."""
    number = _parse_issue_number(issue_id)
    repo = _detect_repo(cwd)

    gh_args = ["issue", "close", str(number), "--repo", repo]
    if note:
        gh_args += ["--comment", note]

    _gh(*gh_args, cwd=cwd)
    return f"https://github.com/{repo}/issues/{number}"


def show_issue(issue_id: str, *, cwd: str | Path | None = None) -> str:
    """Return rendered issue text from GitHub."""
    number = _parse_issue_number(issue_id)
    repo = _detect_repo(cwd)
    return _gh("issue", "view", str(number), "--repo", repo, cwd=cwd)


# ── ID parsing ────────────────────────────────────────────────────────────────


def _parse_issue_number(issue_id: str) -> int:
    """Accept '123', '#123', 'owner/repo#123' — return int. Raises ValueError."""
    s = str(issue_id).strip()
    if "#" in s:
        s = s.rsplit("#", 1)[-1]
    s = s.lstrip("#")
    try:
        n = int(s)
        if n <= 0:
            raise ValueError
        return n
    except ValueError:
        raise ValueError(
            f"invalid issue ID {issue_id!r} — expected GitHub issue number (e.g. 123, #123)"
        ) from None


# ── CLI helpers ────────────────────────────────────────────────────────────────


def _safe_print(text: str, **kwargs) -> None:
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc), **kwargs)


def _safe_col(val: str, width: int) -> str:
    return val.replace("\n", " ").replace("\r", "")[:width]


# ── CLI entry points (called from cli.py) ─────────────────────────────────────


def cmd_issue_new(args: Any) -> dict:
    """Handler for `takkub issue new`."""
    title: str = args.title
    body: str = args.body or ""

    if not body:
        if not sys.stdin.isatty():
            return {
                "ok": False,
                "msg": 'no --body provided and no TTY — pass --body "<text>" explicitly',
            }
        import os
        import shlex
        import subprocess
        import tempfile

        editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "notepad"))
        with tempfile.NamedTemporaryFile(
            suffix=".md", delete=False, mode="w", encoding="utf-8"
        ) as f:
            tmppath = f.name
        try:
            ret = subprocess.call([*shlex.split(editor), tmppath])
            if ret != 0:
                return {"ok": False, "msg": f"editor exited with code {ret}"}
            body = Path(tmppath).read_text(encoding="utf-8")
        finally:
            try:
                os.unlink(tmppath)
            except OSError:
                pass

    if getattr(args, "issues_dir", None):
        print(
            "warn: --issues-dir is deprecated and ignored (issues now stored in GitHub)",
            file=sys.stderr,
        )

    tags = [t.strip() for t in args.tag.split(",")] if getattr(args, "tag", None) else None
    cwd = getattr(args, "cwd", None)

    try:
        number, url = new_issue(
            title,
            body,
            severity=getattr(args, "severity", "med") or "med",
            noticed_in=getattr(args, "noticed_in", None),
            role=getattr(args, "role", None),
            tags=tags or None,
            cwd=cwd,
        )
    except (ValueError, RuntimeError) as exc:
        return {"ok": False, "msg": str(exc)}

    print(f"#{number}  {url}")
    return {"ok": True, "msg": f"created #{number}"}


def cmd_issue_list(args: Any) -> dict:
    """Handler for `takkub issue list`."""
    if getattr(args, "issues_dir", None):
        print("warn: --issues-dir is deprecated and ignored", file=sys.stderr)

    cwd = getattr(args, "cwd", None)
    try:
        items = list_issues(
            filter_open=getattr(args, "open", False),
            filter_closed=getattr(args, "closed", False),
            noticed_in=getattr(args, "noticed_in", None),
            role=getattr(args, "role", None),
            severity=getattr(args, "severity", None),
            cwd=cwd,
        )
    except (ValueError, RuntimeError) as exc:
        return {"ok": False, "msg": str(exc)}

    if not items:
        _safe_print("(no issues)")
        return {"ok": True, "msg": "0 issue(s)"}

    _safe_print(f"{'#':<6} {'SEV':<5} {'STATUS':<8} {'ROLE':<12} {'NOTICED_IN':<14} TITLE")
    _safe_print("-" * 80)
    for item in items:
        _safe_print(
            f"#{item['number']:<5} "
            f"{item.get('severity', ''):<5} "
            f"{item.get('status', ''):<8} "
            f"{item.get('role', ''):<12} "
            f"{item.get('noticed_in', ''):<14} "
            f"{_safe_col(item.get('title', ''), 50)}"
        )
    return {"ok": True, "msg": f"{len(items)} issue(s)"}


def cmd_issue_close(args: Any) -> dict:
    """Handler for `takkub issue close`."""
    if getattr(args, "issues_dir", None):
        print("warn: --issues-dir is deprecated and ignored", file=sys.stderr)

    cwd = getattr(args, "cwd", None)
    try:
        url = close_issue(
            args.id,
            note=getattr(args, "note", "") or "",
            cwd=cwd,
        )
    except (ValueError, RuntimeError) as exc:
        return {"ok": False, "msg": str(exc)}

    print(f"closed: {url}")
    return {"ok": True, "msg": f"closed #{args.id}"}


def cmd_issue_show(args: Any) -> dict:
    """Handler for `takkub issue show`."""
    if getattr(args, "issues_dir", None):
        print("warn: --issues-dir is deprecated and ignored", file=sys.stderr)

    cwd = getattr(args, "cwd", None)
    try:
        content = show_issue(args.id, cwd=cwd)
    except (ValueError, RuntimeError) as exc:
        return {"ok": False, "msg": str(exc)}

    _safe_print(content, end="")
    return {"ok": True, "msg": ""}
