"""Declarative per-provider CLI specs — Wave 3 #6 Phase 0 (issue #103).

Single source of truth for how the cockpit spawns/monitors each terminal CLI
provider (claude / codex / gemini). Pure data layer: no PyQt, no engine
imports — `provider_config.py` / `spawn_engine.py` / `pty_session.py` /
`orchestrator_text.py` read from `PROVIDER_REGISTRY`; never the reverse (see
the `provider-spec-pure` import-linter contract in pyproject.toml).

Phase 0 is a **behavior-neutral refactor**: every value below is copied
verbatim from the call site it replaces (cited in a comment on the field),
not retuned. See docs/plans/2026-07-09-providerspec-design.md (blueprint)
and docs/plans/2026-07-09-providerspec-review.md (constraints this file
honors — most notably: keep enter-delay/self-heal uniform across providers,
keep `_READY_RULES` an ORDERED concat not a set() union, and trust the
current code + test vectors over the design doc where the two disagree,
e.g. codex's post-#99 ready rules).

Only the fields actually wired into a call site in Phase 0 affect runtime
behavior (see the "wired in Phase 0" note on each spec below). The rest
document current reality for the claude branch — which design + review both
concluded should stay hardcoded in spawn_engine.py's Lead/teammate axis for
now (docs/plans/2026-07-09-providerspec-review.md Dimension 1b) — so a later
phase has a faithful starting point instead of having to rediscover it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReadyRule:
    """One ordered (marker, verdict) entry — first substring match wins.

    Mirrors the ``(ready_when, marker)`` tuples pty_session.py hand-wrote in
    its old ``_READY_RULES`` before this refactor."""

    marker: str
    ready_when: bool


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the cockpit knows about one CLI provider.

    Field groups mirror docs/plans/2026-07-09-providerspec-design.md §2.1.
    """

    name: str

    # ─── 1. Binary discovery ───
    binary_names: list[str] = field(default_factory=list)
    install_instructions: str = ""
    custom_discovery_fn: Callable[[], str | None] | None = None

    # ─── 2. Spawn argv builder ───
    autonomy_flags: dict[str, list[str]] = field(default_factory=dict)
    extra_static_args: list[str] = field(default_factory=list)

    # ─── 3. CLI argument mapping flags ───
    mcp_config_flag: str | None = None
    strict_mcp_flag: str | None = None
    system_prompt_flag: str | None = None
    session_id_flag: str | None = None
    session_resume_flag: str | None = None

    # ─── 4. Ready / busy / blocker markers ───
    ready_hard_blockers: tuple[str, ...] = field(default_factory=tuple)
    ready_rules: tuple[ReadyRule, ...] = field(default_factory=tuple)
    ready_wait_ms: int = 45_000

    # ─── 5. Context injection strategy ───
    context_strategy: str = "none"
    cheatsheet_filename: str | None = None
    inline_learned_notes: bool = False
    use_file_guards: bool = False

    # ─── 6. MCP adapter variants ───
    mcp_adapter_variant: str = "none"
    supports_browser_profiles: bool = False

    # ─── 7. Input quirks ───
    paste_threshold: int = 200
    enter_delay_base_ms: int = 800
    enter_delay_per_kb_ms: int = 150
    enter_delay_max_ms: int = 3000
    input_swallow_recovery: bool = True

    # ─── 8. Spawning capability flags ───
    supports_mirror: bool = False
    supports_resume: bool = False
    supports_slash_commands: bool = False
    supports_hooks: bool = False

    # ─── 9. Claude/provider branch specific knobs ───
    plugin_dirs: tuple[str, ...] = field(default_factory=tuple)
    disallowed_tools: tuple[str, ...] = field(default_factory=tuple)
    model_flag: str | None = None
    effort_flag: str | None = None
    fallback_model_flag: str | None = None
    settings_flag: str | None = None
    task_notice_preamble: str | None = None

    # ─── 10. Read-side coupling capability flags ───
    produces_jsonl_transcript: bool = False
    supports_token_meter: bool = False
    supports_remote_history: bool = False


