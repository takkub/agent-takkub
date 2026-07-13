"""Claude CLI updater + compatibility analyzer — backs the status-bar
"⬆ Claude CLI" button.

Separate from `update_helper.py` (which updates *agent-takkub itself* via
git). This module updates the **Claude Code CLI** (`@anthropic-ai/claude-code`,
installed globally via npm) and — before applying — asks Claude itself whether
the new version's changes affect how the cockpit spawns it.

Flow the UI walks through (all the slow parts run off the Qt thread in a
worker):

  current_version() ─┐
  latest_version() ──┤→ has_update?  ─no→  "already latest"
  fetch_changelog() ─┘                ─yes→ analyze_compatibility()
                                            → show report → confirm
                                            → apply_update() → restart prompt

Design rules mirror update_helper.py:
- subprocess.run with no shell, NO_WINDOW flag, finite timeouts so a stale
  network / hung npm never blocks the caller forever.
- Every io function swallows exceptions and returns `(ok, msg)` / data — the
  UI never crashes on an npm or network hiccup.
- Pure helpers (`parse_version`, `compare_versions`, `slice_changelog`,
  `build_analysis_prompt`) are import-safe and unit-tested.

WHY analyze before applying: the cockpit leans on a long list of claude-code
CLI flags (`--append-system-prompt-file`, `--resume`/`--session-id`,
`--mcp-config`/`--strict-mcp-config`, `--plugin-dir`, `--fallback-model`,
`--disallowed-tools`, `--setting-sources`, `--effort`) plus the session JSONL
format the token meter reads. A claude-code release can rename/deprecate any of
these. Surfacing that *before* the user updates turns a silent breakage into an
informed choice.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import urllib.request

from ._win_console import SUBPROCESS_NO_WINDOW

PACKAGE = "@anthropic-ai/claude-code"
CHANGELOG_URL = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"

# The claude-code CLI surface agent-takkub depends on. Fed to the analyzer as
# "here's what we use — tell us if the new version touches any of it". Keep in
# sync with orchestrator._spawn's claude argv when flags are added/removed.
COCKPIT_CLAUDE_USAGE = """\
agent-takkub spawns the Claude Code CLI for the Lead and every claude-backed
teammate pane. It depends on these CLI flags / behaviours:

- `--dangerously-skip-permissions` (autonomous panes, no per-tool prompts)
- `--setting-sources project,local` (skip ~/.claude settings layer)
- `--append-system-prompt-file <md>` (Lead context + per-role specialist prompt)
- `--model` / `--effort` / `--fallback-model` (per-role tier + graceful 529 fallback)
- `--plugin-dir <dir>` (superpowers / agent-skills plugins)
- `--mcp-config <json>` + `--strict-mcp-config` (cockpit-managed MCP servers only)
- `--disallowed-tools Task[ AskUserQuestion]` (block built-in subagent dispatch)
- `--resume <uuid>` / `--session-id <uuid>` (rejoin a pane's exact session)
- CLAUDE.md / AGENTS.md auto-discovery for instruction injection
- session-transcript JSONL `message.usage` shape (token meter reads it on disk)
"""


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """subprocess.run wrapper: no shell, no console window, text output,
    never raises on non-zero (caller inspects returncode)."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        errors="replace",
        creationflags=SUBPROCESS_NO_WINDOW,
    )


def _npm() -> str | None:
    """Resolve the npm executable. On Windows this finds `npm.cmd`, which
    subprocess can run directly when given the full path (no shell needed)."""
    return shutil.which("npm")


# ─────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested)
# ─────────────────────────────────────────────────────────────────────


def parse_version(text: str) -> str | None:
    """Pull a dotted version out of arbitrary CLI output.

    `claude --version` prints e.g. `2.1.156 (Claude Code)`; `npm view`
    prints a bare `2.1.156`. Returns the first `N.N.N`-ish token, or None.
    """
    if not text:
        return None
    m = re.search(r"\b(\d+(?:\.\d+){1,3})\b", text)
    return m.group(1) if m else None


