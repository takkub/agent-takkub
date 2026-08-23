"""design_actions.py (#365 phase 5 — minimal design artifact registry:
publish validates + ensures Preview, approve/revise transition status).
Schema: docs/plans/workspace-1.2.0-design/schemas/design_artifact.schema.json
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.design_actions import (
    ALLOWED_KINDS,
    ALLOWED_STATUSES,
    DesignArtifact,
    DesignArtifactError,
    DesignArtifactRegistry,
    approve,
    publish_design_artifact,
    request_revision,
)
from agent_takkub.preview_controller import PreviewController


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def registry(tmp_path: Path) -> DesignArtifactRegistry:
    return DesignArtifactRegistry("demo", store=JsonlStore(tmp_path / "design_artifacts.jsonl"))


@pytest.fixture
def html_root(tmp_path: Path) -> Path:
    root = tmp_path / "docs" / "design-review"
    root.mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _fake_roots(monkeypatch, html_root: Path):
    """Point `approved_artifact_roots` at `html_root` for every test in this
    file instead of touching real projects.json/DATA_HOME."""
    monkeypatch.setattr(
        "agent_takkub.design_actions.approved_artifact_roots", lambda project_id: [html_root]
    )


# ── DesignArtifactRegistry (latest-record-per-id fold) ───────────────────


class TestRegistry:
    def test_empty_registry_returns_nothing(self, registry: DesignArtifactRegistry) -> None:
        assert registry.all() == []
        assert registry.get("nope") is None

    def test_upsert_then_get(self, registry: DesignArtifactRegistry) -> None:
        artifact = DesignArtifact(
            artifact_id="a1", project_id="demo", title="Dashboard", kind="html", target="x.html"
        )
        registry.upsert(artifact)

        got = registry.get("a1")

        assert got is not None
        assert got.title == "Dashboard"
        assert got.status == "draft"

    def test_second_upsert_wins(self, registry: DesignArtifactRegistry) -> None:
        registry.upsert(
            DesignArtifact(
                artifact_id="a1", project_id="demo", title="v1", kind="html", target="x.html"
            )
        )
        registry.upsert(
            DesignArtifact(
                artifact_id="a1",
                project_id="demo",
                title="v1",
                kind="html",
                target="x.html",
                status="approved",
            )
        )

        got = registry.get("a1")

        assert got is not None
        assert got.status == "approved"
        assert len(registry.all()) == 1  # folded, not two rows


# ── publish_design_artifact ──────────────────────────────────────────────


class TestPublish:
    def test_publish_html_records_draft(
        self, registry: DesignArtifactRegistry, html_root: Path
    ) -> None:
        f = html_root / "dashboard.html"
        f.write_text("<html></html>", encoding="utf-8")

        artifact = publish_design_artifact(
            "demo", str(f), "Dashboard v2", "html", registry=registry
        )

        assert artifact.kind == "html"
        assert artifact.status == "draft"
        assert artifact.target == str(f.resolve())
        assert registry.get(artifact.artifact_id) == artifact

    def test_publish_url_requires_loopback(self, registry: DesignArtifactRegistry) -> None:
        with pytest.raises(DesignArtifactError, match="loopback"):
            publish_design_artifact(
                "demo", "http://example.com:3000", "Live app", "url", registry=registry
            )

    def test_publish_url_ok(self, registry: DesignArtifactRegistry) -> None:
        artifact = publish_design_artifact(
            "demo", "http://127.0.0.1:3000", "Live app", "url", registry=registry
        )
        assert artifact.kind == "url"
        assert artifact.target == "http://127.0.0.1:3000"

    def test_publish_unknown_kind_rejected(self, registry: DesignArtifactRegistry) -> None:
        with pytest.raises(DesignArtifactError, match="unknown kind"):
            publish_design_artifact("demo", "x.html", "X", "pdf", registry=registry)

    def test_publish_file_outside_roots_rejected(
        self, registry: DesignArtifactRegistry, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.html"
        outside.write_text("<html></html>", encoding="utf-8")
        with pytest.raises(ValueError):
            publish_design_artifact("demo", str(outside), "X", "html", registry=registry)

    def test_publish_ensures_preview_when_controller_given(
        self, registry: DesignArtifactRegistry, html_root: Path, qapp: QCoreApplication
    ) -> None:
        f = html_root / "dashboard.html"
        f.write_text("<html></html>", encoding="utf-8")
        controller = PreviewController()

        artifact = publish_design_artifact(
            "demo", str(f), "Dashboard v2", "html", registry=registry, preview_controller=controller
        )

        state = controller.status("demo")
        assert state is not None
        assert state.mode == "file"
        assert state.target == artifact.target

    def test_publish_url_ensures_preview_open_url(
        self, registry: DesignArtifactRegistry, qapp: QCoreApplication
    ) -> None:
        controller = PreviewController()

        publish_design_artifact(
            "demo",
            "http://127.0.0.1:5173",
            "Live app",
            "url",
            registry=registry,
            preview_controller=controller,
        )

        state = controller.status("demo")
        assert state is not None
        assert state.mode == "url"
        assert state.target == "http://127.0.0.1:5173"


# ── approve / request_revision (status transitions) ──────────────────────


class TestTransitions:
    def _publish(self, registry: DesignArtifactRegistry, html_root: Path) -> DesignArtifact:
        f = html_root / "mock.html"
        f.write_text("<html></html>", encoding="utf-8")
        return publish_design_artifact("demo", str(f), "Mock", "html", registry=registry)

    def test_approve_from_draft(self, registry: DesignArtifactRegistry, html_root: Path) -> None:
        artifact = self._publish(registry, html_root)

        approved = approve("demo", artifact.artifact_id, registry=registry)

        assert approved.status == "approved"
        assert registry.get(artifact.artifact_id).status == "approved"

    def test_approve_is_terminal(self, registry: DesignArtifactRegistry, html_root: Path) -> None:
        artifact = self._publish(registry, html_root)
        approve("demo", artifact.artifact_id, registry=registry)

        with pytest.raises(DesignArtifactError, match="terminal"):
            approve("demo", artifact.artifact_id, registry=registry)

    def test_request_revision_from_draft(
        self, registry: DesignArtifactRegistry, html_root: Path
    ) -> None:
        artifact = self._publish(registry, html_root)

        revised = request_revision(
            "demo", artifact.artifact_id, feedback="move the CTA up", registry=registry
        )

        assert revised.status == "revision_requested"

    def test_transition_unknown_artifact_raises(self, registry: DesignArtifactRegistry) -> None:
        with pytest.raises(DesignArtifactError, match="no design artifact"):
            approve("demo", "does-not-exist", registry=registry)

    def test_revision_requested_can_be_approved_next(
        self, registry: DesignArtifactRegistry, html_root: Path
    ) -> None:
        artifact = self._publish(registry, html_root)
        request_revision("demo", artifact.artifact_id, registry=registry)

        approved = approve("demo", artifact.artifact_id, registry=registry)

        assert approved.status == "approved"


# ── schema sanity ──────────────────────────────────────────────────────


def test_kind_and_status_enums_match_schema() -> None:
    # docs/plans/workspace-1.2.0-design/schemas/design_artifact.schema.json
    assert ALLOWED_KINDS == {"html", "url", "review"}
    assert ALLOWED_STATUSES == {"draft", "review", "approved", "revision_requested"}


def test_artifact_as_dict_has_schema_required_fields() -> None:
    artifact = DesignArtifact(
        artifact_id="a1", project_id="demo", title="X", kind="html", target="x.html"
    )
    d = artifact.as_dict()
    for required in ("artifact_id", "project_id", "title", "kind", "target"):
        assert required in d
