"""Declarative per-provider CLI specs — Wave 3 #6 Phase 0 (issue #103).

Single source of truth for how the cockpit spawns/monitors each terminal CLI
provider. Pure data layer: no PyQt, no engine
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
    # Human-facing label (status-bar chip, tooltips). Empty → name.capitalize().
    display_name: str = ""

    # ─── 1. Binary discovery ───
    binary_names: list[str] = field(default_factory=list)
    install_instructions: str = ""
    custom_discovery_fn: Callable[[], str | None] | None = None
    # Machine-actionable installer (provider_install.py / doctor --fix /
    # Settings → Providers & Roles). None = manual install only (e.g.
    # agy ships as a GUI installer download, not a package command) — the
    # human `install_instructions` above is then the only guidance.
    # First token is resolved via shutil.which at run time (npm → npm.cmd on
    # Windows), so keep it a bare program name.
    install_command: list[str] | None = None
    # One-time post-install step the cockpit can NOT do for the user
    # (interactive login/OAuth). Shown after a successful install.
    post_install_note: str = ""

    # ─── 2. Spawn argv builder ───
    autonomy_flags: dict[str, list[str]] = field(default_factory=dict)
    extra_static_args: list[str] = field(default_factory=list)

    # ─── 3. CLI argument mapping flags ───
    mcp_config_flag: str | None = None
    strict_mcp_flag: str | None = None
    # Must accept a FILE path and append it to the interactive session's
    # system prompt. spawn_engine uses this for a fresh pane's one-shot task.
    # A user-prompt string/positional argument is not equivalent: putting a
    # multi-KB task in argv reintroduces Windows length and escaping risk.
    # None keeps the reliable task-file pointer flow.
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
    # Most CLIs accept effort as a regular ``flag value`` pair. Codex instead
    # exposes it through its generic config override:
    # ``-c model_reasoning_effort=<level>``. When effort_config_key is set,
    # spawn_engine prefixes the effort value with ``<key>=`` before passing it
    # to effort_flag. A provider with effort_flag=None receives no effort arg.
    effort_flag: str | None = None
    effort_config_key: str | None = None
    fallback_model_flag: str | None = None
    settings_flag: str | None = None
    task_notice_preamble: str | None = None

    # ─── 10. Read-side coupling capability flags ───
    produces_jsonl_transcript: bool = False
    supports_token_meter: bool = False
    supports_remote_history: bool = False

    # ─── 11. Generic non-claude spawn-branch knobs (#103 Phase 1) ───
    # These three encode the ONLY real divergences between the old
    # hand-written gemini/codex branches in spawn_engine.py, so the generic
    # spec-driven branch can replace both without behavior change.
    #
    # prepend_bin_dir_to_path — put the discovered binary's own directory on
    # the pane PATH (gemini: the Antigravity installer doesn't reliably
    # register agy on PATH, and agy shells out to itself / companion tools).
    prepend_bin_dir_to_path: bool = False
    # auto_trust — drive the provider's first-boot trust/onboarding prompt
    # after attach (gemini/codex; shell never, claude has its own inline path).
    auto_trust: bool = False
    # early_exit_watch — wire `_on_codex_exit`-style early-crash detection
    # (stamps codex_spawn_ts) instead of the stale-guarded `_on_session_exit`.
    early_exit_watch: bool = False


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


def _discover_opencode() -> str | None:
    """Plain PATH lookup — opencode's npm install registers the shim reliably
    on all platforms (unlike agy), so no vendored-path fallback is needed."""
    import shutil

    for name in ("opencode", "opencode.cmd", "opencode.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _discover_kimi() -> str | None:
    """Plain PATH lookup for Kimi CLI's cross-platform installer shims."""
    import shutil

    for name in ("kimi", "kimi.cmd", "kimi.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _discover_cursor() -> str | None:
    """PATH lookup for the Cursor CLI, canonical name first.

    ``cursor-agent`` is the name Cursor's own install/parameter docs use, so it
    is tried before the bare ``agent`` alias — ``agent`` is generic enough that
    an unrelated program of that name can sit earlier on PATH and be mistaken
    for Cursor. Preferring the canonical name makes that misidentification much
    less likely (a full ``--version`` identity probe is still the real fix and
    is tracked as a follow-up).
    """
    import shutil

    for name in ("cursor-agent", "cursor-agent.exe", "agent", "agent.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


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
    install_command=["npm", "install", "-g", "@openai/codex"],
    post_install_note="run `codex login` once to sign in",
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
    # GAP (CLI 0.145.0 --help, checked 2026-07-24): only positional [PROMPT];
    # no file-backed append-system-prompt option. Keep task pointer delivery.
    system_prompt_flag=None,
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
    model_flag="--model",  # verified against the installed binary: `codex --help`
    # documents `-m, --model <MODEL>` for the interactive TUI (and
    # codex_helper.py:118 already passes `--model` to `codex exec`).
    effort_flag="-c",
    effort_config_key="model_reasoning_effort",
    # Codex has no direct --effort option. Its documented session-scoped config
    # override accepts `-c model_reasoning_effort=<low|medium|high>`, so role
    # tier effort can be wired without mutating the user's config.toml.
    produces_jsonl_transcript=False,
    supports_token_meter=False,
    supports_remote_history=False,
    auto_trust=True,  # spawn_engine.py codex branch: auto_trust=True
    early_exit_watch=True,  # spawn_engine.py codex branch: codex_exit=True
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
    install_command=None,  # GUI installer download only — no package command
    post_install_note="run `agy` once to sign in",
    custom_discovery_fn=_discover_gemini,
    autonomy_flags={"default": ["--dangerously-skip-permissions"]},  # spawn_engine.py:1073
    # GAP (agy 1.1.5 --help, checked 2026-07-24): --prompt-interactive accepts
    # a prompt string, not a system-prompt file. Keep task pointer delivery.
    system_prompt_flag=None,
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
    model_flag="--model",  # agy 1.0.5 changelog + confirmed `agy models` subcommand
    effort_flag="--effort",  # agy 1.1.5 --help: low|medium|high
    produces_jsonl_transcript=False,
    supports_token_meter=False,
    supports_remote_history=False,
    prepend_bin_dir_to_path=True,  # spawn_engine.py gemini branch: agy_dir on PATH
    auto_trust=True,  # spawn_engine.py gemini branch: auto_trust=True
)


# ── opencode ──────────────────────────────────────────────────────────────────
# First provider added through the generic spec-driven spawn branch (#103
# Phase 1) — no hand-written branch of its own. opencode is sst's open-source
# multi-provider TUI (https://opencode.ai): one integration exposes 75+ model
# backends (Anthropic, OpenAI, z.ai GLM, Kimi, local Ollama, ...) selected via
# `-m provider/model` or the user's opencode config. Requires a one-time
# `opencode auth login` (or /connect in the TUI) per backend.
opencode_spec = ProviderSpec(
    name="opencode",
    display_name="OpenCode",
    binary_names=["opencode", "opencode.cmd", "opencode.exe"],
    install_instructions=(
        "opencode binary not on PATH. Install with `npm install -g opencode-ai`, "
        "then run `opencode auth login` once to connect a model provider."
    ),
    install_command=["npm", "install", "-g", "opencode-ai"],
    post_install_note="run `opencode auth login` once to connect a model provider",
    custom_discovery_fn=_discover_opencode,
    # `--auto` = auto-approve permissions not explicitly denied (opencode
    # docs /docs/permissions) — parity with claude's
    # --dangerously-skip-permissions / codex's --ask-for-approval never.
    autonomy_flags={"default": ["--auto"]},
    # GAP (opencode 1.18.4 --help, checked 2026-07-24): --prompt is a string;
    # there is no file-backed append-system-prompt option.
    system_prompt_flag=None,
    ready_hard_blockers=(),  # global blockers (esc to interrupt/cancel, press
    # enter to continue) still apply via the cross-provider dedup table below.
    ready_rules=(
        # Idle composer footer, verified by direct ConPTY capture against
        # opencode 1.18.3 on Windows (2026-07-17): the bottom hint row reads
        # "tab agents  ctrl+p commands" once the TUI reaches its input box.
        # "ctrl+p commands" is the distinctive half (no collision with any
        # other provider's markers).
        ReadyRule("ctrl+p commands", True),
        # ⚠ BUSY marker NOT yet calibrated: needs an authenticated session to
        # observe what the footer shows mid-generation (no provider was
        # connected on the calibration machine). Until then the global
        # "esc to interrupt"/"esc to cancel" blockers are the only busy
        # signal — if opencode words its interrupt hint differently, a
        # working pane may read idle. Re-probe after `opencode auth login`
        # and add the observed marker here (#103).
    ),
    ready_wait_ms=90_000,  # cold-boot allowance, parity with codex/gemini
    context_strategy="agents_md_file",  # opencode reads AGENTS.md natively
    cheatsheet_filename="AGENTS.md",
    inline_learned_notes=False,
    use_file_guards=False,
    mcp_adapter_variant="none",  # opencode's MCP lives in opencode.json config;
    # no per-session CLI surface identified yet — documented gap, see #103.
    supports_browser_profiles=False,
    paste_threshold=200,  # uniform defaults — retune only with pty evidence
    enter_delay_base_ms=800,
    enter_delay_per_kb_ms=150,
    enter_delay_max_ms=3000,
    input_swallow_recovery=True,
    supports_mirror=False,
    supports_resume=False,
    supports_slash_commands=False,
    supports_hooks=False,
    model_flag="--model",  # `-m provider/model` — per-role model selection hook
    # GAP (#103): opencode 1.18.4 `opencode --help` exposes no reasoning-effort
    # flag. Keep None so the generic spawn path cannot pass an invented option.
    effort_flag=None,
    produces_jsonl_transcript=False,
    supports_token_meter=False,
    supports_remote_history=False,
    prepend_bin_dir_to_path=False,
    auto_trust=False,  # no folder-trust modal observed on first boot (1.18.3)
    early_exit_watch=False,
)


# ── kimi ────────────────────────────────────────────────────────────────────
# Kimi CLI (MoonshotAI/kimi-cli) uses the same generic spec-driven spawn
# branch as opencode (#103 Phase 1) and has no hand-written branch of its own.
kimi_spec = ProviderSpec(
    name="kimi",
    display_name="Kimi",
    binary_names=["kimi", "kimi.cmd", "kimi.exe"],
    install_instructions=(
        "kimi binary not on PATH. Install with `uv tool install kimi-cli` "
        "(alternative: `pip install kimi-cli`). On Windows, install Git Bash "
        "first; if bash.exe is in a custom location, set `KIMI_CLI_GIT_BASH_PATH` "
        "to its full path. Then launch `kimi` and run `/login` in the TUI."
    ),  # env var name per kimi-cli changelog 1.42.0 (2026-05-11): "locates
    # bash.exe via the KIMI_CLI_GIT_BASH_PATH env override → where.exe git".
    # Pin the interpreter: kimi-cli supports Python 3.12-3.14 and uv would
    # otherwise pick whatever default it finds, which can be outside that range.
    install_command=["uv", "tool", "install", "--python", "3.13", "kimi-cli"],
    post_install_note="launch `kimi` and run `/login` once in the TUI to sign in",
    custom_discovery_fn=_discover_kimi,
    # `--yolo` skips approval for tool calls, file writes, and shell execution.
    # Kimi documents it as mutually exclusive with `--auto`, so only the
    # confirmed full-autonomy flag belongs in this argv.
    autonomy_flags={"default": ["--yolo"]},
    # GAP (kimi 1.49.0 --help, checked 2026-07-24): --prompt is a user string
    # and --agent-file is a whole agent specification, not an append-system-
    # prompt file. Keep the task pointer flow.
    system_prompt_flag=None,
    ready_hard_blockers=(),  # global blockers (esc to interrupt/cancel, press
    # enter to continue) still apply via the cross-provider dedup table below.
    # ⚠ BUSY marker NOT yet calibrated: no authenticated Kimi TUI was
    # available for ConPTY capture. Do not guess at an idle/busy footer; keep
    # this empty until an exact marker is observed (#103).
    ready_rules=(),
    ready_wait_ms=90_000,  # cold-boot allowance, parity with codex/gemini
    # AGENTS.md discovery CONFIRMED (kimi-cli changelog 1.29.0, 2026-04-01:
    # "discovers and merges AGENTS.md files from the git project root down to
    # the working directory"). Without planting it a kimi teammate never learns
    # it must call `takkub done`, so the pane would just hang after finishing.
    context_strategy="agents_md_file",
    cheatsheet_filename="AGENTS.md",
    inline_learned_notes=False,
    use_file_guards=False,
    mcp_adapter_variant="none",
    supports_browser_profiles=False,
    paste_threshold=200,  # uniform defaults — retune only with pty evidence
    enter_delay_base_ms=800,
    enter_delay_per_kb_ms=150,
    enter_delay_max_ms=3000,
    input_swallow_recovery=True,
    supports_mirror=False,
    supports_resume=False,
    supports_slash_commands=False,
    supports_hooks=False,
    model_flag="--model",  # Kimi CLI docs: `--model <id>` (for example `k2.5`)
    # GAP (#103): kimi 1.49.0 only exposes boolean --thinking/--no-thinking,
    # not low|medium|high. Do not collapse three role tiers into that toggle.
    effort_flag=None,
    produces_jsonl_transcript=False,
    supports_token_meter=False,
    supports_remote_history=False,
    prepend_bin_dir_to_path=False,
    auto_trust=False,
    early_exit_watch=False,
)


# ── cursor ──────────────────────────────────────────────────────────────────
# Cursor CLI / cursor-agent uses the same generic spec-driven spawn branch as
# opencode and Kimi (#103 Phase 1), with no hand-written spawn branch. Its
# executable is the unusually generic name `agent`, so PATH discovery can
# collide with unrelated programs; see _discover_cursor above.
cursor_spec = ProviderSpec(
    name="cursor",
    display_name="Cursor",
    binary_names=["cursor-agent", "cursor-agent.exe", "agent", "agent.exe"],
    install_instructions=(
        "Cursor CLI binary `agent` not on PATH. Install manually on Windows "
        "PowerShell with `irm 'https://cursor.com/install?win32=true' | iex`; "
        "on macOS/Linux with `curl https://cursor.com/install -fsS | bash`."
    ),
    # Official installation is a remote script, not a package-manager command.
    # Never auto-execute curl/irm installers from takkub; require an explicit
    # user-run manual install instead.
    install_command=None,
    post_install_note="run `agent` once to sign in via browser OAuth",
    custom_discovery_fn=_discover_cursor,
    # `-f/--force` ("Force allow commands unless explicitly denied", alias
    # `--yolo`) per cursor.com/docs/cli/reference/parameters — the documented
    # full-autonomy flag, parity with claude's --dangerously-skip-permissions.
    # Without it every terminal command stops on a y/n prompt and the pane is
    # unusable as an unattended teammate.
    autonomy_flags={"default": ["--force"]},
    # GAP (official CLI parameter help, checked 2026-07-24; binary unavailable
    # locally): only a positional initial user prompt is documented, with no
    # file-backed append-system-prompt option. Keep task pointer delivery.
    system_prompt_flag=None,
    ready_hard_blockers=(),
    # ⚠ NOT yet calibrated: no Cursor TUI idle/busy markers have been observed.
    # Keep this empty rather than guessing markers that could misroute tasks.
    ready_rules=(),
    ready_wait_ms=90_000,
    # AGENTS.md discovery CONFIRMED (cursor.com/docs/cli/using: "The CLI also
    # reads AGENTS.md and CLAUDE.md at the project root (if present) and applies
    # them as rules"). Needed so a cursor teammate learns to call `takkub done`.
    context_strategy="agents_md_file",
    cheatsheet_filename="AGENTS.md",
    inline_learned_notes=False,
    use_file_guards=False,
    mcp_adapter_variant="none",
    supports_browser_profiles=False,
    paste_threshold=200,
    enter_delay_base_ms=800,
    enter_delay_per_kb_ms=150,
    enter_delay_max_ms=3000,
    input_swallow_recovery=True,
    supports_mirror=False,
    # Cursor supports `--resume [thread-id]`, but cockpit resume is based on a
    # session UUID with different semantics. Keep disabled until adapted.
    supports_resume=False,
    supports_slash_commands=False,
    supports_hooks=False,
    # Cursor CLI parameter reference: `--model <model>`; `agent models` lists ids.
    model_flag="--model",
    # GAP (#103): Cursor's official CLI parameter reference lists no reasoning
    # effort option. Keep None until Cursor documents a session-scoped surface.
    effort_flag=None,
    produces_jsonl_transcript=False,
    supports_token_meter=False,
    supports_remote_history=False,
    prepend_bin_dir_to_path=False,
    auto_trust=False,
    early_exit_watch=False,
)


PROVIDER_REGISTRY: dict[str, ProviderSpec] = {
    "claude": claude_spec,
    "codex": codex_spec,
    "gemini": gemini_spec,
    "opencode": opencode_spec,
    "kimi": kimi_spec,
    "cursor": cursor_spec,
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
    # opencode appended last: its single marker ("ctrl+p commands") shares no
    # substring with any rule above, so position carries no precedence weight —
    # last keeps the historical gemini→codex→claude table byte-identical.
    ("opencode", opencode_spec),
    # Kimi intentionally contributes no rules yet, but keeping an explicit
    # entry makes the future calibrated marker a data-only change (#103).
    ("kimi", kimi_spec),
    # Cursor intentionally contributes no rules until its TUI has been
    # observed; keep the entry explicit so calibration remains data-only.
    ("cursor", cursor_spec),
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
