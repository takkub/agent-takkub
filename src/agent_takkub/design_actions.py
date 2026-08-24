"""Design artifact registry — #365 phase 5's minimal slice of phase 6
("Design workflow: Designer role/policy, publish artifact -> auto-focus
Preview, Approve/Revise"). This module owns validation + the durable
record only; the Designer-role UI/policy lands in phase 6 on top of it.

Record shape mirrors `docs/plans/workspace-1.2.0-design/schemas/design_artifact.schema.json`
exactly (`artifact_id`, `project_id`, `title`, `kind`, `target`, `status`,
`created_by_role`, `created_at`, `metadata`). Storage: one append-only JSONL
per project under the V2 layout (`StorageLayoutV2.project(id).artifacts`),
latest-record-per-`artifact_id` wins — the same upsert-log shape
`core.accounts.registry` already uses for accounts/pools.

`publish_design_artifact` validates + ensures Preview shows it (calling into
`preview_controller`, per `11_EDITOR_PREVIEW_AGENT_PROTOCOL.md`'s "cockpit
validates -> ensures Preview -> opens/focuses"). Notifying Lead (and, since
#371 BUG-006, the live Designer pane) on approve/revise is deliberately NOT
done here: every existing `_notify_lead`/`send` call in this codebase is a
bound method on the live `Orchestrator` instance (not a proxyable module
function like `_log_event`), so that step is the caller's job — see
`Orchestrator.design_approve`/`design_revise` in orchestrator.py, which call
this module's pure `approve`/`request_revision` and then notify Lead
themselves with a short artifact-id-only message ("ไม่ส่ง HTML"), plus route
structured feedback (`format_revision_feedback`, below) to whichever live
pane created the artifact — falling back to Lead-only when no such pane is
live.
"""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .core.storage.jsonl_store import JsonlStore
from .core.storage.layout import storage_layout_v2
from .preview_controller import (
    PreviewController,
    approved_artifact_roots,
    is_loopback_url,
    resolve_preview_file,
)

ALLOWED_KINDS = frozenset({"html", "url", "review"})
ALLOWED_STATUSES = frozenset({"draft", "review", "approved", "revision_requested"})
# approve()/request_revision() are the only two transitions phase 5 needs
# (task spec item 3: "approve/revise เปลี่ยน status") — both are allowed from
# any non-terminal status. "approved" is terminal: a further edit republishes
# as a brand-new artifact rather than reopening this one (phase 6's job).
_NON_TERMINAL_STATUSES = frozenset({"draft", "review", "revision_requested"})


def _log_event(event: str, **details) -> None:
    """Proxy to orchestrator._log_event — lazy import to dodge a cycle, same
    pattern editor_service.py / preview_controller.py each already use."""
    _orch = sys.modules.get("agent_takkub.orchestrator")
    if _orch is not None:
        _orch._log_event(event, **details)


class DesignArtifactError(ValueError):
    """Invalid kind/status/target, or a disallowed status transition."""


@dataclass(frozen=True, slots=True)
class DesignArtifact:
    artifact_id: str
    project_id: str
    title: str
    kind: str  # "html" | "url" | "review"
    target: str
    status: str = "draft"
    created_by_role: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _artifacts_store_path(project_id: str) -> Path:
    return storage_layout_v2().project(project_id).artifacts / "design_artifacts.jsonl"


def _record_to_artifact(record: Mapping[str, Any]) -> DesignArtifact:
    return DesignArtifact(
        artifact_id=record["artifact_id"],
        project_id=record["project_id"],
        title=record.get("title", ""),
        kind=record.get("kind", "html"),
        target=record.get("target", ""),
        status=record.get("status", "draft"),
        created_by_role=record.get("created_by_role"),
        created_at=record.get("created_at"),
        metadata=dict(record.get("metadata") or {}),
    )


class DesignArtifactRegistry:
    """One project's artifact ledger. `all()`/`get()` fold the JSONL log to
    latest-record-per-`artifact_id`, same read shape as
    `core.accounts.registry.AccountRegistry`."""

    def __init__(self, project_id: str, store: JsonlStore | None = None) -> None:
        self.project_id = project_id
        self._store = store or JsonlStore(_artifacts_store_path(project_id))

    def upsert(self, artifact: DesignArtifact) -> None:
        self._store.append(artifact.as_dict())

    def all(self) -> list[DesignArtifact]:
        records, _ = self._store.read_all()
        latest: dict[str, Mapping[str, Any]] = {}
        for record in records:
            aid = record.get("artifact_id")
            if aid:
                latest[aid] = record
        return [_record_to_artifact(r) for r in latest.values()]

    def get(self, artifact_id: str) -> DesignArtifact | None:
        for artifact in self.all():
            if artifact.artifact_id == artifact_id:
                return artifact
        return None