def compare_versions(a: str, b: str) -> int:
    """Numeric, component-wise compare. Returns -1 if a<b, 0 if equal, 1 if a>b.

    Shorter versions are zero-padded (`2.1` == `2.1.0`). Non-numeric junk in a
    component is treated as 0 so we degrade gracefully rather than raising.
    """

    def parts(v: str) -> list[int]:
        out: list[int] = []
        for p in (v or "").split("."):
            try:
                out.append(int(p))
            except ValueError:
                out.append(0)
        return out

    pa, pb = parts(a), parts(b)
    width = max(len(pa), len(pb))
    pa += [0] * (width - len(pa))
    pb += [0] * (width - len(pb))
    for x, y in zip(pa, pb, strict=True):
        if x != y:
            return -1 if x < y else 1
    return 0


def slice_changelog(text: str, current: str, max_chars: int = 12_000) -> str:
    """Return only the changelog entries *newer than* `current`.

    claude-code's CHANGELOG.md uses `## X.Y.Z` headings, newest first. We keep
    everything from the top down to (but not including) the heading whose
    version is <= current. Falls back to the top `max_chars` if the current
    version's heading isn't found (so the analyzer still gets recent context).
    """
    if not text:
        return ""
    lines = text.splitlines()
    heading = re.compile(r"^#{1,3}\s*v?(\d+(?:\.\d+){1,3})\b")
    kept: list[str] = []
    for line in lines:
        m = heading.match(line)
        if m and compare_versions(m.group(1), current) <= 0:
            break  # reached an already-installed version — stop
        kept.append(line)
    sliced = "\n".join(kept).strip()
    if not sliced:
        sliced = text.strip()
    return sliced[:max_chars]


def build_analysis_prompt(current: str, latest: str, changelog_slice: str) -> str:
    """Compose the headless-claude prompt: assess the changelog against the
    cockpit's claude-code usage. Asks for concise Thai markdown."""
    return (
        "คุณคือ release analyst ของโปรเจค agent-takkub (Python Qt cockpit ที่ spawn "
        f"Claude Code CLI). กำลังจะอัพเดต Claude Code จาก v{current} → v{latest}\n\n"
        "## วิธีที่ agent-takkub ใช้ Claude Code CLI\n"
        f"{COCKPIT_CLAUDE_USAGE}\n\n"
        f"## Changelog ของ Claude Code (v{current} → v{latest})\n"
        f"{changelog_slice}\n\n"
        "## งานของคุณ\n"
        "วิเคราะห์ว่า version ใหม่กระทบ agent-takkub ยังไง ตอบเป็น **ภาษาไทย markdown** "
        "กระชับ แบ่ง 3 หัวข้อ (ถ้าหัวข้อไหนไม่มีให้เขียน '— ไม่มี'):\n"
        "1. **⚠️ กระทบ/ต้องแก้** — flag ที่ถูก rename/deprecate/เปลี่ยน behavior, "
        "หรือ JSONL/session format ที่เปลี่ยน (สิ่งที่อาจทำ cockpit พัง)\n"
        "2. **✨ เอามาใช้เพิ่มได้** — feature/flag ใหม่ที่ agent-takkub น่าเอามาใช้\n"
        "3. **✅ ปลอดภัย/ไม่เกี่ยว** — สรุปสั้นๆ ว่าที่เหลือไม่กระทบ\n\n"
        "ปิดท้ายด้วยบรรทัดเดียว: **คำแนะนำ: อัพเดตได้เลย / อัพเดตได้แต่ระวัง X / "
        "ยังไม่ควรอัพเดตเพราะ Y**\n\n"
        "จากนั้น **บรรทัดสุดท้ายสุด** ต้องมี machine-readable block นี้เป๊ะ (cockpit จะ parse "
        "ไปเปิด GitHub issue อัตโนมัติเมื่อต้องแก้ระบบ):\n"
        "<<<TAKKUB\n"
        "ACTION_REQUIRED: yes|no   # yes ถ้าหัวข้อ 1 หรือ 2 มีของที่ agent-takkub ต้องลงมือทำ\n"
        "SEVERITY: high|med|low    # high ถ้าทำ cockpit พัง/ใช้ไม่ได้, med ถ้าควรปรับ, low ถ้าแค่ของเล่นเสริม\n"
        "ISSUE_TITLE: <หัวข้อ issue สั้นๆ ภาษาไทย ถ้า ACTION_REQUIRED: no ใส่ ->\n"
        ">>>"
    )


