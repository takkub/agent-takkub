"""One report shape shared by every `MigrationEngine` stage (inspect/plan/
dry_run/apply/validate/rollback) — plan §5.1: "unknown field ห้ามทิ้งเงียบ
เก็บลง migration report"."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class StepReport:
    step_id: str
    stage: str  # inspect|plan|dry_run|apply|validate|rollback
    ok: bool
    summary: str
    unknown_fields: tuple[str, ...] = ()
    detail: dict = field(default_factory=dict)
