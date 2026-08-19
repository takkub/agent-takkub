"""PermissionEngine (Phase 5c, epic #309 Capability Hub) — the 2-layer
tool-permission gate behind one interface.

Matrix §3 "Tool permission": REUSE + EXTEND — "สองชั้น (MCP gate + shell
guard) เป็นบทเรียนที่พิสูจน์แล้วว่าจำเป็น · Permission Engine ของ V2 ต้อง
รักษาสองชั้นนี้ไว้ ไม่ใช่ยุบเหลือชั้นเดียว". Both layers exist because
**one layer alone is provably not enough** — `pane_guard.py`'s own module
docstring documents the incident (#242/#287): the MCP gate denies the
*sanctioned* tool call, but every cockpit pane is spawned with
`--dangerously-skip-permissions`, so an agent can route around a
denied MCP by shelling out instead (`npx --yes playwright`). This class
therefore never collapses the two checks into one decision — it exposes
them as two separate methods, so a caller that only calls one of them is
visibly only enforcing half the policy, not accidentally getting full
coverage from a merged call it didn't ask for.

Layer 1 — MCP gate (`mcp_allowed`): `pane_tools_policy.effective_mcps`,
  enforced by `spawn_engine` passing `--strict-mcp-config` /
  `-c mcp_servers={}` *before* the pane process starts. Airtight for MCP
  tool calls, by construction — nothing after spawn can add an MCP server
  back in.
Layer 2 — shell gate (`evaluate_shell_command`): `pane_guard.classify`,
  the `PreToolUse`/`Bash` hook (`takkub _guard`, `cli.cmd_guard`) fires on
  every Bash call *after* the pane is already running — this is what
  closes the Bash-bypass hole layer 1 cannot see.

`evaluate_shell_command` audits every DENIED verdict via
`core.capabilities.audit.log_capability_event` (who/agent/provider/
account/tool — matrix §5 "Observability": "เติม audit fields"); it
deliberately does not audit every ALLOWED call too — `pane_guard.classify`
runs on every single Bash invocation in a guarded pane, and logging each
one would turn EVENTS_LOG into a per-command firehose for no benefit an
audit trail actually needs (the interesting signal is what got blocked).

Not yet wired into `cli.cmd_guard` itself (the live `takkub _guard` hook
still calls `pane_guard.classify` directly) — rewiring that hot path
belongs to whichever phase actually replaces the caller, so it can be
proven behavior-neutral under the full suite first; this class is the
target interface for that, not a live behavior change today.
"""

from __future__ import annotations

from agent_takkub import pane_guard, pane_tools_policy

from .audit import log_capability_event


class PermissionEngine:
    """Façade over the two independent tool-permission layers. Delegates
    every decision unchanged to `pane_tools_policy` / `pane_guard` — see
    module docstring for why neither layer is dropped or merged."""

    def mcp_allowed(self, role: str) -> frozenset[str] | None:
        """Layer 1. `None` means "no cockpit MCP policy for this role at
        all" (legacy passthrough); an explicit (possibly empty) frozenset
        means the cockpit has an opinion — see
        `pane_tools_policy.effective_mcps` for the full contract."""
        return pane_tools_policy.effective_mcps(role)

    def evaluate_shell_command(
        self,
        command: str,
        role: str | None,
        *,
        agent: str | None = None,
        provider: str | None = None,
        account: str | None = None,
    ) -> pane_guard.Verdict:
        """Layer 2. Returns `pane_guard.classify`'s `Verdict` unchanged;
        additionally audits a DENIED verdict (see module docstring for why
        only denials are logged)."""
        verdict = pane_guard.classify(command, role)
        if not verdict.allowed:
            log_capability_event(
                "capability.shell_command_denied",
                who=role,
                agent=agent,
                provider=provider,
                account=account,
                tool="bash",
                rule=verdict.rule,
                reason=verdict.reason,
            )
        return verdict
