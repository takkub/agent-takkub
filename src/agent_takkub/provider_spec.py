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

import os
import sys
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
    # Escape sequence terminal.html sends on Shift+Enter/Alt+Enter to request a
    # multiline newline instead of submitting (#149). Confirmed correct only
    # for Ink-based TUIs (claude, gemini/agy), which treat a bare ESC as a
    # harmless no-op immediately followed by CR. codex's ratatui UI treats ESC
    # as interrupt/clear-composer, so it — and every provider whose TUI toolkit
    # hasn't been confirmed (opencode/bubbletea, kimi, cursor) — stays at the
    # safe default None: terminal.html then does NOT intercept the keystroke,
    # so xterm.js sends its native '\r' (pre-#149 behavior, same as plain Enter).
    multiline_newline_seq: str | None = None

    # ─── 8. Spawning capability flags ───
    supports_mirror: bool = False
    supports_resume: bool = False
    supports_slash_commands: bool = False
    supports_hooks: bool = False
    # Does this CLI's own agent harness expose a distinct structured
    # file-read tool, separate from raw shell/exec? (#273). Gates whether
    # `_task_handoff_pointer` may hand a long task off as "read this file
    # yourself" instead of pasting it inline — a pane whose ONLY file
    # access is a shell tool can't honor that instruction at all when shell
    # reads are also disallowed (#104's "never `cat`/`type` a long path"
    # convention, baked into the pointer text itself as "ห้ามรัน path เป็น
    # คำสั่ง shell"), so the pointer becomes an unopenable dead end and the
    # pane fails the assign before the actual work ever starts. Default
    # True (most agent CLIs — including claude — do have one); codex is the
    # one confirmed exception (real incident, issue #273: a codex pane
    # replied "[FAILED] ไม่มี file-read tool ใน pane นี้" and never began
    # the task). Never guess this False for an unconfirmed provider — an
    # unconfirmed provider defaults True and just risks the SAME (rare,
    # already-existing) paste-swallow class `_task_handoff_pointer` was
    # built to avoid, not a hard failure.
    supports_agent_file_read: bool = True

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
    # Effort levels this provider's CLI actually accepts via effort_flag, in
    # UI-display order (lowest→highest). Single source of truth for the
    # Settings "Providers & Roles" per-role Effort dropdown (settings_window.py)
    # and the spawn-time resolver (spawn_engine.py) — neither hardcodes a
    # list. Empty for any provider with effort_flag=None (unsupported) and
    # for providers whose accepted levels are not yet confirmed from the
    # CLI's own --help/docs (see each spec's own note below).
    effort_levels: tuple[str, ...] = ()
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

    # ─── 12. Project/session scoping (opt-in per provider, #132) ───
    # A CLI whose own conversation history isn't already keyed by cwd (agy
    # dumps everything into one shared pseudo-project without this) can plug
    # in a resolver here instead of spawn_engine.py hardcoding its name.
    # project_scope_flag     — argv flag that takes an existing scope/project
    #                          id as its value (None = provider not wired).
    # project_scope_resolver — cwd -> existing scope id, or None when nothing
    #                          matches. Lazy-imports its helper module inside
    #                          the call, mirroring custom_discovery_fn above,
    #                          so this stays a pure data layer.
    # project_scope_new_flag — fallback flag to request a fresh scope id when
    #                          the resolver finds no match (None = provider
    #                          has no such flag; scope arg is simply omitted).
    project_scope_flag: str | None = None
    project_scope_resolver: Callable[[str], str | None] | None = None
    project_scope_new_flag: str | None = None

    # ─── 13. Auth-failure detection (#248/#247 round 2) ───
    # Lower-case substrings that unambiguously mean "this provider's CLI is
    # NOT authenticated" when seen on the pane's footer region (_ready_region
    # — same scope as ready_hard_blockers, so conversation body text quoting
    # one of these can't poison the verdict). Checked instantly — no grace
    # period — via pty_session.PtySession.auth_failure_reason(), which ALSO
    # ORs in provider_spec.GENERIC_AUTH_ERROR_MARKERS below for every
    # provider. Empty here means no provider-specific text has been
    # confirmed yet; the generic table is the only signal until a real
    # failure screen is observed and captured (never guess a marker from a
    # provider's docs alone — a wrong guess either silently matches
    # conversation text or never fires).
    auth_error_markers: tuple[str, ...] = field(default_factory=tuple)
    # Markers that mean "still going through an auth/account-verification
    # step" — legitimately shown for the first few seconds of a normal cold
    # boot, so unlike auth_error_markers these are NOT an instant failure
    # signal. auth_failure_reason() only treats one of these as a failure
    # once the screen has been static (seconds_since_output(), the
    # spinner-normalized content-change clock — see pty_session.py's
    # #248/#247 module note) for AUTH_TRANSIENT_GRACE_SEC — i.e. "still
    # animating a spinner next to this text" reads as normal boot, "frozen
    # on this text" reads as stuck. gemini/agy is the confirmed case (#247:
    # observed hanging on "Signing in..." indefinitely); reuses the exact
    # text already in gemini_spec.ready_hard_blockers below by design (same
    # underlying CLI state, different question — "is it ready" vs "is it
    # stuck").
    auth_transient_markers: tuple[str, ...] = field(default_factory=tuple)

    # ─── 14. Close-time scaffolding filter (#272) ───
    # Process names (matched case-insensitively, `.exe` suffix ignored so one
    # entry covers both Windows and POSIX) that are THIS provider's own
    # CLI-launcher/runtime children — always present under a live pane's pid
    # right before close, never evidence of real unfinished work by
    # themselves. orchestrator._warn_if_live_children (#234) subtracts these
    # (plus the Windows-universal ConPTY baseline in
    # GENERIC_SCAFFOLDING_PROCESS_NAMES_WIN32 below) before deciding whether
    # to warn Lead, so a pane closing with only its own npm/uv shim tree left
    # standing stays silent instead of firing a warning with zero new
    # information (#272: was 100% of closes before this field existed).
    # Empty = no provider-specific scaffolding confirmed yet; the Windows
    # baseline still applies.
    scaffolding_process_names: tuple[str, ...] = field(default_factory=tuple)

    # ─── 15. Boot-stall diagnostic (#273) ───
    # Argv (relative to the discovered binary, via custom_discovery_fn) for a
    # quick, one-shot, non-interactive health-check command this CLI ships —
    # one that prints its OWN real config error on a single line instead of
    # cockpit only ever being able to say the generic "ค้างอยู่ที่ boot phase".
    # Real incident (#273): codex's actual failure was `Error: failed to
    # load bootstrap configuration / Caused by: invalid transport in ...`,
    # fully diagnosable via `codex mcp list` in under a second — but cockpit
    # had no way to surface it, costing ~40 minutes of manual guessing.
    # Run ASYNC (QProcess, never blocking the Qt main thread — mirrors
    # `Orchestrator._check_uncommitted_async`) by
    # `lead_inbox._run_boot_diagnostic_async`, fired once per boot-stall
    # notice, best-effort: a missing/timed-out/errored diagnostic just means
    # no follow-up appears — the original generic notice always still fires
    # unchanged. None = no such command confirmed for this provider yet
    # (never guess one from docs alone — a wrong argv could itself hang or,
    # worse, mutate state).
    boot_diagnostic_argv: tuple[str, ...] | None = None


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


