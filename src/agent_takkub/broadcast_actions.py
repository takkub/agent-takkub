"""BroadcastMixin — broadcast-style orchestrator actions (refactor round 2, step B).

Contains the four methods extracted from ``Orchestrator`` that fan a prompt or
task out to every live pane in a project.  Kept as a mixin rather than a
standalone module so they continue to access ``self._resolve_project``,
``self._project_panes``, ``self._send_when_ready``, and ``self.assign`` from
the full Orchestrator instance via MRO.

**Import constraint:** this module MUST NOT import ``main_window``, ``app``,
or ``cli`` — it is an engine-layer module mixed into Orchestrator.
"""

from __future__ import annotations

from .config import default_cwd_for_role
from .orchestrator_text import _log_event
from .roles import LEAD


class BroadcastMixin:
    """Mixin for broadcast-style actions that fan a task out to every pane."""

    def broadcast_bug_check(self, project: str | None = None) -> tuple[int, list[str]]:
        """Ask every active pane in `project` to introspect for cockpit bugs.

        Each live pane gets a prompt instructing the agent to either:
          * `takkub issue new ...` if it noticed a cockpit/orchestrator/CLI/UI bug
          * `takkub send --to lead 'no bugs to report'` if the session was clean

        Empty / dead-session slots are skipped silently. Cross-project panes
        are not touched (multi-tab isolation).

        Returns (count, role_names) for the cockpit's status-bar feedback.
        """
        project_ns = self._resolve_project(project)
        prompted: list[str] = []
        for role_name, pane in list(self._project_panes(project_ns).items()):
            if pane.session is None or not pane.session.is_alive:
                continue
            # Teammates introspect ("did you notice a bug?"); the Lead gets an
            # active-audit directive so the broadcast doesn't dead-end into
            # everyone waiting for reports. Introspection only catches bugs an
            # agent stumbled into — the Lead must actively run tests / diff /
            # audit to surface latent ones.
            if role_name == LEAD.name:
                prompt = self._build_lead_bug_check_prompt(project_ns)
            else:
                prompt = self._build_bug_check_prompt(role_name, project_ns)
            self._send_when_ready(role_name, prompt, project=project_ns)
            prompted.append(role_name)
        _log_event("broadcast_bug_check", project=project_ns, count=len(prompted), roles=prompted)
        return len(prompted), prompted

    @staticmethod
    def _build_bug_check_prompt(role: str, project: str) -> str:
        """Render the per-pane bug-introspection prompt.

        Static-method so the test suite can call it without a full
        Orchestrator + Qt event loop just to inspect the wording.
        """
        return (
            "🐛 **Bug check** (orchestrator broadcast)\n\n"
            "introspect session ของเรา — เจอบัค **ของ cockpit / orchestrator / CLI / UI** ไหม\n"
            "(ไม่ใช่บัคของ code ที่เรากำลังทำงาน — **เฉพาะบัคของ cockpit เอง**)\n\n"
            "**ถ้าเจอ:** เรียก (issue ลง agent-takkub repo อัตโนมัติ)\n"
            "```\n"
            f'takkub issue new "<title>" --severity <low|med|high> --noticed-in {project} --role {role} --tag <a,b,c> --body "<reproduce + impact>"\n'
            "```\n\n"
            "**ถ้าไม่เจอ:** เรียก\n"
            "```\n"
            'takkub send --to lead "no bugs to report"\n'
            "```\n\n"
            "รายงานกลับเมื่อเสร็จ"
        )

    @staticmethod
    def _build_lead_bug_check_prompt(project: str) -> str:
        """Render the Lead-side ACTIVE bug-audit prompt.

        The teammate prompt is passive (introspect + report). If the Lead got
        the same prompt the whole broadcast would dead-end into "everyone waits
        for reports, nobody checks anything" — the exact stall the user hit.
        This prompt makes the Lead *do* an audit: run the suite, diff recent
        work, eyeball risk subsystems, and only then conclude. Lead's own
        Read/test/diff are auto-fire; spawning an auditor stays propose-first.
        """
        return (
            "🐛 **Bug check — Lead active audit** (orchestrator broadcast)\n\n"
            "คุณคือ Lead — **อย่าแค่รอ report จาก teammate** (introspection จับได้แค่บัค "
            "ที่ agent บังเอิญสะดุดเจอ ไม่ใช่บัคแฝง) ลงมือ audit เชิงรุก **อย่างน้อย 1 อย่างทันที** "
            "ก่อนสรุป:\n\n"
            "1. รัน test suite — `rtk proxy python -m pytest -q` (มี fail/regression ไหม)\n"
            "2. ดู change ล่าสุด — `rtk git log --oneline -10` + `git diff` หา bug แฝง\n"
            "3. ไล่ subsystem เสี่ยง/เพิ่งแตะ — encode path, routing, watchdog, env leak, paste\n"
            "4. ถ้าต้องเจาะลึก → **propose** spawn reviewer/codex audit (pane visible รอ confirm)\n\n"
            "**เจอบัค:** (issue ลง agent-takkub repo อัตโนมัติ — เฉพาะบัค cockpit)\n"
            "```\n"
            f'takkub issue new "<title>" --severity <low|med|high> --noticed-in {project} --body "<reproduce + impact>"\n'
            "```\n"
            "**ไม่เจอหลัง audit จริง:** สรุปสั้นๆ ว่า audit อะไรไปบ้าง + ผล\n\n"
            '❗ ห้ามจบด้วยการ "รอ teammate" เฉยๆ — ต้องมี action เกิดขึ้นก่อนสรุปเสมอ'
        )

    def broadcast_design_review(self, project: str | None = None) -> tuple[int, list[str]]:
        """Spawn the design-review pipeline for `project` — critic + gemini parallel.

        Unlike `broadcast_bug_check` (prompts existing live panes), this
        method assigns fresh tasks to the design-review duo:
          * critic — read shots from runtime/exports/<date>/<project>/screenshots/
            and write a proposal to docs/design-review/<date>-<view>.md
          * gemini — prepare to view images critic will send via `takkub send`

        Substitution doctrine (CLAUDE.md "Claude รับตำแหน่งแทน"): when the
        gemini CLI is unavailable — toggled off in the status bar OR not
        installed — the gemini slot is **still spawned**. The spawn layer
        (`effective_provider_for`) backs it with claude so the slot keeps its
        identity (`gemini` pane) but runs claude, reading `.claude/agents/
        gemini.md` (which knows it's a substitute and reports
        `[claude-substitute for gemini]`). We never silently drop the slot —
        that left critic's task pointing at a pane that was never spawned and
        gave the user no second opinion at all (issue #61). The only cost is
        lost model-diversity, which we flag in the returned label + log so the
        user can decide whether to re-enable real gemini.

        Returns (count, role_names) for status-bar feedback. Roles ordered
        consistently (critic first) so the UI message reads naturally; the
        gemini entry reads `gemini (claude)` when it was substituted.
        """
        from datetime import datetime as _dt

        from .provider_config import GEMINI, effective_provider_for

        project_ns = self._resolve_project(project)
        today = _dt.now().strftime("%Y-%m-%d")
        shot_dir = f"runtime/exports/{today}/{project_ns}/screenshots/"
        proposal_path = f"docs/design-review/{today}-<view>.md"
        cwd = default_cwd_for_role("critic", project=project_ns)

        critic_task = (
            "[ROLE: Design Critic — ทำงานเองโดยตรง ห้าม spawn subagent]\n\n"
            "🎨 **Design review** (orchestrator broadcast)\n\n"
            f"อ่าน screenshots ที่ `{shot_dir}` (ถ้าโฟลเดอร์ยังว่าง บอก Lead ผ่าน "
            "`takkub send --to lead` ขอให้ QA capture ก่อน) — เสนอ:\n"
            "  • **เพิ่ม** — element/affordance ที่ขาด\n"
            "  • **ลบ** — clutter หรือ widget ซ้ำซ้อน\n"
            "  • **ปรับ** — spacing / typography / color / interaction\n\n"
            "สื่อสารกับ gemini pane ผ่าน `takkub send --to gemini` เพื่อขอมุมที่ 2 "
            "จากภาพเดียวกัน (cross-check confirmation bias)\n\n"
            f"เขียน proposal markdown ไปที่ `{proposal_path}` พร้อม frontmatter "
            "(date / scope / shots) แล้ว report กลับผ่าน `takkub done`"
        )

        gemini_task = (
            "[ROLE: gemini — second opinion on visual design]\n\n"
            "🖼️ **Image review co-pilot**\n\n"
            "Design Critic pane จะส่ง path ของ screenshot images ให้ผ่าน "
            "`takkub send` — โหลดภาพอ่านดู แล้วตอบกลับ 1-3 จุดที่:\n"
            "  • รู้สึกขาด / clutter / ไม่ balance\n"
            "  • เป็นไปได้ที่ user จะใช้ผิด\n"
            "  • Heuristic ผิด (Nielsen / contrast / hierarchy)\n\n"
            "ตอบสั้น focus — critic จะรวบรวมเขียน proposal เอง "
            "report กลับผ่าน `takkub done` เมื่อ critic บอกว่าจบรอบ"
        )

        # Will the gemini slot be backed by claude this spawn? (toggled off or
        # CLI not installed). We still spawn it — only the label/log change.
        gemini_substituted = effective_provider_for("gemini", project=project_ns) != GEMINI
        if gemini_substituted:
            gemini_task = (
                "[ROLE: gemini slot — claude รับตำแหน่งแทน (gemini ปิด/ยังไม่ติดตั้ง)]\n"
                "⚠️ คุณคือ Claude ที่รับบท gemini — second opinion ยังให้ได้ แต่ไม่ใช่ "
                "model diversity จริง ขึ้น report ว่า `[claude-substitute for gemini]`\n\n"
            ) + gemini_task

        spawned: list[str] = []
        ok_critic, _ = self.assign("critic", cwd=cwd, task=critic_task, project=project_ns)
        if ok_critic:
            spawned.append("critic")
        ok_gemini, _ = self.assign("gemini", cwd=cwd, task=gemini_task, project=project_ns)
        if ok_gemini:
            spawned.append("gemini (claude)" if gemini_substituted else "gemini")
        _log_event(
            "broadcast_design_review",
            project=project_ns,
            count=len(spawned),
            roles=spawned,
            shot_dir=shot_dir,
            gemini_substituted=gemini_substituted,
        )
        return len(spawned), spawned