def parse_verdict(report: str) -> tuple[bool, str, str | None]:
    """Parse the `<<<TAKKUB … >>>` machine-readable block the analyzer appends.

    Returns (action_required, severity, issue_title). Conservative defaults
    `(False, "med", None)` when the block is missing or malformed — we'd rather
    NOT auto-file an issue than spam the tracker off a model that ignored the
    format. `severity` is clamped to one of high/med/low.
    """
    if not report:
        return False, "med", None
    action = re.search(r"ACTION_REQUIRED:\s*(yes|no)\b", report, re.IGNORECASE)
    sev = re.search(r"SEVERITY:\s*(high|med|low)\b", report, re.IGNORECASE)
    title_m = re.search(r"ISSUE_TITLE:\s*(.+)", report)
    required = bool(action and action.group(1).lower() == "yes")
    severity = sev.group(1).lower() if sev else "med"
    title = None
    if title_m:
        t = title_m.group(1).strip().strip("`").strip()
        if t and t not in ("-", "—"):
            title = t
    return required, severity, title


def build_issue_title(current: str, latest: str, suggested: str | None) -> str:
    """Issue title — prefer the analyzer's suggestion, else a stable default
    that carries the version range (also the dedup key)."""
    base = f"Claude CLI v{current} → v{latest}"
    if suggested:
        # Keep the version range in the title so dedup (substring match) works
        # even when the model phrases the title differently each run.
        return f"{base}: {suggested}" if base not in suggested else suggested
    return f"{base}: ปรับ cockpit ให้เข้ากันกับ version ใหม่"


def build_issue_body(current: str, latest: str, report: str) -> str:
    """Issue body: the analysis report + provenance header. The `<<<TAKKUB>>>`
    verdict block is stripped (it's machine noise, not for a human reader)."""
    cleaned = re.sub(r"<<<TAKKUB.*?>>>", "", report, flags=re.DOTALL).strip()
    return (
        f"> สร้างอัตโนมัติจากปุ่ม **⬆ Claude CLI** ตอนพบ Claude Code "
        f"v{current} → **v{latest}**\n"
        f"> วิเคราะห์ความเข้ากันได้กับ agent-takkub โดย Claude — มาสั่งแก้ทีหลังได้\n\n"
        f"{cleaned}\n"
    )


# ─────────────────────────────────────────────────────────────────────
# IO functions (best-effort, off-Qt-thread)
# ─────────────────────────────────────────────────────────────────────


def current_version() -> str | None:
    """`claude --version` → parsed version string, or None if claude isn't
    runnable / not on PATH."""
    try:
        from .config import find_claude_executable

        claude = find_claude_executable()
    except Exception:
        claude = shutil.which("claude")
    if not claude:
        return None
    try:
        proc = _run([claude, "--version"], timeout=15.0)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return parse_version(proc.stdout or "")


