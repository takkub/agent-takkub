"""Pipeline executor mixin — run_pipeline, hop management, auto-chain, shard fan-out.

Extracted from orchestrator.py (pipeline_and_fanout cluster).  Provides
``PipelineMixin`` which ``Orchestrator`` inherits as its first base so all
``self.*`` calls resolve through the combined MRO as before.

Dependencies on the host class (Orchestrator):
  spawn(role, cwd, project)        — spawn_engine cluster
  _ps(key)                         — spawn_engine cluster
  _notify_lead(project_ns, body)   — lead_notify_pump cluster
  _resolve_project(project)        — static helper
  _pipeline_runs: dict             — state, kept in Orchestrator.__init__
  _shard_groups:  dict             — state, kept in Orchestrator.__init__
  _pane_state:    dict[str, PaneState]  — state, kept in Orchestrator.__init__

Layer rule (enforced by import-linter "pipeline-executor-layer" contract):
  pipeline_executor MUST NOT import main_window / app / cli.
"""

from __future__ import annotations

import itertools
import os
import sys
from dataclasses import dataclass, field

from PyQt6.QtCore import QTimer

# ── Constants ─────────────────────────────────────────────────────────────────

# Pipeline-hop spawn staggering (#44). A multi-role hop spawns its roles via
# _fire_pipeline_hop; firing them back-to-back on one event-loop tick hits the
# same ConPTY collision the cli_server stagger fixes for manual fan-out (the 2nd+
# ConPTY COM call lands during the 1st spawn's input-sync dispatch →
# RPC_E_CANTCALLOUT). Space them across ticks instead. Same env knobs as
# cli_server so the operator tunes one place; codex roles get the larger gap so
# their npm self-update windows don't overlap (#38).
_SPAWN_STAGGER_MS = int(os.environ.get("TAKKUB_SPAWN_STAGGER_MS", "400"))
_CODEX_SPAWN_STAGGER_MS = int(os.environ.get("TAKKUB_CODEX_SPAWN_STAGGER_MS", "10000"))

# Timeout before injecting a partial handoff when shards don't all respond.
_SHARD_GROUP_TIMEOUT_MS: int = 45 * 60 * 1000  # 45 minutes

_shard_generation_counter: itertools.count = itertools.count()


# ── Utilities ─────────────────────────────────────────────────────────────────


def _log_event(event: str, **details) -> None:
    """Proxy to orchestrator._log_event (lazy to avoid circular import at load time).

    Tests patch EVENTS_LOG / _EVENTS_LOG_MAX_BYTES on the orchestrator module; the
    real implementation there reads those names from its own namespace.  By the time
    any PipelineMixin method is called, orchestrator is fully loaded in sys.modules.
    """
    _orch = sys.modules.get("agent_takkub.orchestrator")
    if _orch is not None:
        _orch._log_event(event, **details)


def _split_shard(key: str) -> tuple[str, int | None]:
    """Split a pane key into ``(base_role, shard_index)``.

    ``"qa#2"`` → ``("qa", 2)``;  ``"qa"`` → ``("qa", None)``

    Used to separate instance identity (pane_key) from behaviour identity
    (base_role) so shard panes load the correct role file, provider, cwd
    defaults, and env config while staying independently keyed in the registry.
    """
    if "#" in key:
        role, _, idx = key.partition("#")
        return role, int(idx)
    return key, None


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class ShardGroup:
    """Aggregate state for a parallel QA fan-out (``assign --shards N``).

    Keyed ``"{project_ns}::{base_role}"`` in ``Orchestrator._shard_groups``.
    Created on the first shard assign; closed when all N shards report
    done/failed or the timeout fires.

    ``generation`` is a monotonically-increasing integer unique to each
    ShardGroup instance.  The 45-minute timeout timer captures this value
    at scheduling time and bails early if the group was replaced by a newer
    fan-out with the same key before the timer fires (stale-timer guard, #2).
    """

    base_role: str
    total: int
    done: dict = field(default_factory=dict)  # {shard_key: note}
    failed: set = field(default_factory=set)  # shard_keys that crashed
    closed: bool = False
    generation: int = field(default_factory=lambda: next(_shard_generation_counter))


