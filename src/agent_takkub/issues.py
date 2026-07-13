"""Issue tracker for agent-takkub cockpit — GitHub Issues backend.

All operations delegate to the `gh` CLI.

**Routing default = agent-takkub.** The cockpit's issue tracker is for
cockpit/orchestrator/CLI/UI bugs, so `new_issue` defaults `cockpit_bug=True`:
issues land on the **agent-takkub install repo** regardless of which project's
pane filed them. An agent forgetting a flag can no longer leak a cockpit bug
onto, say, the app-api repo. To deliberately file against the *active project's*
repo (cwd-detected), pass `cockpit_bug=False` (CLI: `--no-cockpit-bug`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import DATA_HOME, REPO_ROOT

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


def _gh(
    *args: str,
    cwd: str | Path | None = None,
    input_text: str | None = None,
    timeout: int = 30,
) -> str:
    """Run gh CLI, return stdout. Raises RuntimeError on non-zero exit.

    A stalled auth/network/credential-helper call would otherwise block the
    caller (and the Qt main thread) indefinitely, so every gh invocation is
    bounded by `timeout` seconds — read/view default to 30s, mutating ops
    (create/close) pass a longer value (issue #12).
    """
    _require_gh()
    cmd = ["gh", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(cwd) if cwd else None,
            input=input_text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"gh {' '.join(args[:2])} timed out after {timeout}s (network/auth stalled?)"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh exited {result.returncode}")
    return result.stdout.strip()


def _warn_local_fallback(op: str, reason: str = "") -> None:
    """Emit a visible stderr warning when an issue op silently falls back to
    the local .takkub_issues.json store because gh was unavailable. Without
    this, a local backlog stays invisible once gh recovers (issue #12)."""
    tail = f" ({reason})" if reason else ""
    print(
        f"⚠ takkub issue: gh unavailable — {op} used the local "
        f".takkub_issues.json store instead of GitHub{tail}. "
        "These items are NOT on GitHub; reconcile once gh works again.",
        file=sys.stderr,
    )


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


def _local_store_cwd(cwd: str | Path | None) -> str | Path | None:
    """Where the LOCAL fallback ``.takkub_issues.json`` actually lives, given
    the cwd used for git-remote detection.

    Cockpit-bug operations detect the repo at ``REPO_ROOT`` (that's where the
    agent-takkub git remote lives, dev checkout or not) — but in an installed
    build ``REPO_ROOT`` resolves into a read-only/ephemeral venv ancestor, so
    local issues filed there would vanish on the next ``pip install --upgrade``
    (see docs/audit/2026-07-05-installed-build-audit-gemini.md, finding 3).
    Redirect just the local-fallback file to ``DATA_HOME`` instead — a no-op
    in a dev checkout, where ``DATA_HOME == REPO_ROOT`` already.
    """
    if cwd is not None and Path(cwd).resolve() == REPO_ROOT.resolve():
        return DATA_HOME
    return cwd


def _get_local_issues_path(cwd: str | Path | None) -> Path:
    return Path(cwd or ".").resolve() / ".takkub_issues.json"


def _load_local_issues(cwd: str | Path | None) -> list[dict[str, Any]]:
    path = _get_local_issues_path(cwd)
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            issues = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read local issue store {path}: {exc}") from exc
    if not isinstance(issues, list):
        raise RuntimeError(f"local issue store {path} must contain a JSON list")
    return issues


def _save_local_issues(issues: list[dict[str, Any]], cwd: str | Path | None) -> None:
    path = _get_local_issues_path(cwd)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(issues, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"could not save local issue store {path}: {exc}") from exc


def new_issue(
    title: str,
    body: str,
    *,
    severity: str = "med",
    noticed_in: str | None = None,
    role: str | None = None,
    tags: list[str] | None = None,
    cwd: str | Path | None = None,
    cockpit_bug: bool = True,
) -> tuple[int, str]:
    """Create an issue. Returns (number, url). Falls back to local store if GitHub is unavailable.

    `cockpit_bug` (default **True**) files the issue against the agent-takkub
    install repo (REPO_ROOT's git remote) regardless of cwd — the cockpit's
    tracker is for cockpit/orchestrator/CLI/UI bugs, so this is the safe
    default that stops a bug noticed inside e.g. an app-api pane from leaking
    onto the app-api repo. `noticed_in` still records where the bug surfaced
    (useful context, independent of the routing target).

    Pass `cockpit_bug=False` to deliberately route to the *active project's*
    repo via cwd-based `gh repo view` detection (CLI: `--no-cockpit-bug`).
    """
    if not title.strip():
        raise ValueError("title must not be empty")
    if severity not in _SEVERITY_VALUES:
        raise ValueError(f"severity must be one of {_SEVERITY_VALUES}, got {severity!r}")

    detect_cwd: str | Path | None = str(REPO_ROOT) if cockpit_bug else cwd
    try:
        repo = _detect_repo(detect_cwd)
        use_local = False
    except RuntimeError:
        use_local = True

    labels: list[str] = [f"severity:{severity}"]
    if role:
        labels.append(f"role:{role}")
    if noticed_in:
        labels.append(f"noticed-in:{noticed_in}")
    if tags:
        labels.extend(tags)

    if not use_local:
        try:
            _ensure_labels(labels, repo, cwd=detect_cwd)
        except RuntimeError as exc:
            print(
                f"warn: could not ensure GitHub issue labels ({exc}); attempting issue create",
                file=sys.stderr,
            )

        try:
            gh_args = ["issue", "create", "--repo", repo, "--title", title, "--body", body or ""]
            for lbl in labels:
                gh_args += ["--label", lbl]

            out = _gh(*gh_args, cwd=detect_cwd, timeout=60)
            url = out.splitlines()[-1] if out else ""
            number = 0
            if url:
                try:
                    number = int(url.rstrip("/").rsplit("/", 1)[-1])
                except (ValueError, IndexError):
                    pass
            return number, url
        except RuntimeError as exc:
            # Repo was detected but the create failed (network/auth/rate
            # limit) — this is the dangerous silent-divergence case worth a
            # visible warning. A bare no-remote project (outer except) is a
            # legit local-only mode and stays quiet.
            _warn_local_fallback(f"new issue '{title[:40]}'", reason=str(exc)[:60])
            use_local = True

    # Local fallback — when cockpit_bug=True, write to REPO_ROOT's
    # .takkub_issues.json so a flaky `gh` doesn't scatter cockpit-bug
    # JSON files across every project the user touches.
    issues = _load_local_issues(_local_store_cwd(detect_cwd))
    number = max([iss.get("number", 0) for iss in issues] or [0]) + 1
    import datetime

    now_iso = datetime.datetime.now().isoformat() + "Z"
    new_iss = {
        "number": number,
        "title": title,
        "body": body or "",
        "status": "open",
        "severity": severity,
        "role": role or "",
        "noticed_in": noticed_in or "",
        "tags": tags or [],
        "url": f"local://issue/{number}",
        "created_at": now_iso,
        "closed_at": "",
    }
    issues.append(new_iss)
    _save_local_issues(issues, _local_store_cwd(detect_cwd))
    return number, new_iss["url"]


def list_issues(
    *,
    filter_open: bool = False,
    filter_closed: bool = False,
    noticed_in: str | None = None,
    role: str | None = None,
    severity: str | None = None,
    cwd: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return list of issue dicts matching filters. Falls back to local store if GitHub is unavailable."""
    try:
        repo = _detect_repo(cwd)
        use_local = False
    except RuntimeError:
        use_local = True

    if not use_local:
        try:
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
                sev = next(
                    (lb.split(":")[-1] for lb in label_names if lb.startswith("severity:")), ""
                )
                r = next((lb.split(":")[-1] for lb in label_names if lb.startswith("role:")), "")
                ni = next(
                    (
                        lb.split("noticed-in:", 1)[-1]
                        for lb in label_names
                        if lb.startswith("noticed-in:")
                    ),
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
        except RuntimeError:
            use_local = True

    # Local fallback
    issues = _load_local_issues(_local_store_cwd(cwd))
    results = []
    for iss in issues:
        status = iss.get("status", "open").lower()
        if filter_open and not filter_closed and status != "open":
            continue
        if filter_closed and not filter_open and status != "closed":
            continue
        if severity and iss.get("severity") != severity:
            continue
        if role and iss.get("role") != role:
            continue
        if noticed_in and iss.get("noticed_in") != noticed_in:
            continue
        results.append(iss)
    return results


def close_issue(
    issue_id: str,
    *,
    note: str = "",
    cwd: str | Path | None = None,
) -> str:
    """Close an issue by number. Returns the issue URL. Falls back to local store if GitHub is unavailable."""
    number = _parse_issue_number(issue_id)
    try:
        repo = _detect_repo(cwd)
        use_local = False
    except RuntimeError:
        use_local = True

    if not use_local:
        try:
            gh_args = ["issue", "close", str(number), "--repo", repo]
            if note:
                gh_args += ["--comment", note]

            _gh(*gh_args, cwd=cwd, timeout=60)
            return f"https://github.com/{repo}/issues/{number}"
        except RuntimeError as exc:
            _warn_local_fallback(f"close #{number}", reason=str(exc)[:60])
            use_local = True

    # Local fallback
    local_cwd = _local_store_cwd(cwd)
    issues = _load_local_issues(local_cwd)
    found = False
    import datetime

    now_iso = datetime.datetime.now().isoformat() + "Z"
    for iss in issues:
        if iss.get("number") == number:
            iss["status"] = "closed"
            iss["closed_at"] = now_iso
            if note:
                if "comments" not in iss:
                    iss["comments"] = []
                iss["comments"].append({"body": note, "created_at": now_iso})
            found = True
            break
    if not found:
        raise RuntimeError(f"local issue #{number} not found")
    _save_local_issues(issues, local_cwd)
    return f"local://issue/{number}"


def show_issue(issue_id: str, *, cwd: str | Path | None = None) -> str:
    """Return rendered issue text. Falls back to local store if GitHub is unavailable."""
    number = _parse_issue_number(issue_id)
    try:
        repo = _detect_repo(cwd)
        use_local = False
    except RuntimeError:
        use_local = True

    if not use_local:
        try:
            return _gh("issue", "view", str(number), "--repo", repo, cwd=cwd)
        except RuntimeError:
            use_local = True

    # Local fallback
    issues = _load_local_issues(_local_store_cwd(cwd))
    iss = next((i for i in issues if i.get("number") == number), None)
    if not iss:
        raise RuntimeError(f"local issue #{number} not found")

    lines = [
        f"title:\t{iss.get('title')}",
        f"state:\t{iss.get('status', 'open').upper()}",
        "author:\tlocal",
        f"created:\t{iss.get('created_at')}",
        f"severity:\t{iss.get('severity')}",
        f"role:\t{iss.get('role')}",
        f"noticed_in:\t{iss.get('noticed_in')}",
        f"tags:\t{', '.join(iss.get('tags', []))}",
        "",
        iss.get("body", ""),
    ]
    comments = iss.get("comments", [])
    if comments:
        lines.append("\n-- comments --")
        for comment in comments:
            lines.append(f"\ncomment:\t{comment.get('created_at')}")
            lines.append(comment.get("body", ""))
    return "\n".join(lines)


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
            if sys.platform == "win32" and os.path.exists(editor):
                editor_args = [editor]
            else:
                editor_args = shlex.split(editor, posix=sys.platform != "win32")
                if sys.platform == "win32":
                    editor_args = [arg.strip('"') for arg in editor_args]
            try:
                ret = subprocess.call([*editor_args, tmppath])
            except (FileNotFoundError, OSError) as exc:
                return {"ok": False, "msg": f"could not launch editor {editor!r}: {exc}"}
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
            cockpit_bug=getattr(args, "cockpit_bug", True),
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
