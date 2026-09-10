"""Storage V2 layout (#309 Phase 8b, blueprint `02_STORAGE_AND_FOLDER_STRUCTURE.md`
+ `13_MIGRATION_MAPPING_FROM_V1.md`, trimmed to the task's own explicit tree).

Physical root: ``config.DATA_HOME`` itself. Through 2.0.x this was nested
under ``config.DATA_HOME / "v2"`` — V1 already owned bare top-level names
like ``projects/``, ``runtime/``, ``cache/`` for a different shape, and
nesting under ``v2/`` kept every migration step copy-never-move-safe while
V1 stayed authoritative. #504 (2.1.0) retires that nesting: the boot-time
ladder's own `core.migration.promote_v1.ArchiveV1LegacyStep` moves every V1
top-level artifact into ``DATA_HOME/backups/v1-archive-<ts>/`` (archived,
never deleted) BEFORE anything reuses those names here, and
`PromoteV2RootStep` relocates a pre-existing nested ``v2/`` root's contents
up to the paths this module now computes directly — so by the time any V2
path below is read for real, the name collision the old nesting existed to
avoid has already been resolved on disk, not by nesting in code.

Every path in this module is a pure computation — nothing here creates a
directory, touches disk, or depends on what currently exists on disk
(`core-is-bottom-layer` / `core-models-pure`-style purity, verified by the
same subprocess Qt-import check `core.storage.paths` already passes, and by
`test_core_storage_layout.py`'s explicit "never stats the filesystem" case).
Only `agent_takkub.config` is imported, same leaf as `core.storage.paths`.
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

    Pure path arithmetic — never creates directories, never stats the
    filesystem. A migration step's `apply()` is the only place that should
    `mkdir()` any of these.

    Dev checkouts (`data_home == config.REPO_ROOT`) keep the pre-#504
    nested ``v2/`` root — same `if DATA_HOME == REPO_ROOT:` gate every other
    dev/installed split in `config.py` already uses (`_resolve_settings_
    home`, `default_claude_config_dir`, `provider_home_env`, ...), not a new
    pattern. A dev checkout's `DATA_HOME` IS the git repo (`config
    ._resolve_data_home`), which already has REAL committed content at some
    of these exact top-level names (this repo's own `capabilities/` skill
    hub, for one) — reusing them unnamespaced there would silently
    interleave that content with V2's. #504's boot-time promote/archive
    ladder is ALSO gated off entirely for dev checkouts
    (`auto_migrate_boot.is_dev_checkout()`), so there is never a real
    machine to promote for this branch — every OTHER caller of this
    function (`core.routing.router`, `provider_config`'s dual-write gate,
    ...) must keep resolving to this same safe, nested spot too."""
    home = data_home if data_home is not None else config.DATA_HOME
    root = (home / "v2") if home == config.REPO_ROOT else home
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
    """``"v1"`` (nothing migrated yet) / ``"v2"`` (fully on the promoted
    top-level layout, no V1 leftovers) / ``"mixed"`` (a pre-#504 nested
    ``v2/`` root is still on disk — `core.migration.promote_v1`'s ladder
    steps haven't finished moving it up yet) — what `doctor`'s layout check
    reports. Existence-only, cheap enough for a doctor check (matches
    `check_core_version_compat`'s no-blocking-I/O posture).

    ``"mixed"`` should only ever be observed transiently, mid-boot, on a
    machine upgrading straight from 2.0.x — `auto_migrate_boot.run_boot_stage`
    finishes the promote before anything else touches storage (#504 item 1),
    so a *running* app never sees it as a resting state; it exists here
    mainly so that boot-time dispatch (and a `doctor` run mid-migration, or
    one that hits a stuck promote) can still tell "not yet promoted" apart
    from "no V1 data ever existed"."""
    home = data_home if data_home is not None else config.DATA_HOME
    if (home / "v2").is_dir():
        return "mixed"
    layout = storage_layout_v2(home)
    # Any well-known top-level V2 domain having actual content counts as
    # "the ladder has run" — NOT just `system_version_json` alone: that
    # file's home flips through `core.storage.paths.core_home()`'s OWN
    # legacy-fallback, tied specifically to whether `CoreInternalStoreStep`
    # has run on this machine, which is exactly the kind of "one step
    # landed, another hasn't yet" gap #362 exists to tolerate — using it
    # alone as the sole V2-presence signal would make a machine that has
    # every OTHER step applied read as `"v1"` again. A bare *empty* tree
    # doesn't count either — a step's own `rollback()` can leave empty
    # parent dirs behind after removing the file(s) it actually wrote
    # (`mkdir(parents=True)` from the original `apply()` was never its job
    # to clean up), which must read as "nothing here", not "V2 present" —
    # so this checks for an actual FILE anywhere under each domain, not
    # merely a non-empty top level.
    any_v2_domain = any(
        p.is_dir() and any(f.is_file() for f in p.rglob("*"))
        for p in (layout.config_dir, layout.models, layout.system, layout.projects_root)
    )
    if not any_v2_domain:
        return "v1"
    # #504: files the ladder's `ArchiveV1LegacyStep` actually moves away —
    # NOT `runtime/` or `config.SETTINGS_HOME` (the pre-#504 markers this
    # replaced): both are permanent, always-present infrastructure on a
    # real install (`runtime/` is #504 item 8's explicit "never touch", and
    # `SETTINGS_HOME == DATA_HOME` on every non-dev install — see
    # `config._resolve_settings_home`), so checking either made `"v2"`
    # unreachable forever, not just until migration finished.
    v1_markers = (
        home / "projects.json",
        home / "custom-roles.json",
        home / ".takkub_issues.json",
    )
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
    CORE_INTERNAL = "core_internal"  # Core V2's own store under core_home(), not a V1 config file
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
    LegacyMappingEntry(
        "RUNTIME_DIR/core/* (version.json, accounts/model_catalog JSONL stores, "
        "conversations/, brain/, ...)",
        "system/ (generic copy of everything under core_home() except the ladder's "
        "own migration_journal.jsonl + migration_backups/)",
        LegacySemantics.CORE_INTERNAL,
        8,
        "core/storage/paths.py core_home() — #360, CoreInternalStoreStep; core_home()'s own "
        "legacy-fallback flips every future caller to system/ the instant this step's "
        "target directory exists",
    ),
)
