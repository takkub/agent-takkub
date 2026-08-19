"""Model vocabulary — which LLM a provider account can run, and a named
tuning of it. NEW (REUSE_VS_REWRITE_MATRIX.md): today `provider_models.py`
only tracks per-role model *selection*, not a model registry.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    """One concrete model a provider can run (e.g. "claude-sonnet-5")."""

    id: str
    provider_id: str
    name: str
    context_window: int | None = None
    supports_vision: bool = False
    supports_tools: bool = False
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """A named tuning of one `ModelDefinition` (e.g. role defaults)."""

    id: str
    name: str
    model_id: str
    description: str = ""
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
