"""Capability vocabulary (Phase 4 target — Capability Hub: skill/mcp/plugin
registry + 2-layer permission engine). NEW — today skill/MCP/tool policy is
scattered across `skill_policy.json`, `pane_tools_policy.py`, `pane_guard.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CapabilityScope(StrEnum):
    GLOBAL = "global"
    WORKSPACE = "workspace"
    PROJECT = "project"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class Skill:
    id: str
    name: str
    description: str = ""
    scope: CapabilityScope = CapabilityScope.GLOBAL
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class MCPServer:
    id: str
    name: str
    transport: str = "stdio"
    scope: CapabilityScope = CapabilityScope.GLOBAL
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class Plugin:
    id: str
    name: str
    version: str = ""
    scope: CapabilityScope = CapabilityScope.GLOBAL
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class Tool:
    id: str
    name: str
    description: str = ""
    scope: CapabilityScope = CapabilityScope.GLOBAL
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