def _resolve_gemini_project_id(cwd: str) -> str | None:
    from .gemini_helper import resolve_agy_project_id

    return resolve_agy_project_id(cwd)


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
    multiline_newline_seq="\x1b\r",  # Ink TUI: bare ESC is a no-op, so ESC+CR
    # inserts a newline instead of submitting (#149).
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
    effort_levels=("low", "medium", "high", "xhigh", "max"),  # claude CLI's
    # documented --effort levels (matches the Claude Code agent()-tool effort
    # tiers already surfaced to this session — see the Workflow tool's own
    # opts.effort schema, same CLI underneath).
    fallback_model_flag="--fallback-model",  # spawn_engine.py:1500
    settings_flag="--settings",  # spawn_engine.py:1456
    task_notice_preamble=None,
    produces_jsonl_transcript=True,
    supports_token_meter=True,
    supports_remote_history=True,
    # ⚠ NOT yet verified against a real claude auth-failure screen (#248/#247
    # round 2) — claude's own login flow runs mostly outside the pane (see
    # doctor.check_claude's credential-file check), so no confirmed in-pane
    # error text exists to list here. GENERIC_AUTH_ERROR_MARKERS is the only
    # signal for this provider until one is observed.
    auth_error_markers=(),
    # config.py's find_claude_executable() deliberately prefers the real
    # claude.exe over the claude.cmd npm shim specifically to avoid the
    # visible cmd.exe console window (see that function's own docstring) —
    # but claude.exe itself still runs on a bundled node runtime, so a plain
    # `node` child is the one scaffolding process actually confirmed under
    # it (#272).
    scaffolding_process_names=("node.exe", "node"),
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
    # `--disable apps` (#283): codex's own built-in `codex_apps` MCP (feature
    # flag `apps`, stable and ON by default) takes minutes to boot AND paints
    # "esc to interrupt" in the status bar the whole time — while its composer
    # is already accepting input. That string is this spec's own
    # `ready_hard_blockers`, so the cockpit reads the pane as busy and refuses
    # to deliver until the delivery times out. Measured on the reporter's
    # machine, same task, same box:
    #
    #     apps ON  → pane reaches ready 342s / 388s (2 trials); task NEVER
    #                delivered (boot-timeout every round)
    #     apps OFF → ready 0s; task delivered in 1s; work starts immediately
    #
    # This is not something the cockpit can route around from the outside:
    # `codex_apps` is internal to codex, so removing it from the injected
    # `shared-mcp-*.json` does nothing. Disabling the feature at spawn is the
    # only lever, and it is session-scoped — `~/.codex/config.toml` is never
    # touched, matching the same rule `mcp_adapter_variant="session_override"`
    # already follows for MCP config.
    autonomy_flags={
        "win32": [
            "--dangerously-bypass-approvals-and-sandbox",
            "--disable",
            "apps",
        ],  # spawn_engine.py:1140-1143
        "default": [
            "--ask-for-approval",
            "never",
            "-s",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=true",
            "--disable",
            "apps",
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
    # #271: 90s was set before codex grew the `code_mode`/`codex_apps`
    # feature — measured cold-boot on real hardware now runs 90-150s EVERY
    # spawn (4/4 trials, 2026-08-16), so the old window routinely expired
    # mid-boot. 180s covers the observed spread with margin; the
    # shows_startup_marker() guard in lead_inbox.py's _check() (#271) is the
    # actual blind-paste backstop, this just keeps the common case from
    # relying on that extension at all.
    ready_wait_ms=180_000,  # lead_inbox.py:434-435 (cold-boot + MCP-boot allowance)
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
    # multiline_newline_seq left at default None: codex's ratatui UI treats a
    # bare ESC as interrupt/clear-composer, not a no-op, so ESC+CR would clear
    # the composer and submit an empty line instead of inserting a newline
    # (#149). Shift+Enter on a codex pane falls back to xterm's native '\r'.
    supports_mirror=False,
    # Codex CLI stable contract: ``codex resume <SESSION_ID>``.  The generic
    # provider spawn path appends this subcommand + the UUID selected by the
    # Remote Mobile picker after all global flags, avoiding Codex's cwd-wide
    # native picker entirely.
    supports_resume=True,
    session_resume_flag="resume",
    supports_slash_commands=False,
    supports_hooks=False,
    # #273: confirmed False by a live incident — a codex pane replied
    # "[FAILED] ไม่มี file-read tool ใน pane นี้ จึงเปิด task spec ตามข้อห้าม
    # shell ไม่ได้" when handed a `_task_handoff_pointer` pointer. Codex's
    # tool set is shell/apply_patch only; it has no structured read-file
    # tool distinct from the shell exec the pointer explicitly forbids
    # using on the path (#104). Long tasks fall back to an inline paste for
    # this provider instead (see `_task_handoff_pointer` callers).
    supports_agent_file_read=False,
    # #273: `codex mcp list` prints the real config error on one line
    # (confirmed: "Error: failed to load bootstrap configuration / Caused
    # by: invalid transport in ...") when ~/.codex/config.toml's
    # [mcp_servers.*] table is malformed. `codex --help` confirms `mcp list`
    # is read-only (lists configured servers) — safe to run unconditionally.
    #
    # ⚠ #281: it does NOT see the MCPs the cockpit itself injects. Those go in
    # session-scoped as `-c mcp_servers.<name>.<key>=…` (see
    # `mcp_adapter_variant="session_override"` below) and never touch
    # ~/.codex/config.toml, so `mcp list` answers "No MCP servers configured
    # yet" while three of them are visibly loading on screen. A whole session
    # was lost to that answer, concluding codex had no MCPs and disabling an
    # unrelated plugin. When a codex pane hangs at boot, the authoritative
    # source is the pane's own "Starting MCP servers (n/N): …" line — which
    # `PtySession.boot_phase_detail()` now captures — plus the role's
    # runtime/shared-mcp-<role>.json.
    boot_diagnostic_argv=("mcp", "list"),
    task_notice_preamble=(
        "[orchestrator note] อ่านก่อนเริ่มงาน:\n"
        "- กฎ `ห้าม spawn subagent เอง` ใน ROLE prefix ห้าม native child\n"
        "  เว้นแต่ Lead สั่ง task นี้ด้วย `--mode subagent`; ไม่รวม shell\n"
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
    effort_levels=("low", "medium", "high"),  # what `model_reasoning_effort` accepts
    # Codex writes structured rollout JSONL under ~/.codex/sessions. Remote
    # consumes only event_msg user/agent messages; tool/reasoning records stay
    # local. The session id in the rollout metadata is also the stable id used
    # by ``codex resume <SESSION_ID>`` and the Remote picker.
    produces_jsonl_transcript=True,
    supports_token_meter=False,
    supports_remote_history=True,
    auto_trust=True,  # spawn_engine.py codex branch: auto_trust=True
    early_exit_watch=True,  # spawn_engine.py codex branch: codex_exit=True
    # ⚠ NOT yet verified against a real codex auth-failure screen (#248/#247
    # round 2 — #248's report was "spawns and produces zero output", which
    # points at the no-content watchdog, not necessarily a rendered auth
    # error) — no confirmed in-pane error text exists to list here.
    # GENERIC_AUTH_ERROR_MARKERS is the only signal until one is observed.
    auth_error_markers=(),
    # codex is an npm-installed CLI on a node runtime (same shim shape as
    # claude/opencode) plus its own `code_mode`/`codex_apps` sandbox host
    # process — both confirmed always-present under a live codex pane at
    # close time, not evidence of unfinished work (#272).
    scaffolding_process_names=(
        "node.exe",
        "node",
        "codex-code-mode-host.exe",
        "codex-code-mode-host",
    ),
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
        # agy 1.1.6 can render its idle footer while these startup/account
        # checks are still swallowing submit. Keep task delivery behind the
        # ready gate until the transient check clears (#126).
        "signing in",
        "verifying your account",
    ),  # pty_session.py:205-209 (agy's own trust/account gates observed on first boot)
    ready_rules=(
        ReadyRule("? for shortcuts", True),  # pty_session.py:222 (agy idle footer)
        ReadyRule("type your message or", True),  # pty_session.py:223 (legacy gemini CLI)
        ReadyRule("gemini cli update available!", True),  # pty_session.py:224 (#51)
        ReadyRule("please try again shortly", True),
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
    # no-op rather than auto-driving a machine-wide side effect on every spawn. Existing
    # global AGY MCP/plugin config can therefore still leak into an explicit-empty Takkub
    # policy; no startup flag that prevents MCP boot is exposed by agy 1.1.6 (#103/#121).
    supports_browser_profiles=False,
    paste_threshold=200,
    enter_delay_base_ms=800,  # kept uniform — see codex_spec note above (review 2-C/2-D)
    enter_delay_per_kb_ms=150,
    enter_delay_max_ms=3000,
    input_swallow_recovery=True,
    multiline_newline_seq="\x1b\r",  # agy is Ink-based like claude — same ESC+CR
    # multiline newline behavior (#149).
    supports_mirror=False,
    supports_resume=True,
    session_resume_flag="--conversation",
    supports_slash_commands=False,
    supports_hooks=False,
    model_flag="--model",  # agy 1.0.5 changelog + confirmed `agy models` subcommand
    # agy 1.1.6 exposes --effort, but its advertised model slugs already encode
    # effort (for example gemini-3.1-pro-low/high). A mismatched pair makes agy
    # discard the explicit --model and silently use its default (#125). With no
    # explicit model there is also no deterministic way to prove the default
    # supports the requested effort before launch, so model preservation wins.
    effort_flag=None,
    produces_jsonl_transcript=True,
    supports_token_meter=False,
    supports_remote_history=True,
    prepend_bin_dir_to_path=True,  # spawn_engine.py gemini branch: agy_dir on PATH
    auto_trust=True,  # spawn_engine.py gemini branch: auto_trust=True
    project_scope_flag="--project",  # agy 1.1.6 --help: "Project ID for the
    # current CLI session" — without it every spawn falls into agy's shared
    # 'default-cli-project' pseudo-project, mixing every takkub project's
    # conversation history together (#132).
    project_scope_resolver=_resolve_gemini_project_id,
    project_scope_new_flag="--new-project",  # agy 1.1.6 --help: mint a fresh
    # project id when the resolver finds no existing match for this cwd.
    # No confirmed hard auth-FAILURE text observed yet (#248/#247 round 2) —
    # GENERIC_AUTH_ERROR_MARKERS is the only instant-fail signal.
    auth_error_markers=(),
    # CONFIRMED transient state (#247's exact report): agy can sit on
    # "Signing in..." indefinitely instead of completing the boot-time OAuth
    # check. Same text as ready_hard_blockers above (still not ready either
    # way) — listed again here because auth_failure_reason() answers a
    # different question ("stuck", gated on sustained staleness) than
    # is_at_ready_prompt() ("not yet ready", true from frame one).
    #
    # "not signed in" (#256): agy's own cold-start banner reads "You are
    # currently not signed in." on EVERY spawn, before the CLI has even
    # started its OAuth check — confirmed by a same-day transcript
    # (gemini-090814.transcript.log) showing banner -> "Signing in..." ->
    # signed-in header (email + model) -> normal task execution, all while
    # GENERIC_AUTH_ERROR_MARKERS's old instant-fail listing of this phrase
    # convicted the pane mid-boot. Moved here instead of staying a
    # zero-grace instant marker (see GENERIC_AUTH_ERROR_MARKERS's #256 note
    # below for why it left the generic table entirely rather than being
    # provider-excluded there): grace-gating on `seconds_since_output()`
    # means it only convicts once the screen has gone STATIC on this text
    # for AUTH_TRANSIENT_GRACE_SEC — a normal cold boot keeps producing new
    # output (banner -> spinner -> signed-in header) so the clock never
    # accumulates that long, and once the header/task output scrolls the
    # banner out of `_ready_region`'s 6-row tail the marker stops matching
    # at all — the same tail-scoping that already protects every other
    # check in this module (module note above `_ready_region`) doubles as
    # the "override a stale marker once a newer identity is on screen"
    # mechanism #256 asked for, with no separate identity-parsing needed.
    auth_transient_markers=("signing in", "verifying your account", "not signed in"),
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
    # merged global/project config can therefore leak MCPs into an explicit-empty role.
    # `--pure` suppresses external plugins, not configured MCPs, and no generic
    # per-session MCP-disable CLI surface exists in opencode 1.18.4 (#103/#121).
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
    # GAP (#103, confirmed 2026-08-13 — BlueParking bug report): no scanner
    # registered in remote/notify.py's _HISTORY_SCANNERS for opencode, so a
    # Lead pane running this provider never mirrors a reply to Remote Mobile
    # — the message IS delivered (cli_server writes straight into the pane;
    # delivery is provider-agnostic), only the read-back is unsupported. As
    # of the fix landing alongside this comment, this is no longer a silent
    # gap: `api.lead_say`'s response and `takkub doctor --live` both surface
    # it explicitly instead of the phone's spinner hanging forever with no
    # explanation. Flip to True only once a real transcript source is found
    # for this provider and a matching _HistoryScanner entry ships.
    supports_remote_history=False,
    prepend_bin_dir_to_path=False,
    auto_trust=False,  # no folder-trust modal observed on first boot (1.18.3)
    early_exit_watch=False,
    # ⚠ NOT yet verified — no authenticated opencode session was available
    # for calibration (#103/#248/#247). GENERIC_AUTH_ERROR_MARKERS only.
    auth_error_markers=(),
    # opencode-ai is npm-installed (same shim shape as claude/codex), so a
    # `node` child is expected scaffolding, not evidence of unfinished work
    # (#272).
    scaffolding_process_names=("node.exe", "node"),
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
    # ⚠ BUSY marker STILL NOT calibrated (#257): the idle footer below was
    # captured against a logged-in, IDLE Kimi TUI only — nobody has yet sent
    # it a real task and watched what the footer/status line reads while
    # Kimi is actively generating. Do not guess it from the idle text; the
    # only busy signal until then is the cross-provider `ready_hard_blockers`
    # dedup table (esc to interrupt/cancel), which may not even apply if
    # Kimi words its own interrupt hint differently — a working pane could
    # misread as ready. Re-probe by assigning kimi a real task and capturing
    # the footer mid-generation, then fill this in as a data-only change.
    ready_rules=(
        # Idle composer footer, captured via direct ConPTY capture against a
        # signed-in kimi-cli 1.49.x session on Windows (#257, 2026-08-16):
        #   main  @: mention files | ctrl-x: toggle mode | shift-tab: plan
        #   mode | ctrl+o: editor
        # "ctrl-x: toggle mode" is the distinctive half — no substring
        # collision with any other provider's ready/busy markers in this
        # file. Before this entry, ready_rules was empty, so
        # is_at_ready_prompt() could never return True for a kimi pane and
        # every assigned task sat undelivered until the 1800s busy-wait
        # ceiling (proven empirically the same day — task text never
        # appeared on screen even after a manual resend).
        ReadyRule("ctrl-x: toggle mode", True),
    ),
    ready_wait_ms=90_000,  # cold-boot allowance, parity with codex/gemini
    # AGENTS.md discovery CONFIRMED (kimi-cli changelog 1.29.0, 2026-04-01:
    # "discovers and merges AGENTS.md files from the git project root down to
    # the working directory"). Without planting it a kimi teammate never learns
    # it must call `takkub done`, so the pane would just hang after finishing.
    context_strategy="agents_md_file",
    cheatsheet_filename="AGENTS.md",
    inline_learned_notes=False,
    use_file_guards=False,
    mcp_adapter_variant="none",  # Kimi auto-loads user/project mcp.json files.
    # Its --mcp-config/--mcp-config-file options add configs but expose no deny-all
    # startup switch in kimi 1.49.0, so explicit-empty role isolation is a #103/#121 gap.
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
    # GAP (#103): same remote-mirror gap as opencode_spec's note above — no
    # _HistoryScanner entry exists for kimi, so a kimi Lead pane never
    # mirrors a reply to Remote Mobile even though delivery itself works.
    supports_remote_history=False,
    prepend_bin_dir_to_path=False,
    auto_trust=False,
    early_exit_watch=False,
    # CONFIRMED (#257, 2026-08-16): a fresh kimi pane spawned with no
    # credentials shows "Model: not set, send /login to login" instead of
    # ever reaching the idle footer above — kimi cannot do anything in this
    # state (no model selected), so unlike gemini's transient boot banner
    # this is a genuine instant failure, not a normal startup step. Narrowed
    # to "send /login to login" (drop the "model: not set" half): the model
    # name is settable per-role independent of login state, so a future
    # "model not set" wording for a DIFFERENT reason must not silently
    # convict the pane on login grounds. "send /login to login" is
    # first-person CLI chrome no unrelated dev-output plausibly reproduces.
    auth_error_markers=("send /login to login",),
    # kimi-cli is installed via `uv tool install` and runs on a python
    # interpreter (uv shims resolve to a python entry point), so a `python`
    # child is expected scaffolding under a live kimi pane, not evidence of
    # unfinished work (#272).
    scaffolding_process_names=("python.exe", "python", "python3"),
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
    mcp_adapter_variant="none",  # Cursor auto-detects the IDE's mcp.json config.
    # Current docs expose interactive enable/disable after launch, but no verified
    # deny-all startup flag; explicit-empty role isolation remains a #103/#121 gap.
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
    # GAP (#103): same remote-mirror gap as opencode_spec's note above — no
    # _HistoryScanner entry exists for cursor, so a cursor Lead pane never
    # mirrors a reply to Remote Mobile even though delivery itself works.
    supports_remote_history=False,
    prepend_bin_dir_to_path=False,
    auto_trust=False,
    early_exit_watch=False,
    # ⚠ NOT yet verified — no Cursor TUI auth-failure screen has been
    # observed (#103/#248/#247). GENERIC_AUTH_ERROR_MARKERS only.
    auth_error_markers=(),
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
    # Kimi (#257): now contributes "ctrl-x: toggle mode" (idle footer). No
    # substring collision with any rule above, so its position here carries
    # no precedence weight either.
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


# ── auth-failure detection (#248/#247 round 2) ──────────────────────────────
# Cross-provider fallback markers: unambiguous "not authenticated" phrasing a
# CLI might show regardless of which provider it is. Applied to EVERY
# provider in addition to that provider's own (currently mostly empty, see
# each spec's auth_error_markers comment above) confirmed list — a new
# provider therefore gets at least this baseline coverage with zero data
# entry required. Deliberately narrow substrings (not just "sign in", which
# would false-match a spinner-adjacent "Signing in..." on gemini/agy — that
# case is handled separately by auth_transient_markers/AUTH_TRANSIENT_GRACE_SEC
# below since it is legitimately transient, not an instant failure) to keep
# the false-positive risk low against ordinary conversation text scrolling
# through the footer region. UNVERIFIED against a real failure screen for any
# provider as of this round — a best-effort baseline pending a live #248/#247
# repro, not a confirmed table like READY_HARD_BLOCKERS above.
#
# Narrowed in the #248/#247 round-2 follow-up: a first pass included several
# phrases that are common HTTP/test-framework vocabulary, NOT CLI-chrome —
# ordinary backend dev output routinely prints these while testing an
# unrelated auth *feature* in the pane's own project, and this table is
# scoped to the footer tail rows (`_ready_region`, 6 lines), not the whole
# scrollback, so a long test run's final lines can easily still be sitting
# there when a poll lands. Dropped: "unauthorized" (any HTTP 401 log line),
# "invalid credentials" / "invalid api key" (typical login-test assertion
# text), "session expired" (common app-level error string), "login
# required" (common 401 body text), "authentication required" /
# "authentication failed" (generic app error strings), and especially "not
# authenticated" — FastAPI's own default 401 detail is the literal string
# "Not authenticated", so keeping it here would auto-fail every FastAPI
# backend pane the moment its own test suite exercised an authless request.
# What remains reads only as first-person CLI chrome telling an operator to
# re-auth — text no unrelated dev-output string plausibly reproduces.
#
# Dropped in the #256 follow-up: "not signed in". Proven UNSAFE as a
# zero-grace, every-provider instant marker — gemini/agy prints "You are
# currently not signed in." in its own cold-start banner on every single
# spawn, before it has even begun the OAuth check that normally follows
# (transcript gemini-090814.transcript.log, same day). That is exactly the
# "plausibly reproduces" failure this table's own design note above warns
# against; unlike the HTTP/test-framework phrases dropped in round 2, this
# one is real first-person CLI chrome, just transient rather than a
# failure. It was NOT re-added per-provider-excluded here (no such
# mechanism exists on this generic table, and building one for a single
# known offender would be premature) — it now lives solely as gemini_spec's
# own auth_transient_markers entry (see that field's comment), which is the
# tier built for exactly this "normal for the first few seconds, only a
# real problem if it never clears" shape. No other provider has confirmed
# this phrase as either an instant failure or a normal boot artifact, so it
# is simply absent from the generic baseline until one does — that
# provider's own `auth_error_markers` is the place to add it, confirmed
# against a real screen, not a guess re-added here.
GENERIC_AUTH_ERROR_MARKERS: tuple[str, ...] = (
    "please sign in again",
    "please log in again",
    "please authenticate",
)

# Gate for auth_transient_markers (#247): a marker in that list only counts
# as "stuck" once the screen has been static (PtySession.seconds_since_output
# — spinner-normalized, so an animating spinner next to the same text does
# NOT reset this) for at least this long. Short relative to
# orchestrator.BUSY_WAIT_CEILING_SEC (1800s default) — the whole point is
# surfacing this to Lead far sooner than that ceiling — but long enough that
# the first few seconds of a completely normal sign-in on a fresh spawn never
# false-positives. Overrideable for slow/loaded machines.
AUTH_TRANSIENT_GRACE_SEC = float(os.environ.get("TAKKUB_AUTH_TRANSIENT_GRACE_SEC", "45"))


def auth_error_markers_for(provider: str) -> tuple[str, ...]:
    """Instant-failure auth markers for `provider`: its own confirmed list
    (``ProviderSpec.auth_error_markers``) plus the generic cross-provider
    baseline, deduped. Unknown provider name → generic markers only."""
    spec = PROVIDER_REGISTRY.get(provider)
    own = spec.auth_error_markers if spec is not None else ()
    return tuple(dict.fromkeys((*own, *GENERIC_AUTH_ERROR_MARKERS)))


def auth_transient_markers_for(provider: str) -> tuple[str, ...]:
    """Sustained-only auth-stall markers for `provider` (see
    ``ProviderSpec.auth_transient_markers``). Empty for a provider with none
    confirmed, or an unknown provider name — never falls back to a generic
    table, unlike ``auth_error_markers_for``, because a transient marker only
    makes sense paired with the exact wording of that provider's own
    boot-time account-check text."""
    spec = PROVIDER_REGISTRY.get(provider)
    return spec.auth_transient_markers if spec is not None else ()


# ── ready-marker calibration status (#257) ──────────────────────────────────
# A provider spawned with an empty `ready_rules` can NEVER satisfy
# is_at_ready_prompt() (no rule can ever match ()), so task delivery silently
# stalls until orchestrator's busy-wait ceiling (1800s default) — proven
# empirically for kimi the same day this was written, before its
# "ctrl-x: toggle mode" rule above was captured. This predicate is a pure
# data-layer query only: no spawn-time Lead warning is wired to it yet — that
# requires touching spawn_engine.py/lead_inbox.py, both out of scope for this
# change (see this task's file boundaries). Whoever wires that follow-up
# should call this instead of re-deriving "empty tuple" as the calibration
# signal, so a future provider only needs a ProviderSpec entry to go green.
def is_ready_marker_calibrated(provider: str) -> bool:
    """True when `provider` has at least one ready rule, so
    `is_at_ready_prompt()` can ever return True for it. False for an unknown
    provider name too — no rules to match means the same "task can never be
    delivered" outcome regardless of the reason."""
    spec = PROVIDER_REGISTRY.get(provider)
    return bool(spec and spec.ready_rules)


def uncalibrated_providers() -> tuple[str, ...]:
    """Every registered provider name whose `ready_rules` is still empty, in
    `PROVIDER_REGISTRY` iteration order. Empty tuple = every provider can, at
    minimum, ever reach a ready verdict (says nothing about busy-marker
    accuracy — see each spec's own busy-marker calibration notes)."""
    return tuple(name for name, spec in PROVIDER_REGISTRY.items() if not spec.ready_rules)


# ── per-model effort exceptions ────────────────────────────────────────────
# A model that rejects its own provider's effort flag even though the
# provider generally supports one — narrower than a whole ProviderSpec field,
# so it lives as a small model-keyed table instead of another dataclass
# field. claude-haiku-4-5 (and the "haiku" alias): confirmed against the
# installed claude CLI — passing `--effort` errors, this tier has no
# extended-thinking/reasoning-effort mode. Keyed by provider so two CLIs that
# happen to share a model id string can't collide.
_MODELS_WITHOUT_EFFORT: dict[str, frozenset[str]] = {
    "claude": frozenset({"claude-haiku-4-5", "haiku"}),
}


def effort_levels_for(provider: str, model: str | None) -> tuple[str, ...]:
    """Effort levels selectable when *provider* spawns *model*.

    Single source of truth for both the spawn-time effort resolver
    (spawn_engine.py) and the Settings "Effort" dropdown
    (settings_window.py) — neither hardcodes level lists or the
    per-model exception table. Empty when the provider has no
    ``effort_flag`` at all, or when *model* is one of that provider's
    ``_MODELS_WITHOUT_EFFORT`` entries.
    """
    spec = PROVIDER_REGISTRY.get(provider)
    if spec is None or not spec.effort_flag:
        return ()
    if model and model.strip() in _MODELS_WITHOUT_EFFORT.get(provider, frozenset()):
        return ()
    return spec.effort_levels


# ── close-time scaffolding filter (#272) ────────────────────────────────────
# Windows-only: ConPTY hosting itself surfaces a cmd.exe/conhost.exe pair
# under a live pane's pid regardless of which provider is running (the npm
# `.cmd` shim most providers install through spawns cmd.exe, which pywinpty
# gives its own conhost) — confirmed present on every provider's close in
# #272's evidence, so unlike scaffolding_process_names above this is not
# provider-specific. POSIX has no equivalent console-host process, so this
# stays empty there; leave it that way rather than guessing a shell name that
# may not actually appear (#272's evidence is Windows-only).
GENERIC_SCAFFOLDING_PROCESS_NAMES_WIN32: tuple[str, ...] = ("cmd.exe", "conhost.exe")


def normalize_process_name(name: str) -> str:
    """Lowercase a process name and drop a trailing ``.exe`` so the same
    scaffolding entry matches both the Windows and POSIX spelling."""
    normalized = name.strip().lower()
    if normalized.endswith(".exe"):
        normalized = normalized[: -len(".exe")]
    return normalized


def scaffolding_process_names_for(provider: str) -> frozenset[str]:
    """Normalized (``normalize_process_name``) set of process names that are
    *expected* scaffolding under a live ``provider`` pane — this provider's
    own confirmed ``scaffolding_process_names`` plus the Windows-universal
    ConPTY baseline (``GENERIC_SCAFFOLDING_PROCESS_NAMES_WIN32``) when running
    on Windows. Unknown provider name → baseline only, same as an
    unrecognized name having no provider-specific scaffolding confirmed.
    """
    spec = PROVIDER_REGISTRY.get(provider)
    own = spec.scaffolding_process_names if spec is not None else ()
    generic = GENERIC_SCAFFOLDING_PROCESS_NAMES_WIN32 if sys.platform == "win32" else ()
    return frozenset(normalize_process_name(n) for n in (*own, *generic))
