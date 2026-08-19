"""Concrete V1→V2 ladder steps (#309 Phase 8b, plan §5.3), lowest risk
first. Steps 1/3/5 are `RegistryCopyStep` instances built by the factory
functions below (each is "copy N small V1 JSON files verbatim into their V2
layout location", nothing more). Steps 2/4/6/7 need real fan-out or
reference-only semantics and get their own classes here.

Every step is copy-never-move: none of them writes to, deletes, or mutates
a V1 source path — `apply()` only ever creates/overwrites files under
`StorageLayoutV2.root`, and always backs up its own prior V2 target first
so `rollback()` can undo just its own writes.

Every step/factory takes an explicit ``data_home``/``settings_home``
override (default: the live `config.DATA_HOME`/`config.SETTINGS_HOME`),
mirroring `disk_usage.py`'s own `data_home` parameter — so a test can point
a step at a `tmp_path` fixture instead of monkeypatching module globals.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_takkub import config

from ..storage.layout import storage_layout_v2
from ..storage.legacy_reader import read_json
from .backup import BackupManager
from .journal import MigrationJournal
from .registry_copy_step import RegistryCopyStep, RegistryMapping, write_json_atomic
from .report import StepReport

# ---------------------------------------------------------------------------
# Step 1 — read-only registries (lowest risk: a bad value just brings back
# the built-in default, nothing user-authored is lost)
# ---------------------------------------------------------------------------


def build_readonly_registries_step(
    journal: MigrationJournal | None = None,
    backups: BackupManager | None = None,
    *,
    data_home: Path | None = None,
    settings_home: Path | None = None,
) -> RegistryCopyStep:
    settings = settings_home if settings_home is not None else config.SETTINGS_HOME
    layout = storage_layout_v2(data_home)
    mappings = (
        RegistryMapping(
            "provider-models", settings / "provider-models.json", layout.models / "registry.json"
        ),
        RegistryMapping(
            "role-models", settings / "role-models.json", layout.models / "aliases.json"
        ),
        RegistryMapping(
            "disabled-providers",
            settings / "disabled-providers.json",
            layout.providers / "registry.json",
        ),
        RegistryMapping(
            "exec-mode", settings / "exec-mode.json", layout.config_dir / "execution.json"
        ),
        RegistryMapping(
            "rtk-enabled",
            settings / "rtk-enabled.json",
            layout.config_dir / "features" / "rtk.json",
        ),
    )
    return RegistryCopyStep(
        "readonly-registries", mappings, journal or MigrationJournal(), backups or BackupManager()
    )


# ---------------------------------------------------------------------------
# Step 3 — capability (pane-tools / skill-policy)
# ---------------------------------------------------------------------------


def build_capability_step(
    journal: MigrationJournal | None = None,
    backups: BackupManager | None = None,
    *,
    data_home: Path | None = None,
    settings_home: Path | None = None,
) -> RegistryCopyStep:
    settings = settings_home if settings_home is not None else config.SETTINGS_HOME
    layout = storage_layout_v2(data_home)
    mappings = (
        RegistryMapping(
            "pane-tools",
            settings / "pane-tools.json",
            layout.capabilities / "mcp" / "permissions.json",
        ),
        RegistryMapping(
            "skill-policy",
            settings / "skill-policy.json",
            layout.capabilities / "skills" / "registry.json",
        ),
    )
    return RegistryCopyStep(
        "capability", mappings, journal or MigrationJournal(), backups or BackupManager()
    )


# ---------------------------------------------------------------------------
# Step 5 — state (issues / autoresume / remote sessions)
# ---------------------------------------------------------------------------


def build_state_step(
    journal: MigrationJournal | None = None,
    backups: BackupManager | None = None,
    *,
    data_home: Path | None = None,
    settings_home: Path | None = None,
) -> RegistryCopyStep:
    home = data_home if data_home is not None else config.DATA_HOME
    settings = settings_home if settings_home is not None else config.SETTINGS_HOME
    layout = storage_layout_v2(data_home)
    mappings = (
        RegistryMapping(
            "local-issues", home / ".takkub_issues.json", layout.state_issues / "local.json"
        ),
        RegistryMapping(
            "issue-dedup", home / "auto_issue_dedup.json", layout.state_issues / "dedup.json"
        ),
        RegistryMapping(
            "autoresume",
            settings / "autoresume.json",
            layout.state_sessions / "autoresume.json",
            "copied as-is; the deeper reshape into Conversation/Checkpoint state "
            "(blueprint migration rule #4) is Phase 5/7's job, not a storage-location move",
        ),
        RegistryMapping(
            "remote-sessions",
            settings / "takkub-remote-sessions.json",
            layout.state_sessions / "remote.json",
        ),
    )
    return RegistryCopyStep(
        "state", mappings, journal or MigrationJournal(), backups or BackupManager()
    )


# ---------------------------------------------------------------------------
# Step 2 — role / agent (custom-roles.json + role-providers.json, global +
# every known project, fanned into config/routing.json; custom role .md
# files copied alongside their registry entry)
# ---------------------------------------------------------------------------


@dataclass
class RoleAgentMigrationStep:
    step_id: str = "role-agent"
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)
    data_home: Path = field(default_factory=lambda: config.DATA_HOME)
    settings_home: Path = field(default_factory=lambda: config.SETTINGS_HOME)
    custom_agents_dir: Path = field(default_factory=lambda: config.CUSTOM_AGENTS_DIR)

    def _custom_roles_source(self) -> Path:
        return self.settings_home / "custom-roles.json"

    def _custom_roles_target(self) -> Path:
        return storage_layout_v2(self.data_home).agents / "custom" / "registry.json"

    def _project_names(self) -> list[str]:
        data = read_json(self.data_home / "projects.json")
        projects = data.get("projects") if isinstance(data, dict) else None
        return list(projects) if isinstance(projects, dict) else []

    def _project_routing_source(self, project: str) -> Path:
        # Mirrors `provider_config._project_slug` (private, so re-derived
        # here rather than importing a leading-underscore name across
        # modules) — filesystem-safe folder name for a project.
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", project) or "default"
        return self.settings_home / "projects" / slug / "role-providers.json"

    def _routing_sources(self) -> dict[str, Path]:
        sources: dict[str, Path] = {"global": self.settings_home / "role-providers.json"}
        for name in self._project_names():
            sources[name] = self._project_routing_source(name)
        return sources

    def _routing_target(self) -> Path:
        return storage_layout_v2(self.data_home).config_dir / "routing.json"

    def _role_md_pairs(self) -> list[tuple[str, Path, Path]]:
        """(role_name, v1_md_path, v2_md_path) for every role the registry
        names whose .md file actually exists — a stale registry entry with
        no file is not an error, just skipped."""
        registry = read_json(self._custom_roles_source())
        pairs: list[tuple[str, Path, Path]] = []
        if not isinstance(registry, dict):
            return pairs
        for role_name in registry:
            src = self.custom_agents_dir / f"{role_name}.md"
            if src.exists():
                pairs.append(
                    (
                        role_name,
                        src,
                        storage_layout_v2(self.data_home).agents / "custom" / f"{role_name}.md",
                    )
                )
        return pairs

    def _routing_payload(self) -> dict:
        sources = self._routing_sources()
        global_src = sources.pop("global")
        return {
            "schema": 1,
            "migrated_at": time.time(),
            "global": read_json(global_src),
            "projects": {name: read_json(p) for name, p in sources.items()},
        }

    def inspect(self) -> StepReport:
        role_pairs = self._role_md_pairs()
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"custom-roles registry {'present' if self._custom_roles_source().exists() else 'missing'}; "
            f"{len(role_pairs)} custom role file(s); "
            f"{len(self._routing_sources())} role-providers source(s) (global + per-project)",
            detail={
                "custom_roles_present": self._custom_roles_source().exists(),
                "role_files": [r[0] for r in role_pairs],
                "routing_sources": [str(p) for p in self._routing_sources().values()],
            },
        )

    def plan(self) -> StepReport:
        return StepReport(
            self.step_id,
            "plan",
            True,
            f"will write custom-roles registry, {len(self._role_md_pairs())} role file(s), "
            "and one merged routing.json (global + per-project)",
            detail={
                "custom_roles_target": str(self._custom_roles_target()),
                "routing_target": str(self._routing_target()),
            },
        )

    def dry_run(self) -> StepReport:
        registry_changed = read_json(self._custom_roles_target()).get("data") != read_json(
            self._custom_roles_source()
        )
        routing_changed = read_json(self._routing_target()).get("global") != read_json(
            self._routing_sources()["global"]
        )
        md_changed = [
            name
            for name, src, dest in self._role_md_pairs()
            if not dest.exists()
            or dest.read_text(encoding="utf-8") != src.read_text(encoding="utf-8")
        ]
        return StepReport(
            self.step_id,
            "dry_run",
            True,
            "no disk writes; "
            f"registry {'would change' if registry_changed else 'unchanged'}, "
            f"routing {'would change' if routing_changed else 'unchanged'}, "
            f"{len(md_changed)} role file(s) would change",
            detail={"md_would_change": md_changed},
        )

    def apply(self) -> StepReport:
        try:
            reg_target = self._custom_roles_target()
            self.backups.backup(f"{self.step_id}__registry", reg_target)
            write_json_atomic(
                reg_target,
                {
                    "schema": 1,
                    "migrated_from": str(self._custom_roles_source()),
                    "migrated_at": time.time(),
                    "data": read_json(self._custom_roles_source()),
                },
            )

            routing_target = self._routing_target()
            self.backups.backup(f"{self.step_id}__routing", routing_target)
            write_json_atomic(routing_target, self._routing_payload())

            written_md: list[str] = []
            for role_name, src, dest in self._role_md_pairs():
                self.backups.backup(f"{self.step_id}__md__{role_name}", dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                written_md.append(role_name)
        except OSError as e:
            self.journal.record(self.step_id, "apply", False, str(e))
            return StepReport(self.step_id, "apply", False, f"write failed: {e}")

        self.journal.record(self.step_id, "apply", True, f"{len(written_md)} role file(s)")
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"wrote custom-roles registry, routing.json, {len(written_md)} role file(s)",
            detail={"role_files": written_md},
        )

    def validate(self) -> StepReport:
        registry_ok = read_json(self._custom_roles_target()).get("data") == read_json(
            self._custom_roles_source()
        )
        routing_doc = read_json(self._routing_target())
        routing_ok = routing_doc.get("global") == read_json(self._routing_sources()["global"])
        md_mismatched = [
            name
            for name, src, dest in self._role_md_pairs()
            if not dest.exists()
            or dest.read_text(encoding="utf-8") != src.read_text(encoding="utf-8")
        ]
        ok = registry_ok and routing_ok and not md_mismatched
        return StepReport(
            self.step_id,
            "validate",
            ok,
            "registry/routing/role files all match V1 source" if ok else "mismatch found",
            detail={
                "registry_ok": registry_ok,
                "routing_ok": routing_ok,
                "md_mismatched": md_mismatched,
            },
        )

    def rollback(self) -> StepReport:
        try:
            for key, target in (
                (f"{self.step_id}__registry", self._custom_roles_target()),
                (f"{self.step_id}__routing", self._routing_target()),
            ):
                backup = self.backups.latest_backup(key, target.name)
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    self.backups.restore(backup, target)
            for role_name, _src, dest in self._role_md_pairs():
                key = f"{self.step_id}__md__{role_name}"
                backup = self.backups.latest_backup(key, dest.name)
                if backup is None:
                    dest.unlink(missing_ok=True)
                else:
                    self.backups.restore(backup, dest)
        except OSError as e:
            self.journal.record(self.step_id, "rollback", False, str(e))
            return StepReport(self.step_id, "rollback", False, f"restore failed: {e}")
        self.journal.record(self.step_id, "rollback", True, "restored")
        return StepReport(self.step_id, "rollback", True, "restored registry/routing/role files")


# ---------------------------------------------------------------------------
# Step 4 — project data (projects.json -> registry.json + per-project
# project.json, worktree ownership recorded as a pointer list — the real
# checkouts stay owned by worktree_manager.py, never copied)
# ---------------------------------------------------------------------------


@dataclass
class ProjectMigrationStep:
    step_id: str = "project"
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)
    data_home: Path = field(default_factory=lambda: config.DATA_HOME)

    def _projects_json(self) -> Path:
        return self.data_home / "projects.json"

    def _registry_target(self) -> Path:
        return storage_layout_v2(self.data_home).projects_root / "registry.json"

    def _project_target(self, project_id: str) -> Path:
        return storage_layout_v2(self.data_home).project(project_id).project_json

    def _worktrees_owned(self, project_id: str) -> list[str]:
        wt_dir = self.data_home / "worktrees" / project_id
        if not wt_dir.is_dir():
            return []
        try:
            return sorted(p.name for p in wt_dir.iterdir() if p.is_dir())
        except OSError:
            return []

    def _load(self) -> dict:
        data = read_json(self._projects_json())
        return data if isinstance(data, dict) else {}

    def inspect(self) -> StepReport:
        data = self._load()
        project_ids = list(data.get("projects", {}))
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"projects.json {'present' if self._projects_json().exists() else 'missing'}; "
            f"{len(project_ids)} known project(s)",
            detail={"projects": project_ids},
        )

    def plan(self) -> StepReport:
        data = self._load()
        project_ids = list(data.get("projects", {}))
        return StepReport(
            self.step_id,
            "plan",
            True,
            f"will write registry.json + {len(project_ids)} project.json + worktree ownership pointers",
            detail={pid: str(self._project_target(pid)) for pid in project_ids},
        )

    def dry_run(self) -> StepReport:
        data = self._load()
        would_change = [
            pid
            for pid, pdata in data.get("projects", {}).items()
            if read_json(self._project_target(pid)).get("data") != pdata
        ]
        return StepReport(
            self.step_id,
            "dry_run",
            True,
            f"no disk writes; {len(would_change)}/{len(data.get('projects', {}))} project(s) would change",
            detail={"would_change": would_change},
        )

    def apply(self) -> StepReport:
        data = self._load()
        try:
            self.backups.backup(f"{self.step_id}__registry", self._registry_target())
            write_json_atomic(
                self._registry_target(),
                {
                    "schema": 1,
                    "migrated_from": str(self._projects_json()),
                    "migrated_at": time.time(),
                    "data": data,
                },
            )
            written: list[str] = []
            for pid, pdata in data.get("projects", {}).items():
                target = self._project_target(pid)
                self.backups.backup(f"{self.step_id}__project__{pid}", target)
                write_json_atomic(
                    target,
                    {
                        "schema": 1,
                        "migrated_from": str(self._projects_json()),
                        "migrated_at": time.time(),
                        "data": pdata,
                        "worktrees_owned": self._worktrees_owned(pid),
                    },
                )
                written.append(pid)
        except OSError as e:
            self.journal.record(self.step_id, "apply", False, str(e))
            return StepReport(self.step_id, "apply", False, f"write failed: {e}")
        self.journal.record(self.step_id, "apply", True, f"{len(written)} project(s)")
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"wrote registry + {len(written)} project(s)",
            detail={"written": written},
        )

    def validate(self) -> StepReport:
        data = self._load()
        registry_ok = read_json(self._registry_target()).get("data") == data
        mismatched = [
            pid
            for pid, pdata in data.get("projects", {}).items()
            if read_json(self._project_target(pid)).get("data") != pdata
        ]
        ok = registry_ok and not mismatched
        return StepReport(
            self.step_id,
            "validate",
            ok,
            "registry + every project match V1 source" if ok else "mismatch found",
            detail={"registry_ok": registry_ok, "mismatched": mismatched},
        )

    def rollback(self) -> StepReport:
        data = self._load()
        try:
            for key, target in [
                (f"{self.step_id}__registry", self._registry_target()),
                *[
                    (f"{self.step_id}__project__{pid}", self._project_target(pid))
                    for pid in data.get("projects", {})
                ],
            ]:
                backup = self.backups.latest_backup(key, target.name)
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    self.backups.restore(backup, target)
        except OSError as e:
            self.journal.record(self.step_id, "rollback", False, str(e))
            return StepReport(self.step_id, "rollback", False, f"restore failed: {e}")
        self.journal.record(self.step_id, "rollback", True, "restored")
        return StepReport(self.step_id, "rollback", True, "restored registry + project.json files")


# ---------------------------------------------------------------------------
# Step 6 — credential / account (highest risk — REFERENCE ONLY, never
# copies a credential byte; claude/codex/opencode config-dir presence
# becomes a pointer record, same secret_ref scheme
# `core.accounts.legacy_reader` already uses)
# ---------------------------------------------------------------------------


@dataclass
class CredentialReferenceStep:
    step_id: str = "credential-reference"
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)
    data_home: Path = field(default_factory=lambda: config.DATA_HOME)
    # Injectable so tests never need a real claude/codex/opencode home on
    # the machine running the suite — production callers leave these None
    # and get the live `config` resolvers.
    refs_override: dict[str, Path] | None = None

    def _refs(self) -> dict[str, Path]:
        if self.refs_override is not None:
            return dict(self.refs_override)
        refs: dict[str, Path] = {"claude": config.default_claude_config_dir()}
        codex_env = config.provider_home_env("codex")
        if codex_env.get("CODEX_HOME"):
            refs["codex"] = Path(codex_env["CODEX_HOME"])
        opencode_env = config.provider_home_env("opencode")
        if opencode_env.get("XDG_DATA_HOME"):
            refs["opencode"] = Path(opencode_env["XDG_DATA_HOME"])
        return refs

    def _provider_target(self, provider: str) -> Path:
        return storage_layout_v2(self.data_home).providers / provider / "provider.json"

    def _account_target(self, provider: str) -> Path:
        return storage_layout_v2(self.data_home).accounts / provider / "account.json"

    def _record(self, provider: str, config_dir: Path) -> dict:
        return {
            "schema": 1,
            "migrated_at": time.time(),
            "provider": provider,
            "config_dir": str(config_dir),
            "config_dir_exists": config_dir.is_dir(),
            "secret_ref": f"secret://{provider}/default",
            "note": "reference only — credential bytes are never copied (plan §5.1 rule)",
        }

    def inspect(self) -> StepReport:
        refs = self._refs()
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"{len(refs)} provider(s) with a resolvable config-dir "
            f"({sum(1 for p in refs.values() if p.is_dir())} exist on disk)",
            detail={p: {"path": str(d), "exists": d.is_dir()} for p, d in refs.items()},
        )

    def plan(self) -> StepReport:
        refs = self._refs()
        return StepReport(
            self.step_id,
            "plan",
            True,
            f"will write {len(refs) * 2} reference record(s) (provider.json + account.json per provider) — "
            "no credential bytes ever copied",
            detail={p: {"provider_target": str(self._provider_target(p))} for p in refs},
        )

    def dry_run(self) -> StepReport:
        refs = self._refs()
        return StepReport(
            self.step_id,
            "dry_run",
            True,
            f"no disk writes; would write reference records for {len(refs)} provider(s)",
            detail={"providers": list(refs)},
        )

    def apply(self) -> StepReport:
        refs = self._refs()
        written: list[str] = []
        try:
            for provider, config_dir in refs.items():
                record = self._record(provider, config_dir)
                prov_target = self._provider_target(provider)
                acct_target = self._account_target(provider)
                self.backups.backup(f"{self.step_id}__provider__{provider}", prov_target)
                self.backups.backup(f"{self.step_id}__account__{provider}", acct_target)
                write_json_atomic(prov_target, record)
                write_json_atomic(acct_target, record)
                written.append(provider)
        except OSError as e:
            self.journal.record(self.step_id, "apply", False, str(e))
            return StepReport(self.step_id, "apply", False, f"write failed: {e}")
        self.journal.record(self.step_id, "apply", True, f"{len(written)} provider(s)")
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"wrote reference records for {len(written)} provider(s)",
            detail={"written": written},
        )

    def validate(self) -> StepReport:
        refs = self._refs()
        missing = [
            p
            for p in refs
            if not self._provider_target(p).exists() or not self._account_target(p).exists()
        ]
        ok = not missing
        return StepReport(
            self.step_id,
            "validate",
            ok,
            "every provider has a reference record"
            if ok
            else f"{len(missing)} provider(s) missing a record",
            detail={"missing": missing},
        )

    def rollback(self) -> StepReport:
        refs = self._refs()
        try:
            for provider in refs:
                for key, target in (
                    (f"{self.step_id}__provider__{provider}", self._provider_target(provider)),
                    (f"{self.step_id}__account__{provider}", self._account_target(provider)),
                ):
                    backup = self.backups.latest_backup(key, target.name)
                    if backup is None:
                        target.unlink(missing_ok=True)
                    else:
                        self.backups.restore(backup, target)
        except OSError as e:
            self.journal.record(self.step_id, "rollback", False, str(e))
            return StepReport(self.step_id, "rollback", False, f"restore failed: {e}")
        self.journal.record(self.step_id, "rollback", True, "restored")
        return StepReport(
            self.step_id, "rollback", True, "restored provider/account reference records"
        )


# ---------------------------------------------------------------------------
# Step 7 — runtime/ triage (audit D6): permanent state (tasks, sessions,
# role-memory, knowledge) copied into V2 state/; transient/junk dirs
# (tunnel, browser-profiles) are only classified here, never touched — the
# task explicitly keeps deletion out of this step's scope.
# ---------------------------------------------------------------------------

# runtime-subdir name -> V2 StorageLayoutV2 attribute, or "attr/subpath" for
# a nested target (state/registry has no dedicated task-list bucket for
# role-memory/knowledge, so they land under its catch-all "registry").
RUNTIME_STATE_TARGETS: dict[str, str] = {
    "tasks": "state_tasks",
    "sessions": "state_sessions",
    "role-memory": "state_registry/role-memory",
    "knowledge": "state_registry/knowledge",
}
# Classified, never moved/deleted by this step (disk_usage.py already prunes
# these on request; this table only feeds `takkub doctor --storage-layout`'s
# read-only awareness, per the task's "ยังไม่ลบอัตโนมัติ" instruction).
RUNTIME_CACHE_ENTRIES: frozenset[str] = frozenset({"tunnel", "browser-profiles"})


def _runtime_state_target(data_home: Path, name: str) -> Path:
    layout = storage_layout_v2(data_home)
    mapping = RUNTIME_STATE_TARGETS[name]
    if "/" in mapping:
        attr, sub = mapping.split("/", 1)
        return getattr(layout, attr) / sub
    return getattr(layout, mapping)


@dataclass
class RuntimeTriageStep:
    step_id: str = "runtime-triage"
    journal: MigrationJournal = field(default_factory=MigrationJournal)
    backups: BackupManager = field(default_factory=BackupManager)
    data_home: Path = field(default_factory=lambda: config.DATA_HOME)
    runtime_dir: Path = field(default_factory=lambda: config.RUNTIME_DIR)

    def _present_state_dirs(self) -> list[str]:
        return [n for n in RUNTIME_STATE_TARGETS if (self.runtime_dir / n).is_dir()]

    def _present_cache_dirs(self) -> list[str]:
        return [n for n in RUNTIME_CACHE_ENTRIES if (self.runtime_dir / n).is_dir()]

    def _target(self, name: str) -> Path:
        return _runtime_state_target(self.data_home, name)

    def inspect(self) -> StepReport:
        state_dirs = self._present_state_dirs()
        cache_dirs = self._present_cache_dirs()
        return StepReport(
            self.step_id,
            "inspect",
            True,
            f"{len(state_dirs)} permanent-state dir(s) to copy; "
            f"{len(cache_dirs)} cache/junk dir(s) classified only (not touched)",
            detail={"state_dirs": state_dirs, "cache_dirs": cache_dirs},
        )

    def plan(self) -> StepReport:
        state_dirs = self._present_state_dirs()
        return StepReport(
            self.step_id,
            "plan",
            True,
            f"will copy {len(state_dirs)} state dir(s) into V2 state/; "
            "cache/junk dirs are read-only classified, never pruned by this step",
            detail={n: str(self._target(n)) for n in state_dirs},
        )

    def dry_run(self) -> StepReport:
        state_dirs = self._present_state_dirs()
        would_change = [n for n in state_dirs if not self._target(n).is_dir()]
        return StepReport(
            self.step_id,
            "dry_run",
            True,
            f"no disk writes; {len(would_change)}/{len(state_dirs)} state dir(s) not yet copied",
            detail={"would_change": would_change},
        )

    def apply(self) -> StepReport:
        copied: list[str] = []
        try:
            for name in self._present_state_dirs():
                src = self.runtime_dir / name
                dest = self._target(name)
                self.backups.backup(f"{self.step_id}__{name}", dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src, dest)
                copied.append(name)
        except OSError as e:
            self.journal.record(self.step_id, "apply", False, str(e))
            return StepReport(self.step_id, "apply", False, f"copy failed: {e}")
        self.journal.record(self.step_id, "apply", True, f"{len(copied)} dir(s)")
        return StepReport(
            self.step_id,
            "apply",
            True,
            f"copied {len(copied)} state dir(s)",
            detail={"copied": copied},
        )

    def validate(self) -> StepReport:
        missing = [n for n in self._present_state_dirs() if not self._target(n).is_dir()]
        ok = not missing
        return StepReport(
            self.step_id,
            "validate",
            ok,
            "every present state dir was copied" if ok else f"{len(missing)} dir(s) missing in V2",
            detail={"missing": missing},
        )

    def rollback(self) -> StepReport:
        try:
            for name in self._present_state_dirs():
                dest = self._target(name)
                backup = self.backups.latest_backup(f"{self.step_id}__{name}", dest.name)
                if backup is None:
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    self.backups.restore(backup, dest)
        except OSError as e:
            self.journal.record(self.step_id, "rollback", False, str(e))
            return StepReport(self.step_id, "rollback", False, f"restore failed: {e}")
        self.journal.record(self.step_id, "rollback", True, "restored")
        return StepReport(self.step_id, "rollback", True, "restored/removed copied state dirs")
