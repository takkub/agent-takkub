"""Tests for `Orchestrator.broadcast_design_review` — the "🎨 UI Review" button.

Unlike `broadcast_bug_check` which prompts every existing live pane, the
design-review broadcast *spawns* the design-review pipeline:

  - critic pane assigned to read shots from runtime/exports/<date>/<project>/
    screenshots/ and write a proposal to docs/design-review/<date>-<view>.md
  - gemini pane assigned to be ready to view images critic will send via
    `takkub send`

Both panes are spawned in parallel via `assign()`. Substitution doctrine
(CLAUDE.md "Claude รับตำแหน่งแทน"): if the gemini CLI is unavailable —
toggled off in `~/.takkub/disabled-providers.json` OR not installed — the
gemini slot is STILL spawned; the spawn layer backs it with claude. The
broadcast never silently drops the slot (issue #61) — it only flags the
substitution in the returned label (`gemini (claude)`) and the log event.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator

ACTIVE_PROJECT = "proj"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def minimal_project_json(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps(
            {
                "active": ACTIVE_PROJECT,
                "projects": {
                    ACTIVE_PROJECT: {
                        "paths": {
                            "web": str(tmp_path / "proj" / "web"),
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    cockpit = tmp_path / "cockpit"
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    return pj


@pytest.fixture
def orch(
    qapp: QCoreApplication,
    minimal_project_json: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or ACTIVE_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda *_: None))
    return o


class TestBroadcastDesignReview:
    def test_spawns_critic_and_gemini_parallel(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default case: both critic and gemini get assigned via assign()."""
        calls: list[tuple[str, str]] = []  # (role, task[:60])

        def fake_assign(role_name, cwd, task, **_kw):
            calls.append((role_name, task[:60]))
            return True, f"queued {role_name}"

        monkeypatch.setattr(orch, "assign", fake_assign)
        # Pretend no providers disabled.
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: role,  # every role available — no substitution
        )

        count, roles = orch.broadcast_design_review(project=ACTIVE_PROJECT)
        assert count == 2
        assert set(roles) == {"critic", "gemini"}
        assigned = {role for role, _ in calls}
        assert assigned == {"critic", "gemini"}

    def test_critic_prompt_points_to_today_shot_dir(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The critic task must reference runtime/exports/<date>/<project>/screenshots/
        so the agent reads the right images."""
        captured: list[tuple[str, str]] = []

        def fake_assign(role_name, cwd, task, **_kw):
            captured.append((role_name, task))
            return True, ""

        monkeypatch.setattr(orch, "assign", fake_assign)
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: role,  # every role available — no substitution
        )

        orch.broadcast_design_review(project=ACTIVE_PROJECT)
        critic_task = next(t for r, t in captured if r == "critic")
        assert "runtime/exports/" in critic_task
        assert ACTIVE_PROJECT in critic_task
        assert "screenshots" in critic_task

    def test_gemini_prompt_describes_image_review_role(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gemini task must say it's preparing to view images via takkub send."""
        captured: list[tuple[str, str]] = []

        def fake_assign(role_name, cwd, task, **_kw):
            captured.append((role_name, task))
            return True, ""

        monkeypatch.setattr(orch, "assign", fake_assign)
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: role,  # every role available — no substitution
        )

        orch.broadcast_design_review(project=ACTIVE_PROJECT)
        gemini_task = next(t for r, t in captured if r == "gemini")
        assert "takkub send" in gemini_task or "critic" in gemini_task.lower()
        assert "image" in gemini_task.lower() or "ภาพ" in gemini_task or "รูป" in gemini_task

    def test_substitutes_claude_when_gemini_disabled(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If gemini is unavailable (toggled off / not installed), the slot is
        STILL spawned — backed by claude — never silently dropped (issue #61).
        The gemini entry is flagged `gemini (claude)` so the user knows model
        diversity was lost."""
        calls: list[tuple[str, str]] = []

        def fake_assign(role_name, cwd, task, **_kw):
            calls.append((role_name, task))
            return True, ""

        monkeypatch.setattr(orch, "assign", fake_assign)
        # gemini unavailable → spawn layer would back it with claude.
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "claude" if role == "gemini" else role,
        )

        count, roles = orch.broadcast_design_review(project=ACTIVE_PROJECT)
        assert count == 2
        assert set(roles) == {"critic", "gemini (claude)"}
        assigned = {role for role, _ in calls}
        assert assigned == {"critic", "gemini"}  # pane still keeps the gemini identity
        gemini_task = next(t for r, t in calls if r == "gemini")
        assert "claude-substitute for gemini" in gemini_task

    def test_logs_gemini_substitution_flag(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit event records whether gemini was substituted."""
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(orch_mod, "_log_event", lambda event, **d: events.append((event, d)))
        monkeypatch.setattr(orch, "assign", lambda *a, **k: (True, ""))
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: "claude" if role == "gemini" else role,
        )

        orch.broadcast_design_review(project=ACTIVE_PROJECT)
        ev = next(d for k, d in events if k == "broadcast_design_review")
        assert ev["gemini_substituted"] is True

    def test_critic_prompt_mentions_proposal_doc_path(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critic must know to write the proposal to docs/design-review/<date>-<view>.md
        so Lead has a stable place to read it back."""
        captured: list[tuple[str, str]] = []

        def fake_assign(role_name, cwd, task, **_kw):
            captured.append((role_name, task))
            return True, ""

        monkeypatch.setattr(orch, "assign", fake_assign)
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: role,  # every role available — no substitution
        )

        orch.broadcast_design_review(project=ACTIVE_PROJECT)
        critic_task = next(t for r, t in captured if r == "critic")
        assert "docs/design-review/" in critic_task

    def test_logs_event(self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
        """Method must emit `broadcast_design_review` event for audit trail."""
        events: list[tuple[str, dict]] = []

        def fake_log(event, **details):
            events.append((event, details))

        monkeypatch.setattr(orch_mod, "_log_event", fake_log)
        monkeypatch.setattr(orch, "assign", lambda *a, **k: (True, ""))
        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for",
            lambda role, project=None: role,  # every role available — no substitution
        )

        orch.broadcast_design_review(project=ACTIVE_PROJECT)
        kinds = [e[0] for e in events]
        assert "broadcast_design_review" in kinds
