"""Orchestrator side of Skill Learning (backlog 2c6cb77c) — kept out of the
god file: three thin methods that hand off to the Qt-free
`skill_learning` package and bring its one-line report back to the Lead.
"""

from __future__ import annotations

import os
import sys

from .skill_learning.reflector import CHILD_ENV

SKIP_ENV = "TAKKUB_SKIP_SKILL_LEARNING"


def _log_event(event: str, **details) -> None:
    orch = sys.modules.get("agent_takkub.orchestrator")
    if orch is not None:
        orch._log_event(event, **details)


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
            )
            self.__dict__.setdefault("_skill_learning_last", {})[(project_ns, role)] = ev
            pipeline.submit(ev, notify=self.skillLearningReport.emit)
        except Exception as exc:
            _log_event(
                "skill_learning_hook_error", role=role, project=project_ns, error=str(exc)[:200]
            )

    def _on_skill_learning_report(self, project_ns: str, line: str) -> None:
        """Qt thread. Digest-able (not a blocking notice) — Lead sees it batched."""
        _log_event("skill_learning_report", project=project_ns, line=line[:200])
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
