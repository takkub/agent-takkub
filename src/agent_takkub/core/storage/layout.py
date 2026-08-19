"""Storage V2 layout (#309 Phase 8b, blueprint `02_STORAGE_AND_FOLDER_STRUCTURE.md`
+ `13_MIGRATION_MAPPING_FROM_V1.md`, trimmed to the task's own explicit tree).

Physical root: ``config.DATA_HOME / "v2"`` — deliberately NOT bare top-level
names under ``DATA_HOME`` (blueprint uses ``projects/``, ``runtime/``,
``cache/`` at the root, but V1 already owns those exact names for a
DIFFERENT shape; reusing them unnamespaced would let a migration step
silently interleave V1 and V2 files in one directory). Nesting under ``v2/``
keeps every migration step copy-never-move-safe: V1 stays untouched at
``DATA_HOME``'s existing top level, V2 lives entirely alongside it until a
future deprecation phase (plan §2, Phase 10 ladder) removes V1.

Every path in this module is a pure computation — nothing here creates a
directory or touches disk (`core-is-bottom-layer` / `core-models-pure`-style
purity, verified by the same subprocess Qt-import check `core.storage.paths`
already passes). Only `agent_takkub.config` is imported, same leaf as
`core.storage.paths`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agent_takkub import config


@dataclass(frozen=True, slots=True)
class ProjectLayoutV2:
    """One project's V2-owned subtree — task's own list: ``project.json,
    worktrees, artifacts, conversations, brain, checkpoints, logs, state``."""

    root: Path
    project_json: Path
    worktrees: Path
    artifacts: Path
    conversations: Path
    brain: Path
    checkpoints: Path
    logs: Path
    state: Path


@dataclass(frozen=True, slots=True)
class StorageLayoutV2:
    """Root V2 layout — task's own list:
    ``config/ providers/ accounts/ models/ capabilities/ agents/
    projects/<id>/{...} brain/{global,operational}
    state/{providers,accounts,sessions,tasks,issues,registry}
    runtime/ cache/ secrets/ system/{version.json,schemas,migrations,backups}``.
    """

    root: Path
    config_dir: Path
    providers: Path
    accounts: Path
    models: Path
    capabilities: Path
    agents: Path
    projects_root: Path
    brain_global: Path
    brain_operational: Path
    state_providers: Path
    state_accounts: Path
    state_sessions: Path
    state_tasks: Path
    state_issues: Path
    state_registry: Path
    runtime: Path
    cache: Path
    secrets: Path
    system: Path
    system_version_json: Path
    system_schemas: Path
    system_migrations: Path
    system_backups: Path

    def project(self, project_id: str) -> ProjectLayoutV2:
        """Per-project subtree for *project_id*. No validation here — callers
        that take an untrusted name (CLI, UI) must run it through
        `config.validate_name` first, same rule `config.project_skills_dir`
        already follows for its own project-scoped path."""
        p_root = self.projects_root / project_id
        return ProjectLayoutV2(
            root=p_root,
            project_json=p_root / "project.json",
            worktrees=p_root / "worktrees",
            artifacts=p_root / "artifacts",
            conversations=p_root / "conversations",
            brain=p_root / "brain",
            checkpoints=p_root / "checkpoints",
            logs=p_root / "logs",
            state=p_root / "state",
        )


def storage_layout_v2(data_home: Path | None = None) -> StorageLayoutV2:
    """Build the V2 layout rooted at *data_home* (default `config.DATA_HOME`).

    Pure path arithmetic — never creates directories. A migration step's
    `apply()` is the only place that should `mkdir()` any of these.
    """
    home = data_home if data_home is not None else config.DATA_HOME
    root = home / "v2"
    system = root / "system"
    return StorageLayoutV2(
        root=root,
        config_dir=root / "config",
        providers=root / "providers",
        accounts=root / "accounts",
        models=root / "models",
        capabilities=root / "capabilities",
        agents=root / "agents",
        projects_root=root / "projects",
        brain_global=root / "brain" / "global",
        brain_operational=root / "brain" / "operational",
        state_providers=root / "state" / "providers",
        state_accounts=root / "state" / "accounts",
        state_sessions=root / "state" / "sessions",
        state_tasks=root / "state" / "tasks",
        state_issues=root / "state" / "issues",
        state_registry=root / "state" / "registry",
        runtime=root / "runtime",
        cache=root / "cache",
        secrets=root / "secrets",
        system=system,
        system_version_json=system / "version.json",
        system_schemas=system / "schemas",
        system_migrations=system / "migrations",
        system_backups=system / "backups",
    )


def layout_state(data_home: Path | None = None) -> str:
    """``"v1"`` (no V2 root yet) / ``"v2"`` (V2 root exists, V1 top-level
    dirs it maps from are all gone) / ``"mixed"`` (both present) — what
    `doctor`'s layout check reports. Existence-only, cheap enough for a
    doctor check (matches `check_core_version_compat`'s no-blocking-I/O
    posture)."""
    home = data_home if data_home is not None else config.DATA_HOME
    layout = storage_layout_v2(home)
    v2_exists = layout.root.is_dir()
    if not v2_exists:
        return "v1"
    v1_markers = (home / "projects.json", home / "runtime", config.SETTINGS_HOME)
    v1_still_present = any(m.exists() for m in v1_markers)
    return "mixed" if v1_still_present else "v2"


class LegacySemantics(StrEnum):
    """What kind of V1→V2 migration a mapped path needs (plan §5.3's ladder
    groups; `13_MIGRATION_MAPPING_FROM_V1.md`'s "Migration Rules")."""

    READ_ONLY_REGISTRY = "read_only_registry"  # flat copy, wrong value = defaults come back
    ROLE_AGENT = "role_agent"  # custom-roles / role-providers -> AgentTemplate + routing
    CAPABILITY = "capability"  # pane-tools / skill-policy
    PROJECT = "project"  # projects.json fan-out + worktree ownership
    STATE = "state"  # issues / autoresume / remote sessions
    CREDENTIAL = "credential"  # reference only, never copy the secret itself
    UNKNOWN = "unknown"  # semantics not established from source code yet


@dataclass(frozen=True, slots=True)
class LegacyMappingEntry:
    """One row of the V1→V2 map (`13_MIGRATION_MAPPING_FROM_V1.md` + audit
    §4), annotated with which ladder step (plan §5.3, 1=lowest risk) owns it
    and why. Data only — no step reads this table today (each step in
    `core.migration` hardcodes its own source/target paths so it stays
    independently testable), but it is the single documented answer to
    "what happens to V1 file X" that `takkub doctor --storage-layout` and
    the phase report both point at, per rule #1 "Do not move files blindly".
    """

    v1_path: str  # human-readable, relative to DATA_HOME or SETTINGS_HOME
    v2_path: str  # relative to StorageLayoutV2.root
    semantics: LegacySemantics
    ladder_step: int  # 0 = not yet scheduled
    note: str = ""


# One row per V1 artifact named in `13_MIGRATION_MAPPING_FROM_V1.md` and
# confirmed against the actual source path in this checkout (grepped, not
# guessed — audit §4 rule). `ladder_step` matches
# `docs/v2/V2_IMPLEMENTATION_PLAN.md` §5.3's ordering.
LEGACY_MAPPING: tuple[LegacyMappingEntry, ...] = (
    LegacyMappingEntry(
        "SETTINGS_HOME/provider-models.json",
        "models/registry.json",
        LegacySemantics.READ_ONLY_REGISTRY,
        1,
        "provider_models.py",
    ),
    LegacyMappingEntry(
        "SETTINGS_HOME/role-models.json",
        "models/aliases.json",
        LegacySemantics.READ_ONLY_REGISTRY,
        1,
        "role_models.py — full ModelProfile split is a later phase's job, not storage-move",
    ),
    LegacyMappingEntry(
        "SETTINGS_HOME/disabled-providers.json",
        "providers/registry.json",
        LegacySemantics.READ_ONLY_REGISTRY,
        1,
        "provider_state.py",
    ),
    LegacyMappingEntry(
        "SETTINGS_HOME/exec-mode.json",
        "config/execution.json",
        LegacySemantics.READ_ONLY_REGISTRY,
        1,
        "exec_mode.py",
    ),
    LegacyMappingEntry(
        "SETTINGS_HOME/rtk-enabled.json",
        "config/features/rtk.json",
        LegacySemantics.READ_ONLY_REGISTRY,
        1,
        "rtk_helper.py",
    ),
    LegacyMappingEntry(
        "SETTINGS_HOME/custom-roles.json",
        "agents/custom/registry.json",
        LegacySemantics.ROLE_AGENT,
        2,
        "custom_roles.py",
    ),
    LegacyMappingEntry(
        "CUSTOM_AGENTS_DIR/<role>.md",
        "agents/custom/<role>.md",
        LegacySemantics.ROLE_AGENT,
        2,
        "custom_roles.py role file, copied alongside registry",
    ),
    LegacyMappingEntry(
        "SETTINGS_HOME/role-providers.json (+ projects/<slug>/role-providers.json)",
        "config/routing.json",
        LegacySemantics.ROLE_AGENT,
        2,
        "provider_config.py — global + every known project, keyed {'global':..,'projects':{slug:..}}",
    ),
    LegacyMappingEntry(
        "SETTINGS_HOME/pane-tools.json",
        "capabilities/mcp/permissions.json",
        LegacySemantics.CAPABILITY,
        3,
        "pane_tools_policy.py",
    ),
    LegacyMappingEntry(
        "SETTINGS_HOME/skill-policy.json",
        "capabilities/skills/registry.json",
        LegacySemantics.CAPABILITY,
        3,
        "skill_policy.py",
    ),
    LegacyMappingEntry(
        "DATA_HOME/projects.json",
        "projects/registry.json",
        LegacySemantics.PROJECT,
        4,
        "config.py PROJECTS_JSON — index; per-project fan-out below",
    ),
    LegacyMappingEntry(
        "DATA_HOME/projects.json (per project)",
        "projects/<id>/project.json",
        LegacySemantics.PROJECT,
        4,
        "one file per known project name",
    ),
    LegacyMappingEntry(
        "DATA_HOME/worktrees/<project>/*",
        "projects/<id>/worktrees (ownership record only)",
        LegacySemantics.PROJECT,
        4,
        "worktree_manager.py owns the real checkouts; V2 records owner pointers, never copies checkouts",
    ),
    LegacyMappingEntry(
        "DATA_HOME/.takkub_issues.json",
        "state/issues/local.json",
        LegacySemantics.STATE,
        5,
        "maintenance.py check_local_issue_backlog",
    ),
    LegacyMappingEntry(
        "DATA_HOME/auto_issue_dedup.json",
        "state/issues/dedup.json",
        LegacySemantics.STATE,
        5,
        "auto_issue_capture.py _DEDUP_PATH",
    ),
    LegacyMappingEntry(
        "SETTINGS_HOME/autoresume.json",
        "state/sessions/autoresume.json",
        LegacySemantics.STATE,
        5,
        "auto_resume.py — copied as-is; the deeper reshape into Conversation/Checkpoint "
        "state (migration rule #4) is Phase 5/7's job, not a storage-location move",
    ),
    LegacyMappingEntry(
        "SETTINGS_HOME/takkub-remote-sessions.json",
        "state/sessions/remote.json",
        LegacySemantics.STATE,
        5,
        "remote/session_store.py",
    ),
    LegacyMappingEntry(
        "claude default_claude_config_dir()",
        "providers/claude/ + accounts/claude/ (reference only)",
        LegacySemantics.CREDENTIAL,
        6,
        "credential bytes NEVER copied — reference record only (path + secret_ref)",
    ),
    LegacyMappingEntry(
        "DATA_HOME/codex-home",
        "providers/codex/ + accounts/codex/ (reference only)",
        LegacySemantics.CREDENTIAL,
        6,
        "config.provider_home_env('codex')",
    ),
    LegacyMappingEntry(
        "DATA_HOME/opencode-home",
        "providers/opencode/ + accounts/opencode/ (reference only)",
        LegacySemantics.CREDENTIAL,
        6,
        "config.provider_home_env('opencode')",
    ),
    LegacyMappingEntry(
        "graft-graphs/ graft-staging/",
        "capabilities/plugins/ (subsystem cache)",
        LegacySemantics.UNKNOWN,
        0,
        "blueprint §13 itself says 'subsystem-specific path หรือ plugin storage' — "
        "not scheduled on the ladder; left as an explicit gap, not silently dropped",
    ),
    LegacyMappingEntry(
        "RUNTIME_DIR/tasks",
        "state/tasks",
        LegacySemantics.STATE,
        7,
        "task_ledger.py — audit D6 'state ถาวร', not runtime cache",
    ),
    LegacyMappingEntry(
        "RUNTIME_DIR/sessions",
        "state/sessions",
        LegacySemantics.STATE,
        7,
        "orchestrator_text.py / lead_context.py decision notes",
    ),
    LegacyMappingEntry(
        "RUNTIME_DIR/role-memory",
        "state/registry/role-memory",
        LegacySemantics.STATE,
        7,
        "role_memory.py ROLE_MEMORY_DIR",
    ),
    LegacyMappingEntry(
        "RUNTIME_DIR/knowledge",
        "state/registry/knowledge",
        LegacySemantics.STATE,
        7,
        "vault_mirror.py",
    ),
    LegacyMappingEntry(
        "RUNTIME_DIR/tunnel",
        "(classified only — cache, never copied)",
        LegacySemantics.UNKNOWN,
        0,
        "remote/tunnel.py — transient process state, D6 'ขยะ' bucket",
    ),
    LegacyMappingEntry(
        "RUNTIME_DIR/browser-profiles",
        "(classified only — cache, never copied)",
        LegacySemantics.UNKNOWN,
        0,
        "browser_chrome.py — already prunable via disk_usage.py",
    ),
)