# ── binary discovery wrappers ────────────────────────────────────────────────
# Each does its `from .<helper> import find_*` INSIDE the call (not at module
# scope) so a test monkeypatching e.g. `agent_takkub.codex_helper.find_codex_executable`
# still takes effect — the import re-reads the module's current attribute at
# call time instead of binding a stale reference at spec-construction time.
# Mirrors the lazy-import pattern provider_config._provider_available already
# used pre-refactor.
def _discover_claude() -> str | None:
    from .config import find_claude_executable

    return find_claude_executable()


def _discover_codex() -> str | None:
    from .codex_helper import find_codex_executable

    return find_codex_executable()


def _discover_gemini() -> str | None:
    from .gemini_helper import find_agy_executable

    return find_agy_executable()


# ── claude ──────────────────────────────────────────────────────────────────
# NOT wired into spawn_engine.py's claude argv builder in Phase 0 (see module
# docstring) — fields below are faithful documentation of spawn_engine.py's
# claude branch (lines ~1439-1651) and pty_session.py's claude ready markers,
# for a future phase to consume without re-deriving them from scratch.
claude_spec = ProviderSpec(
    name="claude",
    binary_names=["claude", "claude.exe"],  # config.py:542 find_claude_executable
    install_instructions=(
        "Install Anthropic Claude Code via npm: `npm install -g @anthropic-ai/claude-code`"
    ),
    custom_discovery_fn=_discover_claude,
    autonomy_flags={"default": ["--dangerously-skip-permissions"]},  # spawn_engine.py:1441
    extra_static_args=["--setting-sources", "project,local"],  # spawn_engine.py:1442-1443
    mcp_config_flag="--mcp-config",  # spawn_engine.py:1566
    strict_mcp_flag="--strict-mcp-config",  # spawn_engine.py:1570
    system_prompt_flag="--append-system-prompt-file",  # spawn_engine.py:1538
    session_id_flag="--session-id",  # spawn_engine.py:1651
    session_resume_flag="--resume",  # spawn_engine.py:1630/1647
    ready_hard_blockers=(
        "trust this folder",
        "do you trust the contents of this directory",
        "press enter to continue",
        "esc to interrupt",
        "esc to cancel",
    ),  # pty_session.py:204-210 (full original 5 — claude is the provider observed
    # showing the folder-trust modal, so it keeps all of them)
    ready_rules=(
        ReadyRule("bypass permissions", True),  # pty_session.py:249
        ReadyRule("shift+tab to cycle", True),  # pty_session.py:250
    ),  # WIRED in Phase 0 — see provider_spec.READY_RULES compat concat below
    ready_wait_ms=45_000,  # lead_inbox.py:426 default (claude keeps it; not degraded)
    context_strategy="append_system_prompt_file",
    cheatsheet_filename="CLAUDE.md",
    inline_learned_notes=True,
    use_file_guards=True,
    mcp_adapter_variant="strict",
    supports_browser_profiles=True,
    paste_threshold=200,  # orchestrator_text.py:112 BRACKETED_PASTE_THRESHOLD
    enter_delay_base_ms=800,  # orchestrator_text.py:152 _PASTE_ENTER_DELAY_MS
    enter_delay_per_kb_ms=150,  # orchestrator_text.py:158 _PASTE_PER_KB_DELAY_MS
    enter_delay_max_ms=3000,  # orchestrator_text.py:159 _PASTE_MAX_ENTER_DELAY_MS
    input_swallow_recovery=True,
    supports_mirror=True,
    supports_resume=True,
    supports_slash_commands=True,
    supports_hooks=True,
    plugin_dirs=("TAKKUB_EXTRA_PLUGINS",),  # spawn_engine.py:1529-1536 (env var name)
    disallowed_tools=("Task",),  # spawn_engine.py:351 _teammate_disallowed_tools() default
    # (AskUserQuestion is a SECOND, separate --disallowed-tools flag at
    # spawn_engine.py:1594-1608 — not collapsed into this one field in Phase 0)
    model_flag="--model",  # spawn_engine.py:1483
    effort_flag="--effort",  # spawn_engine.py:1486
    fallback_model_flag="--fallback-model",  # spawn_engine.py:1500
    settings_flag="--settings",  # spawn_engine.py:1456
    task_notice_preamble=None,
    produces_jsonl_transcript=True,
    supports_token_meter=True,
    supports_remote_history=True,
)


