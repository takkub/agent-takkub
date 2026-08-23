"""#365 phase 10 — `Orchestrator.workspace_status` (`takkub doctor
--workspace`'s live IPC gather).

Same technique as `test_orchestrator_ram_status.py`: call the unbound method
against a bare `SimpleNamespace` carrying only the attributes it touches,
rather than constructing a full `Orchestrator`.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.preview_controller import PreviewController


def _fake(**overrides) -> SimpleNamespace:
    base = dict(
        _editor_host_ref=None,
        _preview_controller=None,
        _workspace_diag_sources={},
    )
    base.update(overrides)
    fake = SimpleNamespace(**base)
    fake._resolve_project = lambda p: p or "default"
    return fake


def test_editor_host_not_registered() -> None:
    fake = _fake()
    result = Orchestrator.workspace_status(fake)
    assert result["editor_host"] == {"registered": False}


def test_editor_host_registered_reports_view_and_tab_count() -> None:
    host = SimpleNamespace(has_view=lambda: True, open_count=lambda: 3)
    fake = _fake(_editor_host_ref=host)

    result = Orchestrator.workspace_status(fake)

    assert result["editor_host"] == {"registered": True, "has_view": True, "open_count": 3}


def test_preview_states_scoped_to_requested_project() -> None:
    controller = PreviewController()
    controller.open_url("proj1", "http://127.0.0.1:3000")
    controller.open_url("proj2", "http://127.0.0.1:4000")
    fake = _fake(_preview_controller=controller)

    result = Orchestrator.workspace_status(fake, project="proj1")

    assert set(result["preview"]) == {"proj1"}
    assert result["preview"]["proj1"]["target"] == "http://127.0.0.1:3000"


def test_preview_states_include_nav_block_count() -> None:
    controller = PreviewController()
    controller.open_url("proj1", "http://127.0.0.1:3000")
    controller.check_navigation("proj1", "http://evil.example.com/")
    fake = _fake(_preview_controller=controller)

    result = Orchestrator.workspace_status(fake, project="proj1")

    assert result["preview"]["proj1"]["nav_blocks"] == 1


def test_no_preview_controller_yields_empty_preview_dict() -> None:
    fake = _fake()
    result = Orchestrator.workspace_status(fake, project="proj1")
    assert result["preview"] == {}


def test_design_artifacts_grouped_by_status(monkeypatch) -> None:
    import agent_takkub.design_actions as design_actions_mod

    art_draft = SimpleNamespace(status="draft")
    art_approved = SimpleNamespace(status="approved")

    class _FakeRegistry:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def all(self):
            return [art_draft, art_approved, art_draft]

    monkeypatch.setattr(design_actions_mod, "DesignArtifactRegistry", _FakeRegistry)

    fake = _fake()
    result = Orchestrator.workspace_status(fake, project="proj1")

    assert result["design_artifacts"]["proj1"] == {
        "count": 3,
        "by_status": {"draft": 2, "approved": 1},
    }


def test_design_artifacts_empty_project_omitted(monkeypatch) -> None:
    import agent_takkub.design_actions as design_actions_mod

    class _EmptyRegistry:
        def __init__(self, project_id: str) -> None:
            pass

        def all(self):
            return []

    monkeypatch.setattr(design_actions_mod, "DesignArtifactRegistry", _EmptyRegistry)

    fake = _fake()
    result = Orchestrator.workspace_status(fake, project="proj1")

    assert result["design_artifacts"] == {}


def test_design_artifacts_read_error_reported_not_raised(monkeypatch) -> None:
    import agent_takkub.design_actions as design_actions_mod

    class _BrokenRegistry:
        def __init__(self, project_id: str) -> None:
            pass

        def all(self):
            raise OSError("disk gone")

    monkeypatch.setattr(design_actions_mod, "DesignArtifactRegistry", _BrokenRegistry)

    fake = _fake()
    result = Orchestrator.workspace_status(fake, project="proj1")

    assert "disk gone" in result["design_artifacts"]["proj1"]["error"]


def test_per_project_sources_reads_registered_diagnostics_objects() -> None:
    file_watch = SimpleNamespace(diagnostics=lambda: {"pending_count": 2})
    tree_index = SimpleNamespace(diagnostics=lambda: {"last_scan_ms": 5.0})
    fake = _fake(
        _workspace_diag_sources={
            "proj1": {"file_watch": file_watch, "tree_index": tree_index},
        }
    )

    result = Orchestrator.workspace_status(fake, project="proj1")

    assert result["per_project"]["proj1"]["file_watch"] == {"pending_count": 2}
    assert result["per_project"]["proj1"]["tree_index"] == {"last_scan_ms": 5.0}
    assert "git_changes" not in result["per_project"]["proj1"]


def test_per_project_unregistered_project_is_absent() -> None:
    fake = _fake()
    result = Orchestrator.workspace_status(fake, project="proj1")
    assert result["per_project"] == {}


def test_no_project_arg_covers_every_registered_project(monkeypatch) -> None:
    import agent_takkub.config as config_mod

    monkeypatch.setattr(config_mod, "load_projects", lambda: {"projects": {}})
    file_watch1 = SimpleNamespace(diagnostics=lambda: {"pending_count": 1})
    file_watch2 = SimpleNamespace(diagnostics=lambda: {"pending_count": 2})
    fake = _fake(
        _workspace_diag_sources={
            "proj1": {"file_watch": file_watch1},
            "proj2": {"file_watch": file_watch2},
        }
    )
    fake._resolve_project = lambda p: p  # never called when project is None

    result = Orchestrator.workspace_status(fake, project=None)

    assert set(result["per_project"]) == {"proj1", "proj2"}