def latest_version(timeout: float = 20.0) -> tuple[bool, str]:
    """`npm view @anthropic-ai/claude-code version` → (ok, version-or-error)."""
    npm = _npm()
    if not npm:
        return False, "npm not on PATH"
    try:
        proc = _run([npm, "view", PACKAGE, "version"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "npm view timed out"
    except Exception as e:
        return False, f"npm view failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or "npm view failed").strip().splitlines()
        return False, tail[-1] if tail else "npm view failed"
    ver = parse_version(proc.stdout or "")
    return (True, ver) if ver else (False, "could not parse npm version")


def fetch_changelog(timeout: float = 15.0) -> tuple[bool, str]:
    """GET the raw claude-code CHANGELOG.md. (ok, text-or-error). Best-effort —
    offline / 404 just means the analyzer runs without changelog context."""
    try:
        req = urllib.request.Request(CHANGELOG_URL, headers={"User-Agent": "agent-takkub"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return True, raw.decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"changelog fetch failed: {e}"


def analyze_compatibility(
    current: str, latest: str, changelog_slice: str, timeout: float = 150.0
) -> tuple[bool, str]:
    """Run headless `claude -p <prompt>` to assess the changelog against the
    cockpit's usage. Returns (ok, markdown-report-or-error).

    Print mode needs no tools (pure reasoning over the provided text), so we
    don't pass --dangerously-skip-permissions. Uses the user's existing
    Claude auth (Max OAuth) — same binary the cockpit already spawns.
    """
    try:
        from .config import find_claude_executable

        claude = find_claude_executable()
    except Exception:
        claude = shutil.which("claude")
    if not claude:
        return False, "claude binary not found — cannot analyze"
    prompt = build_analysis_prompt(current, latest, changelog_slice)
    try:
        proc = _run([claude, "-p", prompt], timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "compatibility analysis timed out"
    except Exception as e:
        return False, f"analysis failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or "analysis failed").strip().splitlines()
        return False, tail[-1] if tail else "analysis failed"
    out = (proc.stdout or "").strip()
    return (True, out) if out else (False, "analysis returned empty output")


def build_updater_script(
    npm: str,
    python_exe: str,
    repo_root: str,
    log_path: str,
    is_windows: bool,
    cockpit_pid: int,
) -> str:
    """Return a self-contained updater script body.

    The cockpit can't `npm install -g` Claude while its own panes hold the
    files open (Windows sharing-violation → the brick that disabled
    autoupdate). So instead we hand this script to a DETACHED process and
    quit the cockpit: once this process is the last one standing, it waits
    for the cockpit + panes to die, runs the install with nothing locking
    claude, then relaunches the cockpit (`python -m agent_takkub`).

    Instead of a blind `sleep 3` (a race: a slow-exiting cockpit could still
    hold claude.exe when npm runs → brick), this *polls* until *cockpit_pid*
    actually exits (30s cap), then a 1s grace for the OS to release handles.
    The install exit code is captured and a ``<log_path>.failed`` sentinel is
    written on non-zero so a silent failure is visible; the cockpit is
    relaunched either way so the user is never left without it.

    Pure string builder so it's unit-testable; the caller owns writing it to
    disk and spawning it detached. Output is tee'd to `log_path` for
    post-mortem if the relaunch doesn't come back.
    """
    failed_sentinel = f"{log_path}.failed"
    if is_windows:

        def _ps_quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        npm_q = _ps_quote(npm)
        python_q = _ps_quote(python_exe)
        repo_q = _ps_quote(repo_root)
        log_q = _ps_quote(log_path)
        failed_q = _ps_quote(failed_sentinel)
        return (
            "$ErrorActionPreference = 'Continue'\r\n"
            # Wait for the cockpit to die so its panes release claude.exe (30s cap).
            "$deadline = (Get-Date).AddSeconds(30)\r\n"
            f"while ((Get-Process -Id {cockpit_pid} -ErrorAction SilentlyContinue) "
            "-and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }\r\n"
            "Start-Sleep -Seconds 1\r\n"  # grace for the OS to release file handles
            f"& {npm_q} install -g {PACKAGE}@latest *>&1 | Tee-Object -FilePath {log_q}\r\n"
            "$code = $LASTEXITCODE\r\n"
            f'if ($code -ne 0) {{ "FAILED exit=$code" | Out-File -FilePath {failed_q} }}\r\n'
            f"Start-Process -FilePath {python_q} -ArgumentList '-m','agent_takkub' "
            f"-WorkingDirectory {repo_q}\r\n"
        )
    import shlex

    npm_q = shlex.quote(npm)
    python_q = shlex.quote(python_exe)
    repo_q = shlex.quote(repo_root)
    log_q = shlex.quote(log_path)
    failed_q = shlex.quote(failed_sentinel)
    return (
        "#!/bin/sh\n"
        f"pid={cockpit_pid}\n"
        # Wait for the cockpit to die (150 × 0.2s = 30s cap), then a 1s grace.
        "i=0\n"
        'while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 150 ]; do sleep 0.2; i=$((i+1)); done\n'
        "sleep 1\n"
        f"{npm_q} install -g {PACKAGE}@latest > {log_q} 2>&1\n"
        "code=$?\n"
        f'[ "$code" -ne 0 ] && echo "FAILED exit=$code" > {failed_q}\n'
        f"cd {repo_q} && {python_q} -m agent_takkub &\n"
    )


def apply_update(timeout: float = 300.0) -> tuple[bool, str]:
    """`npm install -g @anthropic-ai/claude-code@latest` → (ok, message).

    CAUTION (caller's responsibility): on Windows, npm replacing claude.exe
    while a pane holds it open can brick the install (the reason cockpit
    autoupdate is disabled). The UI must ensure no claude pane is alive before
    calling this.
    """
    npm = _npm()
    if not npm:
        return False, "npm not on PATH"
    try:
        proc = _run([npm, "install", "-g", f"{PACKAGE}@latest"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "npm install timed out"
    except Exception as e:
        return False, f"npm install failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "npm install failed").strip().splitlines()
        return False, tail[-1] if tail else "npm install failed"
    return True, "claude CLI updated"