# ── codex ────────────────────────────────────────────────────────────────────
# WIRED in Phase 0: autonomy_flags + install_instructions feed spawn_engine.py's
# codex branch; ready_hard_blockers/ready_rules feed pty_session.py's compat
# concat; task_notice_preamble feeds orchestrator_text.py's codex task rewrite.
codex_spec = ProviderSpec(
    name="codex",
    binary_names=["codex", "codex.cmd", "codex.bat"],
    install_instructions=(
        "codex binary not on PATH. Install with "
        "`npm install -g @openai/codex`, then run `codex login` once."
    ),  # spawn_engine.py:1097-1100 (byte-identical to codex_helper.py:69-72)
    custom_discovery_fn=_discover_codex,
    autonomy_flags={
        "win32": ["--dangerously-bypass-approvals-and-sandbox"],  # spawn_engine.py:1140-1143
        "default": [
            "--ask-for-approval",
            "never",
            "-s",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=true",
        ],  # spawn_engine.py:1144-1153
    },
    ready_hard_blockers=("esc to interrupt", "esc to cancel"),  # pty_session.py:208-209
    ready_rules=(
        # Current-code truth (post-#99 fix): the banner-alone rule
        # ("openai codex (v", True) that an earlier design draft listed was
        # deliberately REMOVED by issue #99 (see pty_session.py's #99 comment
        # by the old _READY_RULES) and must NOT come back — the banner paints
        # before codex finishes auto-booting its MCP servers, so treating it
        # alone as ready raced task delivery into a still-busy composer.
        ReadyRule("update available!", False),  # pty_session.py:225
        ReadyRule("fast off", True),  # pty_session.py:247
        ReadyRule("fast on", True),  # pty_session.py:248
    ),
    ready_wait_ms=90_000,  # lead_inbox.py:434-435 (cold-boot + MCP-boot allowance)
    context_strategy="agents_md_file",
    cheatsheet_filename="AGENTS.md",
    inline_learned_notes=False,
    use_file_guards=False,
    mcp_adapter_variant="session_override",  # issue #100 — native `-c mcp_servers.<name>.<key>=…`
    # dotted overrides, wired per-pane in spawn_engine.py via mcp_bridge.py.
    # Additive/session-scoped only — never touches ~/.codex/config.toml
    # (confirmed empirically against codex-cli 0.144.1, 2026-07-11).
    supports_browser_profiles=False,
    paste_threshold=200,  # orchestrator_text.py:112 (shared/uniform — Phase 0 behavior-neutral)
    enter_delay_base_ms=800,  # orchestrator_text.py:152 (kept uniform — review action item 2-C:
    enter_delay_per_kb_ms=150,  # do NOT tune codex lower than claude in Phase 0, it would
    enter_delay_max_ms=3000,  # regress the #99 enter-swallow fix. Tuning is a separate change.)
    input_swallow_recovery=True,  # review action item 2-D: keep True (self-heal stays active)
    supports_mirror=False,
    supports_resume=False,
    supports_slash_commands=False,
    supports_hooks=False,
    task_notice_preamble=(
        "[orchestrator note] อ่านก่อนเริ่มงาน:\n"
        "- `ห้าม spawn subagent` ใน ROLE prefix หมายถึง AI subagent\n"
        "  เท่านั้น (Task tool / codex delegation flags) — ไม่รวม shell\n"
        "  command ที่คุณรันเองในเทอร์มินัลนี้\n"
        "- เมื่อเสร็จงาน ต้อง **รัน shell command** ผ่าน Bash tool:\n"
        '      takkub done "<one-line summary>"\n'
        '  ห้ามพิมพ์ "takkub done" เป็นข้อความตอบในแชท (orchestrator\n'
        "  มองไม่เห็น → Lead ไม่ทราบว่างานเสร็จ → pane idle ตลอด)\n"
        "- review / analysis tasks: save findings ลงไฟล์ docs/ ก่อน\n"
        "  แล้วค่อย `takkub done` (pane auto-close ~2.5s หลัง done)\n"
        "\n"
        "------ task ------\n"
    ),  # WIRED in Phase 0 — byte-identical to the pre-refactor
    # orchestrator_text.py _CODEX_TASK_NOTICE constant, which now sources
    # this field instead of owning the text (single source of truth).
    produces_jsonl_transcript=False,
    supports_token_meter=False,
    supports_remote_history=False,
)


