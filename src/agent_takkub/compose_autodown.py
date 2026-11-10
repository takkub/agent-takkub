"""#984: take a project's local docker compose stacks down when its work ends.

Containers a pane started (`docker compose up` for e2e, a staging copy, a DB)
used to run on for days after the work finished or shipped to prod — nothing
in the cockpit ever stopped them, and the RAM stays pinned inside Docker's VM.

Triggers (wired in the orchestrator): `takkub end-session` with no teammate
pane left, closing the project tab, and a devops done that confirms a prod
deploy. Each runs `docker compose -p <name> down` — containers + networks go,
named volumes and images stay (never `-v` / `--rmi`). A down also removes the
project's Exited containers, which is the leftover-cleanup half of the issue.

Ownership is decided by the compose label
``com.docker.compose.project.working_dir``: a stack belongs to the project
whose configured path (or managed worktree root) contains that directory most
specifically. A stack whose working dir is equally claimed by another OPEN
tab is skipped as shared. Containers not created by compose carry no label
and are never looked at. There is deliberately no per-project switch: the
user does not edit projects.json — the safety rules above decide on their own.

Every docker call has a timeout and never raises — Docker not installed or not
running reads as "nothing to do".
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

LABEL_PROJECT = "com.docker.compose.project"
LABEL_WORKDIR = "com.docker.compose.project.working_dir"
_PS_TIMEOUT = 15
_STATS_TIMEOUT = 20
_DOWN_TIMEOUT = 180


@dataclass
class Container:
    name: str
    state: str
    compose_project: str
    working_dir: str


@dataclass
class AutoDownReport:
    project: str
    trigger: str
    downed: list[tuple[str, list[str]]] = field(default_factory=list)
    freed_mib: float = 0.0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    docker_unavailable: bool = False

    @property
    def acted(self) -> bool:
        return bool(self.downed or self.failed or self.skipped)

    def summary(self) -> str:
        parts: list[str] = []
        if self.downed:
            names = ", ".join(n for _stack, ns in self.downed for n in ns)
            stacks = ", ".join(s for s, _ in self.downed)
            mib = self.freed_mib
            freed = (
                f" · คืน RAM ~{mib:.0f} MiB"
                if mib >= 10
                else (f" · คืน RAM ~{mib:.1f} MiB" if mib else "")
            )
            parts.append(f"🐳 down {stacks} ({names}){freed}")
        if self.failed:
            parts.append(
                "⚠️ down ไม่สำเร็จ: "
                + "; ".join(f"{s}: {why[:160]}" for s, why in self.failed)
                + " — container ยังรันอยู่ ปิดเองด้วย `docker compose -p <name> down`"
            )
        if self.skipped:
            parts.append("ข้าม: " + "; ".join(f"{s} ({why})" for s, why in self.skipped))
        return f"[compose auto-down · {self.trigger}] " + " · ".join(parts) if parts else ""


def _docker(args: list[str], timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except FileNotFoundError:
        return 127, "docker not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"docker {' '.join(args[:2])} timed out after {timeout}s"
    except Exception as exc:  # never raise into a close/done path
        return 1, f"docker error: {exc}"
    out = proc.stdout or ""
    if proc.returncode != 0:
        out = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode, out


def list_compose_containers() -> list[Container] | None:
    """Every compose-labelled container (running or not); None when docker is
    missing or its daemon is not answering."""
    fmt = "\t".join(
        (
            "{{.Names}}",
            "{{.State}}",
            '{{.Label "' + LABEL_PROJECT + '"}}',
            '{{.Label "' + LABEL_WORKDIR + '"}}',
        )
    )
    rc, out = _docker(
        ["ps", "-a", "--filter", f"label={LABEL_PROJECT}", "--format", fmt], _PS_TIMEOUT
    )
    if rc != 0:
        return None
    found: list[Container] = []
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) < 4 or not cols[2].strip():
            continue
        found.append(
            Container(cols[0].strip(), cols[1].strip().lower(), cols[2].strip(), cols[3].strip())
        )
    return found


def _norm(path: str | Path) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(raw))))


def _match_len(working_dir: str, roots: Iterable[str | Path]) -> int:
    """Length of the most specific root containing *working_dir* (0 = none)."""
    wd = _norm(working_dir)
    best = 0
    if not wd:
        return 0
    for root in roots:
        r = _norm(root)
        if r and (wd == r or wd.startswith(r.rstrip("\\/") + os.sep)) and len(r) > best:
            best = len(r)
    return best


# #985: a stack named for a long-running environment (`evcstg`, `app-staging`,
# `shop_prod`, `uat2`) is one devops brought up ON PURPOSE to stay up — a
# staging copy the user reached through a tunnel went down right after the
# devops done that deployed it. Word-bounded, or a trailing suffix glued onto
# the project slug (compose names are often `<slug><env>`).
_LONG_RUNNING_STACK_RE = re.compile(
    r"(?:^|[-_.])(?:stg|stage|staging|prod|production|preprod|uat)(?:\d*)(?:$|[-_.])"
    r"|(?:stg|staging|prod|uat)\d*$",
    re.IGNORECASE,
)


def is_long_running_stack(stack: str) -> bool:
    return bool(_LONG_RUNNING_STACK_RE.search(stack or ""))


def plan_stacks(
    containers: Iterable[Container],
    own_roots: Iterable[str | Path],
    other_open: Mapping[str, Iterable[str | Path]],
) -> tuple[dict[str, list[Container]], list[tuple[str, str]]]:
    """Split compose stacks into (ours → containers, skipped → reason)."""
    own = [str(r) for r in own_roots]
    others = {name: [str(r) for r in roots] for name, roots in other_open.items()}
    by_stack: dict[str, list[Container]] = {}
    for c in containers:
        by_stack.setdefault(c.compose_project, []).append(c)
    ours: dict[str, list[Container]] = {}
    skipped: list[tuple[str, str]] = []
    for stack, members in sorted(by_stack.items()):
        own_len = max((_match_len(c.working_dir, own) for c in members), default=0)
        if not own_len:
            continue
        sharer = next(
            (
                name
                for name, roots in others.items()
                if max((_match_len(c.working_dir, roots) for c in members), default=0) >= own_len
            ),
            None,
        )
        if sharer:
            skipped.append((stack, f"แท็บ {sharer} ที่เปิดอยู่ใช้ stack นี้ด้วย"))
            continue
        if is_long_running_stack(stack):
            skipped.append((stack, "ชื่อ stack เป็น staging/prod — ตั้งใจให้รันค้าง ไม่ down อัตโนมัติ"))
            continue
        ours[stack] = members
    return ours, skipped


_MEM_RE = re.compile(r"([\d.]+)\s*([KMGT]?i?B)", re.IGNORECASE)
_UNIT_MIB = {"b": 1 / 1048576, "kib": 1 / 1024, "kb": 1 / 1024, "mib": 1.0, "mb": 1.0}
_UNIT_MIB.update({"gib": 1024.0, "gb": 1024.0, "tib": 1048576.0, "tb": 1048576.0})


def memory_mib(names: list[str]) -> float:
    """Current memory of the named running containers, in MiB (best-effort)."""
    if not names:
        return 0.0
    rc, out = _docker(
        ["stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}", *names], _STATS_TIMEOUT
    )
    if rc != 0:
        return 0.0
    total = 0.0
    for line in out.splitlines():
        _name, _, usage = line.partition("\t")
        m = _MEM_RE.match(usage.strip())
        if m:
            total += float(m.group(1)) * _UNIT_MIB.get(m.group(2).lower(), 0.0)
    return total


def run_auto_down(
    project: str,
    trigger: str,
    own_roots: Iterable[str | Path],
    other_open: Mapping[str, Iterable[str | Path]],
) -> AutoDownReport:
    report = AutoDownReport(project=project, trigger=trigger)
    containers = list_compose_containers()
    if containers is None:
        report.docker_unavailable = True
        return report
    ours, report.skipped = plan_stacks(containers, own_roots, other_open)
    for stack, members in ours.items():
        running = [c.name for c in members if c.state in ("running", "restarting", "paused")]
        freed = memory_mib(running)
        rc, out = _docker(["compose", "-p", stack, "down"], _DOWN_TIMEOUT)
        if rc == 0:
            report.downed.append((stack, [c.name for c in members]))
            report.freed_mib += freed
        else:
            report.failed.append((stack, out or f"exit {rc}"))
    return report


_PROD_RE = re.compile(r"\bprod(?:uction)?\b|โปรดักชัน|โปรดักชั่น", re.IGNORECASE)
_DEPLOY_RE = re.compile(r"deploy|release|rollout|ดีพลอย|ขึ้น\s*prod", re.IGNORECASE)
_HEALTHY_RE = re.compile(
    r"health(?:y|check)?\s*(?:ok|pass|passed|green|200)|healthy|\b200\b|สำเร็จ|ผ่าน|success|succeeded",
    re.IGNORECASE,
)
_FAILED_RE = re.compile(
    r"\bfail|rollback|rolled back|ไม่ผ่าน|ล้มเหลว|ไม่สำเร็จ|error|blocked|unhealthy", re.IGNORECASE
)


def is_prod_deploy_success(note: str) -> bool:
    """A done note that states a prod deploy finished healthy — all of: prod,
    deploy, a success/health word, and no failure word anywhere."""
    text = note or ""
    return bool(
        _PROD_RE.search(text)
        and _DEPLOY_RE.search(text)
        and _HEALTHY_RE.search(text)
        and not _FAILED_RE.search(text)
    )
