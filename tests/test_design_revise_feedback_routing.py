"""#371 BUG-006: `design_revise`/`design_approve` used to only ever notify
Lead, even when the artifact's own author (or a `designer` pane) was live
and could act on the feedback directly. Covers:
  - a live pane for the artifact's `created_by_role` gets the structured
    feedback (artifact id/title/kind/target/feedback + republish command),
    Lead still gets its coordination/audit notice, noting the route
  - a live `designer` pane is used when `created_by_role` isn't set/live
  - no live candidate -> Lead-only fallback, notice says so
  - the target pane's provider is irrelevant (fake pane, no `provider`
    assertions) — routing must not be claude-only
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.design_actions import publish_design_artifact
from agent_takkub.orchestrator import Orchestrator


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_alive_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    return s


def _make_pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

    with (
        patch.object(Orchestrator, "_start_hot_md_timer", lambda self: None, create=True),
        patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None),
        patch(
            "agent_takkub.orchestrator.Orchestrator._start_browser_mcps",
            lambda self: None,
            create=True,
        ),
    ):
        o = Orchestrator.__new__(Orchestrator)
        from PyQt6.QtCore import QObject

        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
    return o


@pytest.fixture
def html_root(tmp_path):
    root = tmp_path / "docs" / "design-review"
    root.mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _fake_roots(monkeypatch, html_root):
    monkeypatch.setattr(
        "agent_takkub.design_actions.approved_artifact_roots", lambda project_id: [html_root]
    )


@pytest.fixture(autouse=True)
def _isolated_data_home(monkeypatch, tmp_path):
    """`design_revise`/`design_approve` build their own `DesignArtifactRegistry`
    (no `registry=` override, unlike the pure-function tests in
    test_design_actions.py) — it must land under tmp_path, never the real
    user DATA_HOME."""
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path / "data-home")


def _publish(html_root, *, created_by_role: str | None) -> str:
    """Publish a real artifact through the same registry design_revise/
    design_approve read from (no `registry=` override on the orchestrator
    methods themselves), returning its artifact_id."""
    f = html_root / "mock.html"
    f.write_text("<html></html>", encoding="utf-8")
    artifact = publish_design_artifact(
        "p", str(f), "Dashboard v2", "html", created_by_role=created_by_role
    )
    return artifact.artifact_id


class TestDesignReviseRouting:
    def test_live_creator_pane_gets_structured_feedback(
        self, orch: Orchestrator, html_root
    ) -> None:
        artifact_id = _publish(html_root, created_by_role="frontend")
        pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})["frontend"] = pane

        ok, _msg, data = orch.design_revise("p", artifact_id, feedback="move the CTA up")

        assert ok is True
        assert data["status"] == "revision_requested"
        pane.session.write.assert_called()
        sent_text = "".join(call.args[0] for call in pane.session.write.call_args_list)
        assert artifact_id in sent_text
        assert "Dashboard v2" in sent_text
        assert "html" in sent_text
        assert "move the CTA up" in sent_text
        assert "takkub design publish" in sent_text
        assert "<html>" not in sent_text  # never the raw artifact content

    def test_falls_back_to_designer_role_when_creator_not_live(
        self, orch: Orchestrator, html_root
    ) -> None:
        artifact_id = _publish(html_root, created_by_role="frontend")  # frontend has no pane
        designer_pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})["designer"] = designer_pane

        ok, _msg, _data = orch.design_revise("p", artifact_id, feedback="fix contrast")

        assert ok is True
        designer_pane.session.write.assert_called()

    def test_no_live_designer_falls_back_to_lead_only(self, orch: Orchestrator, html_root) -> None:
        artifact_id = _publish(html_root, created_by_role="frontend")
        notified = []
        orch._notify_lead = lambda project_ns, body, **kw: notified.append(body)

        ok, _msg, _data = orch.design_revise("p", artifact_id, feedback="fix contrast")

        assert ok is True
        assert notified, "Lead must still get a notice when no designer is live"
        assert "fallback" in notified[-1].lower()

    def test_lead_still_gets_audit_notice_when_routed_to_designer(
        self, orch: Orchestrator, html_root
    ) -> None:
        artifact_id = _publish(html_root, created_by_role="frontend")
        pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})["frontend"] = pane
        notified = []
        orch._notify_lead = lambda project_ns, body, **kw: notified.append(body)

        orch.design_revise("p", artifact_id, feedback="fix contrast")

        assert notified
        assert artifact_id in notified[-1]
        assert "frontend" in notified[-1]

    def test_non_claude_provider_pane_still_receives_feedback(
        self, orch: Orchestrator, html_root
    ) -> None:
        """Routing keys off pane liveness only — provider-agnostic (#371's
        multi-provider requirement). A fake pane standing in for any
        provider (codex/gemini-agy/opencode/kimi/cursor) must qualify the
        same way a claude pane would."""
        artifact_id = _publish(html_root, created_by_role="designer")
        pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})["designer"] = pane

        ok, _msg, _data = orch.design_revise("p", artifact_id, feedback="tighten spacing")

        assert ok is True
        pane.session.write.assert_called()


class TestDesignApproveRouting:
    def test_live_creator_pane_gets_short_approve_notice(
        self, orch: Orchestrator, html_root
    ) -> None:
        artifact_id = _publish(html_root, created_by_role="frontend")
        pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})["frontend"] = pane

        ok, _msg, data = orch.design_approve("p", artifact_id)

        assert ok is True
        assert data["status"] == "approved"
        pane.session.write.assert_called()

    def test_no_live_creator_pane_no_error(self, orch: Orchestrator, html_root) -> None:
        artifact_id = _publish(html_root, created_by_role="frontend")

        ok, _msg, data = orch.design_approve("p", artifact_id)

        assert ok is True
        assert data["status"] == "approved"