@dataclass
class PipelineRun:
    """State for a running pipeline template execution.

    Keyed ``"{project_ns}::{run_id}"`` in ``Orchestrator._pipeline_runs``.
    Created by ``run_pipeline()``; closed when the last hop completes or all
    hops are skipped due to spawn failures.
    """

    run_id: str
    template_id: str
    template_name: str
    hops: list  # list[list[dict]] — validated copies of template hops
    current_hop: int = 0
    hop_pending: set = field(default_factory=set)  # roles in current hop not yet done
    hop_failed: set = field(default_factory=set)  # roles closed without done
    closed: bool = False


# ── Mixin ─────────────────────────────────────────────────────────────────────


class PipelineMixin:
    """Pipeline execution mixin for Orchestrator.

    All methods resolve through the combined MRO; ``self.spawn``,
    ``self._ps``, ``self._notify_lead``, ``self._resolve_project``,
    ``self._pipeline_runs``, and ``self._shard_groups`` come from the
    Orchestrator side of the MRO — state dicts remain in
    ``Orchestrator.__init__`` to avoid MRO/init-order issues.
    """

    # ──────────────────────────────────────────────────────────────
    # Pipeline executor
    # ──────────────────────────────────────────────────────────────

    def pipeline_precheck(self, template_id: str, project: str | None = None) -> tuple[bool, str]:
        """Validate that *template_id* exists and has runnable hops, with no
        side effects.

        Lets an async caller (cli_server schedules run_pipeline on a QTimer and
        replies immediately) verify the run BEFORE acking, instead of always
        replying ok=true and failing silently when the template is missing or
        empty. run_pipeline re-checks defensively, so this is purely the
        early-honest-reply seam.
        """
        from . import pipeline_config

        project_ns = self._resolve_project(project)
        templates = {
            t["id"]: t for t in pipeline_config.load(project=project_ns).get("templates", [])
        }
        tpl = templates.get(template_id)
        if tpl is None:
            return False, f"pipeline template not found: {template_id!r}"
        if not [hop for hop in tpl.get("hops", []) if hop]:
            return False, f"pipeline {template_id!r}: no runnable hops"
        return True, "ok"

    def run_pipeline(self, template_id: str, project: str | None = None) -> tuple[bool, str]:
        """Load *template_id* from pipeline_config and fire hop 0.

        Sequential between hops (hop N+1 starts only after all roles in hop N
        report done), parallel within each hop (all roles in a hop are spawned
        simultaneously). Hop advancement is driven by ``done()`` / ``close()``
        events, never by busy-wait.

        Returns ``(ok, message)``.  Errors on unknown template or empty hops.
        """
        from . import pipeline_config

        project_ns = self._resolve_project(project)

        data = pipeline_config.load(project=project_ns)
        templates = {t["id"]: t for t in data.get("templates", [])}
        tpl = templates.get(template_id)
        if tpl is None:
            return False, f"pipeline template not found: {template_id!r}"

        hops = [hop for hop in tpl.get("hops", []) if hop]
        if not hops:
            return False, f"pipeline {template_id!r}: no runnable hops"

        # HIGH#2 fix (docs/reviews/2026-07-11-full-system-review-codex.md):
        # reject a new run instead of silently overwriting an existing
        # pipeline's pane ownership. Without this check, _fire_pipeline_hop's
        # _spawn_one unconditionally clobbers PaneState.pipeline_run_id for
        # every role in hop 0 — if that role is still the pending owner of an
        # older, non-closed run, the older run is orphaned forever (no
        # timeout, no other owner left to advance it). "One active owner per
        # pane" is the simpler, safer model (codex's recommendation) — a role
        # with no existing owner (never assigned to a pipeline, or a manually
        # assigned live pane with no pipeline_run_id) is still freely adopted.
        conflicts = self._pipeline_hop_conflicts(project_ns, hops[0])
        if conflicts:
            conflict_desc = ", ".join(
                f"{role} (already owned by run {rid})" for role, rid in sorted(conflicts.items())
            )
            return (
                False,
                f"pipeline {template_id!r}: rejected — {conflict_desc}; "
                "finish or close the running pipeline first",
            )

        import uuid as _uuid

        run_id = str(_uuid.uuid4())[:8]
        run = PipelineRun(
            run_id=run_id,
            template_id=template_id,
            template_name=tpl.get("name", template_id),
            hops=hops,
        )
        pipeline_key = f"{project_ns}::{run_id}"
        self._pipeline_runs[pipeline_key] = run

        _log_event(
            "pipeline_run_start",
            project=project_ns,
            template=template_id,
            run_id=run_id,
            total_hops=len(hops),
        )
        self._fire_pipeline_hop(project_ns, run_id, run)
        return True, f"pipeline {tpl['name']!r} started (run {run_id}, {len(hops)} hops)"

    def _pipeline_hop_conflicts(self, project_ns: str, hop: list) -> dict:
        """Return ``{role: existing_run_id}`` for every role in *hop* that is
        currently owned by a different, non-closed pipeline run in this
        project.

        A role with no ``pipeline_run_id`` (never touched by a pipeline, or a
        live pane that was manually assigned) is not a conflict — the new run
        is free to adopt it. Read-only: uses ``_pane_state.get`` rather than
        ``_ps`` so a conflict check never creates PaneState entries as a side
        effect.
        """
        conflicts: dict = {}
        pane_state = getattr(self, "_pane_state", {})
        for entry in hop:
            role = entry.get("role")
            if not role:
                continue
            ps = pane_state.get(f"{project_ns}::{role}")
            existing_id = ps.pipeline_run_id if ps is not None else None
            if not existing_id:
                continue
            existing_run = self._pipeline_runs.get(f"{project_ns}::{existing_id}")
            if existing_run is not None and not existing_run.closed:
                conflicts[role] = existing_id
        return conflicts

    def _defer(self, delay_ms: int, fn) -> None:
        """Run *fn* on the Qt event loop after *delay_ms* (non-blocking seam).

        A thin wrapper over ``QTimer.singleShot`` so the pipeline-hop staggering
        is injectable: tests patch this to run inline, preserving the old
        synchronous spawn behaviour, while production spaces spawns across ticks."""
        QTimer.singleShot(delay_ms, fn)

    def _fire_pipeline_hop(self, project_ns: str, run_id: str, run: PipelineRun) -> None:
        """Spawn the current hop's roles (staggered) and notify Lead when done.

        Staggering (#44): a multi-role hop's spawns are deferred across ticks via
        ``_defer`` so back-to-back ConPTY COM calls don't collide
        (RPC_E_CANTCALLOUT). hop_pending is pre-populated with ALL roles up front
        so a fast done()/close() landing between staggered spawns can't empty it
        and advance the hop prematurely; a spawn that FAILS removes its role. The
        last scheduled spawn calls ``_finalize_pipeline_hop`` (abort if every
        spawn failed, else notify Lead). codex roles get the larger gap (#38)."""
        from .config import default_cwd_for_role
        from .provider_config import CODEX, effective_provider_for

        hop_idx = run.current_hop
        hop = run.hops[hop_idx]
        total = len(run.hops)
        entries = list(hop)
        n = len(entries)

        # Optimistic: every role is pending until its spawn proves otherwise.
        run.hop_pending = {e["role"] for e in entries}
        run.hop_failed = set()
        spawned_ok: set[str] = set()

        def _spawn_one(idx: int, entry: dict) -> None:
            if run.closed:
                return  # a done()/close() already resolved this hop
            role = entry["role"]
            cwd = (entry.get("cwd") or "").strip() or default_cwd_for_role(role, project_ns)
            ok, _msg = self.spawn(role, cwd=cwd, project=project_ns)
            if ok:
                self._ps(f"{project_ns}::{role}").pipeline_run_id = run_id
                spawned_ok.add(role)
            else:
                run.hop_pending.discard(role)
                run.hop_failed.add(role)
                _log_event(
                    "pipeline_spawn_fail",
                    project=project_ns,
                    run_id=run_id,
                    hop=hop_idx,
                    role=role,
                    msg=_msg,
                )
            if idx == n - 1:
                self._finalize_pipeline_hop(project_ns, run_id, run, hop_idx, total, spawned_ok)

        delay = 0
        for i, entry in enumerate(entries):
            self._defer(delay, lambda idx=i, e=entry: _spawn_one(idx, e))
            base = str(entry.get("role", "")).split("#", 1)[0]
            try:
                is_codex = effective_provider_for(base, project_ns) == CODEX
            except Exception:
                is_codex = base == "codex"
            delay += _CODEX_SPAWN_STAGGER_MS if is_codex else _SPAWN_STAGGER_MS

    @staticmethod
    def _pipeline_tag(run: PipelineRun) -> str:
        """Common ``[pipeline:<id>] <name> — `` prefix shared by every hop-status
        message sent to Lead (abort / hop-start / complete). tok-7."""
        return f"[pipeline:{run.run_id}] {run.template_name} — "

    def _finalize_pipeline_hop(
        self,
        project_ns: str,
        run_id: str,
        run: PipelineRun,
        hop_idx: int,
        total: int,
        spawned_ok: set,
    ) -> None:
        """After the hop's last staggered spawn: abort if every spawn failed,
        otherwise inject the hop-start message to Lead."""
        if run.closed:
            return
        if not spawned_ok:
            # Every spawn failed — abort and notify Lead. (Guard on spawned_ok,
            # NOT hop_pending: with staggering, hop_pending can also be empty
            # because survivors already reported done() during the stagger gap
            # and only the remainder failed — that's a COMPLETE hop, handled
            # below, not an abort.)
            run.closed = True
            self._pipeline_runs.pop(f"{project_ns}::{run_id}", None)
            failed_roles = ", ".join(sorted(run.hop_failed))
            err = self._pipeline_tag(run) + (
                f"hop {hop_idx + 1}/{total} aborted: all spawns failed ({failed_roles})"
            )
            self._inject_to_lead(project_ns, err, log_event="pipeline_hop_abort")
            return
        if not run.hop_pending:
            # Some roles spawned but every survivor already reported done() during
            # the stagger window and the rest failed to spawn → the hop is
            # complete; advance. done() could NOT have advanced earlier (the
            # not-yet-spawned/failed role kept hop_pending non-empty until its own
            # _spawn_one ran here), so finalize is the sole advancer — no double.
            self._advance_pipeline(project_ns, f"{project_ns}::{run_id}", run)
            return

        roles_str = ", ".join(sorted(spawned_ok))
        lines = [
            self._pipeline_tag(run) + f"hop {hop_idx + 1}/{total}",
            f"Panes spawned and ready: {roles_str}",
            "Assign each their task. Pipeline auto-advances when all done (no confirm needed).",
        ]
        if run.hop_failed:
            lines.append(f"⚠ spawn failed (skipped): {', '.join(sorted(run.hop_failed))}")
        if hop_idx + 1 < total:
            next_roles = [e["role"] for e in run.hops[hop_idx + 1]]
            lines.append(f"Next hop ({hop_idx + 2}/{total}): {', '.join(next_roles)}")
        self._inject_to_lead(project_ns, "\n".join(lines), log_event="pipeline_hop_start")
        _log_event(
            "pipeline_hop_fired",
            project=project_ns,
            run_id=run_id,
            hop=hop_idx,
            roles=sorted(spawned_ok),
        )

    def _advance_pipeline(self, project_ns: str, pipeline_key: str, run: PipelineRun) -> None:
        """Advance *run* to the next hop, or inject a completion notice if done."""
        run.current_hop += 1
        if run.current_hop >= len(run.hops):
            run.closed = True
            self._pipeline_runs.pop(pipeline_key, None)
            total = len(run.hops)
            if run.hop_failed:
                status = f"completed with failures ({', '.join(sorted(run.hop_failed))} closed without done)"
            else:
                status = "all hops complete ✓"
            msg = self._pipeline_tag(run) + f"{status} ({total}/{total} hops)"
            self._inject_to_lead(project_ns, msg, log_event="pipeline_run_complete")
            _log_event(
                "pipeline_complete",
                project=project_ns,
                template=run.template_id,
                run_id=run.run_id,
                total_hops=total,
            )
        else:
            self._fire_pipeline_hop(project_ns, run.run_id, run)

    def _maybe_fire_auto_chain_handoff(self, project_ns: str, was_auto_chain: bool) -> None:
        """Fire the verify-hop handoff iff *was_auto_chain* and no pane in the
        project still carries the auto_chain tag.

        Shared by done(), close(), and the crash-cap / stuck-give-up paths
        (bug-1 orch, review 2026-06-16). A pane that dies WITHOUT a done event
        used to only clear its own flag, so if it was the last blocker the chain
        deadlocked and the completed siblings' verify hop never fired. The Qt
        event loop is single-threaded, so once pending is empty no tagged pane
        remains to re-trigger → exactly-once firing across all four call sites.
        """
        if not was_auto_chain:
            return
        pending = [
            k
            for k, s in getattr(self, "_pane_state", {}).items()
            if k.startswith(f"{project_ns}::") and s.auto_chain
        ]
        if not pending:
            self._inject_auto_chain_handoff(project_ns)

    def _inject_auto_chain_handoff(self, project_ns: str) -> None:
        """Send a pre-authorisation prompt to Lead telling it to run the final
        verify SEQUENCE — devops local bring-up (port-safe), then QA last —
        WITHOUT proposing or waiting for user confirmation. Fires after all
        panes assigned with --auto-chain in the project have reported done.

        QA always runs LAST: all dev work must be complete, and (when the
        project ships a docker compose) the stack must be up on non-clashing
        ports first, so QA tests the real running system as the single final
        gate. If the Lead pane is absent, the prompt is queued via
        _pending_done_notices and delivered when Lead next spawns.
        """
        prompt = (
            "[auto-chain handoff] impl panes spawned with --auto-chain "
            "in this project have all reported done.\n"
            "You are pre-authorized to run the FINAL VERIFY SEQUENCE below "
            "WITHOUT proposing or waiting for user confirmation.\n"
            "\n"
            "Rule: QA runs LAST — only after ALL dev work is done AND (if the "
            "project has docker compose) the stack is up on non-clashing ports.\n"
            "\n"
            "Steps:\n"
            "1. Re-read the recent [<role> done] notes above; "
            "(optional) `git -C <project_path> diff --stat`.\n"
            "2. Bring-up gate — IF the project has a compose file "
            "(docker-compose.yml / compose.yaml / compose.yml):\n"
            "   fire devops FIRST to `docker compose up -d` locally on ports "
            "that do NOT clash with already-running containers (devops checks "
            "`docker ps`, picks free ports / offsets compose, healthchecks), "
            "then WAIT for the devops done event. Tell devops to report the "
            "live ports/URLs so QA knows where to test.\n"
            "   IF no compose file: skip this step.\n"
            "3. THEN fire QA LAST as the single final gate against the running "
            "stack: `takkub assign --role qa ...` (no --auto-chain — QA is "
            "terminal). Pass QA the live ports/URLs from devops.\n"
            "   If QA is BROWSER e2e/smoke (Playwright / mb) spanning MULTIPLE "
            "pages/flows, use `--plan --shards N` (N≈3–4): a planner pane splits "
            "the pages into balanced parallel buckets so the slow browser work "
            "finishes in parallel. For a single-flow smoke or non-browser test "
            "(unit suite / API integration), use plain `qa` — the ~1-min planner "
            "hop isn't worth it.\n"
            "4. After the qa done event: resume normal propose-then-confirm "
            "flow. (reviewer = at PR time per policy, not in this auto gate "
            "unless a trust-boundary / schema / migration change.)\n"
            "\n"
            "Do NOT add --auto-chain on the devops or QA fire (terminal hops)."
        )
        self._notify_lead(project_ns, prompt)
        _log_event("auto_chain_handoff", project=project_ns)

    def _inject_shard_fanout_handoff(
        self, project_ns: str, group: ShardGroup, timed_out: bool = False
    ) -> None:
        """Inject a consolidated fan-out result to Lead after all N shards finish."""
        done_count = len(group.done)
        fail_count = len(group.failed)
        total = group.total

        if timed_out:
            status = (
                f"[qa fan-out timeout: {done_count}/{total} shards done"
                + (f", {fail_count} failed" if fail_count else "")
                + "]"
            )
        else:
            status = (
                f"[qa fan-out complete: {done_count}/{total} shards done"
                + (f", {fail_count} failed" if fail_count else "")
                + "]"
            )

        lines = [status]
        for shard_key in sorted(group.done):
            _, idx = _split_shard(shard_key)
            lines.append(f"  shard {idx}: {group.done[shard_key] or 'done'}")
        for shard_key in sorted(group.failed):
            _, idx = _split_shard(shard_key)
            lines.append(f"  shard {idx}: CRASHED (respawn-capped)")
        if timed_out:
            reported = set(group.done) | group.failed
            for n in range(1, total + 1):
                shard_key = f"{group.base_role}#{n}"
                if shard_key not in reported:
                    lines.append(f"  shard {n}: NO RESPONSE (timeout)")

        message = "\n".join(lines)
        self._notify_lead(project_ns, message)
        _log_event(
            "shard_fanout_complete",
            project=project_ns,
            base_role=group.base_role,
            total=total,
            done=done_count,
            failed=fail_count,
            timed_out=timed_out,
        )

    def _check_shard_group_timeout(
        self, project_ns: str, group_key: str, generation: int | None = None
    ) -> None:
        """Fire a partial handoff if the shard group hasn't completed yet.

        ``generation`` is captured by the scheduling lambda (#2 fix): if the
        group was replaced by a newer fan-out with the same key before this
        timer fires, the generations won't match and we bail early so we don't
        clobber the live fan-out.
        """
        group = self._shard_groups.get(group_key)
        if group is None or group.closed:
            return
        if generation is not None and group.generation != generation:
            return  # stale timer from a previous fan-out with same key
        group.closed = True
        self._inject_shard_fanout_handoff(project_ns, group, timed_out=True)
        self._shard_groups.pop(group_key, None)
        _log_event(
            "shard_group_timeout",
            project=project_ns,
            base_role=group.base_role,
            done=len(group.done),
            failed=len(group.failed),
            total=group.total,
        )

    # ──────────────────────────────────────────────────────────────
    # QA plan-then-fan-out (assign --role qa --plan --shards N)
    # ──────────────────────────────────────────────────────────────

    def _qa_plan_file(self, project_ns: str, base_role: str):
        """Deterministic path for the planner's bucket plan, shared by the
        assign side (writes the instruction) and the done side (reads the
        result). One file per (project, role) — overwritten each plan run.
        Pure path computation, no mkdir: the planner creates the parent dir."""
        from .config import RUNTIME_DIR

        safe = base_role.replace("#", "_")
        return RUNTIME_DIR / "qa-plans" / f"{project_ns}-{safe}-plan.json"

    @staticmethod
    def _wrap_planner_task(base_task: str, plan_file, shards: int) -> str:
        """Prefix the QA task with planner-mode instructions: analyse the app,
        split test work into *shards* balanced + independent buckets, write the
        plan JSON to *plan_file*, then ``takkub done``. The orchestrator fans
        out the real shards from that file once the planner reports done."""
        return (
            "━━ QA PLANNER MODE — วางแผนแบ่งงานก่อน fan-out (ยังไม่เทส) ━━\n"
            f"คุณคือ qa-planner — รอบนี้ไม่ใช่การเทส แต่คือวางแผนแบ่งงานเทสให้ {shards} "
            "QA shards ทำพร้อมกันได้เร็วสุดโดยไม่ทับซ้อนกัน\n\n"
            "ขั้นตอน:\n"
            "1. วิเคราะห์แอป/codebase (routes, pages, flows, API, components) หาขอบเขตที่ต้องเทส\n"
            f"2. แบ่งงานเทสเป็น {shards} buckets ที่: (ก) balanced พอๆ กัน "
            "(ข) independent — flow ที่ depend กันอยู่ bucket เดียวกัน "
            "(ค) ครอบคลุมทั้งหมดไม่ตกหล่น\n"
            "3. เขียน plan เป็น JSON ลงไฟล์ path นี้เป๊ะๆ (สร้าง dir parent ก่อนถ้ายังไม่มี):\n"
            f"   {plan_file}\n"
            "   schema:\n"
            '   {"shards": [{"n": 1, "scope": "<หน้า/flow ที่ shard นี้เทส>", '
            '"focus": "<จุดเน้น/edge cases>"}, ...]}\n'
            f"   ต้องมี {shards} entries (n=1..{shards}); scope เขียนเป็น route/flow รูปธรรม\n"
            '4. `takkub done "แบ่ง N buckets: <สรุปสั้น>"`\n\n'
            "ห้ามเริ่มเทสเอง — แค่วางแผน เขียนไฟล์ แล้ว done; "
            "orchestrator จะ spawn shards ตาม plan ให้อัตโนมัติ\n\n"
            "━━ โจทย์เทสต้นฉบับ (บริบทสำหรับวางแผน) ━━\n"
            f"{base_task}"
        )

    def _fire_qa_plan_fanout(
        self, project_ns: str, base_role: str, cfg: dict, planner_note: str = ""
    ) -> None:
        """Read the planner's bucket plan and fan out the QA shards.

        Each shard gets the base task + its assigned bucket (scope/focus),
        staggered like the pipeline-hop spawns. When the plan file is missing
        or unparseable, degrade to a plain N-shard fan-out (shards self-split
        via TAKKUB_SHARD) and warn Lead — the parallel run still happens
        instead of stalling on a bad plan."""
        import json
        import pathlib

        requested = int(cfg.get("shards", 0) or 0)
        base_task = str(cfg.get("task", ""))
        cwd = cfg.get("cwd")
        plan_path = pathlib.Path(str(cfg.get("plan_file", "")))

        buckets: list[tuple[str, str]] = []
        parse_err = ""
        try:
            data = json.loads(plan_path.read_text(encoding="utf-8"))
            raw_shards = data.get("shards") if isinstance(data, dict) else None
            if isinstance(raw_shards, list):
                for item in raw_shards:
                    if isinstance(item, dict):
                        scope = str(item.get("scope", "")).strip()
                        focus = str(item.get("focus", "")).strip()
                        if scope:
                            buckets.append((scope, focus))
        except FileNotFoundError:
            parse_err = "plan file not found"
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            parse_err = f"plan parse error: {exc}"

        degraded = not buckets
        if degraded:
            k = max(1, requested)
            buckets = [("", "")] * k
        else:
            # Never exceed what the operator asked for (--shards N).
            if requested:
                buckets = buckets[:requested]
            k = len(buckets)

        delay = 0
        fired: list[str] = []
        for n, (scope, focus) in enumerate(buckets, start=1):
            shard_role = f"{base_role}#{n}"
            if scope:
                shard_task = (
                    f"{base_task}\n\n"
                    f"━━ SHARD {n}/{k} SCOPE (จาก qa-planner — เทสเฉพาะส่วนนี้) ━━\n"
                    f"ขอบเขต: {scope}\n"
                    + (f"โฟกัส: {focus}\n" if focus else "")
                    + "อย่าเทสนอกขอบเขตนี้ — shard อื่นรับผิดชอบส่วนที่เหลือ"
                )
            else:
                shard_task = base_task  # degraded: self-split via TAKKUB_SHARD
            self._defer(
                delay,
                lambda r=shard_role, t=shard_task, kk=k: self.assign(
                    r, cwd=cwd, task=t, shard_total=kk, project=project_ns
                ),
            )
            fired.append(shard_role)
            delay += _SPAWN_STAGGER_MS

        if degraded:
            self._notify_lead(
                project_ns,
                f"⚠️ [qa plan fallback] อ่าน plan ไม่ได้ ({parse_err or 'no buckets'}) — "
                f"degrade เป็น {k}-shard self-split (modulo). fan-out: {', '.join(fired)}",
            )
        else:
            lines = [
                f"[qa plan ready] qa-planner แบ่งเป็น {k} buckets → "
                f"fan-out {', '.join(fired)} (รันพร้อมกัน)"
            ]
            for n, (scope, _focus) in enumerate(buckets, start=1):
                lines.append(f"  {base_role}#{n}: {scope[:90]}")
            lines.append("รอ consolidated handoff เมื่อทุก shard report done")
            self._notify_lead(project_ns, "\n".join(lines))
        _log_event(
            "qa_plan_fanout",
            project=project_ns,
            base_role=base_role,
            shards=k,
            degraded=degraded,
            parse_err=parse_err,
        )
