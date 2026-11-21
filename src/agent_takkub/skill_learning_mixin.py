"""Orchestrator side of Skill Learning (backlog 2c6cb77c) — kept out of the
god file: three thin methods that hand off to the Qt-free
`skill_learning` package and bring its one-line report back to the Lead.
"""

from __future__ import annotations

import os
import sys
import time

from .skill_learning.reflector import CHILD_ENV

SKIP_ENV = "TAKKUB_SKIP_SKILL_LEARNING"

# #1038: a skill was distilled from a done note 6 minutes before the reviewer
# proved its central claim wrong; it was linked into every new worktree and
# the next pane repeated the mistake. A clean done from a non-checker role
# now waits: a checker's PASS releases it, a checker's FAILED discards it,
# and with no checker at work it is learned after HOLD_S. In memory only — a
# cockpit restart inside the window loses those episodes (not durable).
CHECKER_ROLES = frozenset({"qa", "critic", "reviewer", "tester"})
HOLD_S = float(os.environ.get("TAKKUB_SKILL_LEARNING_HOLD_S", "1200"))
HOLD_MAX_S = 3 * 3600.0


def _log_event(event: str, **details) -> None:
    orch = sys.modules.get("agent_takkub.orchestrator")
    if orch is not None:
        orch._log_event(event, **details)


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def claude_session_dirs(pane) -> dict[str, str]:
    """Where *pane*'s claude session JSONL lives — the CLAUDE_CONFIG_DIR /
    CLAUDE_CODE_PROJECT_DIR_NAME its PtySession was spawned with (#804)."""
    session = getattr(pane, "session", None)
    return {
        "claude_config_dir": _str(getattr(session, "_claude_config_dir", "")),
        "claude_project_dir_name": _str(getattr(session, "_claude_project_dir_name", "")),
    }


def _disabled() -> bool:
    return bool(os.environ.get(SKIP_ENV) or os.environ.get(CHILD_ENV))


