"""Orchestrator: owns all AgentPanes, exposes high-level operations.

Public API (called by main_window UI and cli_server JSON requests):

  spawn(role, cwd=None)          -> bool, message
  assign(role, cwd, task)        -> bool, message
  send(to_role, msg, from_role)  -> bool, message
  close(role)                    -> bool, message
  done(from_role, note)          -> bool, message
  list_status()                  -> dict[role, state]
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from datetime import datetime

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .agent_pane import AgentPane
from .config import (
    EVENTS_LOG,
    REPO_ROOT,
    RUNTIME_DIR,
    active_project,
    agent_role_dir,
    default_cwd_for_role,
    ensure_runtime,
    find_claude_executable,
    lead_cwd,
)
from .pty_session import PtySession
from .roles import LEAD


def _log_event(event: str, **details) -> None:
    """Append a JSONL event line to runtime/events.log. Best-effort; never
    raises so an audit-log failure can't take down the orchestrator."""
    try:
        ensure_runtime()
        line = json.dumps(
            {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **details},
            ensure_ascii=False,
        )
        with open(EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


RESUME_WINDOW_SEC = 5 * 60  # respawn within this window → claude --continue

# Idle watchdog: when a teammate pane sits at the ready prompt (claude is
# idle, no "esc to interrupt") while pane.state is still "working", the
# orchestrator assumes the agent finished its task but forgot to call
# `takkub done`. After IDLE_REMIND_AFTER_S of continuous idle we inject a
# one-line reminder, then back off for IDLE_REMIND_COOLDOWN_S before another.
# Set IDLE_REMIND_AFTER_S to 0 to disable the watchdog entirely.
IDLE_REMIND_AFTER_S = 45
IDLE_REMIND_COOLDOWN_S = 90
IDLE_WATCHDOG_INTERVAL_MS = 5_000
IDLE_REMINDER_TEXT = (
    "🔔 [auto-reminder] task เสร็จแล้วใช่มั้ย? ถ้าใช่ run `takkub done [note]` "
    "รายงาน Lead เลย ถ้ายังทำต่อ ignore ข้อความนี้"
)

# Plugins we want spawned agents to inherit *explicitly* (skipping user-level
# settings to avoid claude-obsidian's broken SessionStart hook). Each entry
# is a *marketplace name* under ~/.claude/plugins/cache/. We pick the highest
# semver-ish version directory found.
_SAFE_PLUGINS: tuple[str, ...] = ("superpowers-dev", "addy-agent-skills")


def _render_lead_context() -> str | None:
    """Render Lead's spawn-time system prompt: cockpit CLAUDE.md + an
    auto-injected `BLOCKED_DIRS` paragraph listing the active project's paths.

    Lead's hybrid policy (see cockpit CLAUDE.md "Lead direct-edit policy")
    forbids direct file edits inside project paths; this function bakes the
    *current* project's paths into the prompt every spawn so the rule has
    teeth even when projects.json switches between sessions.

    Returns the rendered file path (string), or None if there's no cockpit
    CLAUDE.md to render from.
    """
    cockpit_md = REPO_ROOT / "CLAUDE.md"
    if not cockpit_md.exists():
        return None

    base = cockpit_md.read_text(encoding="utf-8")
    name, proj = active_project()
    paths = list((proj.get("paths") or {}).values()) if proj else []

    if paths:
        blocked = "\n".join(f"- `{p}`" for p in paths)
        header = f"active project: **{name}**" if name else "active project:"
    else:
        blocked = "- (no active project — projects.json `active` not set)"
        header = "active project:"

    suffix = f"""

---

## 🚫 BLOCKED_DIRS (auto-injected at spawn)

{header}

ไดเรกทอรีต่อไปนี้คือ project code — Lead **ห้ามใช้ Edit / Write / MultiEdit / NotebookEdit** ในไฟล์ใต้ paths เหล่านี้เด็ดขาด:

{blocked}

ถ้างานต้องแก้ไฟล์ในเส้นทางข้างบน → ใช้ `takkub assign --role <role> --cwd <path> "<task>"` เสมอ

✅ ทำเองได้:
- Read / Grep / Glob ทุกที่ (สำหรับวางแผน + เขียน task spec)
- Edit / Write ใน cockpit ({REPO_ROOT}) เช่น CLAUDE.md, projects.json, .claude/agents/*
- `git status` / `git log` / `git diff` (inspection ไม่กระทบไฟล์)

❌ ห้ามทำเองแม้แค่บรรทัดเดียว:
- ทุกไฟล์ใต้ BLOCKED_DIRS ข้างบน
- งานที่ touch > 1 ไฟล์
- งานที่ edit > 30 บรรทัดในรอบเดียว

ละเมิดข้อใดข้อหนึ่ง → หยุดทันทีแล้ว delegate ผ่าน `takkub assign`
"""
    ensure_runtime()
    out = RUNTIME_DIR / "lead-context.md"
    out.write_text(base + suffix, encoding="utf-8")
    return str(out)


def _default_plugin_dirs() -> list[str]:
    """Resolve ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/ for
    each plugin in `_SAFE_PLUGINS`, returning the directories that actually
    contain a `.claude-plugin/plugin.json`. Best-effort; never raises."""
    home = pathlib.Path.home()
    cache = home / ".claude" / "plugins" / "cache"
    out: list[str] = []
    if not cache.exists():
        return out
    for marketplace in _SAFE_PLUGINS:
        mp_dir = cache / marketplace
        if not mp_dir.is_dir():
            continue
        # one plugin per marketplace; one version per plugin (typically)
        for plugin in sorted(mp_dir.iterdir()):
            if not plugin.is_dir():
                continue
            versions = sorted((v for v in plugin.iterdir() if v.is_dir()), reverse=True)
            for v in versions:
                if (v / ".claude-plugin" / "plugin.json").exists():
                    out.append(str(v))
                    break
    return out


class Orchestrator(QObject):
    """Owns the pane registry and routes commands.

    Layout policy: Lead is always pre-registered (created by main_window) and
    fills the window initially. Teammate panes are created on demand the
    first time we spawn that role, via the `paneRequested` signal which
    main_window connects to its own add-pane logic.
    """

    statusChanged = pyqtSignal()
    leadInjected = pyqtSignal(str)
    paneRequested = pyqtSignal(str)  # role_name — main_window should add this pane
    paneClosed = pyqtSignal(str)  # role_name — main_window should remove this pane
    agentDone = pyqtSignal(str, str)  # role_name, note — for desktop notifications

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.panes: dict[str, AgentPane] = {}
        # last-known cwd per role, used to decide whether to pass --continue
        # on a fresh spawn (must match the previous cwd for resume to be valid)
        self._recent_exits: dict[str, dict] = {}  # role -> {cwd, ts}

        # Idle watchdog bookkeeping. Per-role:
        #   first_idle_ts   — when the pane was first seen idle in this streak
        #                     (None = currently processing or not "working")
        #   last_reminder_ts — last time we injected a reminder (0 = never)
        self._idle_state: dict[str, dict[str, float | None]] = {}
        self._idle_watchdog = QTimer(self)
        self._idle_watchdog.setInterval(IDLE_WATCHDOG_INTERVAL_MS)
        self._idle_watchdog.timeout.connect(self._check_idle_teammates)
        if IDLE_REMIND_AFTER_S > 0:
            self._idle_watchdog.start()

    # ──────────────────────────────────────────────────────────────
    # registration (main_window builds panes and registers them)
    # ──────────────────────────────────────────────────────────────
    def register_pane(self, pane: AgentPane) -> None:
        self.panes[pane.role.name] = pane
        pane.spawnRequested.connect(self._on_pane_spawn_clicked)
        pane.closeRequested.connect(self._on_pane_close_clicked)
        pane.inputBytes.connect(self._on_pane_input)
        self.statusChanged.emit()

    def unregister_pane(self, role_name: str) -> None:
        pane = self.panes.pop(role_name, None)
        if pane is None:
            return
        if pane.session is not None:
            pane.session.terminate()
        self.statusChanged.emit()

    # ──────────────────────────────────────────────────────────────
    # high-level operations
    # ──────────────────────────────────────────────────────────────
    def spawn(self, role_name: str, cwd: str | None = None) -> tuple[bool, str]:
        role_name = role_name.lower().strip()
        pane = self.panes.get(role_name)
        if pane is None:
            # ask main_window to create + register the pane, then retry
            self.paneRequested.emit(role_name)
            pane = self.panes.get(role_name)
            if pane is None:
                return False, f"unknown role: {role_name}"

        if pane.session is not None and pane.session.is_alive:
            return True, f"{role_name} already running"

        # Fresh spawn — clear any stale idle tracking from a prior session.
        self._idle_state.pop(role_name, None)

        # Resolve cwd:
        #   Lead          → repo root (so CLAUDE.md auto-discovery picks up the
        #                   Lead instructions at agent-takkub/CLAUDE.md)
        #   teammate      → explicit --cwd, else active project's role-matched
        #                   path (frontend→web, backend→api, ...), else the
        #                   role's runtime staging dir
        role_md_file: str | None = None
        if role_name == LEAD.name:
            # Lead works *on* the active project, not on the cockpit's own
            # source. cwd defaults to the project's root (common parent of
            # its `paths`) so claude reads the project's CLAUDE.md, runs
            # `git status` against the right repo, and tools land in the
            # user's actual codebase. The cockpit's CLAUDE.md (takkub
            # cheatsheet + role guide) is appended as system prompt so
            # Lead still knows about `takkub assign / send / done / ...`.
            spawn_cwd = cwd or lead_cwd() or str(REPO_ROOT)
            # Render Lead's system prompt fresh each spawn so BLOCKED_DIRS
            # tracks whatever project is active in projects.json right now.
            # Skip injection when Lead is anchored at the cockpit itself
            # (no project context to enforce).
            if spawn_cwd != str(REPO_ROOT):
                role_md_file = _render_lead_context()
        else:
            staging = agent_role_dir(role_name)
            spawn_cwd = cwd or default_cwd_for_role(role_name) or str(staging)
            # When cwd is a project path, claude auto-discovers the project's
            # CLAUDE.md, not the role's specialist override. Pass the role's
            # markdown to --append-system-prompt-file so the specialist rules
            # always apply regardless of where we land. (Using the *file*
            # variant avoids command-line escaping problems with multiline
            # markdown containing backticks, asterisks, and Thai text.)
            role_md_path = staging / "CLAUDE.md"
            if role_md_path.exists():
                role_md_file = str(role_md_path)

        try:
            claude = find_claude_executable()
        except RuntimeError as e:
            return False, str(e)

        env = os.environ.copy()
        env["TAKKUB_ROLE"] = role_name
        bin_dir = str(REPO_ROOT / "bin")
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

        # --setting-sources controls which settings.json layers claude loads.
        # We default to `project,local` (skip ~/.claude/settings.json) because
        # the claude-obsidian plugin currently ships a SessionStart hook that
        # crashes with `ToolUseContext is required for prompt hooks. This is a
        # bug.` whenever it fires inside a cockpit-spawned session.
        #
        # To still give agents access to superpowers + agent-skills, we hand
        # those plugins to claude *explicitly* via --plugin-dir (see below).
        # Override the whole policy with TAKKUB_SETTING_SOURCES env var.
        sources = os.environ.get("TAKKUB_SETTING_SOURCES", "project,local")
        argv: list[str] = [
            claude,
            "--dangerously-skip-permissions",
            "--setting-sources",
            sources,
        ]

        # Explicit plugin allowlist (skip the broken claude-obsidian hook).
        # Set TAKKUB_EXTRA_PLUGINS env var to a `;`-separated list of plugin
        # root dirs (must each contain `.claude-plugin/plugin.json`) to add
        # more, or set it to empty string to suppress the defaults.
        plugin_default = ";".join(_default_plugin_dirs())
        plugin_dirs_raw = os.environ.get("TAKKUB_EXTRA_PLUGINS", plugin_default)
        for pdir in [p.strip() for p in plugin_dirs_raw.split(";") if p.strip()]:
            if (pathlib.Path(pdir) / ".claude-plugin" / "plugin.json").exists():
                argv.extend(["--plugin-dir", pdir])
        if role_md_file:
            argv.extend(["--append-system-prompt-file", role_md_file])

        # Session resume: if this same role exited recently from the same
        # cwd, ask claude to continue the previous conversation instead of
        # starting fresh. Useful for crash recovery + accidental closes.
        resumed = False
        prior = self._recent_exits.get(role_name)
        if (
            prior
            and prior.get("cwd") == spawn_cwd
            and (time.time() - prior.get("ts", 0)) < RESUME_WINDOW_SEC
        ):
            argv.append("--continue")
            resumed = True

        session = PtySession(cols=110, rows=36, parent=self)
        try:
            session.spawn(argv=argv, cwd=spawn_cwd, env=env)
        except Exception as e:
            return False, f"failed to spawn claude: {e}"

        pane.attach_session(session, cwd=spawn_cwd)
        # record exits for resume detection
        session.processExited.connect(
            lambda _code, r=role_name, c=spawn_cwd: self._on_session_exit(r, c)
        )
        # forget the prior exit record now that we've spawned successfully
        if role_name in self._recent_exits:
            del self._recent_exits[role_name]

        self._auto_trust(role_name)
        self.statusChanged.emit()
        _log_event(
            "spawn",
            role=role_name,
            cwd=spawn_cwd,
            resumed=resumed,
        )
        suffix = " (resumed)" if resumed else ""
        return True, f"{role_name} spawned in {spawn_cwd}{suffix}"

    def _on_session_exit(self, role_name: str, cwd: str) -> None:
        """Track recent exits so a quick respawn can pass --continue."""
        self._recent_exits[role_name] = {"cwd": cwd, "ts": time.time()}

    # ──────────────────────────────────────────────────────────────
    def _auto_trust(self, role_name: str) -> None:
        """Watch the pane and auto-press Enter on claude's trust folder modal.

        Polls every 500ms for up to 30s. Stops as soon as the prompt is
        accepted (or the session dies / never shows it).
        """
        pane = self.panes.get(role_name)
        if pane is None:
            return
        elapsed = [0]
        max_ms = 30_000

        def _check() -> None:
            if pane.session is None or not pane.session.is_alive:
                return
            if pane.session.is_at_trust_prompt():
                # option 1 (Yes) is preselected; just hit Enter
                pane.session.write("\r")
                return
            elapsed[0] += 500
            if elapsed[0] >= max_ms:
                return
            QTimer.singleShot(500, _check)

        QTimer.singleShot(1_000, _check)

    def assign(self, role_name: str, cwd: str | None, task: str) -> tuple[bool, str]:
        ok, msg = self.spawn(role_name, cwd=cwd)
        if not ok:
            return ok, msg

        self._send_when_ready(role_name, task)
        _log_event("assign", role=role_name, cwd=cwd, task_preview=task[:120])
        return True, f"task queued for {role_name} (sending when ready)"

    def _send_when_ready(self, role_name: str, task: str, max_wait_ms: int = 45_000) -> None:
        """Poll until claude's main prompt is idle, then paste task + Enter.

        Replaces the old fixed 12s wait so we don't paste into the trust modal
        or while claude is still bootstrapping. Falls back to a hard timeout
        so a hung claude doesn't silently swallow the task.
        """
        pane = self.panes.get(role_name)
        if pane is None:
            return
        elapsed = [0]
        sent = [False]

        def _deliver() -> None:
            if sent[0]:
                return
            sent[0] = True
            if pane.session is None or not pane.session.is_alive:
                return
            pane.set_state("working", note=task[:60])
            pane.session.write(task)
            QTimer.singleShot(200, lambda: pane.session and pane.session.write("\r"))

        def _check() -> None:
            if sent[0]:
                return
            if pane.session is None or not pane.session.is_alive:
                return
            if pane.session.is_at_ready_prompt():
                _deliver()
                return
            elapsed[0] += 500
            if elapsed[0] >= max_wait_ms:
                # hard timeout — paste anyway so user sees the task land
                _deliver()
                return
            QTimer.singleShot(500, _check)

        QTimer.singleShot(1_000, _check)

    def send(self, to_role: str, msg: str, from_role: str | None = None) -> tuple[bool, str]:
        to_role = to_role.lower().strip()
        pane = self.panes.get(to_role)
        if pane is None:
            return False, f"unknown role: {to_role}"
        if pane.session is None or not pane.session.is_alive:
            return False, f"{to_role} is not running (spawn it first)"

        header = f"[{from_role} → {to_role}] " if from_role and from_role != to_role else ""
        body = header + msg
        pane.session.write(body)
        QTimer.singleShot(150, lambda: pane.session and pane.session.write(b"\r"))

        # CC Lead unless source was Lead and target was a teammate, or vice versa
        if from_role and from_role not in (None, LEAD.name) and to_role != LEAD.name:
            lead = self.panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                lead.session.write(f"[CC] {body}")
                QTimer.singleShot(150, lambda: lead.session and lead.session.write(b"\r"))

        _log_event("send", to=to_role, from_=from_role, msg_preview=msg[:120])
        return True, f"sent to {to_role}"

    def close(self, role_name: str) -> tuple[bool, str]:
        role_name = role_name.lower().strip()
        pane = self.panes.get(role_name)
        if pane is None:
            return False, f"unknown role: {role_name}"
        was_alive = pane.session is not None
        if was_alive:
            # mark exit as expected so the pane doesn't surface "exited"/crash
            pane.mark_expected_exit()
            pane.session.terminate()
            pane.set_state("empty", note=None)
        self._idle_state.pop(role_name, None)
        # For teammates, fully remove from the layout so the right column
        # collapses back. Lead stays as it always anchors the cockpit.
        if role_name != LEAD.name:
            self.paneClosed.emit(role_name)
        self.statusChanged.emit()
        _log_event("close", role=role_name)
        return True, f"{role_name} closed"

    def done(self, from_role: str, note: str = "") -> tuple[bool, str]:
        from_role = from_role.lower().strip()
        pane = self.panes.get(from_role)
        if pane is None:
            return False, f"unknown role: {from_role}"
        # Agent finished cleanly — clear any pending idle reminder state so
        # we don't re-fire after the pane closes / respawns.
        self._idle_state.pop(from_role, None)

        # notify Lead
        lead = self.panes.get(LEAD.name)
        notice = f"[{from_role} done] {note}".rstrip()
        if lead and lead.session and lead.session.is_alive:
            lead.session.write(notice)
            QTimer.singleShot(150, lambda: lead.session and lead.session.write(b"\r"))
            self.leadInjected.emit(notice)

        # mark pane done, auto-close after a delay so user can see it
        pane.set_state("done", note=note[:80] if note else "done")
        QTimer.singleShot(2_500, lambda: self.close(from_role))
        _log_event("done", role=from_role, note=note[:200])
        self.agentDone.emit(from_role, note)
        return True, f"{from_role} reported done"

    def list_status(self) -> dict[str, str]:
        return {name: p.state for name, p in self.panes.items()}

    # ──────────────────────────────────────────────────────────────
    # idle watchdog — nudge teammates that forgot to `takkub done`
    # ──────────────────────────────────────────────────────────────
    def _check_idle_teammates(self) -> None:
        """Inject a `takkub done` reminder into any teammate pane that's been
        at the ready prompt for IDLE_REMIND_AFTER_S while still flagged
        'working'. Lead is exempt — only Lead is allowed to orchestrate, and
        Lead never calls `done` on itself."""
        now = time.time()
        for name, pane in list(self.panes.items()):
            if name == LEAD.name:
                continue
            if pane.state != "working":
                self._idle_state.pop(name, None)
                continue
            if pane.session is None or not pane.session.is_alive:
                self._idle_state.pop(name, None)
                continue

            entry = self._idle_state.setdefault(
                name, {"first_idle_ts": None, "last_reminder_ts": 0.0}
            )

            if not pane.session.is_at_ready_prompt():
                # claude is processing — reset the idle streak so a long
                # build doesn't count toward the reminder threshold.
                entry["first_idle_ts"] = None
                continue

            if entry["first_idle_ts"] is None:
                entry["first_idle_ts"] = now
                continue

            idle_for = now - entry["first_idle_ts"]
            since_last_reminder = now - entry["last_reminder_ts"]
            if idle_for >= IDLE_REMIND_AFTER_S and since_last_reminder >= IDLE_REMIND_COOLDOWN_S:
                self._inject_idle_reminder(name, pane)
                entry["last_reminder_ts"] = now
                # restart the idle streak so we don't fire again until the
                # agent stays idle for another full IDLE_REMIND_AFTER_S past
                # the cooldown.
                entry["first_idle_ts"] = now

    def _inject_idle_reminder(self, role_name: str, pane: AgentPane) -> None:
        if pane.session is None or not pane.session.is_alive:
            return
        pane.session.write(IDLE_REMINDER_TEXT)
        QTimer.singleShot(150, lambda: pane.session and pane.session.write(b"\r"))
        _log_event("idle_reminder", role=role_name)

    def close_all_teammates(self) -> tuple[bool, str]:
        """Close every non-Lead pane. Used by Lead to reset the board."""
        names = [n for n in list(self.panes.keys()) if n != LEAD.name]
        if not names:
            return True, "no teammates to close"
        for n in names:
            self.close(n)
        return True, f"closed {len(names)} teammate(s): {', '.join(names)}"

    # ──────────────────────────────────────────────────────────────
    # internal: handlers wired from AgentPane signals
    # ──────────────────────────────────────────────────────────────
    def _on_pane_spawn_clicked(self, role_name: str) -> None:
        self.spawn(role_name)

    def _on_pane_close_clicked(self, role_name: str) -> None:
        self.close(role_name)

    def _on_pane_input(self, role_name: str, data: bytes) -> None:
        pane = self.panes.get(role_name)
        if pane is None or pane.session is None:
            return
        pane.session.write(data)
