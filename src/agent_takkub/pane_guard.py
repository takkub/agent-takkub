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

A third rule, ``host_destructive`` (#169), blocks kill-by-image-name commands
(`taskkill /IM`, `pkill`, `killall`, PowerShell `Stop-Process -Name`) for
*every* guarded role, no allowlist — unlike ``browser_driver`` there is no
role that legitimately needs to kill processes host-wide. Root incident
(2026-07-08): a `frontend` pane ran `taskkill /F /T /IM node.exe` to clear a
stuck dev-server port and killed every node process on the box, including
other panes' Claude Code processes — `takkub list` came back with nothing but
`lead`. `HOST_DESTRUCTIVE_RULE_TEXT` is the prose counterpart, pinned in role
files by `tests/test_agent_role_files_have_host_destructive_guard.py`.

A fourth rule, ``pip_editable`` (#202), blocks `pip install -e`/`--editable`
for *every* guarded role, no allowlist. `pip install -e .` rewrites the
`__editable__*.pth` in the shared venv's `site-packages` to point at whatever
directory the caller ran it from. Root incident (2026-08-14): a `backend`
pane ran it from inside its own `--isolation worktree` checkout, repointing
the *shared* venv used by every pane at that worktree's `src/`; once the Lead
removed the worktree after merging, the main tree's `.venv` and the `takkub`
CLI itself broke (`ModuleNotFoundError`). Worse, while the pane was still
running, every other process sharing that venv — including a `qa` full-suite
run mid-flight — silently imported code from the wrong worktree, so the gate
result couldn't be trusted. `PIP_EDITABLE_RULE_TEXT` is the prose
counterpart, pinned in role files by
`tests/test_agent_role_files_have_pip_editable_guard.py`.

A fifth rule, ``pane_poll_loop`` (#287), blocks hand-rolled loops that poll
`takkub list`/`status` with a `sleep` — and is the one rule that deliberately
applies to `lead` as well, since Lead is the only role with teammates to poll.
See its pattern block below for why the #242 prose ban never bound.

A sixth rule, ``git_lead_only`` (#314), blocks `git commit` / `push` /
`reset --hard` / `branch -D` / `tag -d` / `rebase` / `merge` / `checkout` for
every guarded role. Every role file already carried this prohibition in
prose (see "Version control (required)" in each `.claude/agents/*.md`) —
but prose alone is exactly the gap the module docstring above describes for
`browser_driver`: real, observed session behavior (#314) showed a `backend`
and a custom `admin` role self-committing on a task instruction of "commit
เอง" while a `frontend` pane in the same session refused the identical
instruction, citing the same role-file prose both panes had. Whether the
prose held was down to how convincingly the task text argued past it, not
policy — the one thing that should decide it. This rule makes "only Lead
commits" a real `PreToolUse` deny instead of a suggestion an agent can be
talked out of, closing the same hole `browser_driver` closed for the MCP
policy.

A seventh rule, ``host_network`` (#400), blocks commands that change the
*host machine's* network configuration — for every guarded role, no
allowlist, same shape as ``host_destructive``. Root incident: a pane ran
`netsh wlan connect` to test a networking change and switched the host's
active Wi-Fi network, dropping the user (and every other pane's live
sessions) off the internet with zero warning. The host's network belongs to
the user sitting at the keyboard, never to a sandboxed pane — a pane that
needs a second network path should ask the user to bring a phone or a second
device, not repoint the machine's own adapter. Windows: `netsh wlan
connect|disconnect`, `netsh wlan add|delete profile`, `netsh interface
set/add/delete` (covers `ip`/`ipv4`/`ipv6` subcommand mutations),
`ipconfig /release|/renew`, `route add|delete|change`, `rasdial`, `netsh
winhttp set|reset proxy`. macOS: `networksetup -setairportnetwork
|-setairportpower|-setnetworkserviceenabled|-set*proxy*`, `ifconfig <if>
up|down`, `route add|delete`, `scutil --proxy`, all under `sudo` too (the
existing `_CMD_START` sudo branch covers it — same mechanism as
``host_destructive``). Read-only diagnostics stay allowed: `netsh wlan
show`, `ipconfig` alone, `route print`, `networksetup -getairportnetwork`,
`ifconfig` alone. `HOST_NETWORK_RULE_TEXT` is the prose counterpart, pinned
in role files by `tests/test_agent_role_files_have_host_network_guard.py`.
A denial also fires a best-effort, fire-and-forget notice to Lead (`cli.
cmd_guard`, via the same `progress` IPC path `takkub progress` uses) — the
severity here (user loses internet access with no warning) warrants Lead
knowing immediately, not just the blocked pane.

One carve-out: `git commit` (only — never push/reset/rebase/merge/checkout)
is allowed when the pane's cwd is inside a cockpit-managed
`.../worktrees/...` checkout. That is `--isolation worktree` (issue #81):
the pane owns a private branch nobody else touches, and
`orchestrator_text._append_worktree_hint` already instructs it to commit
there itself — "the 'wait for Lead' policy is for the shared tree only".
A hard block here would fight that already-shipped, intentional workflow
(and would have made this exact fix's own commit impossible). The
detection is a plain cwd substring match, not an import of
`worktree_manager` — this module stays a stdlib-only leaf (see top of this
docstring); `cli.cmd_guard` passes the hook payload's `cwd` field through.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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

# Prose handed to role files verbatim (#169). Kept in sync with the patterns
# below by tests/test_agent_role_files_have_host_destructive_guard.py.
HOST_DESTRUCTIVE_RULE_TEXT = (
    "ห้ามสั่ง kill process ด้วยชื่อ (image name / process name) — `taskkill /IM`, "
    "`pkill`, `killall`, PowerShell `Stop-Process -Name` ฆ่าทุก process ชื่อนั้นทั้งเครื่อง "
    "ไม่แยกว่าเป็นของ pane ตัวเองหรือไม่ (เคสจริง #169: `taskkill /F /T /IM node.exe` "
    "ฆ่า node ทั้งเครื่อง รวม teammate panes อื่น). "
    "Target เฉพาะ PID ที่ pane ตัวเอง spawn เอง — `taskkill /PID <pid>`, "
    "`Stop-Process -Id <pid>`, `kill <pid>` แทน."
)

# Prose handed to role files verbatim (#400). Kept in sync with the patterns
# below by tests/test_agent_role_files_have_host_network_guard.py.
HOST_NETWORK_RULE_TEXT = (
    "ห้ามสั่งเปลี่ยน network configuration ของเครื่อง host — network ของ host เป็นของ user "
    "ไม่ใช่ sandbox ของ pane (เคสจริง #400: pane รัน `netsh wlan connect` ทดสอบแล้ว user หลุดเน็ตทั้งเครื่อง "
    "ไม่มีเตือนล่วงหน้า). Windows: `netsh wlan connect/disconnect`, `netsh wlan add/delete profile`, "
    "`netsh interface set/add/delete` (รวม ip/ipv4/ipv6), `ipconfig /release` / `/renew`, "
    "`route add/delete/change`, `rasdial`, `netsh winhttp set/reset proxy`. "
    "macOS: `networksetup -setairportnetwork/-setairportpower/-setnetworkserviceenabled/-set*proxy*`, "
    "`ifconfig <if> up/down`, `route add/delete`, `scutil --proxy` (รวม sudo variant ทั้งหมด). "
    "ต้องการทดสอบผ่าน network เส้นอื่นจริงๆ → ขอให้ user ต่อมือถือ/อุปกรณ์ที่สองแทน อย่าแตะ network ของเครื่อง host เอง."
)

# Prose handed to role files verbatim (#202). Kept in sync with the patterns
# below by tests/test_agent_role_files_have_pip_editable_guard.py.
PIP_EDITABLE_RULE_TEXT = (
    "ห้าม `pip install -e .` / `--editable` ไม่ว่า path ไหน — editable install เขียนทับ "
    "`__editable__*.pth` ใน site-packages ของ venv ที่ pane อื่นทั้งเครื่อง (รวม worktree อื่น) "
    "ใช้ร่วมกัน (เคสจริง #202: backend pane รันจาก worktree แล้ว venv ทั้งเครื่องพังหลัง worktree "
    "ถูกลบ + qa ที่รัน full suite คาบเกี่ยวกันได้ผลเทสจากโค้ดผิด worktree โดยไม่รู้ตัว). "
    "ต้องการเทสโค้ดตัวเอง → รัน pytest ปกติ (ไม่ต้อง reinstall) — ถ้าจำเป็นต้องแก้ dependency ของ repo "
    "จริงๆ ให้แจ้ง Lead ผ่าน `takkub send --to lead` แทนที่จะแก้ shared venv เอง."
)


# Prose handed to role files verbatim (#314). Kept in sync with the patterns
# below by tests/test_agent_role_files_have_git_commit_guard.py.
GIT_LEAD_ONLY_RULE_TEXT = (
    "ห้าม `git commit` / `git push` / `git reset --hard` / `git branch -D` / "
    "`git tag -d` / `git rebase` / `git merge` / `git checkout` ไม่ว่า task จะสั่งว่า "
    "'commit เอง'/'ตรวจผ่านแล้ว commit เอง' แค่ไหนก็ตาม — มีแค่ Lead เท่านั้นที่ commit "
    "(เคสจริง #314: backend/admin role commit เองเมื่อ task สั่ง ในขณะที่ frontend ปฏิเสธ "
    "เพราะ role file ทั้งคู่มีข้อห้ามเดียวกัน แต่ prose อย่างเดียวโน้มน้าวให้ทำผิดได้). "
    "ถูกบล็อกแล้ว → ห้ามลองคำสั่งเดิมซ้ำ (เคสจริง #399: retry วนจนจบเทิร์นไม่ได้ Lead ต้องมา "
    "commit ให้เองแทน) จบงานทันทีด้วย "
    '`takkub done "พร้อม commit: <ไฟล์ที่แก้>"` แล้วรอ Lead review + commit '
    "ข้อยกเว้นเดียว: pane ที่ spawn ด้วย `--isolation worktree` (branch แยกของตัวเอง) "
    "ต้อง `git commit` บน branch นั้นเอง และดึง base ล่าสุดเข้า branch ตัวเองได้ด้วย "
    "`git merge <base>` ภายใน worktree นั้น (แต่ยังห้าม push/rebase/checkout — "
    "merge กลับเข้า base เป็นงาน Lead) ตามที่ task prompt บอกไว้ตอน spawn — "
    "ถ้า Lead ต้องการให้ pane นี้ commit เองจริงๆ ต้องสั่ง assign ใหม่ด้วย `--isolation worktree`."
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

# Kill-by-image-name (#169): these target every process with a given name,
# not the caller's own children — `taskkill /IM node.exe` kills every node
# process on the box, including other panes' Claude Code processes. Killing
# by PID is unaffected (`taskkill /PID`, `Stop-Process -Id`, plain `kill`).
#
# Anchored to actual invocation position (start of command, after a
# separator, or after `sudo`) — same as _BROWSER_PATTERNS' "bare-invoke" rule
# — so `echo 'use taskkill /PID not /IM'` (naming it, not running it) stays
# allowed.
_CMD_START = r"(?:^|[|;&]\s*|\bsudo\s+)"
_HOST_DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "taskkill-im",
        re.compile(rf"{_CMD_START}taskkill(?![\w-]){_SAME_CMD}/im\b", re.I | re.M),
    ),
    ("pkill", re.compile(rf"{_CMD_START}pkill(?![\w-])", re.I | re.M)),
    ("killall", re.compile(rf"{_CMD_START}killall(?![\w-])", re.I | re.M)),
    (
        "stop-process-name",
        re.compile(rf"{_CMD_START}Stop-Process(?![\w-]){_SAME_CMD}-Name\b", re.I | re.M),
    ),
)

# Host network reconfiguration (#400): the host machine's network belongs to
# the user at the keyboard, not a sandboxed pane. Root incident: a pane ran
# `netsh wlan connect` to test a networking change and dropped the whole
# machine off the internet with zero warning — every other pane's live
# session went with it. No allowlist: no guarded role legitimately needs to
# repoint the host's own network adapter (a pane that needs a second network
# path should ask the user to bring a phone/second device instead).
#
# `netsh interface set/add/delete` deliberately matches the mutating VERB
# rather than the `ip`/`ipv4`/`ipv6` subcommand family literally, so it
# catches every real spelling (`netsh interface ip set address`, `netsh
# interface ipv4 add address`, `netsh interface set interface "Wi-Fi" ...`)
# while `netsh interface show interface` (read-only) stays allowed — same
# add/delete/set-vs-show split `_HOST_DESTRUCTIVE_PATTERNS`' PID/name split
# and `route add/delete/change` (vs `route print`) below use.
_HOST_NETWORK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Windows — Wi-Fi profile switch/mutation
    (
        "netsh-wlan-connect",
        re.compile(
            rf"{_CMD_START}netsh(?![\w-]){_SAME_CMD}\bwlan\b{_SAME_CMD}"
            rf"\b(?:connect|disconnect)\b",
            re.I | re.M,
        ),
    ),
    (
        "netsh-wlan-profile",
        re.compile(
            rf"{_CMD_START}netsh(?![\w-]){_SAME_CMD}\bwlan\b{_SAME_CMD}"
            rf"\b(?:add|delete)\b{_SAME_CMD}\bprofile\b",
            re.I | re.M,
        ),
    ),
    # Windows — adapter/IP reconfiguration
    (
        "netsh-interface-mutate",
        re.compile(
            rf"{_CMD_START}netsh(?![\w-]){_SAME_CMD}\binterface\b{_SAME_CMD}"
            rf"\b(?:set|add|delete)\b",
            re.I | re.M,
        ),
    ),
    (
        "netsh-winhttp-proxy",
        re.compile(
            rf"{_CMD_START}netsh(?![\w-]){_SAME_CMD}\bwinhttp\b{_SAME_CMD}"
            rf"\b(?:set|reset)\b{_SAME_CMD}\bproxy\b",
            re.I | re.M,
        ),
    ),
    (
        "ipconfig-release-renew",
        re.compile(
            rf"{_CMD_START}ipconfig(?![\w-]){_SAME_CMD}/(?:release6?|renew6?)\b", re.I | re.M
        ),
    ),
    (
        "route-mutate",
        re.compile(rf"{_CMD_START}route(?![\w-]){_SAME_CMD}\b(?:add|delete|change)\b", re.I | re.M),
    ),
    ("rasdial", re.compile(rf"{_CMD_START}rasdial(?![\w-])", re.I | re.M)),
    # macOS — Wi-Fi / network service / proxy mutation
    (
        "networksetup-mutate",
        re.compile(
            rf"{_CMD_START}networksetup(?![\w-]){_SAME_CMD}"
            rf"-(?:setairportnetwork|setairportpower|setnetworkserviceenabled|set\w*proxy\w*)\b",
            re.I | re.M,
        ),
    ),
    (
        "ifconfig-updown",
        re.compile(rf"{_CMD_START}ifconfig(?![\w-]){_SAME_CMD}\b(?:up|down)\b", re.I | re.M),
    ),
    (
        "scutil-proxy",
        re.compile(rf"{_CMD_START}scutil(?![\w-]){_SAME_CMD}--proxy\b", re.I | re.M),
    ),
)

# Hand-rolled pane-polling loops (#287). `docs/lead/role-and-workflow.md` has
# called this "แพทเทิร์นต้องห้ามเด็ดขาด" since #242 — and Lead kept doing it,
# because prose was the ONLY layer: `lead` sits in `_UNGUARDED_ROLES`, so the
# one mechanism that actually enforces anything never saw its commands. Same
# two-layer lesson as the browser rule above, discovered the same way.
#
# Observed 2026-08-17 in the Lead pane, 4m53s into a foreground turn:
#
#     for i in $(seq 1 40); do s=$(takkub list | grep -E "^\s+backend\s" \
#       | awk '{print $2}'); if [ "$s" != "working" ]; then break; fi; \
#       sleep 20; done
#
# Up to 13 minutes parked in one Bash call. The cost is not the socket calls —
# it is that Lead cannot read anything while its turn is blocked, so the
# user's own queued messages sat unread behind it, AND the delivery pipeline
# saw Lead "busy" for the whole window (the very condition #279's
# busy-deliver escalation had to be invented to survive).
#
# All three signals are required together — loop construct, a takkub *status*
# read, and a sleep — so a legitimate one-shot fan-out
# (`for p in a b c; do takkub list --project $p; done`) and the blessed
# non-takkub verification polls (`until curl -sf localhost:3000; do sleep 2;
# done`, see docs/lead/patterns.md) both stay allowed.
_POLL_LOOP_LOOP = re.compile(
    r"(?:^|[|;&(]\s*|\bdo\s+)(?:for|while|until)(?![\w-])|\bForEach-Object\b|\b\d+\.\.\d+\b",
    re.I | re.M,
)
_POLL_LOOP_TAKKUB_READ = re.compile(
    r"(?<![\w-])takkub(?:\.(?:exe|cmd|bat))?(?![\w-])\s+(?:list|status|inbox|ledger)(?![\w-])",
    re.I,
)
_POLL_LOOP_SLEEP = re.compile(
    r"(?<![\w-])(?:sleep|Start-Sleep)(?![\w-])|(?<![\w-])timeout(?![\w-])\s+/t\b",
    re.I,
)

# A heredoc body is DATA handed to a program (`gh issue create --body <<'EOF'`,
# `git commit -F -`), not shell the pane executes — so the poll-loop rule must
# not read it as code. Found the moment the rule shipped: writing #287's own
# bug report, which quotes the offending loop verbatim, was denied by the rule
# it was documenting. A guard that makes its own incident report unwritable
# teaches panes to route around the guard.
#
# The exception is a heredoc fed to a shell (`bash <<'EOF' … EOF`), where the
# body genuinely IS executed — stripping that would be a one-line bypass of
# the whole rule, so those keep their body.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1(?P<body>.*?)^\s*\2\s*$", re.S | re.M)
_HEREDOC_SHELL_SINK = re.compile(
    r"(?<![\w-])(?:ba|z|k|da)?sh(?![\w-])|(?<![\w-])pwsh(?![\w-])"
    r"|(?<![\w-])python3?(?![\w-])|(?<![\w-])(?:eval|source)(?![\w-])",
    re.I,
)


def _strip_heredoc_bodies(cmd: str) -> str:
    """Blank out heredoc bodies that are data rather than shell code.

    The interpreter check looks at the text from the start of the heredoc's
    own line up to the `<<`, which is where the sink command sits.
    """

    def _replace(match: re.Match[str]) -> str:
        line_start = cmd.rfind("\n", 0, match.start()) + 1
        introducer = cmd[line_start : match.start()]
        if _HEREDOC_SHELL_SINK.search(introducer):
            return match.group(0)
        return match.group(0).replace(match.group("body"), "\n")

    return _HEREDOC.sub(_replace, cmd)


# Prose handed to role files verbatim (#287). Kept in sync with the patterns
# above by tests/test_lead_poll_loop_guard.py.
POLL_LOOP_RULE_TEXT = (
    "ห้ามเขียน loop เฝ้า pane เอง (`for`/`while`/`until` + `takkub list|status` + `sleep`) — "
    "ระหว่างที่ loop รันอยู่ turn ของ Lead ถูกบล็อกทั้งอัน อ่านอะไรไม่ได้เลย "
    "รวมถึง**ข้อความที่ user พิมพ์ค้างไว้** และ delivery pipeline ก็เห็น Lead ยุ่งตลอดช่วงนั้น "
    "(เคสจริง #287: loop เดียวกินไป 4 นาที 53 วินาที เพดาน 13 นาที). "
    "ค่าเริ่มต้นคือ **ไม่ต้องรอ — จบเทิร์นไปเลย** รายงาน done/FAILED จะถูกส่งเข้า pane ของ Lead "
    "แล้วปลุกเทิร์นใหม่เอง (นั่นคือหน้าที่ของ delivery pipeline ทั้งอัน). "
    "ถ้าจำเป็นต้องคาไว้จริงๆ (ไม่มีงานอื่นทำเลย และต้องขยับทันทีที่รายงานถึง) ใช้ "
    "`takkub wait [--role <r>]... [--timeout <s>]` — ตัวเดียวต่อ project, ตื่นเองเมื่อมี blocking report."
)

# pip/python -m pip install with -e/--editable, any target (#202): rewrites
# __editable__*.pth in the SHARED venv's site-packages to point at the
# caller's cwd — deadly when the caller is a `--isolation worktree` checkout
# that later gets deleted. No allowlist: no guarded role needs to reinstall
# the package into a venv every other pane shares.
_PIP_EDITABLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pip-install",
        re.compile(
            rf"{_CMD_START}(?:pip3?|python3?\s+-m\s+pip)(?![\w-]){_SAME_CMD}"
            rf"install{_SAME_CMD}(?:-e\b|--editable\b)",
            re.I | re.M,
        ),
    ),
)

# git subcommand gate (#314): "only Lead commits" — see module docstring for
# why prose alone wasn't enough. Flags between `git` and the subcommand are
# skipped ONLY when they look like bare flags (`-c foo=bar` style values are
# not consumed), so `git log --grep=commit` never matches (`log` sits where
# the subcommand is expected, "commit" only appears deep inside a flag
# value) while `git commit -m "..."` and `git -C dir commit` both do.
_GIT_SUBCMD_GAP = r"(?:\s+-{1,2}[\w-]+)*\s+"

# `git commit` alone is the one carve-out for a worktree-isolated pane (see
# module docstring) — kept separate from the no-exception rules below so
# `classify()` can gate it on `_is_worktree_cwd()`.
# `(?![\w-])` (not `\b`) after every subcommand name (#385): `\b` treats `-`
# as a boundary, so `merge\b` also matched `git merge-base` — a read-only
# ancestry lookup that a worktree pane runs to find how far it is behind
# base. The subcommand ends only at whitespace/end-of-command; a hyphenated
# longer name (`merge-base`, `merge-tree`, `merge-file`, `checkout-index`,
# `commit-tree`, `commit-graph`) is a different command and never a hit.
_SUBCMD_END = r"(?![\w-])"
_GIT_COMMIT_PATTERN = re.compile(
    rf"{_CMD_START}git(?![\w-]){_GIT_SUBCMD_GAP}commit{_SUBCMD_END}", re.M
)

# No allowlist, no cwd exception — push/reset --hard/branch -D/tag -d/rebase/
# checkout stay Lead-only even from an isolated worktree branch (the
# worktree carve-out is "commit your own branch", never "push it" or
# "rewrite/switch it"). `merge` is the one rule with a worktree carve-out
# (#385, `_GIT_MERGE_PATTERN` below).
_GIT_LEAD_ONLY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("push", re.compile(rf"{_CMD_START}git(?![\w-]){_GIT_SUBCMD_GAP}push{_SUBCMD_END}", re.M)),
    (
        "reset-hard",
        re.compile(
            rf"{_CMD_START}git(?![\w-]){_GIT_SUBCMD_GAP}reset{_SUBCMD_END}{_SAME_CMD}--hard\b", re.M
        ),
    ),
    (
        "branch-delete",
        re.compile(
            rf"{_CMD_START}git(?![\w-]){_GIT_SUBCMD_GAP}branch{_SUBCMD_END}{_SAME_CMD}-D\b", re.M
        ),
    ),
    (
        "tag-delete",
        re.compile(
            rf"{_CMD_START}git(?![\w-]){_GIT_SUBCMD_GAP}tag{_SUBCMD_END}{_SAME_CMD}-d\b", re.M
        ),
    ),
    (
        "rebase",
        re.compile(rf"{_CMD_START}git(?![\w-]){_GIT_SUBCMD_GAP}rebase{_SUBCMD_END}", re.M),
    ),
    (
        "checkout",
        re.compile(rf"{_CMD_START}git(?![\w-]){_GIT_SUBCMD_GAP}checkout{_SUBCMD_END}", re.M),
    ),
)

# `git merge` (#385): Lead-only on the shared tree, ALLOWED from inside a
# pane's own `--isolation worktree` checkout. There the pane's branch is the
# only thing that moves — merging base INTO it is how a second worktree pane
# picks up what a sibling already landed, without a Lead round-trip for
# every sync (the report's bottleneck). Merging the pane's branch INTO base
# happens on the shared tree, which this carve-out never covers.
_GIT_MERGE_PATTERN = re.compile(
    rf"{_CMD_START}git(?![\w-]){_GIT_SUBCMD_GAP}merge{_SUBCMD_END}", re.M
)

# A cockpit-managed `--isolation worktree` checkout always lives under
# `<DATA_HOME>/worktrees/<project>/<role>-<ts>/` (worktree_manager.worktree_dest).
# Matching the literal path segment — not importing worktree_manager — keeps
# this module free of the config/DATA_HOME import chain (stdlib-only leaf,
# see module docstring). Both slash styles: the hook payload's cwd is
# whatever the pane's OS reports (backslash on Windows, forward slash on
# macOS/Linux).
_WORKTREE_CWD = re.compile(r"[/\\]worktrees[/\\]", re.I)


def _is_worktree_cwd(cwd: str | None) -> bool:
    """True when *cwd* looks like an `--isolation worktree` checkout (#81),
    the one place a non-Lead role legitimately commits on its own branch."""
    return bool(cwd) and bool(_WORKTREE_CWD.search(cwd))


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


def classify(
    command: str,
    role: str | None,
    *,
    mb_fallback_check: Callable[[], bool] | None = None,
    cwd: str | None = None,
) -> Verdict:
    """Decide whether `role` may run `command`.

    Fail-open by design: an unknown/empty role means the CLI was invoked
    outside a cockpit pane (a human at a terminal), and an empty command
    means the hook payload was malformed. Neither is worth blocking on — the
    guard exists to stop an agent routing around policy, not to police
    people.

    *mb_fallback_check* (#304 point 3): an optional, lazily-called predicate
    the caller supplies — `cli.cmd_guard` passes one bound to `mcp_fallback.
    is_granted()`. Only invoked in the one mb-shard-deny branch below (never
    on every command), so this module's "stdlib only, no I/O" guarantee
    (see module docstring) holds for every OTHER rule — the fallback state
    itself lives in `mcp_fallback.py`, not here, precisely so this stays a
    pure leaf. `None` (the default, and every caller that doesn't pass one)
    behaves exactly as before: unconditional deny.

    *cwd* (#314): the hook payload's working-directory string, used only to
    tell an `--isolation worktree` pane apart from a shared-tree one for the
    `git commit` carve-out (see `_is_worktree_cwd`). `None`/missing defaults
    to "not a worktree" — i.e. `git commit` stays blocked, the same
    conservative direction every other rule here fails toward.
    """
    cmd = (command or "").strip()
    if not cmd:
        return Verdict(True)

    name = normalise_role(role)

    # #287: checked BEFORE the _UNGUARDED_ROLES exit on purpose — `lead` is the
    # role this rule exists for. It is the only role that owns teammates to
    # poll and the only one handed `takkub wait`, and it is exempt from every
    # rule below it, which is precisely why the prose ban survived from #242 to
    # #287 without ever binding. `shell` is included for the same reason (it
    # can reach the same CLI); an unknown/empty role still fails open, since
    # that means a human at a real terminal rather than a pane.
    #
    # A user typing this loop themselves via `!` would also be denied. That is
    # accepted: the verdict text says what to do instead, and rephrasing costs
    # a keystroke — whereas leaving the hole open is what produced #287.
    if name:
        code = _strip_heredoc_bodies(cmd)
        if (
            _POLL_LOOP_LOOP.search(code)
            and _POLL_LOOP_TAKKUB_READ.search(code)
            and _POLL_LOOP_SLEEP.search(code)
        ):
            return Verdict(
                False,
                rule="pane_poll_loop:takkub-status-sleep",
                reason=(
                    f"role `{name}` เขียน loop เฝ้า pane เองไม่ได้ (นโยบาย cockpit). "
                    f"{POLL_LOOP_RULE_TEXT}"
                ),
            )

    if not name or name in _UNGUARDED_ROLES:
        return Verdict(True)

    raw_role = (role or "").strip().lower()
    if "#" in raw_role and is_browser_role(name) and _MB_INVOKE.search(cmd):
        if mb_fallback_check is not None and mb_fallback_check():
            return Verdict(True)
        return Verdict(
            False,
            rule="browser_driver:mb-shard-cdp-9222",
            reason=(
                f"role `{raw_role}` ใช้ mb ไม่ได้: mb client hardcode CDP 9222 "
                "ทำให้ทุก shard ขับ Chrome ตัวเดียวกัน (#92). "
                "ใช้ Playwright MCP ที่ cockpit แยก profile ให้ต่อ shard แทน — "
                "ถ้า Playwright MCP ต่อไม่ติดจริง (#146/#304) ให้ขอ fallback ก่อน: "
                '`takkub mcp-fallback request --reason "..."`'
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

    for rule, pattern in _HOST_DESTRUCTIVE_PATTERNS:
        if pattern.search(cmd):
            return Verdict(
                False,
                rule=f"host_destructive:{rule}",
                reason=(
                    f"role `{name}` ใช้คำสั่งนี้ไม่ได้ (นโยบาย cockpit). {HOST_DESTRUCTIVE_RULE_TEXT}"
                ),
            )

    for rule, pattern in _HOST_NETWORK_PATTERNS:
        if pattern.search(cmd):
            return Verdict(
                False,
                rule=f"host_network:{rule}",
                reason=(f"role `{name}` ใช้คำสั่งนี้ไม่ได้ (นโยบาย cockpit). {HOST_NETWORK_RULE_TEXT}"),
            )

    for rule, pattern in _PIP_EDITABLE_PATTERNS:
        if pattern.search(cmd):
            return Verdict(
                False,
                rule=f"pip_editable:{rule}",
                reason=(f"role `{name}` ใช้คำสั่งนี้ไม่ได้ (นโยบาย cockpit). {PIP_EDITABLE_RULE_TEXT}"),
            )

    if not _is_worktree_cwd(cwd) and _GIT_COMMIT_PATTERN.search(cmd):
        return Verdict(
            False,
            rule="git_lead_only:commit",
            reason=(f"role `{name}` commit เองไม่ได้ (นโยบาย cockpit). {GIT_LEAD_ONLY_RULE_TEXT}"),
        )

    if not _is_worktree_cwd(cwd) and _GIT_MERGE_PATTERN.search(cmd):
        return Verdict(
            False,
            rule="git_lead_only:merge",
            reason=(f"role `{name}` ใช้คำสั่งนี้ไม่ได้ (นโยบาย cockpit). {GIT_LEAD_ONLY_RULE_TEXT}"),
        )

    for rule, pattern in _GIT_LEAD_ONLY_PATTERNS:
        if pattern.search(cmd):
            return Verdict(
                False,
                rule=f"git_lead_only:{rule}",
                reason=(f"role `{name}` ใช้คำสั่งนี้ไม่ได้ (นโยบาย cockpit). {GIT_LEAD_ONLY_RULE_TEXT}"),
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
