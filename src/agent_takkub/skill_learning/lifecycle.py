"""Usage signals + the daemon-free lifecycle (graduation / capacity).

Three signals are kept apart, as in autoharness:
  * use   — the skill was FOLLOWED: `[skill: name]` in the done note (any
            provider, the index asks for it) or a real Skill tool call in the
            transcript (claude). The only thing the survival rate counts.
  * view  — the transcript opened `<...>/skills/<name>/SKILL.md`: recall
            value, not adherence.
  * patch — the promoter improved it (counted by `store.rewrite_skill`).

Requests are opportunity-relative (one done = one request), so a closed
laptop never ages a skill out.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import store
from .settings import LearningSettings

_NOTE_USE_RE = re.compile(r"\[skill:\s*([a-z0-9][a-z0-9-]{1,63})\s*\]", re.IGNORECASE)


def detect_usage(names: set[str], note: str, transcript: str) -> tuple[set[str], set[str]]:
    """(used, viewed) among *names* — never reports a name we don't manage."""
    used = {m.lower() for m in _NOTE_USE_RE.findall(note or "")} & names
    for name in names:
        n = re.escape(name)
        if re.search(rf"\[tool_use Skill\][^\n]*\"skill\":\s*\"(?:[\w-]+:)?{n}\"", transcript):
            used.add(name)
    # Opening the file is only a view; it becomes a use when the agent also
    # declared or invoked the skill (above).
    # `.claude/skills/<name>/`, `<global>/skills/<name>/` and the project
    # store's `project-skills/<ns>/<name>/` all count.
    viewed = {
        name
        for name in names
        if re.search(rf"skills[\\/](?:[^\\/\s]+[\\/])?{re.escape(name)}[\\/]SKILL\.md", transcript)
    }
    return used, viewed - used


def record_usage(project_ns: str, used: set[str], viewed: set[str]) -> None:
    if not used and not viewed:
        return
    now = time.time()
    for rec in store.list_skills(project_ns):
        if rec.name in used:
            store.update_sidecar(rec, uses=int(rec.sidecar.get("uses") or 0) + 1, last_used=now)
        elif rec.name in viewed:
            store.update_sidecar(rec, views=int(rec.sidecar.get("views") or 0) + 1)


@dataclass(frozen=True, slots=True)
class Retirement:
    name: str
    layer: str
    reason: str


def _requests_since(rec: store.SkillRecord, layer_requests: dict[str, int]) -> int:
    return max(0, layer_requests[rec.layer] - int(rec.sidecar.get("requests_at_create") or 0))


def usage_rate(rec: store.SkillRecord, layer_requests: dict[str, int]) -> float:
    since = _requests_since(rec, layer_requests)
    return int(rec.sidecar.get("uses") or 0) / since if since else 0.0


def review(project_ns: str, s: LearningSettings, *, apply: bool = True) -> list[Retirement]:
    """Graduation review then capacity contention, per layer. Archives
    (never deletes) and returns what it retired."""
    layer_requests = {"project": store.requests(project_ns), "global": store.requests(None)}
    maturity = {"project": s.maturity_project, "global": s.maturity_global}
    capacity = {"project": s.capacity_project, "global": s.capacity_global}
    out: list[Retirement] = []
    for layer in store.LAYERS:
        recs = [r for r in store.list_skills(project_ns) if r.layer == layer]
        mature = []
        for rec in recs:
            if _requests_since(rec, layer_requests) < maturity[layer]:
                continue  # probation: recalled as usual, never evictable
            uses = int(rec.sidecar.get("uses") or 0)
            views = int(rec.sidecar.get("views") or 0)
            if uses == 0 and views == 0:
                # No evidence of use AND no evidence of recall over a fair
                # sample — the only graduation failure.
                out.append(Retirement(rec.name, layer, "graduation: never used or viewed"))
                continue
            mature.append(rec)
        overflow = len(mature) - capacity[layer]
        if overflow > 0:
            mature.sort(
                key=lambda r: (usage_rate(r, layer_requests), r.sidecar.get("last_used") or 0)
            )
            for rec in mature[:overflow]:
                out.append(
                    Retirement(
                        rec.name,
                        layer,
                        f"capacity: usage rate {usage_rate(rec, layer_requests):.3f} lowest of {len(mature)}",
                    )
                )
        if apply:
            by_name = {r.name: r for r in recs}
            for ret in out:
                if ret.layer == layer and ret.name in by_name:
                    store.archive_skill(by_name[ret.name], reason=ret.reason)
    return out