def _validate_kind_and_target(kind: str, target: str) -> None:
    if kind not in ALLOWED_KINDS:
        raise DesignArtifactError(f"unknown kind {kind!r} (want one of {sorted(ALLOWED_KINDS)})")
    if not target:
        raise DesignArtifactError("target must not be empty")
    if kind == "url":
        if not is_loopback_url(target):
            raise DesignArtifactError(
                f"url-kind artifact target must be loopback (127.0.0.1/localhost/::1) + port: {target!r}"
            )


def publish_design_artifact(
    project_id: str,
    path: str,
    title: str,
    mode: str,
    *,
    created_by_role: str | None = None,
    metadata: dict[str, Any] | None = None,
    preview_controller: PreviewController | None = None,
    registry: DesignArtifactRegistry | None = None,
) -> DesignArtifact:
    """Validate *path* (a URL when `mode == "url"`, otherwise a file under
    the project's approved artifact roots), record a new `draft` artifact,
    and — when a live `preview_controller` is supplied (the CLI/orchestrator
    path always has one; a unit test exercising the registry alone can pass
    none) — ensure Preview shows it, per the protocol doc's "cockpit
    validates -> ensures Preview -> opens/focuses" concept.

    `mode` is the artifact `kind` ("html" | "url" | "review") — matches
    `06_LIVE_PREVIEW_SPEC.md`'s own url/file split one-for-one (`url` kind
    is Preview's URL mode; `html`/`review` are both Preview's file mode,
    `review` just marking a critic-authored design-review doc instead of a
    raw mockup).
    """
    _validate_kind_and_target(mode, path)
    if mode == "url":
        target = path
    else:
        resolved = resolve_preview_file(path, approved_artifact_roots(project_id))
        target = str(resolved)

    artifact = DesignArtifact(
        artifact_id=uuid.uuid4().hex,
        project_id=project_id,
        title=title,
        kind=mode,
        target=target,
        status="draft",
        created_by_role=created_by_role,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        metadata=dict(metadata or {}),
    )
    reg = registry or DesignArtifactRegistry(project_id)
    reg.upsert(artifact)
    _log_event(
        "workspace.design.published",
        project=project_id,
        artifact_id=artifact.artifact_id,
        kind=artifact.kind,
    )

    if preview_controller is not None:
        if mode == "url":
            preview_controller.open_url(project_id, target)
        else:
            preview_controller.open_file(project_id, target, approved_artifact_roots(project_id))

    return artifact


def _transition(
    project_id: str,
    artifact_id: str,
    new_status: str,
    registry: DesignArtifactRegistry | None,
    event: str,
    **event_details: Any,
) -> DesignArtifact:
    reg = registry or DesignArtifactRegistry(project_id)
    current = reg.get(artifact_id)
    if current is None:
        raise DesignArtifactError(f"no design artifact {artifact_id!r} for project {project_id!r}")
    if current.status not in _NON_TERMINAL_STATUSES:
        raise DesignArtifactError(
            f"artifact {artifact_id!r} is {current.status!r} (terminal) — cannot transition"
        )
    updated = DesignArtifact(
        artifact_id=current.artifact_id,
        project_id=current.project_id,
        title=current.title,
        kind=current.kind,
        target=current.target,
        status=new_status,
        created_by_role=current.created_by_role,
        created_at=current.created_at,
        metadata=dict(current.metadata),
    )
    reg.upsert(updated)
    _log_event(
        event, project=project_id, artifact_id=artifact_id, status=new_status, **event_details
    )
    return updated


def approve(
    project_id: str, artifact_id: str, *, registry: DesignArtifactRegistry | None = None
) -> DesignArtifact:
    return _transition(project_id, artifact_id, "approved", registry, "workspace.design.approved")


def request_revision(
    project_id: str,
    artifact_id: str,
    *,
    feedback: str = "",
    registry: DesignArtifactRegistry | None = None,
) -> DesignArtifact:
    return _transition(
        project_id,
        artifact_id,
        "revision_requested",
        registry,
        "workspace.design.revision_requested",
        feedback_len=len(feedback),
    )


def format_revision_feedback(artifact: DesignArtifact, feedback: str) -> str:
    """Structured message routed to the live pane that should act on a
    revision (#371 BUG-006) — record fields only (id/title/kind/target),
    never the artifact's HTML/file content, plus the republish command.
    Pure/testable without an Orchestrator instance; the caller
    (`Orchestrator.design_revise`) owns picking *who* gets this and
    actually delivering it via `self.send`."""
    return "\n".join(
        [
            f'[design-revise] artifact {artifact.artifact_id} needs a revision — "{artifact.title}"',
            f"kind: {artifact.kind}",
            f"target: {artifact.target}",
            f"feedback: {feedback or '(no feedback text provided)'}",
            f'→ publish the revision with: takkub design publish --path <file> --title "{artifact.title}" --mode {artifact.kind}',
        ]
    )
