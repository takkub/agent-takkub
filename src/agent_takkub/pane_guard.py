"""Pane command guard — the shell-side half of the per-role tool policy.

`pane_tools_policy.py` decides which **MCP servers** a pane gets, and
`spawn_engine` enforces it with `--strict-mcp-config`. That gate is airtight
for MCP… and completely irrelevant to `Bash`, because every cockpit pane is
spawned with `--dangerously-skip-permissions`. So a role that is denied the
browser MCP simply routes around the block:

    npx --yes playwright         # then drive Chromium from an ad-hoc script

which is exactly what a `frontend` pane was caught doing (2026-07-23) — plus a
`find / -maxdepth 6 -iname playwright` whole-disk sweep that pinned the disk.
The MCP policy denied the *sanctioned* path and left the *unsanctioned* one
wide open, so the agent took it.

This module closes that hole. It is a pure leaf (stdlib only, no PyQt, no
config I/O) so `takkub _guard` — wired as a `PreToolUse`/`Bash` hook by
`hook_wiring.py` and fired on **every** Bash call — stays cheap to import.

Two rules:

* ``browser_driver`` — installing or invoking Playwright / Puppeteer /
  Selenium / a headless Chrome, for any role outside `BROWSER_ROLES`. Browser
  verification belongs to `qa` (and `critic`/`designer` for visual review):
  they get the real Playwright MCP with cockpit-managed, per-shard browser
  profiles. An ad-hoc `npx playwright` bypasses that isolation, re-downloads
  Chromium (the cache on the dev box had grown to 2.88 GB across four
  chromium builds), and is invisible to the cockpit's resource accounting.
* ``disk_scan`` — `find` (or `Get-ChildItem -Recurse`) rooted at `/` or a
  drive root. Minutes of disk I/O, and on Windows it stalls the whole box.

Both rules **only** ever inspect the command string. Reading *about* these
tools is always fine: `grep playwright`, `cat package.json`, `ls
~/AppData/Local/ms-playwright` are all allowed — only acquisition and
execution are denied.

Multi-provider (#103): Claude Code hooks are claude-only, so this is hard
enforcement for claude panes and prompt-level only for codex / gemini-agy /
opencode / kimi / cursor, whose role files carry the same rule in prose.
`GUARD_RULE_TEXT` is the single source of that prose so the role files and
this module can never drift (guarded by
`tests/test_agent_role_files_have_browser_guard.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Roles allowed to drive a browser. `qa` owns e2e/smoke; `critic` and
# `designer` need to look at rendered pages for visual review. Everyone else
# writes unit tests and hands browser verification to qa — which is what
# `.claude/agents/frontend.md` already says ("integration/e2e เป็นหน้าที่ QA").
BROWSER_ROLES: frozenset[str] = frozenset({"qa", "critic", "designer"})

# Panes the user types into directly (mirrors `roles.USER_DRIVEN_ROLES` — kept
# as a literal so this module stays import-free of the role registry). The
# guard never second-guesses a human at a keyboard.
_UNGUARDED_ROLES: frozenset[str] = frozenset({"lead", "shell"})

# Prose handed to role files verbatim. Keep in sync with the rules below.
GUARD_RULE_TEXT = (
    "ห้ามติดตั้งหรือรัน browser driver เอง (playwright / puppeteer / selenium / "
    "headless chrome) ไม่ว่าช่องทางไหน — รวมถึง `npx playwright`, `npm i puppeteer`, "
    "`pip install playwright` และ ad-hoc node/python script ที่ require มัน. "
    "ต้อง verify ผ่าน browser → เขียนใน note ตอน `takkub done` "
    "แล้วให้ Lead ส่งงานต่อให้ qa (qa มี Playwright MCP + browser profile ที่ cockpit จัดการให้)."
)


@dataclass(frozen=True)
class Verdict:
    """Outcome of `classify`. `allowed=False` means the Bash call is blocked."""

    allowed: bool
    rule: str = ""
    reason: str = ""


# ── patterns ────────────────────────────────────────────────────────────────
# A browser-automation package token. The lookbehind keeps `ms-playwright`
# (the browser *cache* directory — listing it is harmless) from matching,
# while `playwright-core` still does because the suffix is spelled out.
_PKG = (
    r"(?:"
    r"(?<![\w./-])@(?:playwright|puppeteer)/[\w-]+"
    r"|(?<![\w@./-])(?:"
    r"playwright(?:-core|-chromium|-extra)?"
    r"|puppeteer(?:-core|-extra)?"
    r"|selenium(?:-webdriver)?"
    r"|webdriverio"
    r")(?![\w-])"
    r")"
)

# Anything but a command separator — keeps `npx foo && cat playwright.md` from
# tripping rule 1 on the *second* command (which `cat` makes harmless).
_SAME_CMD = r"[^|;&\n]*?"

_BROWSER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # npx / pnpm dlx / yarn dlx / bunx — the exact route the frontend pane took
    ("npx", re.compile(rf"(?<![\w-])(?:npx|bunx)(?![\w-]){_SAME_CMD}{_PKG}", re.I)),
    # explicit package-manager install/add/exec
    (
        "pkg-install",
        re.compile(
            rf"(?<![\w-])(?:npm|pnpm|yarn|bun)(?![\w-])\s+"
            rf"(?:i|install|add|dlx|exec|create)(?![\w-]){_SAME_CMD}{_PKG}",
            re.I,
        ),
    ),
    # pip install playwright / python -m playwright install
    (
        "pip-install",
        re.compile(
            rf"(?<![\w-])(?:pip3?|python3?\s+-m\s+pip)(?![\w-]){_SAME_CMD}"
            rf"install{_SAME_CMD}{_PKG}",
            re.I,
        ),
    ),
    ("python-module", re.compile(rf"(?<![\w-])python3?\s+-m\s+{_PKG}", re.I)),
    # bare invocation at the start of a command or after a separator:
    #   playwright test / npx-installed shim on PATH
    ("bare-invoke", re.compile(rf"(?:^|[|;&]\s*|\bsudo\s+){_PKG}\s+\S", re.I | re.M)),
    # driving it from an inline script body (heredoc, node -e, python -c)
    (
        "inline-import",
        re.compile(
            r"(?:require\s*\(\s*['\"]|from\s+['\"]?|import\s+['\"]?)"
            r"(?:@?(?:playwright|puppeteer)(?:[/-][\w-]+)?|selenium)",
            re.I,
        ),
    ),
    # launching a browser binary in automation mode
    (
        "headless-chrome",
        re.compile(
            r"(?<![\w-])(?:chrome|chromium|msedge|chrome-headless-shell|google-chrome)"
            r"[\w.-]*(?![\w-])" + _SAME_CMD + r"--(?:headless|remote-debugging-port)",
            re.I,
        ),
    ),
)

# `find` rooted at the filesystem root (POSIX `/`, Git-Bash `/c/`, or a Windows
# drive root). `find . -name x` and `find src -name x` stay allowed.
_DISK_SCAN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "find-root",
        re.compile(
            r"(?<![\w-])find(?![\w-])\s+(?:-[\w-]+\s+)*"
            r"(?:/|[A-Za-z]:[\\/]?|/[a-z]/)(?=\s|$)",
            re.I,
        ),
    ),
    (
        "gci-root",
        re.compile(
            r"(?<![\w-])(?:Get-ChildItem|gci|ls)(?![\w-])[^|;&\n]*"
            r"(?:-Path\s+)?(?:[A-Za-z]:[\\/]?|/)(?=\s)[^|;&\n]*-Recurse",
            re.I,
        ),
    ),
)

# mini-browser's client is fixed to CDP 9222. A qa/critic/designer shard may
# still use its isolated Playwright MCP, but must never drive mb's one shared
# Chrome session (#92).
_MB_INVOKE = re.compile(r"(?:^|[|;&]\s*)(?:mb|mb-start-chrome)(?:\s|$)", re.I | re.M)


def normalise_role(role: str | None) -> str:
    """Canonical role name: lowercased, whitespace-trimmed, shard suffix
    dropped so `qa#3` is treated exactly like `qa`."""
    name = (role or "").strip().lower()
    return name.split("#", 1)[0] if "#" in name else name


def is_browser_role(role: str | None) -> bool:
    """Whether `role` may drive a browser directly."""
    return normalise_role(role) in BROWSER_ROLES


def classify(command: str, role: str | None) -> Verdict:
    """Decide whether `role` may run `command`.

    Fail-open by design: an unknown/empty role means the CLI was invoked
    outside a cockpit pane (a human at a terminal), and an empty command
    means the hook payload was malformed. Neither is worth blocking on — the
    guard exists to stop an agent routing around policy, not to police
    people.
    """
    cmd = (command or "").strip()
    if not cmd:
        return Verdict(True)

    name = normalise_role(role)
    if not name or name in _UNGUARDED_ROLES:
        return Verdict(True)

    raw_role = (role or "").strip().lower()
    if "#" in raw_role and is_browser_role(name) and _MB_INVOKE.search(cmd):
        return Verdict(
            False,
            rule="browser_driver:mb-shard-cdp-9222",
            reason=(
                f"role `{raw_role}` ใช้ mb ไม่ได้: mb client hardcode CDP 9222 "
                "ทำให้ทุก shard ขับ Chrome ตัวเดียวกัน (#92). "
                "ใช้ Playwright MCP ที่ cockpit แยก profile ให้ต่อ shard แทน"
            ),
        )

    if not is_browser_role(name):
        for rule, pattern in _BROWSER_PATTERNS:
            if pattern.search(cmd):
                return Verdict(
                    False,
                    rule=f"browser_driver:{rule}",
                    reason=(
                        f"role `{name}` ขับ browser เองไม่ได้ (นโยบาย cockpit). {GUARD_RULE_TEXT}"
                    ),
                )

    for rule, pattern in _DISK_SCAN_PATTERNS:
        if pattern.search(cmd):
            return Verdict(
                False,
                rule=f"disk_scan:{rule}",
                reason=(
                    "สแกนทั้งไดรฟ์ถูกบล็อก — กิน disk I/O จนเครื่องกระตุกทั้งเครื่อง. "
                    "ใช้ Glob/Grep tool หรือจำกัด path ให้แคบ "
                    "(เช่น `find src -name '*.ts'`) แทน"
                ),
            )

    return Verdict(True)