class SkillLearningMixin:
    def _skill_learning_on_done(
        self,
        project_ns: str,
        role: str,
        *,
        task: str,
        note: str,
        failed: bool,
        provider: str,
        cwd: str,
        session_id: str,
        pty_transcript: str,
        assigned_at: float,
        claude_config_dir: str = "",
        claude_project_dir_name: str = "",
    ) -> None:
        """Queue the learning pass for one done. Never raises, never blocks."""
        if _disabled():
            return
        try:
            from .skill_learning import pipeline
            from .skill_learning.episode import DoneEvent

            ev = DoneEvent(
                project_ns=project_ns,
                role=role,
                provider=provider if isinstance(provider, str) else "",
                task=task if isinstance(task, str) else "",
                note=note if isinstance(note, str) else "",
                failed=bool(failed),
                cwd=cwd if isinstance(cwd, str) else "",
                session_id=session_id if isinstance(session_id, str) else "",
                pty_transcript=str(pty_transcript) if pty_transcript else "",
                assigned_at=float(assigned_at) if isinstance(assigned_at, int | float) else 0.0,
                claude_config_dir=_str(claude_config_dir),
                claude_project_dir_name=_str(claude_project_dir_name),
            )
            self.__dict__.setdefault("_skill_learning_last", {})[(project_ns, role)] = ev
            held = self.__dict__.setdefault("_skill_learning_held", {}).setdefault(project_ns, [])
            if role.partition("#")[0] in CHECKER_ROLES:
                # #1038: the checker's verdict decides what happens to the
                # work that was waiting for it.
                if failed and held:
                    _log_event(
                        "skill_learning_unverified_dropped",
                        project=project_ns,
                        checker=role,
                        roles=[e.role for _t, e in held][:10],
                    )
                    held.clear()
                else:
                    self._skill_learning_release(project_ns)
                pipeline.submit(ev, notify=self.skillLearningReport.emit)
            elif failed:
                pipeline.submit(ev, notify=self.skillLearningReport.emit)
            else:
                held.append((time.time(), ev))
        except Exception as exc:
            _log_event(
                "skill_learning_hook_error", role=role, project=project_ns, error=str(exc)[:200]
            )

    def _skill_learning_release(self, project_ns: str) -> None:
        from .skill_learning import pipeline

        held = self.__dict__.get("_skill_learning_held", {}).pop(project_ns, [])
        for _ts, ev in held:
            pipeline.submit(ev, notify=self.skillLearningReport.emit)

    def _skill_learning_tick(self, now: float) -> None:
        """Idle tick: learn from a finished task once nothing is left to
        contradict it — `HOLD_S` with no checker working in the project, or
        `HOLD_MAX_S` regardless. Never raises."""
        try:
            for project_ns, held in list(self.__dict__.get("_skill_learning_held", {}).items()):
                if not held:
                    continue
                age = now - held[0][0]
                if age < HOLD_S:
                    continue
                checking = any(
                    name.partition("#")[0] in CHECKER_ROLES
                    and getattr(pane, "state", None) == "working"
                    for name, pane in self._project_panes(project_ns).items()
                )
                if not checking or age >= HOLD_MAX_S:
                    self._skill_learning_release(project_ns)
        except Exception as exc:
            _log_event("skill_learning_hook_error", stage="tick", error=str(exc)[:200])

    def _on_skill_learning_report(self, project_ns: str, line: str) -> None:
        """Qt thread. Digest-able (not a blocking notice) — Lead sees it batched."""
        _log_event("skill_learning_report", project=project_ns, line=line[:200])
        # #816: link a just-landed project skill into the project's
        # `.claude/skills/` now — panes already running would otherwise only
        # see it after a respawn ("Unknown skill" in the Skill tool).
        try:
            from .lead_context import _allowed_project_roots
            from .skill_scan import ensure_project_skill_links

            for root in _allowed_project_roots(project_ns):
                ensure_project_skill_links(root, project_ns)
        except Exception as exc:
            _log_event("skill_learning_link_error", project=project_ns, error=str(exc)[:200])
        self._notify_lead(
            project_ns,
            line,
            from_role="skill-learning",
            note="skill_learning",
            kind="skill-learning",
        )

    def skill_learn_now(
        self, role: str, project: str | None = None, note: str = ""
    ) -> tuple[bool, str]:
        """`takkub skills learned reflect --role X` — the /learn equivalent:
        reflect on X's latest episode now, bypassing worth/cooldown gates.
        For a role that hasn't reported done (incl. Lead) the live pane's
        session is used."""
        if _disabled():
            return False, f"skill learning is disabled in this process ({SKIP_ENV})"
        try:
            from .skill_learning import pipeline
            from .skill_learning.episode import DoneEvent

            project_ns = self._resolve_project(project)
            ev = self.__dict__.get("_skill_learning_last", {}).get((project_ns, role))
            if ev is None:
                pane = self._project_panes(project_ns).get(role)
                if pane is None:
                    return False, f"no pane or recent done for role {role!r} in {project_ns}"
                ps = self._ps(f"{project_ns}::{role}")
                ev = DoneEvent(
                    project_ns=project_ns,
                    role=role,
                    provider=str(
                        getattr(getattr(pane, "model", None), "provider_name", "") or "claude"
                    ),
                    task=str(getattr(ps, "last_assigned_task", "") or ""),
                    cwd=str(getattr(pane, "_session_cwd", "") or ""),
                    session_id=str(self._session_uuid_for(f"{project_ns}::{role}") or ""),
                    pty_transcript=str(getattr(pane, "_transcript_path", "") or ""),
                    **claude_session_dirs(pane),
                )
            if note:
                ev.note = (ev.note + "\n\n" + note).strip()
            pipeline.submit(ev, notify=self.skillLearningReport.emit, force=True)
        except Exception as exc:
            return False, f"could not queue reflection: {exc}"
        return (
            True,
            f"queued a reflection on {role} ({project_ns}) — result arrives as a Lead notice",
        )