# ── gemini (agy / Antigravity) ────────────────────────────────────────────────
# WIRED in Phase 0: autonomy_flags + install_instructions feed spawn_engine.py's
# gemini branch; ready_hard_blockers/ready_rules feed pty_session.py's compat
# concat.
gemini_spec = ProviderSpec(
    name="gemini",
    binary_names=["agy", "agy.exe"],
    install_instructions=(
        "agy binary not on PATH. Install the Antigravity CLI from "
        "https://antigravity.google/download, then run `agy` once to sign in."
    ),  # spawn_engine.py:1050-1053
    custom_discovery_fn=_discover_gemini,
    autonomy_flags={"default": ["--dangerously-skip-permissions"]},  # spawn_engine.py:1073
    ready_hard_blockers=(
        "esc to interrupt",
        "esc to cancel",
        "press enter to continue",
    ),  # pty_session.py:205-209 (agy's own trust/press-enter observed on first boot)
    ready_rules=(
        ReadyRule("? for shortcuts", True),  # pty_session.py:222 (agy idle footer)
        ReadyRule("type your message or", True),  # pty_session.py:223 (legacy gemini CLI)
        ReadyRule("gemini cli update available!", True),  # pty_session.py:224 (#51)
    ),
    ready_wait_ms=90_000,  # lead_inbox.py:431-435 (agy cold-boot allowance)
    context_strategy="agents_md_file",
    cheatsheet_filename="AGENTS.md",
    inline_learned_notes=False,
    use_file_guards=False,
    mcp_adapter_variant="plugin_import",  # issue #100 — `agy plugin import <path>` DOES bridge
    # MCP servers from a claude-style plugin's `.mcp.json` (confirmed empirically against
    # agy 1.1.1), but stages them into a GLOBAL `~/.gemini/config/plugins/<name>/` registry
    # with no per-session/per-cwd scope — mcp_bridge.py leaves this variant a documented
    # no-op rather than auto-driving a machine-wide side effect on every spawn.
    supports_browser_profiles=False,
    paste_threshold=200,
    enter_delay_base_ms=800,  # kept uniform — see codex_spec note above (review 2-C/2-D)
    enter_delay_per_kb_ms=150,
    enter_delay_max_ms=3000,
    input_swallow_recovery=True,
    supports_mirror=False,
    supports_resume=False,
    supports_slash_commands=False,
    supports_hooks=False,
    produces_jsonl_transcript=False,
    supports_token_meter=False,
    supports_remote_history=False,
)


PROVIDER_REGISTRY: dict[str, ProviderSpec] = {
    "claude": claude_spec,
    "codex": codex_spec,
    "gemini": gemini_spec,
}


# ── legacy compat layer: ordered concat, NOT set() (design §5.5 + review 2-A) ──
# CONCATENATION ORDER IS CRITICAL: gemini rules MUST precede codex rules so the
# substring collision (gemini's "gemini cli update available!" CONTAINS codex's
# bare "update available!") resolves to gemini's ready=True verdict instead of
# matching codex's ready=False rule first. Reproduces the exact order
# pty_session.py hand-wrote before this refactor (gemini rules, then codex
# rules, then claude rules).
_READY_RULES_BY_PROVIDER: tuple[tuple[str, ProviderSpec], ...] = (
    ("gemini", gemini_spec),
    ("codex", codex_spec),
    ("claude", claude_spec),
)

READY_RULES: tuple[tuple[bool, str], ...] = tuple(
    (rule.ready_when, rule.marker)
    for _, spec in _READY_RULES_BY_PROVIDER
    for rule in spec.ready_rules
)

# Hard blockers: order is irrelevant (`any(...)` short-circuit, no precedence
# to preserve) so a plain dedup — not a real set() reorder — reconstructs the
# exact original 5-item table.
READY_HARD_BLOCKERS: tuple[str, ...] = tuple(
    dict.fromkeys(
        blocker for spec in PROVIDER_REGISTRY.values() for blocker in spec.ready_hard_blockers
    )
)
