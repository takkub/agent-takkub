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

An eighth rule, ``full_suite`` (#528), blocks a raw, un-narrowed test-runner
invocation for every guarded role except `tester` (see below): `pytest`/
`python -m pytest` with no path/`-k`/`-m`, `vitest run` with no path, bare
`jest`, `turbo run test` with no `--filter`, `pnpm`/`yarn -r test`.
`#485`'s "targeted mid-batch,
full gate once via `takkub qa-gate --auto`" was prose-only in the root
CLAUDE.md every pane already reads — nothing technical stopped a pane (or
Lead, mid-task) from reaching for the raw runner anyway, repeatedly pinning
the user's box at 100% CPU/RAM across sessions. `takkub qa-gate` itself is
never caught by this: `qa_gate.py` calls `subprocess.run` directly inside the
CLI process, never through a Bash tool call this hook ever observes, so
gating the raw path here cannot also gate the gate's own internal runs — no
exception needed in the pattern set for that. `FULL_SUITE_RULE_TEXT` is the
prose counterpart pointing at `takkub qa-gate --targeted <paths>`.

One role IS allowlisted for this specific rule: `tester` (optional, spawned
on demand — `.claude/agents/tester.md`), whose entire job is running a raw
test suite on its own pane so other roles don't each fork one in parallel.
It stays subject to every other rule in this module (version control,
browser driving, host-destructive commands, ...) — the exemption is narrow
to `_full_suite_rule` alone.

A ninth rule (#609) extends `git_lead_only` to `git stash` (any subcommand
except the read-only `list`/`show`), `git restore`, and `git clean -f*` on the
shared tree. Root incident: a `frontend` pane ran `git stash && vitest ...;
git stash pop` on the shared tree while a `backend` pane had ~165 files of
uncommitted work in progress — every dirty file's mtime changed and the
backend pane's work was one `git stash drop` away from gone. `git checkout --
<path>` and `git reset --hard` were already covered by the existing
`checkout`/`reset-hard` patterns above; `stash`/`restore`/`clean -f*` were the
gap. Same worktree carve-out as `reset-hard`/`checkout`/`branch-delete`
(#545): safe unconditionally inside the pane's own `--isolation worktree`
checkout, since nothing there is shared with another pane. Deliberately NOT
gated on "does dirty state belong to another pane" (a #601-style ownership
diff would need a `git status` subprocess on every guarded Bash call, which
this stdlib-only leaf hook — fired on EVERY Bash call — can't afford) — shared
tree + non-readonly stash/restore/clean is blocked outright, mirroring how
`reset-hard`/`checkout`/`branch-delete` already ask no ownership question
either.

Carve-outs for `--isolation worktree`: `git commit` is allowed unconditionally
when the pane's cwd is inside a cockpit-managed `.../worktrees/...` checkout;
`git push` is allowed ONLY when every target it names is that pane's own
`wt/<role>-<ts>` branch (#438 — see `_push_is_own_worktree_branch`), never a
bare/unnamed push and never any other branch. `reset --hard`/`checkout`/
`branch -D` are likewise allowed unconditionally from inside that same
checkout (#545 — see `_WORKTREE_SAFE_RULES`): that checkout is disposable by
definition, so blocking them there protects nothing and only forces worse
workarounds (a real incident: `merge` used in place of `reset --hard` to
rewrite a branch, `git archive | tar -x` used in place of `checkout` to
materialize a different ref). `rebase`/`tag -d` stay Lead-only with no
exception — both act on refs a worktree pane can't safely disown its own copy
of. `merge` has its own narrow carve-out (merging the CURRENT base into the
pane's own branch only — see `_GIT_MERGE_PATTERN`). That is `--isolation
worktree` (issue #81):
the pane owns a private branch nobody else touches, and
`orchestrator_text._append_worktree_hint` already instructs it to commit
there itself — "the 'wait for Lead' policy is for the shared tree only".
A hard block here would fight that already-shipped, intentional workflow
(and would have made this exact fix's own commit impossible). The
detection is a plain cwd substring match, not an import of
`worktree_manager` — this module stays a stdlib-only leaf (see top of this
docstring); `cli.cmd_guard` passes the hook payload's `cwd` field through.

A tenth rule, ``git_shared_default_deny`` (#609 round 4), flips the shared-
tree posture from deny-list to default-deny. Rounds 1-3 each closed a real
bypass by adding one more subcommand to `_GIT_LEAD_ONLY_PATTERNS` — round 3's
own re-verify (`docs/audit/2026-09-15-batch-2.1.9-review.md`, "Round 3")
found three more (`git apply`/`apply -R`, `git read-tree --reset -u`, a
directory junction planted INSIDE a pane's own worktree pointing at the
shared tree) in the same shape: a subcommand nobody had thought to deny yet.
Chasing individual subcommands is an unbounded list. Instead, once a git
invocation's effective target resolves to somewhere this role's worktree
does NOT own (`in_worktree` is False — the shared tree, or another role's
worktree), only an explicit allow-list of subcommands that provably never
mutate another pane's working tree/index/refs is permitted — see
`_GIT_SHARED_TREE_ALLOW_PLAIN`/`_GIT_SHARED_TREE_RESTRICTED_CHECKS` and
`_git_shared_tree_deny_rule`. Everything else (`apply`, `read-tree`,
`write-tree`, `checkout-index`, `cherry-pick`, `revert`, `pull`, `am`,
`submodule`, `sparse-checkout`, `prune`, an unrecognised future verb, ...) is
denied by default rather than waiting for its own incident. `commit`/`push`/
`merge`/`stash`/`checkout`/`restore`/`switch`/`rebase` are skipped here
(`_GIT_SHARED_TREE_HANDLED_ELSEWHERE`) since each already has its own
carve-out-aware verdict earlier in `classify()` — this rule never
second-guesses those. A few subcommands the existing deny-list only
partially covered (`branch`, `tag`, `clean`, `config`, plumbing reads like
`commit-tree`/`merge-tree`) are kept fully permissive here, matching their
pre-round-4 behavior exactly, because `tests/test_pane_guard.py` already
pins them allowed and none of them mutates another pane's working tree
(`branch -D`/`tag -d`/`clean -f*` stay denied unconditionally by their own
existing, more specific patterns above, which run first).

Ownership itself also got more careful this round: `_worktree_role_owns`
used to check the CALLER-given path's text for a `<role>-<ts>` segment
without ever resolving where that path actually leads on disk. A directory
junction (`mklink /J`, no admin required on Windows) planted inside a pane's
own worktree but pointing at the shared tree — or at a sibling role's
worktree — has the right text (it lives under `.../worktrees/<role>-<ts>/…`)
while actually landing somewhere this role does not own; the pre-round-4
check granted the carve-out anyway. Every path fed to `_worktree_role_owns`
now goes through `_safe_realpath` first (`os.path.realpath`, which resolves
symlinks *and* Windows junctions since Python 3.8), so ownership is judged
on where the path actually leads, not its literal spelling.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

# Roles allowed to drive a browser. `qa` owns e2e/smoke; `critic` and
# `designer` need to look at rendered pages for visual review. Everyone else
# writes unit tests and hands browser verification to qa — which is what
# `.claude/agents/frontend.md` already says ("integration/e2e เป็นหน้าที่ QA").
# #433 (user directive 2026-08-29): `frontend` and `mobile` are browser roles
# too — a UI change must be self-verified with a real screenshot by the pane
# that made it (mobile 390px + desktop 1440px, path in the done note) instead
# of every UI task costing a second qa round. qa keeps regression / e2e /
# cross-model review; it is no longer the visual check for fresh UI work.
BROWSER_ROLES: frozenset[str] = frozenset({"qa", "critic", "designer", "frontend", "mobile"})

# Roles whose `takkub done` is gated on screenshot evidence for UI-shaped
# tasks (`orchestrator_text.ui_evidence_gate`, #433).
UI_SELF_VERIFY_ROLES: frozenset[str] = frozenset({"frontend", "mobile"})

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

# Prose for the UI roles (#433) — the permission half plus the self-verify
# duty. Pinned into `.claude/agents/frontend.md` / `mobile.md` by
# tests/test_agent_role_files_have_browser_guard.py.
UI_SELF_VERIFY_RULE_TEXT = (
    "งาน UI ทุกชิ้นต้อง self-verify ก่อน `takkub done`: รัน app ขึ้นมาจริง จับ screenshot "
    "ของทุกหน้า/คอมโพเนนต์ที่แตะ ที่ mobile 390px และ desktop 1440px ดูด้วยตาเทียบกับโจทย์ "
    "บันทึกลง `$TAKKUB_ARTIFACTS_DIR/screenshots/` แล้วใส่ path ของภาพ (บรรทัดละไฟล์ ไม่ embed รูป) "
    "ใน done note — done ที่ไม่มี path ภาพจริง หรือเขียนว่า 'ยังไม่ได้เปิดจริง / route ไป qa' จะถูกปฏิเสธ "
    "(งานที่ไม่มีผลต่อหน้าจอเลย ใส่ `[no-ui]` ใน note)."
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
    "`git tag -d` / `git rebase` / `git merge` / `git checkout` / `git stash` "
    "(ยกเว้น `list`/`show`) / `git restore` / `git clean -f*` ไม่ว่า task จะสั่งว่า "
    "'commit เอง'/'ตรวจผ่านแล้ว commit เอง' แค่ไหนก็ตาม — มีแค่ Lead เท่านั้นที่ commit "
    "(เคสจริง #314: backend/admin role commit เองเมื่อ task สั่ง ในขณะที่ frontend ปฏิเสธ "
    "เพราะ role file ทั้งคู่มีข้อห้ามเดียวกัน แต่ prose อย่างเดียวโน้มน้าวให้ทำผิดได้). "
    "ถูกบล็อกแล้ว → ห้ามลองคำสั่งเดิมซ้ำ (เคสจริง #399: retry วนจนจบเทิร์นไม่ได้ Lead ต้องมา "
    "commit ให้เองแทน) จบงานทันทีด้วย "
    '`takkub done "พร้อม commit: <ไฟล์ที่แก้>"` แล้วรอ Lead review + commit '
    "ข้อยกเว้นเดียว: pane ที่ spawn ด้วย `--isolation worktree` (branch แยกของตัวเอง) "
    "ต้อง `git commit` บน branch นั้นเอง และดึง base ล่าสุดเข้า branch ตัวเองได้ด้วย "
    "`git merge <base>` ภายใน worktree นั้น และ push ได้**เฉพาะ branch wt/<role>-* ของตัวเอง "
    "แบบระบุชื่อ** (`git push -u origin wt/<role>-<ts>` — ห้าม force/delete, #438) "
    "เพื่อให้ CI ตรวจ branch ได้ก่อน done (แต่ยังห้าม rebase/checkout — "
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
#
# `rtk` is the mandated command-runner prefix for every pane's Bash call —
# root CLAUDE.md: "Golden rule — always prefix shell commands with `rtk`"
# (it applies its own filter to commands it recognises and passes everything
# else through unchanged; `rtk proxy <cmd>` explicitly bypasses filtering
# for debugging). Every rule sharing this constant only ever recognised a
# command at true start-of-string, right after a shell separator, or right
# after `sudo ` — never after that mandated prefix. #466: confirmed this let
# `rtk git push origin main --force`, `rtk taskkill /F /T /IM node.exe`,
# `rtk pip install -e .` etc sail straight through unblocked — the
# org-mandated prefix silently defeated host_destructive (#169),
# host_network (#400), pip_editable (#202) and git_lead_only (#314/#438)
# alike, for every guarded role that actually followed the golden rule.
# `_RTK_PREFIX` lets one optional `rtk [proxy] ` wrapper sit between the
# anchor and the real verb, exactly like `sudo ` already does.
_RTK_PREFIX = r"(?:rtk(?:\.(?:exe|cmd|bat|ps1))?(?![\w-])\s+(?:proxy(?![\w-])\s+)?)?"
# #609 round 3: a bare `VAR=value` env-assignment prefix (`GIT_DIR=<path> git
# stash drop`, no `env` keyword) sat outside every rule's reach the same way
# an unwrapped shell wrapper once did — `_CMD_START` only ever tolerated an
# optional `sudo `/`rtk [proxy] ` prefix between the anchor and the real
# verb, never a raw env-var assignment, so the literal `git` token was never
# reachable at position 0 (confirmed live: `GIT_DIR=<path> git stash drop`
# run via git-bash dropped a stash entry belonging to an unrelated repo —
# `classify` never even recognised `git` was being invoked). Kept
# independent of `_unwrap_segment`'s existing `env VAR=value` stripping
# (which DELETES the prefix from `cmd`, losing the value
# `_git_dir_env_override` below needs) — this only widens where a match can
# START; the text itself stays intact for that later scan.
# `env` keyword optional per repetition (not just once overall) so `env
# FOO=1 GIT_DIR=x git …` and a bare `GIT_DIR=x git …` are both consumed the
# same way; `_unwrap_segment` already peels an `env VAR=value` prefix off
# too, but ONLY when a recognised wrapper sits behind it — a plain `env
# GIT_DIR=x git stash drop` has no wrapper behind the prefix, so
# `_unwrap_segment` deliberately gives up and returns the segment
# unchanged (see its docstring), leaving `_CMD_START` as the only place
# left that can still recognise `git` past this prefix.
_ENV_ASSIGN_PREFIX = r"(?:(?:env\s+)?[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)*"
_CMD_START = rf"(?:^|[|;&]\s*|\bsudo\s+){_ENV_ASSIGN_PREFIX}{_RTK_PREFIX}"
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

# #632: Block `python -m agent_takkub ...` for specialist roles. Specialist
# commands must be called directly with `takkub <cmd>` (which is on PATH).
# Running `python -m agent_takkub` can boot the GUI app rather than CLI.
# Submodules like `python -m agent_takkub.foo` remain allowed.
_PYTHON_M_AGENT_TAKKUB = re.compile(
    r"""(?:^|[;&|]\s*|\bexec\s+)(?:[\w.\\/:-]*[/\\])?(?:python[0-9.]*(?:\.exe)?|py(?:\.exe)?)\s+(?:-[a-zA-Z0-9_-]+(?:\s+[^\s-]+)?\s+)*-m\s+["']?agent_takkub["']?(?:\s+|$|;)""",
    re.IGNORECASE,
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

# Raw full-suite test runner invocations (#528). #485 said "targeted mid-
# batch, full gate once via `takkub qa-gate --auto`" in prose only — nothing
# technical stopped a pane from typing the raw runner itself. Repeat offense
# across sessions: a pane (or Lead, mid-task) reaches for `pytest` / `vitest
# run` / `pnpm -r test` with no narrowing, forking every worker the runner
# owns and pinning the box at 100% CPU/RAM for everyone else on it — the same
# shape of harm `host_destructive` exists for, just from a tool instead of a
# kill command. `takkub qa-gate` itself never trips this: `qa_gate.py` calls
# `subprocess.run` directly inside the CLI process, never through a Bash tool
# call this hook ever sees — so gating a raw runner here cannot also gate the
# gate's own internal narrowed/full runs, by construction, no exception coded.
#
# Each rule's *tail* — captured with the SAME `[^\n|;&]*` used by `_GIT_PUSH_
# TAIL` above, so a follow-on command past `&&`/`;`/`|` is never read as this
# command's own arguments — decides narrow vs full via `_tail_is_narrow`: any
# non-flag positional token that is not a bare `.`/`./`/`*` (a real path or
# node id), or a recognised filter flag (`-k`/`-m` for pytest, `-t`/
# `--testNamePattern`/`--testPathPattern`/`--grep` for jest/vitest/mocha-
# shaped runners, `--filter` for turbo/pnpm workspace scoping) counts as
# targeted. A file/dir path or `-k EXPR` is exactly the shape
# `docs/qa-gate-policy.md` and this project's own sessions already use
# (`pytest tests/test_x.py tests/test_y.py -k 'foo or bar'`) — that must stay
# allowed, only the zero-argument reflex is what caused the incidents.
_NARROW_FLAG = re.compile(
    r"^(?:-k|-m|-t|--testNamePattern|--testPathPattern|--grep|--filter)(?:=.*)?$", re.I
)


def _tail_is_narrow(tail: str) -> bool:
    """True when *tail* (the text after the test-runner invocation) carries
    a real path/node-id argument or an explicit filter flag (`-k`/`-m` match
    `_NARROW_FLAG` as a bare token — pytest always takes the expression as a
    separate token, never `-kEXPR`) — see the `_FULL_SUITE_*` block above for
    the full contract."""
    for tok in tail.split():
        if tok.startswith("-"):
            if _NARROW_FLAG.match(tok):
                return True
            continue
        if tok not in (".", "./", "*"):
            return True
    return False


_PYTEST_INVOKE = re.compile(
    rf"{_CMD_START}(?:pytest|py\.test|python3?\s+-m\s+pytest)(?![\w-])(?P<tail>[^\n|;&]*)",
    re.I | re.M,
)
_PM_RUNNER_PREFIX = r"(?:(?:npx|bunx)\s+|(?:npm|pnpm|yarn|bun)\s+(?:exec|dlx)\s+)?"
_VITEST_RUN_INVOKE = re.compile(
    rf"{_CMD_START}{_PM_RUNNER_PREFIX}vitest(?![\w-]){_SAME_CMD}\brun\b(?P<tail>[^\n|;&]*)",
    re.I | re.M,
)
_JEST_INVOKE = re.compile(
    rf"{_CMD_START}{_PM_RUNNER_PREFIX}jest(?![\w-])(?P<tail>[^\n|;&]*)", re.I | re.M
)
_TURBO_RUN_TEST = re.compile(
    rf"{_CMD_START}turbo(?![\w-]){_SAME_CMD}\brun\b{_SAME_CMD}\btest\b(?P<tail>[^\n|;&]*)",
    re.I | re.M,
)
_PM_RECURSIVE_TEST = re.compile(
    rf"{_CMD_START}(?:pnpm|yarn)(?![\w-]){_SAME_CMD}"
    rf"(?:-r\b|--recursive\b|\bworkspaces\s+run\b){_SAME_CMD}\btest\b(?P<tail>[^\n|;&]*)",
    re.I | re.M,
)

FULL_SUITE_RULE_TEXT = (
    "ห้ามรัน test runner แบบเต็ม suite ตรงๆ ผ่าน Bash เอง — `pytest`/`python -m pytest` "
    "ไม่มี target แคบ, `vitest run` ไม่มี path, `jest` เปล่า, `turbo run test` ไม่มี "
    "`--filter`, `pnpm`/`yarn -r test` (workspace ทั้งหมด) ล้วนกิน CPU/RAM ทุก worker "
    "จนเครื่อง user ค้างทั้งเครื่อง (#485/#528 — กติกาเดิมเป็นแค่ text ไม่มีตัวบังคับ). "
    "ใช้ `takkub qa-gate --targeted <paths>` แทน — full gate เต็ม suite เป็นหน้าที่ qa "
    "ครั้งเดียวท้าย batch เท่านั้น (`takkub qa-gate --auto`). ต้องการรัน test runner ตรงๆ "
    "จริงๆ ให้ระบุ path/pattern แคบตรงกับไฟล์ที่แก้เสมอ เช่น "
    "`pytest tests/test_x.py -k 'foo or bar'`, `vitest run src/x.test.ts`, "
    "`jest src/x.test.js`, `turbo run test --filter=<pkg>`."
)

_FULL_SUITE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pytest", _PYTEST_INVOKE),
    ("vitest-run", _VITEST_RUN_INVOKE),
    ("jest", _JEST_INVOKE),
    ("turbo-run-test", _TURBO_RUN_TEST),
    ("pm-recursive-test", _PM_RECURSIVE_TEST),
)


def _full_suite_rule(cmd: str) -> str | None:
    """First matched raw-full-suite rule name in *cmd*, or None when every
    match found is narrowly targeted (or there is no match at all)."""
    for rule, pattern in _FULL_SUITE_PATTERNS:
        m = pattern.search(cmd)
        if m and not _tail_is_narrow(m.group("tail")):
            return rule
    return None


# #585: When task scope is "tiny", full-suite and takkub qa-gate are denied.
_PM_TEST = re.compile(
    rf"{_CMD_START}(?:npm|pnpm|yarn|bun)(?![\w-])\s+(?:run\s+)?test\b(?P<tail>[^\n|;&]*)",
    re.I | re.M,
)
_TAKKUB_QA_GATE = re.compile(
    rf"{_CMD_START}(?:python3?\s+-m\s+agent_takkub\.cli|takkub)\s+qa-gate\b",
    re.I | re.M,
)
SCOPE_TINY_DENY_TEXT = "งานนี้ถูกตีเป็นงานเล็ก — ถ้าจำเป็นต้อง gate จริง ให้ takkub progress ขอ Lead ปรับ scope"


# #585 round 2: Heavy build and test suite detection for busy-machine gate
def _is_heavy_build_or_suite(cmd: str) -> tuple[bool, str]:
    """Check if *cmd* is a heavy build or full test suite command (#585 round 2)."""
    if _TAKKUB_QA_GATE.search(cmd):
        return True, "qa_gate"
    fs = _full_suite_rule(cmd)
    if fs:
        return True, fs
    pm_t = _PM_TEST.search(cmd)
    if pm_t and not _tail_is_narrow(pm_t.group("tail")):
        return True, "pm_test"
    m_build = re.search(
        rf"{_CMD_START}(?:npm|pnpm|yarn|bun)(?![\w-])\s+(?:run\s+)?build\b", cmd, re.I | re.M
    )
    if m_build:
        return True, "pm_build"
    if re.search(rf"{_CMD_START}(?:npx\s+)?next\s+build\b", cmd, re.I | re.M):
        return True, "next_build"
    if re.search(rf"{_CMD_START}(?:npx\s+)?vite\s+build\b", cmd, re.I | re.M):
        return True, "vite_build"
    m_tsc = re.search(rf"{_CMD_START}(?:npx\s+)?tsc(?![\w-])(?P<tail>[^\n|;&]*)", cmd, re.I | re.M)
    if m_tsc:
        tail = m_tsc.group("tail")
        if re.search(r"\b-(?:b|p|-build|-project)\b", tail, re.I) or not re.search(
            r"\b[\w./\\-]+\.tsx?\b", tail, re.I
        ):
            return True, "tsc_project"
    m_pw = re.search(
        rf"{_CMD_START}(?:npx\s+)?playwright\s+test\b(?P<tail>[^\n|;&]*)", cmd, re.I | re.M
    )
    if m_pw and not _tail_is_narrow(m_pw.group("tail")):
        return True, "playwright_test"
    return False, ""


def _load_fresh_busy_snapshot(path: pathlib.Path) -> dict | None:
    """Read *path* and return its dict iff it exists and is fresh (<=30s old
    by mtime, and by any `saved_at`/`ts` field it carries) — else None.

    Shared by `_is_machine_busy_from_snapshot`'s two sources (#587 B1):
    `machine-state.json` (a bare epoch `ts` field) and `last-session.json`
    (an ISO `saved_at` field). Either field is checked when present so one
    function covers both schemas without the caller knowing which it got.
    """
    if not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime
        if (time.time() - mtime) > 30.0:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    saved_at = data.get("saved_at")
    if saved_at:
        try:
            dt = datetime.fromisoformat(saved_at)
            if (datetime.now() - dt).total_seconds() > 30.0:
                return None
        except Exception:
            pass

    ts = data.get("ts")
    if ts is not None:
        try:
            if (time.time() - float(ts)) > 30.0:
                return None
        except Exception:
            pass

    return data


def _is_machine_busy_from_snapshot(
    role: str,
    snapshot_path: pathlib.Path | None = None,
    machine_state_path: pathlib.Path | None = None,
) -> tuple[bool, str]:
    """Check if the machine is busy from a recent session snapshot (#585
    round 2; #587 B1 adds the machine-state.json source).

    Reads the light, frequently-refreshed `machine-state.json` first (see
    that file's module comment in orchestrator.py for why) and falls back to
    the heavier, more-throttled `last-session.json` only when the former is
    missing or stale. `snapshot_path` alone (no `machine_state_path`) skips
    the machine-state.json lookup entirely and checks only that one file —
    the exact pre-#587 single-file contract every existing caller/test
    already relies on; passing `machine_state_path` explicitly (tests) opts
    a call back into the two-source chain against caller-chosen paths
    instead of the real `RUNTIME_DIR`.
    """
    from .config import RUNTIME_DIR

    data: dict | None = None
    if machine_state_path is not None:
        data = _load_fresh_busy_snapshot(machine_state_path)
    elif snapshot_path is None:
        data = _load_fresh_busy_snapshot(RUNTIME_DIR / "machine-state.json")
    if data is None:
        data = _load_fresh_busy_snapshot(
            snapshot_path if snapshot_path is not None else (RUNTIME_DIR / "last-session.json")
        )
    if data is None:
        return False, ""

    # Condition 1: Other pane is working
    working_panes = data.get("working_panes")
    if working_panes is None:
        working_panes = []
        for entries in (data.get("projects") or {}).values():
            for e in entries:
                if e.get("state") == "working":
                    working_panes.append(e.get("role"))

    norm_role = normalise_role(role)
    other_working = [r for r in working_panes if normalise_role(r) != norm_role]
    if other_working:
        return True, f"มี pane อื่นกำลังทำงาน ({', '.join(other_working)})"

    # Condition 2: RAM or CPU exceeds governor ceiling
    if bool(data.get("overloaded")):
        return True, "ระบบอยู่ในสถานะ overloaded"
    ram_pct = data.get("ram_percent")
    if ram_pct is not None and ram_pct >= 85.0:
        return True, f"RAM สูง ({ram_pct:.1f}%)"
    cpu_pct = data.get("cpu_percent")
    if cpu_pct is not None and cpu_pct >= 90.0:
        return True, f"CPU สูง ({cpu_pct:.1f}%)"

    return False, ""


def _is_note_exempt(file_path: str) -> bool:
    """#587 A2: a `*.md`/`*.txt` note is never counted toward, or denied by,
    Lead's direct-edit caps (carve-out #474 already allows these in prose;
    this makes the guard agree instead of pushing Lead to write them
    through Bash to dodge `lines_per_call`)."""
    lower = file_path.replace("\\", "/").rstrip("/").lower()
    return lower.endswith(".md") or lower.endswith(".txt")


def _is_direct_edit_exempt(file_path: str, cwd: str | None, project: str | None) -> bool:
    """#587 A2/F2: paths that must never be counted toward, or denied by,
    Lead's direct-edit caps — cockpit's own runtime state under
    `config.RUNTIME_DIR` (any depth), a `runtime/` directory sitting
    directly at one of the project's configured roots (its own state, not
    project source), and anything outside the project's configured paths
    entirely (scratchpad, memory files under `~/.claude-work/`, etc —
    confirmed live: Lead writing its OWN task-spec file to scratchpad was
    denied at 62 lines even though it is neither source nor even inside
    the project).

    F2 fix: the runtime carve-out used to match ANY `runtime` path segment
    anywhere (`"runtime" in segments`), so a project with real source at
    `src/runtime/app.py` had that file exempted from every cap — narrowed
    to only a `runtime` segment immediately under a known root.

    Deliberately does NOT check `.md`/`.txt` — that carve-out
    (`_is_note_exempt`) and the deep-file-category deny both run in
    `evaluate_lead_direct_edit` BEFORE this function (#587 F3), so a
    repo-level file like `package.json` that happens to live outside every
    configured root still hits the deep-category deny instead of slipping
    past via this exemption.

    Only narrows what counts — a file that IS project source is unaffected.
    """
    resolved = pathlib.Path(file_path)
    if not resolved.is_absolute() and cwd:
        resolved = pathlib.Path(cwd) / resolved
    try:
        resolved = resolved.resolve()
    except OSError:
        return False  # unresolvable path — don't exempt, keep prior (counted) behavior

    try:
        from .config import RUNTIME_DIR

        resolved.relative_to(RUNTIME_DIR.resolve())
        return True  # cockpit's own runtime state, anywhere under it
    except (ValueError, OSError):
        pass

    roots: list[pathlib.Path] = []
    if project:
        try:
            from .lead_context import _allowed_project_roots

            roots = _allowed_project_roots(project)
        except Exception:
            roots = []
    if not roots and cwd:
        try:
            roots = [pathlib.Path(cwd).resolve()]
        except OSError:
            roots = []
    if not roots:
        return False  # no project and no cwd to compare against — keep counting

    for root in roots:
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        # inside a known project root — exempt only its own top-level
        # `runtime/` dir, not project source anywhere else under it.
        return bool(rel.parts) and rel.parts[0].lower() == "runtime"
    return True  # outside every known root


# #611 M1: a REAL test folder is a trustworthy exemption on its own — the
# `.spec.`/`.test.` filename SUFFIX alone is not, when a sensitive folder
# also sits in the path. Confirmed live: `src/auth/verify.spec.ts` (no real
# test folder, suffix only) let `return verifySignature(value)` become
# `return true` straight past this exemption, because the old regex treated
# the suffix as sufficient on its own — a `.spec.ts` file can be imported by
# a production entrypoint exactly like any other module; the filename
# proves nothing.
_DEEP_TEST_FOLDER_EXEMPT = re.compile(
    r"(?:^|/)(?:tests?|__tests__|e2e|[\w-]*-e2e|spec)(?:/|$)", re.I
)
_DEEP_TEST_SUFFIX_EXEMPT = re.compile(r"\.(?:spec|test)\.[^./]+$", re.I)
_SENSITIVE_PATH_SEGMENTS = frozenset(
    {"auth", "security", "payment", "payments", "crypto", "token", "tokens"}
)


def _normalize_identifier_boundaries(text: str) -> str:
    """#628: Split camelCase/PascalCase word transitions and treat underscores
    as word delimiters so sensitive identifiers (check_auth_token, refresh_token,
    api_auth, checkAuthToken, isAdminUser, bypass_auth, etc.) produce true word
    boundaries for regex word matching."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    s = s.replace("_", " ")
    return s.lower()


def _is_deep_test_path(norm_file: str) -> bool:
    """True when `norm_file` (already lowercased, `/`-separated) is trusted
    enough to skip the sensitive-keyword PATH leg below (#611/#611-M1) —
    unconditionally for a real test/e2e folder, or for a bare `.spec.`/
    `.test.` filename suffix ONLY when no sensitive folder SEGMENT (a whole
    path component — not a substring of the filename itself, so
    `src/payments.test.ts`'s filename doesn't count) sits in the path."""
    if _DEEP_TEST_FOLDER_EXEMPT.search(norm_file):
        return True
    if not _DEEP_TEST_SUFFIX_EXEMPT.search(norm_file):
        return False
    segments = norm_file.split("/")[:-1]
    return not any(
        seg in _SENSITIVE_PATH_SEGMENTS
        or any(
            re.search(rf"\b{s}\b", _normalize_identifier_boundaries(seg))
            for s in _SENSITIVE_PATH_SEGMENTS
        )
        for seg in segments
    )


def _direct_edit_diff_text(tool_name: str, tool_input: dict) -> str:
    """The text Lead is actually about to write (#611) — old+new for Edit,
    full content for Write — used by the sensitive-keyword deep-category
    check below so a genuine secret/auth change hiding inside a test file
    still denies even though its PATH is exempt."""
    if tool_name == "Edit":
        old_str = str(tool_input.get("old_string") or "")
        new_str = str(tool_input.get("new_string") or "")
        return f"{old_str}\n{new_str}"
    return str(tool_input.get("content") or "")


def evaluate_lead_direct_edit(
    tool_name: str,
    tool_input: dict,
    *,
    cwd: str | None = None,
    project: str | None = None,
    scope: str | None = None,
    state_file: pathlib.Path | None = None,
) -> Verdict:
    """Evaluate whether Lead is permitted to perform a direct Edit/Write on source code (#585 round 2).

    Conditions (all required):
    1. scope = tiny (if scope is known and not tiny -> deny)
    2. <= 15 lines touched in this tool call (Edit: max(old, new) lines; Write: diff vs existing file)
    3. Cumulative <= 2 distinct files per task
    4. Cumulative <= 30 lines per task
    5. NOT in deep category (schema, migration, auth, security, tokens/secrets, crypto, payment, infra, lockfiles)

    None of the above applies to a `.md`/`.txt` note (`_is_note_exempt`) or
    when the project's team preset has `lead_may_implement=True`
    (``solo-lead``/``pair``) — a prior `lead_context` function used to reach
    the same outcome (Edit/Write allowed unconditionally) through a settings
    file that spawn never actually wired in, and has since been removed.
    This is the ONLY place that guarantee is enforced now.

    The structural deep-category deny runs BEFORE the runtime/outside-root
    exemption (#587 F3) — a repo-level file like `package.json` that happens
    to live outside every configured project root must still be denied, not
    silently pass through the exemption meant for scratchpad/memory notes.
    Only after structural deep patterns does `_is_direct_edit_exempt` allow
    scratchpad/runtime files unconditionally (#625), ahead of sensitive-keyword
    patterns (#628) and the scope/line/cumulative caps below.

    #611/#628: the "sensitive keyword" half of condition 5 (auth/security/token/
    crypto/payment — see `sensitive_deep_patterns` below) is checked against
    both the file's diff text AND its path, with the path leg skipped for a
    test/e2e path (`_DEEP_TEST_PATH_EXEMPT`) — a folder merely named
    `security-e2e/` denied a 1-line type fix even though nothing sensitive
    was touched. Identifiers are normalized across snake_case, camelCase,
    kebab-case, dot-notation, and screaming snake to prevent word boundary
    bypasses (#628). The "structural" half (schema/migration/lockfile/manifest/
    CI/Dockerfile) stays path-only with no test exemption — those are deep
    because of WHERE the file lives, not a word in its name, per Lead's own
    package.json/pyproject.toml version-bump experience the same night.
    """
    if not isinstance(tool_input, dict):
        return Verdict(True)

    file_path = str(tool_input.get("file_path") or "").strip()
    if not file_path:
        return Verdict(True)

    # #633: Protected DATA_HOME check runs BEFORE note exemption and BEFORE
    # lead_may_implement — an edit targeting another cockpit instance's
    # DATA_HOME (e.g. ~/.agent-takkub/projects.json) is NEVER allowed.
    in_prot, prot_home = is_in_protected_data_home(file_path, cwd=cwd)
    if in_prot:
        return Verdict(
            False,
            rule="instance_guard:protected_data_home",
            reason=(
                f"ไฟล์ {file_path} อยู่ใน Protected DATA_HOME ของ cockpit instance อื่น "
                f"({prot_home}) — ห้ามทุก role แก้ไข ย้าย หรือลบ เพื่อป้องกันข้อมูลเสียหาย (#633)"
            ),
        )

    if _is_note_exempt(file_path):
        return Verdict(True)

    if project:
        try:
            from .team_preset import lead_may_implement

            if lead_may_implement(project):
                return Verdict(True)
        except Exception:
            pass

    # Deep patterns on file path — checked before the runtime/outside-root
    # exemption below (#587 F3), see docstring.
    norm_file = file_path.replace("\\", "/").lower()

    # Structural deep categories: deep because of WHERE the file lives, not
    # because of a word that can appear in an unrelated directory name — path
    # is the only signal that makes sense here, and there's no legitimate
    # "test version" of a lockfile/migration/CI workflow to exempt. Unchanged
    # by #611 below.
    structural_deep_patterns = (
        r"\b(?:schema|prisma)\b|schemas?/",
        r"\bmigrations?\b",
        r"(?:package\.json|requirements\.txt|pyproject\.toml|go\.mod|cargo\.toml)$",
        r"(?:package-lock\.json|pnpm-lock\.yaml|yarn\.lock|uv\.lock|poetry\.lock|bun\.lock)$",
        r"\.github/(?:workflows|actions)",
        r"(?:dockerfile|docker-compose.*\.ya?ml)$",
    )
    for pat in structural_deep_patterns:
        if re.search(pat, norm_file):
            return Verdict(
                False,
                rule="lead_direct_edit:deep_category",
                reason=f"ไฟล์ {file_path} อยู่ในหมวด deep ({pat}) — ห้าม Lead แก้เอง ต้อง delegate ผ่าน takkub assign --role <role>",
            )

    # #625: files outside project root (scratchpad in %TEMP%, memory files,
    # cockpit runtime) are exempt from direct-edit caps and sensitive content
    # checks (#587 F3 preserved: structural deep files like package.json/lockfile
    # above are still denied everywhere).
    if _is_direct_edit_exempt(file_path, cwd, project):
        return Verdict(True)

    # #611: these are word-based, and a word alone can sit in a path that
    # isn't actually sensitive source — `apps/api/src/security-e2e/
    # test-harness.ts` denied a 1-line type fix in a TEST file because
    # "security" is in the folder name. Path is only trusted here when the
    # path is NOT a test/e2e path (`_DEEP_TEST_PATH_EXEMPT`); independently,
    # the actual diff text (old_string+new_string for Edit, content for
    # Write) is always checked, so a genuinely sensitive change hiding
    # inside a test file (e.g. a hardcoded secret in a fixture) still denies.
    #
    # #628: identifiers in snake_case (check_auth_token, refresh_token,
    # api_auth, is_admin_user...), camelCase (checkAuthToken, refreshToken),
    # dot-notation, and kebab-case are normalized so word boundaries
    # (`\b`) match cleanly across all casing conventions.
    sensitive_deep_patterns = (
        r"\b(?:auth|oauth|jwt|login|signup|password|permission|privilege|otp|mfa|rate[\s_-]?limit)\b|(?:\b|\D)2fa\b",
        r"(?:bypass|skip|disable|remove)[\s\-_]*(?:verif|valid|signature|sanitiz|auth|check)",
        r"(?:admin|role|session)[\s\-_]*(?:permission|access|based|privilege|panel|timeout|expiry|cookie)",
        r"\b(?:security|vulnerabilit|cve|xss|csrf)\b",
        r"\b(?:tokens?|api[\s\-_]?keys?|secrets?)\b",
        r"\b(?:crypto|encryption|bcrypt)\b",
        r"\b(?:payments?|stripe|billing)\b",
        # #611 M1 / #628: camelCase/snake_case identifiers a bland-looking one-line
        # diff can hide a real bypass behind — `verifySignature`/`isAdmin`
        # word-parts and a bare `return true`, the exact shape of the proven
        # bypass (`return verifySignature(value)` -> `return true`).
        r"verify[\s\-_]*signature|is[\s\-_]*admin|\bbypass\b|return\s+true\b",
    )
    is_test_path = _is_deep_test_path(norm_file)
    diff_text = _direct_edit_diff_text(tool_name, tool_input)
    norm_diff = _normalize_identifier_boundaries(diff_text)
    norm_path = _normalize_identifier_boundaries(file_path.replace("\\", "/"))
    for pat in sensitive_deep_patterns:
        path_hit = (not is_test_path) and (
            bool(re.search(pat, norm_file)) or bool(re.search(pat, norm_path))
        )
        content_hit = bool(re.search(pat, diff_text.lower())) or bool(re.search(pat, norm_diff))
        if path_hit or content_hit:
            return Verdict(
                False,
                rule="lead_direct_edit:deep_category",
                reason=f"ไฟล์ {file_path} อยู่ในหมวด deep ({pat}) — ห้าม Lead แก้เอง ต้อง delegate ผ่าน takkub assign --role <role>",
            )

    # 1. Non-tiny scope check
    norm_scope = (scope or "").strip().lower()
    if norm_scope and norm_scope != "tiny":
        return Verdict(
            False,
            rule=f"lead_direct_edit:{norm_scope}_scope",
            reason=f"Lead direct-edit ไม่อนุญาตในงาน scope={norm_scope} (อนุญาตเฉพาะ scope=tiny) — ต้อง delegate ผ่าน takkub assign --role <role>",
        )

    # 2. Line count per tool call (<= 15 lines)
    if tool_name == "Edit":
        old_str = str(tool_input.get("old_string") or "")
        new_str = str(tool_input.get("new_string") or "")
        old_count = len(old_str.splitlines()) if old_str else 0
        new_count = len(new_str.splitlines()) if new_str else 0
        lines_this_call = max(old_count, new_count)
    else:  # Write
        content = str(tool_input.get("content") or "")
        new_lines = content.splitlines() if content else []
        resolved = pathlib.Path(file_path)
        if not resolved.is_absolute() and cwd:
            resolved = pathlib.Path(cwd) / resolved
        if resolved.is_file():
            try:
                old_lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
                import difflib

                matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
                lines_this_call = sum(
                    max(e1 - s1, e2 - s2)
                    for tag, s1, e1, s2, e2 in matcher.get_opcodes()
                    if tag != "equal"
                )
            except Exception:
                lines_this_call = len(new_lines)
        else:
            lines_this_call = len(new_lines)

    if lines_this_call > 15:
        return Verdict(
            False,
            rule="lead_direct_edit:lines_per_call",
            reason=(
                f"Lead direct-edit เกินเพดาน 15 บรรทัดต่อครั้ง (แตะ {lines_this_call} บรรทัด) "
                "— ต้อง delegate ผ่าน takkub assign --role <role>"
            ),
        )

    # 3. Cumulative file count (<= 2) and line count (<= 30)
    norm_path = os.path.normcase(os.path.normpath(file_path))
    s_path = state_file
    if s_path is None:
        try:
            from .config import RUNTIME_DIR

            s_dir = RUNTIME_DIR / "lead_edits"
            s_dir.mkdir(parents=True, exist_ok=True)
            p_name = re.sub(r"[^\w.-]", "_", project or "default")
            s_path = s_dir / f"{p_name}.json"
        except Exception:
            s_path = None

    files_list: list[str] = []
    accum_lines: int = 0
    updated_at_dt: datetime | None = None
    if s_path and s_path.is_file():
        try:
            s_data = json.loads(s_path.read_text(encoding="utf-8"))
            updated_at_str = s_data.get("updated_at")
            is_stale = False
            if updated_at_str:
                try:
                    updated_at_dt = datetime.fromisoformat(updated_at_str)
                    if (datetime.now() - updated_at_dt).total_seconds() > 1800.0:
                        is_stale = True
                except Exception:
                    pass
            if not is_stale:
                files_list = s_data.get("files", [])
                accum_lines = int(s_data.get("total_lines", 0))
            else:
                updated_at_dt = None
        except Exception:
            files_list = []
            accum_lines = 0

    remaining_minutes = 30
    if updated_at_dt:
        elapsed_s = (datetime.now() - updated_at_dt).total_seconds()
        remaining_s = max(0.0, 1800.0 - elapsed_s)
        remaining_minutes = max(1, math.ceil(remaining_s / 60.0))

    distinct_files = set(files_list)
    if norm_path not in distinct_files and len(distinct_files) >= 2:
        return Verdict(
            False,
            rule="lead_direct_edit:cumulative_files",
            reason=(
                f"Lead direct-edit เกินเพดานสะสม 2 ไฟล์ต่อ task (แตะไฟล์ที่ {len(distinct_files) + 1}: {file_path}) "
                f"— ต้อง delegate ผ่าน takkub assign --role <role> "
                f"(เพดานจะรีเซ็ตเองใน {remaining_minutes} นาที หรือใช้ takkub lead-edits --reset)"
            ),
        )

    new_accum = accum_lines + lines_this_call
    if new_accum > 30:
        return Verdict(
            False,
            rule="lead_direct_edit:cumulative_lines",
            reason=(
                f"Lead direct-edit เกินเพดานสะสม 30 บรรทัดต่อ task (สะสม {new_accum} บรรทัด) "
                f"— ต้อง delegate ผ่าน takkub assign --role <role> "
                f"(เพดานจะรีเซ็ตเองใน {remaining_minutes} นาที หรือใช้ takkub lead-edits --reset)"
            ),
        )

    # Update state
    if s_path:
        try:
            distinct_files.add(norm_path)
            state_data = {
                "files": list(distinct_files),
                "total_lines": new_accum,
                "updated_at": datetime.now().isoformat(),
            }
            tmp = s_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state_data), encoding="utf-8")
            tmp.replace(s_path)
        except Exception:
            pass

    return Verdict(True)


def reset_lead_edits(
    project: str | None = None,
    *,
    state_file: pathlib.Path | None = None,
) -> bool:
    """Reset the lead_edits accumulation state for a project (#585 round 4)."""
    s_path = state_file
    if s_path is None:
        try:
            from .config import RUNTIME_DIR

            s_dir = RUNTIME_DIR / "lead_edits"
            p_name = re.sub(r"[^\w.-]", "_", project or "default")
            s_path = s_dir / f"{p_name}.json"
        except Exception:
            return False

    if s_path and s_path.is_file():
        try:
            s_path.unlink()
            return True
        except Exception:
            return False
    return True


def get_lead_edits_status(
    project: str | None = None,
    *,
    state_file: pathlib.Path | None = None,
) -> dict:
    """Get the current lead_edits accumulation status for a project (#585 round 4)."""
    s_path = state_file
    if s_path is None:
        try:
            from .config import RUNTIME_DIR

            s_dir = RUNTIME_DIR / "lead_edits"
            p_name = re.sub(r"[^\w.-]", "_", project or "default")
            s_path = s_dir / f"{p_name}.json"
        except Exception:
            s_path = None

    files_list: list[str] = []
    accum_lines = 0
    updated_at = None
    if s_path and s_path.is_file():
        try:
            s_data = json.loads(s_path.read_text(encoding="utf-8"))
            updated_at_str = s_data.get("updated_at")
            is_stale = False
            if updated_at_str:
                try:
                    dt = datetime.fromisoformat(updated_at_str)
                    if (datetime.now() - dt).total_seconds() > 1800.0:
                        is_stale = True
                except Exception:
                    pass
            if not is_stale:
                files_list = s_data.get("files", [])
                accum_lines = int(s_data.get("total_lines", 0))
                updated_at = updated_at_str
        except Exception:
            pass

    return {
        "files": files_list,
        "files_count": len(set(files_list)),
        "total_lines": accum_lines,
        "updated_at": updated_at,
    }


# git subcommand gate (#314): "only Lead commits" — see module docstring for
# why prose alone wasn't enough. Flags between `git` and the subcommand are
# skipped ONLY when they look like bare flags (`-c foo=bar` style two-token
# values are NOT consumed), so `git log --grep=commit` never matches (`log`
# sits where the subcommand is expected, "commit" only appears deep inside a
# flag value) while `git commit -m "..."` matches.
#
# `-C <path>` is the one deliberate exception to "no two-token flags" (#466):
# #438 case 2's own worktree-target detection (`_command_targets_worktree`
# below) is built entirely around a pane running `git -C <worktree> <sub>`
# instead of `cd`-ing there first — but before this fix `-C <path>` silently
# broke the subcommand match for every gated rule that used this gap, not
# just push. `git -C <wt> reset --hard`, `git -C <wt> push origin main
# --force`, `git -C <wt> commit -m x` were all allowed outright, because
# neither the deny pattern NOR the #438 own-branch-push validator could find
# the subcommand past the unconsumed path argument — a silent bypass, not
# even a false deny (confirmed: `tests/test_pane_guard_worktree_push.py`'s
# `git -C . push -u origin wt/...` case was "passing" only because the
# guard couldn't see `push` at all, not because it validated the branch —
# `git -C . push origin main --force` was equally allowed until this fix).
#
# #609 H2 round 2: the gap also has to swallow a quoted `-C "path with
# spaces"` (previously `\S+` stopped at the first space, leaving the rest of
# the quoted path sitting where the subcommand was expected — silent
# bypass, not a false deny) and a `-c <key>=<value>` config override (two
# tokens, value containing `.`/`=` that the old bare-flag alternative could
# never swallow) so the literal subcommand token is still reachable past
# either. `git.exe`/`git.cmd` (Windows PATHEXT resolution) is accepted the
# same way `_RTK_PREFIX` already accepts them for `rtk`.
_GIT_BIN = r"git(?:\.(?:exe|cmd))?"
# #609 round 3: the two-token `-C <path>` exception (#466) never covered
# `--git-dir <path>`/`--work-tree <path>` (identical shape, different flag
# names) or a bare `--flag=value` long option (`--git-dir=<path>`,
# `--work-tree=<path>`, …) — either shape left every subcommand pattern
# below unable to reach past it, the same silent-bypass mechanism #466
# already fixed for `-C`/`-c` (confirmed live: `git --git-dir=<path>
# --work-tree=<path> restore .` never matched the `restore` pattern at all
# — the "restore" token was simply unreachable, so this wasn't even a false
# ALLOW from a bad ownership check, `classify` never recognised a `restore`
# invocation was present). `--flag=value` is swallowed generically, not just
# for git-dir/work-tree, since that's how every git long option is spelled.
# Split out as its own name (no trailing `\s+`) so `_GIT_INVOCATION_RE` below
# can wrap it in a capturing group and recover the raw flags text for the
# `-c`/`--config-env` key-safety scan (#609/#611 round 5) without touching
# any of the ~15 other patterns built from `_GIT_SUBCMD_GAP` — the
# concatenation is byte-identical to the old single constant, so none of
# them change behavior.
_GIT_SUBCMD_GAP_FLAGS = (
    r"(?:\s+(?:-C|--git-dir|--work-tree|--config-env)\s+(?:\"[^\"]*\"|'[^']*'|\S+)"
    r"|\s+-c\s+(?:\"[^\"]*\"|'[^']*'|\S+)"
    r"|\s+--[\w-]+=(?:\"[^\"]*\"|'[^']*'|\S+)"
    r"|\s+-{1,2}[\w-]+)*"
)
_GIT_SUBCMD_GAP = _GIT_SUBCMD_GAP_FLAGS + r"\s+"


def _strip_matching_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


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
    rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}commit{_SUBCMD_END}", re.M
)

# tag -d/rebase stay Lead-only with NO exception even from an isolated
# worktree branch — both act on refs a worktree pane can't safely own a
# private copy of. `push`, `merge`, and (#545) `reset-hard`/`checkout`/
# `branch-delete` DO carve out a worktree exception: `push` only when every
# target is the pane's own `wt/<role>-<ts>` branch by name (#438,
# `_push_is_own_worktree_branch`), `merge` only against the current base
# into that same branch (#385, `_GIT_MERGE_PATTERN` below), and
# `reset-hard`/`checkout`/`branch-delete` unconditionally inside the pane's
# own worktree checkout (#545, `_WORKTREE_SAFE_RULES` below — that checkout
# is disposable by definition, so there is nothing narrower to shape-check).
# Kept as full deny patterns in this tuple — `classify()` overrides the
# verdict for the carved-out rules when the exception actually applies, so
# this list itself needs no per-rule cwd branching.
_GIT_LEAD_ONLY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "push",
        re.compile(rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}push{_SUBCMD_END}", re.M),
    ),
    (
        "reset-hard",
        re.compile(
            rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}reset{_SUBCMD_END}{_SAME_CMD}--hard\b",
            re.M,
        ),
    ),
    # Round 5: no longer in `_WORKTREE_SAFE_RULES` — `-D` names ANY branch by
    # string, not necessarily one scoped to the caller's own worktree (the
    # same cross-role blast radius R3 already found for `worktree remove`),
    # so it is now Lead-only unconditionally, same as `-f`/`-M` below.
    (
        "branch-delete",
        re.compile(
            rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}branch{_SUBCMD_END}{_SAME_CMD}-D\b",
            re.M,
        ),
    ),
    # Round 5: `-f`/`--force` force-creates/resets a branch to a new tip
    # (silently overwriting wherever it pointed before) and `-M` force-
    # renames over an existing destination — both are the "ref overwrite"
    # analogue of `-D`, unconditional for the same reason.
    # `(?<!\S)` anchors the flag to a real token start (preceded by
    # whitespace or nothing) — without it, `_SAME_CMD`'s lazy `.*?` let the
    # flag alternation match mid-word (proven live: `git branch new-feature`
    # false-positived on the "-feature" substring, since `-[A-Za-z]*f[A-Za-z]
    # *\b` doesn't care what comes before the `-`).
    (
        "branch-force",
        re.compile(
            rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}branch{_SUBCMD_END}{_SAME_CMD}"
            r"(?<!\S)(?:--force\b|-[A-Za-z]*f[A-Za-z]*\b)",
            re.M,
        ),
    ),
    (
        "branch-force-rename",
        re.compile(
            rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}branch{_SUBCMD_END}{_SAME_CMD}-M\b",
            re.M,
        ),
    ),
    (
        "tag-delete",
        re.compile(
            rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}tag{_SUBCMD_END}{_SAME_CMD}-d\b",
            re.M,
        ),
    ),
    # Round 5: `--template=<dir>` (init or clone) seeds the new repo's
    # `.git/hooks` from that directory's `hooks/` subfolder — hook scripts
    # that fire on the NEXT ordinary `commit`/`checkout`/etc in that repo,
    # a persistent code-execution vector no different in kind from
    # `diff.external`. Unconditional — a worktree pane cloning/init-ing a
    # nested repo has no legitimate need for a custom template dir either.
    (
        "init-clone-template",
        re.compile(
            rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}(?:init|clone){_SUBCMD_END}"
            rf"{_SAME_CMD}--template\b",
            re.M,
        ),
    ),
    (
        "rebase",
        re.compile(rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}rebase{_SUBCMD_END}", re.M),
    ),
    (
        "checkout",
        re.compile(rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}checkout{_SUBCMD_END}", re.M),
    ),
    # #609: `git restore` is the modern alias for `checkout -- <path>` — same
    # blast radius (overwrites working-tree files from HEAD/index), same
    # worktree carve-out as `checkout` below.
    (
        "restore",
        re.compile(rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}restore{_SUBCMD_END}", re.M),
    ),
    # #609 round 3: `git switch` is the modern alias for `checkout <branch>`
    # (its `--discard-changes`/`-f`/`--force`/`-C`/`--force-create` are the
    # equivalent of `checkout -f`) — same blast radius, so it gets the exact
    # same blanket, unconditional-on-any-flag-shape deny as `checkout` above
    # (proven live: `git switch --discard-changes <branch>` and `git switch
    # -f <branch>` both returned `allowed=True` and discarded real
    # uncommitted work on a shared-tree fixture) and the same worktree
    # carve-out (`_WORKTREE_SAFE_RULES` below).
    (
        "switch",
        re.compile(rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}switch{_SUBCMD_END}", re.M),
    ),
    # #609 round 3: `worktree remove`/`move`/`prune`/`lock`/`unlock` mutate
    # or delete ANOTHER checkout's directory outright — round 2's own
    # `_command_targets_worktree`/`_in_worktree` machinery only ever checked
    # ownership of the CALLER's cwd/explicit target, never the worktree path
    # actually being removed, so any role could name a DIFFERENT role's
    # worktree as the argument and still get the carve-out (proven live:
    # `frontend`, sitting in its own worktree, ran `git -C <repo> worktree
    # remove --force <backend's worktree>` and backend's uncommitted file
    # was gone). There is nothing narrower to shape-check here the way
    # `push`/`merge` are — deliberately NOT added to `_WORKTREE_SAFE_RULES`:
    # Lead-only unconditionally, regardless of the caller's own cwd or
    # target (`takkub worktree clean` is the cockpit-managed way to do
    # this). `worktree add`/`list` stay unaffected — only these five
    # sub-verbs (`remove`/`move`/`prune`/`lock`/`unlock`) are gated.
    (
        "worktree-admin",
        re.compile(
            rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}worktree{_SUBCMD_END}{_SAME_CMD}"
            r"\b(?:remove|move|prune|lock|unlock)\b",
            re.M,
        ),
    ),
    # #609: `-f`/`--force` in any combined short-flag form (`-fd`, `-fdx`, …).
    # A bare `git clean` with no force flag only prints what WOULD be removed
    # (dry-run by default) and stays allowed.
    (
        "clean-force",
        re.compile(
            rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}clean{_SUBCMD_END}{_SAME_CMD}"
            rf"(?:--force\b|-[A-Za-z]*f[A-Za-z]*\b)",
            re.M,
        ),
    ),
)

# #609: every `git stash` subcommand except the read-only `list`/`show` —
# `push`/pop`/`apply`/`drop`/`clear`/`branch`, and a bare `git stash` (which
# defaults to `push`). Handled outside `_GIT_LEAD_ONLY_PATTERNS` (like
# `commit`/`merge` above it) because the read-only carve-out needs the
# matched tail, not just a yes/no `.search()`.
_GIT_STASH_PATTERN = re.compile(
    rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}stash{_SUBCMD_END}(?P<tail>[^\n|;&]*)", re.M
)

# #609/#611 H1: `refs/stash` is ONE stack shared by every linked worktree of
# the same repository (that's how `git worktree` is designed — it is not
# private state a pane's own checkout owns). `push`/`save` (and a bare
# `git stash`, which defaults to `push`) only ADD an entry — safe from any
# worktree. `apply` copies an entry's content into the CALLER's own working
# tree without removing it from the stack — also safe from any worktree.
# `pop` (= apply + drop), `drop`, `clear`, and `branch` (= apply + drop on
# success) all REMOVE an entry other panes/checkouts can still see in their
# own `git stash list` — repo-wide destructive, so these stay Lead-only
# EVEN inside the pane's own worktree (proven live: #609's own worktree
# carve-out let a `clear` from one worktree delete a stash pushed from a
# sibling checkout — `ART/stash_probe.py`, `ART/stash-cross-worktree.json`).
_STASH_SHARED_REF_MUTATING = frozenset({"pop", "drop", "clear", "branch"})
_STASH_WORKTREE_SAFE = frozenset({"push", "save", "apply"})


def _git_stash_verdict(cmd: str, in_worktree: bool) -> bool:
    """True when *cmd* contains a `git stash` call that must be denied —
    conservative: a single denied call denies the whole command, same
    direction as every other rule here. `list`/`show` stay read-only-allowed
    everywhere; `push`/`save`/`apply` are worktree-safe (see
    `_STASH_WORKTREE_SAFE`); everything else (including an unrecognised
    future subcommand — fail closed) is Lead-only unconditionally."""
    for m in _GIT_STASH_PATTERN.finditer(cmd):
        first_tok = m.group("tail").split()[:1]
        sub = first_tok[0].lower() if first_tok else "push"
        if sub in ("list", "show"):
            continue
        if sub in _STASH_WORKTREE_SAFE:
            if not in_worktree:
                return True
            continue
        return True  # _STASH_SHARED_REF_MUTATING or unrecognised — always denied
    return False


# #609/#611 round 5: `git config` writes to the repo's SHARED `.git/config`
# by default — every linked worktree of one repo reads that same file unless
# `extensions.worktreeConfig` + `--worktree` scope is explicitly used, which
# nothing in this codebase does — so a config write from inside a role's own
# worktree is exactly as repo-wide as one from the shared tree (same class
# of bug as #609/#611 H1's shared `refs/stash`). Combined with a dangerous
# key (`diff.external`, `core.hooksPath`, `alias.<name>=!<shell>`, …) this is
# also how `-c`'s RCE persists PAST the one invocation that set it — a
# `diff.external` written to config fires on every future `git diff` ANY
# pane runs against this checkout. Denied unconditionally (no worktree
# carve-out) except the same key safe-list `-c` uses, plus `user.name`/
# `user.email` set locally from a role's own worktree (needed for that
# pane's own commit authorship; narrow enough that even if it did leak to
# the shared file, it's not a code-execution or ref-redirection vector).
_GIT_CONFIG_PATTERN = re.compile(
    rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}config{_SUBCMD_END}(?P<tail>[^\n|;&]*)", re.M
)
_CONFIG_READ_FLAGS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--show-scope"}
)
_CONFIG_WRITE_ONLY_FLAGS = frozenset(
    {
        "--add",
        "--unset",
        "--unset-all",
        "--replace-all",
        "--rename-section",
        "--remove-section",
        "--edit",
        "-e",
    }
)
_CONFIG_SCOPE_OR_FILE_FLAG_PREFIXES = (
    "--file",
    "-f",
    "--blob",
    "--global",
    "--system",
    "--worktree",
)
_CONFIG_WORKTREE_LOCAL_KEYS = frozenset({"user.name", "user.email"})


def _git_config_verdict(cmd: str, in_worktree: bool) -> bool:
    """True when *cmd* contains a `git config` call that must be denied. Read
    forms are always allowed everywhere. A plain `config <key> <value>`
    write is allowed only for a key `_config_key_value_safe` already trusts,
    or (in a role's own worktree only) `user.name`/`user.email`. Every other
    write shape (`--add`/`--unset*`/`--replace-all`/`--rename-section`/
    `--remove-section`/`--edit`/any explicit scope or `--file`/`--blob`
    target) is denied unconditionally, fail-closed for an unrecognised
    shape too."""
    for m in _GIT_CONFIG_PATTERN.finditer(cmd):
        tokens = m.group("tail").split()
        if any(t in _CONFIG_READ_FLAGS for t in tokens):
            continue
        if any(
            t in _CONFIG_WRITE_ONLY_FLAGS or t.startswith(_CONFIG_SCOPE_OR_FILE_FLAG_PREFIXES)
            for t in tokens
        ):
            return True
        positionals = [t for t in tokens if not t.startswith("-")]
        if len(positionals) <= 1:
            continue  # bare `config` or `config <key>` alone = read form
        key, value = positionals[0], positionals[1]
        if in_worktree and key.lower() in _CONFIG_WORKTREE_LOCAL_KEYS:
            continue
        if _config_key_value_safe(key, value):
            continue
        return True
    return False


# #545: unlike `push`/`merge` (each shape-checked to a narrow safe form),
# these three are safe UNCONDITIONALLY inside the pane's own worktree
# checkout — there is nothing left to rewrite that isn't already scoped to
# a private, disposable branch nobody else touches. Before this carve-out
# both were still Lead-only even in-worktree, which forced worse
# workarounds in practice: a `merge` used in place of `reset --hard` to land
# a rewritten branch (mangling its history), and `git archive | tar -x`
# used in place of `checkout` to materialize a different ref's files.
# `tag-delete`/`rebase` are deliberately excluded — both act on refs a
# worktree pane can't safely disown its own copy of, unlike a plain branch
# reset/switch/delete confined to the checkout itself. Round 5: `branch-
# delete` (`-D`) removed from this set — see its own comment above
# `_GIT_LEAD_ONLY_PATTERNS` — it names an arbitrary branch by string, not
# necessarily one scoped to the caller's own worktree.
_WORKTREE_SAFE_RULES = frozenset({"reset-hard", "checkout", "restore", "clean-force", "switch"})

# `git merge` (#385): Lead-only on the shared tree, ALLOWED from inside a
# pane's own `--isolation worktree` checkout. There the pane's branch is the
# only thing that moves — merging base INTO it is how a second worktree pane
# picks up what a sibling already landed, without a Lead round-trip for
# every sync (the report's bottleneck). Merging the pane's branch INTO base
# happens on the shared tree, which this carve-out never covers.
_GIT_MERGE_PATTERN = re.compile(
    rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}merge{_SUBCMD_END}", re.M
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


# #609 round 4 R3-H1: `os.path.realpath` resolves symlinks AND (Python >=
# 3.8) Windows directory junctions — `mklink /J` needs no admin rights, so a
# pane can plant one INSIDE its own worktree pointing at the shared tree (or
# a sibling role's worktree) and have `_worktree_role_owns` see only the
# junction's own, correctly-owned-looking path text. Resolving to the real
# target first closes that. Falls back to the input unresolved on any OSError
# (a malformed path, or one containing characters the OS rejects) — same
# conservative "can't verify, don't widen" posture as every other override
# check in this module; the caller still runs `_worktree_role_owns` on
# *something*, and an unresolved junction path won't spuriously match this
# role's `<role>-<ts>` segment any more than a resolved one would.
def _safe_realpath(path: str | None) -> str | None:
    if not path:
        return path
    try:
        return os.path.realpath(path)
    except OSError:
        return path


# #609 H2: a path merely containing "/worktrees/" isn't enough — every
# role's checkout lives under the same `<project>/` parent, so a `cwd`/`-C`
# that happens to sit inside a DIFFERENT role's worktree must not grant
# THIS role's carve-out (proven live: `frontend` with cwd inside
# `backend-123`'s checkout got the worktree exception outright). The
# `<role>-<ts>` (or sharded `<role>-<n>-<ts>`) path segment must start with
# *this* role's own slug.
def _worktree_role_owns(path: str | None, role: str | None) -> bool:
    if not path or not _WORKTREE_CWD.search(path):
        return False
    # Base role, shard suffix stripped: the worktree DIRECTORY is one
    # checkout per base role (`backend-<ts>`), not one per shard — a shard's
    # `wt/<role>-<n>-<ts>` BRANCH name (`_role_slug`) is a different thing
    # from the checkout directory it lives in, confirmed by the existing
    # `backend#3` fixture sharing the plain `backend-<ts>` worktree path.
    base = normalise_role(role)
    if not base:
        return False
    prefix = f"{base}-"
    # Case-insensitive: Windows paths can reach here in either case even
    # though `base` (from `normalise_role`) is always lowercased already.
    norm = path.replace("\\", "/").lower()
    return any(seg == base or seg.startswith(prefix) for seg in norm.split("/") if seg)


# #438 case 2: the hook payload's cwd is the pane's *session* cwd. A pane
# respawned without `--isolation worktree` that keeps working inside its old
# checkout does so with `git -C <wt> …` / `cd <wt> && git …` — the command
# itself names the worktree even though cwd is the shared tree. Honour that
# the same way cwd is honoured: what matters is where the git command runs.
_WORKTREE_TARGET = re.compile(
    r"""(?:(?<![\w-])-C\s+|(?:^|[|;&]\s*)cd\s+)["']?[^\s"'|;&]*[/\\]worktrees[/\\][^\s"'|;&]*""",
    re.I | re.M,
)


def _command_targets_worktree(cmd: str) -> bool:
    """True when *cmd* explicitly runs git inside a cockpit worktree checkout
    (`git -C …/worktrees/… <sub>` or `cd …/worktrees/… && git <sub>`)."""
    return bool(_WORKTREE_TARGET.search(cmd))


# #609 H2: the ONE explicit `-C`/`--git-dir`/`--work-tree`/`cd` target a
# command names — not just whether it happens to mention a worktree path.
# An ABSOLUTE target is authoritative over cwd (a worktree cwd must not
# launder a command that plainly names a different, shared-tree location);
# a relative one (bare `.`, a subdir, …) can't be resolved without cwd, so
# it stays deferred to the cwd-based checks below.
_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")
# #609 round 3: `--git-dir`/`--work-tree` take their value either way
# (`--git-dir=<path>` OR `--git-dir <path>`) — only the `=` form was
# recognised here before, so the space form silently fell through to the
# cwd-based checks below instead of being treated as the explicit target it
# is.
_EXPLICIT_TARGET_RE = re.compile(
    r"""(?:(?<![\w-])-C\s+(?P<c>"[^"]*"|'[^']*'|\S+)
        |--(?:git-dir|work-tree)(?:=|\s+)(?P<gd>"[^"]*"|'[^']*'|\S+)
        |(?:^|[|;&]\s*)cd\s+(?P<cd>"[^"]*"|'[^']*'|\S+))""",
    re.I | re.M | re.X,
)


def _explicit_git_target(cmd: str) -> str | None:
    """The LAST explicit `-C`/`--git-dir`/`--work-tree`/`cd` path named in
    *cmd*, quotes stripped — or `None` if the command names no target."""
    last: str | None = None
    for m in _EXPLICIT_TARGET_RE.finditer(cmd):
        path = m.group("c") or m.group("gd") or m.group("cd")
        if path:
            last = _strip_matching_quotes(path)
    return last


def _in_worktree(cmd: str, cwd: str | None, role: str | None = None) -> bool:
    target = _explicit_git_target(cmd)
    if target:
        # #609 round 3b: a RELATIVE target (bare `.`, `..`, `../sibling`, a
        # subdir, …) can't be judged against its own text — `-C ..` and
        # `-C .` both look "relative" but land in very different places.
        # Resolve it against cwd the same way the shell/git would, THEN
        # check ownership of the resolved path — not the raw cwd, which
        # would silently ignore a `..` that walks the target back OUT of
        # this role's checkout (proven live: `git -C .. restore .` from a
        # worktree cwd reached the shared parent `worktrees/<project>/`
        # dir — every role's checkouts live under it — while the old cwd
        # check granted the carve-out outright).
        resolved = target if _ABS_PATH_RE.match(target) else _resolve_relative_target(target, cwd)
        if resolved is not None:
            # #609 round 4 R3-H1: resolve symlinks/junctions before judging
            # ownership — see `_safe_realpath`'s docstring.
            return _worktree_role_owns(_safe_realpath(os.path.normpath(resolved)), role)
        # Relative target, no cwd to resolve it against: unresolvable, so
        # fall through to the cwd-based checks below rather than guessing —
        # same conservative posture as the config-override/GIT_DIR checks.
    if _is_worktree_cwd(cwd):
        return _worktree_role_owns(_safe_realpath(cwd), role)
    if _command_targets_worktree(cmd):
        # `target` is always None here (the explicit-target branch above
        # already returned when it wasn't) — this is the legacy fallback
        # that matches a worktree path embedded anywhere in `cmd`'s text, not
        # an actual filesystem path, so there's nothing for `_safe_realpath`
        # to resolve.
        return _worktree_role_owns(cmd, role)
    return False


def _resolve_relative_target(target: str, cwd: str | None) -> str | None:
    """Resolve a RELATIVE `-C`/`--git-dir`/`--work-tree`/`cd` *target*
    against *cwd*, collapsing `.`/`..` — `None` when *cwd* is unknown and
    the target can't be resolved. See `_in_worktree`."""
    if not cwd:
        return None
    return os.path.normpath(os.path.join(cwd, target))


# #609 round 3: `GIT_DIR`/`GIT_WORK_TREE`/`GIT_COMMON_DIR`/`GIT_INDEX_FILE`
# redirect where git actually operates exactly like `--git-dir=`/
# `--work-tree=` do, but set via a bare prefix (`GIT_DIR=<path> git …`),
# `env VAR=… git …`, cmd.exe `set VAR=… && git …`, or PowerShell
# `$env:VAR = …; git …` — none of which `_explicit_git_target`'s flag-only
# regex ever looked for (confirmed live: `GIT_DIR=<path> git stash drop` run
# via git-bash dropped a stash entry belonging to a repo with no worktree
# relationship to the caller at all — worse than the flag form since it
# doesn't even need a linked worktree to reach; `classify` returned
# `allowed=True`). Matched with a bare `\bVAR\b` (no anchoring to
# `_CMD_START`) since `set`/`$env:` sit in their OWN chain segment ahead of
# `git`, not immediately in front of it. Deliberately conservative the same
# way an unresolvable `-c core.*` override already is: found-but-not-
# provably-this-role's-own-worktree (relative, unparseable, or another
# role's path) disables the worktree carve-out outright rather than
# silently falling through to a cwd check that has nothing to do with where
# this override actually points.
_GIT_ENV_TARGET_RE = re.compile(
    r"(?:\bset\s+|\$env:)?"
    r"\b(?P<var>GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_INDEX_FILE)\b"
    r"\s*=\s*(?P<val>\"[^\"]*\"|'[^']*'|[^\s&;|\r\n]*)",
    re.I | re.M,
)


def _git_dir_env_override(raw_cmd: str, role: str | None) -> bool:
    """True when *raw_cmd* sets any GIT_DIR-family env var whose value isn't
    provably an absolute path inside this *role*'s own worktree — see
    `_GIT_ENV_TARGET_RE`'s note above. One matching assignment is enough to
    force a deny; conservative in the same direction as every other
    override check in this module. *raw_cmd* must be the PRE-unwrap command
    text — `_unwrap_segment`'s `env VAR=value` stripping deletes the very
    prefix this needs to see (see `classify`'s `raw_cmd` capture)."""
    for m in _GIT_ENV_TARGET_RE.finditer(raw_cmd):
        val = _strip_matching_quotes(m.group("val"))
        if not val or not _ABS_PATH_RE.match(val) or not _worktree_role_owns(val, role):
            return True
    return False


# #438 case 1: a worktree pane may push ITS OWN `wt/<role>-<ts>` branch so CI
# can verify it before `takkub done` — the one thing it could not do without
# a Lead round-trip. Strictly shaped: the branch must be named explicitly (a
# bare `git push` would push whatever HEAD is, which the guard can't see),
# must carry the pane's own role slug, and force/delete/mirror/all forms are
# never allowed. Everything else about `push` stays Lead-only.
_GIT_PUSH_TAIL = re.compile(
    rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}push{_SUBCMD_END}(?P<tail>[^\n|;&]*)", re.M
)
_PUSH_FORBIDDEN_FLAGS = frozenset(
    {
        "-f",
        "--force",
        "--force-with-lease",
        "--delete",
        "-d",
        "--mirror",
        "--all",
        "--tags",
        "--prune",
    }
)
_PUSH_OK_FLAGS = frozenset(
    {"-u", "--set-upstream", "--no-verify", "-q", "--quiet", "-v", "--verbose"}
)
_WT_BRANCH_RE = re.compile(r"^wt/([A-Za-z0-9_.-]+?)-\d+$")


def _role_slug(role: str | None) -> str:
    """Mirror of worktree_manager.sanitize_ref_component for the role part of
    a `wt/<role>-<ts>` branch (kept local — this module is a stdlib leaf):
    `backend#3` → `backend-3`."""
    raw = (role or "").strip().lower()
    return re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("-")


def _push_is_own_worktree_branch(cmd: str, role: str | None) -> bool:
    """Every `git push` in *cmd* names only the pane's own `wt/<slug>-<ts>`
    branch with benign flags — see `_GIT_PUSH_TAIL`'s note."""
    slug = _role_slug(role)
    if not slug:
        return False
    hits = list(_GIT_PUSH_TAIL.finditer(cmd))
    if not hits:
        return False
    for m in hits:
        positionals: list[str] = []
        for tok in m.group("tail").split():
            if tok.startswith("-"):
                if tok.startswith("--force") or tok in _PUSH_FORBIDDEN_FLAGS:
                    return False
                if tok not in _PUSH_OK_FLAGS:
                    return False
                continue
            positionals.append(tok)
        # `git push <remote> <refspec>…` — the remote is never a refspec; a
        # bare `git push` / `git push origin` pushes an unnamed HEAD → deny.
        remote, refspecs = positionals[:1], positionals[1:]
        if not remote or not refspecs or "/" in remote[0] or ":" in remote[0]:
            return False
        for ref in refspecs:
            if ref.startswith("+") or ":" in ref:
                return False
            wm = _WT_BRANCH_RE.match(ref)
            if not wm or wm.group(1) != slug:
                return False
    return True


# #609 H2: `cmd /c <inner>` / `pwsh -c "<inner>"` / `powershell -Command
# "<inner>"` / `bash -c '<inner>'` / `sh -c "<inner>"` run *inner* exactly
# as if it had been typed directly — every `_CMD_START`-anchored rule in
# this module only ever recognised a command at true start-of-string, right
# after a shell separator, or right after `sudo `/the `rtk` prefix, so a
# destructive command spelled behind one of these shell wrappers sailed
# straight through unrecognised (confirmed live: `cmd /c git restore .` and
# `pwsh -c "git restore ."` both bypassed the restore guard outright).
_SHELL_WRAPPER_RE = re.compile(
    r"^\s*(?:cmd(?:\.exe)?(?:\s+/[A-Za-z]+)*\s+/c\s+"
    r"|pwsh(?:\.exe)?(?:\s+-[\w-]+)*\s+(?:-c|-Command)\s+"
    r"|powershell(?:\.exe)?(?:\s+-[\w-]+)*\s+(?:-c|-Command)\s+"
    r"|bash(?:\s+-[\w-]+)*\s+-c\s+"
    r"|sh(?:\s+-[\w-]+)*\s+-c\s+)(?P<body>.+)$",
    re.I | re.S,
)

# #609 H2 round 2: the wrapper regex above was only ever applied to the
# WHOLE raw command, so `foo && cmd /c git restore .` and `rtk proxy cmd /c
# git stash` sailed through unrecognised — the wrapper never sat at true
# start-of-string, it sat after a chain separator or the `rtk`/`rtk proxy`
# prefix (proven live: `pane_guard.classify` on both shapes still ALLOWed a
# shared-tree `git restore`/`git stash`). Every rule below already tolerates
# a wrapper anywhere a *segment* can start (`_CMD_START` = start-of-string,
# after `&&`/`||`/`;`/`|`/newline, or after `sudo `) — this now unwraps at
# each of those positions too, not just position zero of the whole string.
_SEG_RTK_PREFIX_RE = re.compile(
    r"^\s*rtk(?:\.(?:exe|cmd|bat|ps1))?(?![\w-])\s+(?:proxy(?![\w-])\s+)?", re.I
)
_SEG_SUDO_PREFIX_RE = re.compile(r"^\s*sudo\s+", re.I)
_SEG_TIME_PREFIX_RE = re.compile(r"^\s*time\s+", re.I)
_SEG_ENV_PREFIX_RE = re.compile(r"^\s*env\s+(?:[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)*", re.I)
_SEG_PREFIX_PATTERNS = (
    _SEG_RTK_PREFIX_RE,
    _SEG_SUDO_PREFIX_RE,
    _SEG_TIME_PREFIX_RE,
    _SEG_ENV_PREFIX_RE,
)

# Quote-aware split of a command into chain segments on top-level
# `&&`/`||`/`;`/`|`/newline — the alternatives before the `sep` group consume
# a whole quoted string atomically so a separator CHARACTER inside a quoted
# argument (`echo "cmd /c git restore ."`, no chain operator at all here,
# but the same machinery would also protect `echo "a && b" && cmd /c ...`)
# is never mistaken for a split point.
_CHAIN_SEP_RE = re.compile(r'"(?:[^"\\]|\\.)*"' r"|'[^']*'" r"|(?P<sep>\r\n|\n|\|\||&&|[;|&])")


def _split_chain_segments(cmd: str) -> list[str]:
    """Split *cmd* into `[seg0, sep0, seg1, sep1, ..., segN]` — always starts
    and ends with a (possibly empty) segment, separators verbatim so
    rejoining with `"".join(...)` reproduces *cmd* unchanged."""
    parts: list[str] = []
    pos = 0
    for m in _CHAIN_SEP_RE.finditer(cmd):
        sep = m.group("sep")
        if sep is None:
            continue  # matched a quoted string, not a split point
        parts.append(cmd[pos : m.start()])
        parts.append(sep)
        pos = m.end()
    parts.append(cmd[pos:])
    return parts


def _unwrap_segment(segment: str, _depth: int = 0) -> str:
    """Peel off up to 4 layers of `rtk [proxy]`/`sudo`/`time`/`env VAR=x`
    prefix plus one recognised shell/interpreter wrapper from a single chain
    segment, recursively (#609 H2). Returns *segment* unchanged when no
    wrapper is present — losing a plain prefix on an unwrapped segment is
    harmless since `_CMD_START` already treats the prefix as optional."""
    if _depth >= 4:
        return segment
    stripped = segment
    while True:
        for pattern in _SEG_PREFIX_PATTERNS:
            m = pattern.match(stripped)
            if m and m.end() > 0:
                stripped = stripped[m.end() :]
                break
        else:
            break
    m = _SHELL_WRAPPER_RE.match(stripped)
    if not m:
        return segment
    inner = _strip_matching_quotes(m.group("body"))
    return _unwrap_segment(inner, _depth + 1)


def _unwrap_shell_wrappers(cmd: str) -> str:
    """Unwrap a recognised shell/interpreter wrapper in EVERY chain segment
    of *cmd* (#609 H2), not just at position zero of the whole string."""
    parts = _split_chain_segments(cmd)
    for i in range(0, len(parts), 2):
        parts[i] = _unwrap_segment(parts[i])
    return "".join(parts)


# ── #633: Cross-cockpit protection & app boot guard ──
# Root incident (2026-09-15 11:39): a dev cockpit pane ran `python -m agent_takkub`
# using system python which booted prod installed data_home (~/.agent-takkub),
# killed the prod cockpit process, and wrote empty configuration files into prod root.
#
# This guard layer enforces:
# 1. Protection of every other cockpit DATA_HOME on the machine (write/move/delete denied).
# 2. Protection of every other cockpit process / child process (kill denied).
# 3. Blocking booting bare `agent_takkub` or launchers or global npm installs
#    across ALL roles including Lead, unless temp AGENT_TAKKUB_HOME is supplied.


def _instance_lock_key(data_home: pathlib.Path | str | None = None) -> str:
    """Short stable key derived from DATA_HOME, identical to app.py's formula."""
    if data_home is None:
        home = get_own_data_home()
    else:
        home = pathlib.Path(data_home).resolve()
    return hashlib.sha1(str(home).encode("utf-8")).hexdigest()[:12]


def get_own_data_home() -> pathlib.Path:
    """Return the resolved canonical DATA_HOME path of the cockpit instance
    this pane belongs to."""
    override = os.environ.get("TAKKUB_PORT_FILE", "").strip()
    if override:
        p = pathlib.Path(override)
        if p.name == "port" and p.parent.name == "runtime":
            return p.parent.parent.resolve()
    storage = os.environ.get("TAKKUB_STORAGE_ROOT", "").strip()
    if storage:
        sp = pathlib.Path(storage)
        if sp.name == "v2":
            return sp.parent.resolve()
        return sp.resolve()
    artifacts = os.environ.get("TAKKUB_ARTIFACTS_DIR", "").strip()
    if artifacts:
        ap = pathlib.Path(artifacts)
        parts = ap.parts
        if "runtime" in parts:
            idx = parts.index("runtime")
            return pathlib.Path(*parts[:idx]).resolve()
    try:
        from . import config

        return config.DATA_HOME.resolve()
    except Exception:
        return pathlib.Path.cwd().resolve()


def get_protected_data_homes(*, own_home: pathlib.Path | None = None) -> frozenset[pathlib.Path]:
    """Return all known cockpit DATA_HOME directories on the host that do NOT
    belong to this pane's own instance."""
    if own_home is None:
        own_home = get_own_data_home()
    own_resolved = own_home.resolve()

    candidates: set[pathlib.Path] = set()

    # 1. Standard installed default
    candidates.add((pathlib.Path.home() / ".agent-takkub").resolve())

    # 2. Source repo root (if dev checkout)
    try:
        from . import config

        candidates.add(config.REPO_ROOT.resolve())
    except Exception:
        pass

    # 3. Explicit AGENT_TAKKUB_HOME in environment
    env_home = os.environ.get("AGENT_TAKKUB_HOME", "").strip()
    if env_home:
        candidates.add(pathlib.Path(env_home).resolve())

    # 4. Explicit test/custom override in environment
    env_extra = os.environ.get("TAKKUB_PROTECTED_DATA_HOMES", "").strip()
    if env_extra:
        for item in re.split(r"[,;]", env_extra):
            if item.strip():
                candidates.add(pathlib.Path(item.strip()).resolve())

    # 5. Check active lock files / port files in tempdir
    try:
        tdir = pathlib.Path(tempfile.gettempdir())
        for port_file in tdir.glob("agent-takkub-port.*"):
            try:
                from .config import check_cockpit_port_alive

                port_num = int(port_file.read_text(encoding="utf-8").strip())
                alive, info = check_cockpit_port_alive(port_num, timeout=0.1)
                if alive and info and info.get("data_home"):
                    candidates.add(pathlib.Path(info["data_home"]).resolve())
            except Exception:
                pass
    except Exception:
        pass

    return frozenset(c for c in candidates if c != own_resolved)


def get_foreign_cockpit_pids(*, own_home: pathlib.Path | None = None) -> frozenset[int]:
    """Return all process IDs that belong to another cockpit instance (or child
    processes of another cockpit instance)."""
    if own_home is None:
        own_home = get_own_data_home()
    own_resolved = own_home.resolve()
    own_lock_key = _instance_lock_key(own_resolved)

    pids: set[int] = set()

    # 1. Explicit test/custom override in environment
    env_pids = os.environ.get("TAKKUB_FOREIGN_PIDS", "").strip()
    if env_pids:
        for p_str in env_pids.split(","):
            if p_str.strip().isdigit():
                pids.add(int(p_str.strip()))

    # 2. From lock files in tempdir
    try:
        tdir = pathlib.Path(tempfile.gettempdir())
        for lock_file in tdir.glob("agent-takkub-cockpit-*.lock"):
            m = re.match(r"agent-takkub-cockpit-([a-f0-9]+)\.lock$", lock_file.name)
            if m and m.group(1) != own_lock_key:
                try:
                    line1 = lock_file.read_text(encoding="utf-8").splitlines()[0].strip()
                    if line1.isdigit():
                        pids.add(int(line1))
                except Exception:
                    pass
    except Exception:
        pass

    # 3. From active port files of protected DATA_HOMEs
    try:
        from .config import check_cockpit_port_alive

        for prot_home in get_protected_data_homes(own_home=own_resolved):
            pf = prot_home / "runtime" / "port"
            if pf.is_file():
                try:
                    port_num = int(pf.read_text(encoding="utf-8").strip())
                    alive, info = check_cockpit_port_alive(port_num, timeout=0.1)
                    if alive and info and info.get("pid"):
                        pids.add(int(info["pid"]))
                except Exception:
                    pass
    except Exception:
        pass

    # 4. Include descendants of foreign cockpit PIDs via psutil
    try:
        import psutil

        extra_children = set()
        for fpid in list(pids):
            try:
                proc = psutil.Process(fpid)
                if proc.is_running():
                    for child in proc.children(recursive=True):
                        extra_children.add(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        pids.update(extra_children)
    except Exception:
        pass

    return frozenset(pids)


def is_in_protected_data_home(
    target_path: str | pathlib.Path,
    *,
    cwd: str | pathlib.Path | None = None,
    own_home: pathlib.Path | None = None,
    protected_homes: Iterable[pathlib.Path] | None = None,
) -> tuple[bool, pathlib.Path | None]:
    """Check whether `target_path` resolves inside any protected DATA_HOME.
    Returns (True, matching_protected_home) if inside a protected DATA_HOME,
    or (False, None) if safe."""
    if not target_path:
        return False, None
    raw = str(target_path).strip().strip("\"'")
    if not raw:
        return False, None
    raw = os.path.expandvars(raw)
    raw = re.sub(
        r"\$env:([A-Za-z0-9_]+)", lambda m: os.environ.get(m.group(1), ""), raw, flags=re.I
    )
    raw = re.sub(r"\$([A-Za-z0-9_]+)", lambda m: os.environ.get(m.group(1), ""), raw)
    raw = os.path.expanduser(raw)
    p = pathlib.Path(raw)
    if not p.is_absolute():
        base = pathlib.Path(cwd) if cwd else pathlib.Path.cwd()
        p = base / p
    try:
        resolved = p.resolve()
    except (OSError, ValueError):
        resolved = p.absolute()

    norm_target = resolved.as_posix().lower().rstrip("/")
    if protected_homes is None:
        protected_homes = get_protected_data_homes(own_home=own_home)

    for home in protected_homes:
        norm_home = home.resolve().as_posix().lower().rstrip("/")
        if norm_target == norm_home or norm_target.startswith(norm_home + "/"):
            return True, home
    return False, None


_PYTHON_M_BARE_AGENT_TAKKUB = re.compile(
    r"""(?:^|[;&|]\s*|\bexec\s+)(?:[\w.\\/:-]*[/\\])?(?:python[0-9.]*(?:\.exe)?|pythonw[0-9.]*(?:\.exe)?|py(?:\.exe)?)\s+(?:-[a-zA-Z0-9_-]+(?:\s+[^\s-]+)?\s+)*-m\s+["']?agent_takkub["']?(?:\s+|$|[;&|])""",
    re.IGNORECASE,
)
_AGENT_TAKKUB_LAUNCHER = re.compile(
    r"""(?:^|[;&|]\s*|\bexec\s+)(?:[\w.\\/:-]*[/\\])?agent-takkub(?:\.exe|\.cmd|\.bat|\.ps1)?(?:\s+|$|[;&|])""",
    re.IGNORECASE,
)
_NPM_GLOBAL_AGENT_TAKKUB = re.compile(
    r"""\bnpm\s+(?:i|install|add|update)\s+.*?(?:-g|--global)\s+.*?agent-takkub\b|\bnpm\s+(?:i|install|add|update)\s+.*?agent-takkub\b.*?(?:-g|--global)""",
    re.IGNORECASE,
)


def _has_temp_agent_takkub_home(cmd: str) -> bool:
    m = re.search(
        r"""(?:\bAGENT_TAKKUB_HOME\s*=\s*|\$env:AGENT_TAKKUB_HOME\s*=\s*|\bset\s+AGENT_TAKKUB_HOME\s*=\s*)["']?([^"';&\r\n]+)["']?""",
        cmd,
        re.I,
    )
    if not m:
        return False
    val = m.group(1).strip()
    norm = val.replace("\\", "/").lower()
    tdir = tempfile.gettempdir().replace("\\", "/").lower()
    return (tdir in norm) or ("/tmp" in norm) or ("/temp" in norm) or ("temp" in norm)


_PID_KILL_PATTERNS = (
    re.compile(r"\btaskkill(?:\.exe)?\s+.*?(?:/pid|-pid|--pid)\s+(\d+)", re.I),
    re.compile(r"\b(?:Stop-Process|spp)\s+.*?(?:-Id|-PID)\s+(\d+)", re.I),
    re.compile(r"(?:^|[;&|\s])kill\s+(?:-[A-Za-z0-9]+\s+)*(\d+)", re.I),
    re.compile(r"\bpkill\s+(?:-[A-Za-z0-9]+\s+)*(\d+)", re.I),
    re.compile(r"\bos\.kill\s*\(\s*(\d+)\s*,", re.I),
    re.compile(r"\bpsutil\.Process\s*\(\s*(\d+)\s*\)\.(?:kill|terminate)", re.I),
)

_IMAGE_KILL_PATTERNS = (
    re.compile(r"\btaskkill(?:\.exe)?\s+.*?(?:/im|-im|--im)\s+([^\s;&|]+)", re.I),
    re.compile(r"\b(?:Stop-Process|spp)\s+.*?(?:-Name|-ProcessName)\s+([^\s;&|]+)", re.I),
    re.compile(r"\bpkill\s+(?:-[A-Za-z0-9]+\s+)*([^\s;&|0-9][^\s;&|]*)", re.I),
    re.compile(r"\bkillall\s+(?:-[A-Za-z0-9]+\s+)*([^\s;&|]+)", re.I),
)

_REDIRECTION_TARGET_RE = re.compile(r"""(?:>>|>|1>|2>|\*>)\s*["']?([^\s"'&|;<>\r\n]+)["']?""")
_POWERSHELL_OUT_FILE_RE = re.compile(
    r"""\|\s*(?:Out-File|Set-Content|sc|Add-Content|ac|tee)\s+(?:(?:-FilePath|-Path)\s+)?["']?([^\s"'&|;\r\n]+)["']?""",
    re.I,
)

_FILE_DELETE_CMDS = re.compile(
    r"""(?:^|[;&|]\s*|\bexec\s+)(?:rm|del|erase|rmdir|rd|unlink|shred|Remove-Item|ri)\b(?P<args>[^;&|\r\n]+)""",
    re.I,
)
_FILE_MOVE_CMDS = re.compile(
    r"""(?:^|[;&|]\s*|\bexec\s+)(?:mv|move|Move-Item|mi)\b(?P<args>[^;&|\r\n]+)""",
    re.I,
)
_FILE_COPY_CMDS = re.compile(
    r"""(?:^|[;&|]\s*|\bexec\s+)(?:cp|copy|Copy-Item|cpi)\b(?P<args>[^;&|\r\n]+)""",
    re.I,
)
_FILE_WRITE_CMDS = re.compile(
    r"""(?:^|[;&|]\s*|\bexec\s+)(?:touch|mkdir|md|New-Item|ni|Set-Content|sc|Add-Content|ac|Out-File|truncate)\b(?P<args>[^;&|\r\n]+)""",
    re.I,
)
_GIT_MUTATE_CMD = re.compile(
    r"""\bgit\s+(?:-[A-Za-z0-9_-]+\s+)*(?:-C|--git-dir=|--work-tree=)\s*["']?([^\s"';&|\r\n]+)["']?\s+([A-Za-z0-9_-]+)""",
    re.I,
)
_PYTHON_INLINE_WRITE = re.compile(
    r"""(?:python[0-9.]*(?:\.exe)?|pythonw[0-9.]*(?:\.exe)?|py(?:\.exe)?)\s+[^;&|]*-c\s+["'](?P<code>.+)["']""",
    re.I,
)


def _split_args_tokens(args_str: str) -> list[str]:
    return [m.group(0).strip("\"'") for m in re.finditer(r"""[^\s"']+|"[^"]*"|'[^']*'""", args_str)]


def evaluate_instance_guard(
    command: str,
    role: str | None = None,
    *,
    cwd: str | None = None,
    own_home: pathlib.Path | None = None,
    protected_homes: Iterable[pathlib.Path] | None = None,
    foreign_pids: Iterable[int] | None = None,
) -> Verdict | None:
    """Evaluate #633 cross-instance protection and app boot guard rules on a command.
    Returns a Verdict(False, ...) if blocked, or None if safe."""
    cmd = (command or "").strip()
    if not cmd:
        return None

    if protected_homes is None:
        protected_homes = get_protected_data_homes(own_home=own_home)

    if foreign_pids is None:
        foreign_pids_set = get_foreign_cockpit_pids(own_home=own_home)
    else:
        foreign_pids_set = frozenset(foreign_pids)

    # Check for temporary AGENT_TAKKUB_HOME anywhere in the full command
    has_temp_home = _has_temp_agent_takkub_home(cmd)

    parts = _split_chain_segments(cmd)
    for i in range(0, len(parts), 2):
        seg = _unwrap_segment(parts[i]).strip()
        while True:
            for pat in _SEG_PREFIX_PATTERNS:
                m = pat.match(seg)
                if m and m.end() > 0:
                    seg = seg[m.end() :].strip()
                    break
            else:
                break
        if not seg:
            continue

        # 1. Check App Booting
        if _PYTHON_M_BARE_AGENT_TAKKUB.search(seg):
            if not has_temp_home:
                role_desc = f"role `{role}`" if role else "Pane"
                return Verdict(
                    False,
                    rule="cli_invocation:python_m_agent_takkub",
                    reason=(
                        f"{role_desc} รัน `python -m agent_takkub` ไม่ได้ (นโยบาย cockpit #633). "
                        "ห้ามบูต app ซ้อนหรือเข้าถึง prod DATA_HOME เว้นแต่มี AGENT_TAKKUB_HOME ชี้ temp ในคำสั่งเดียวกัน. "
                        "เรียก CLI ด้วย `takkub <cmd>` ตรงๆ"
                    ),
                )
        if _AGENT_TAKKUB_LAUNCHER.search(seg):
            if not has_temp_home:
                role_desc = f"role `{role}`" if role else "Pane"
                return Verdict(
                    False,
                    rule="instance_guard:app_boot",
                    reason=(
                        f"{role_desc} เรียก app launcher `agent-takkub` ไม่ได้ (นโยบาย cockpit #633). "
                        "ห้ามบูต app ซ้อนเว้นแต่มี AGENT_TAKKUB_HOME ชี้ temp ในคำสั่งเดียวกัน"
                    ),
                )
        if _NPM_GLOBAL_AGENT_TAKKUB.search(seg):
            role_desc = f"role `{role}`" if role else "Pane"
            return Verdict(
                False,
                rule="instance_guard:app_boot",
                reason=(
                    f"{role_desc} ติดตั้ง global package `npm -g agent-takkub` ไม่ได้ (นโยบาย cockpit #633)"
                ),
            )

        # 2. Check Process Termination
        for pat in _PID_KILL_PATTERNS:
            for m in pat.finditer(seg):
                target_pid = int(m.group(1))
                if target_pid in foreign_pids_set:
                    return Verdict(
                        False,
                        rule="instance_guard:kill_foreign_instance",
                        reason=f"ห้าม kill process PID {target_pid} ซึ่งเป็นของ cockpit instance อื่น (นโยบาย cockpit #633)",
                    )
                try:
                    import psutil

                    proc = psutil.Process(target_pid)
                    if proc.is_running():
                        proc_cwd = proc.cwd()
                        cwd_prot, prot_home = is_in_protected_data_home(
                            proc_cwd, cwd=cwd, own_home=own_home, protected_homes=protected_homes
                        )
                        if cwd_prot:
                            return Verdict(
                                False,
                                rule="instance_guard:kill_foreign_instance",
                                reason=f"ห้าม kill process PID {target_pid} ซึ่งทำงานอยู่ใน Protected DATA_HOME ({prot_home}) (นโยบาย cockpit #633)",
                            )
                except Exception:
                    pass

        # Image kill of python/pythonw/agent-takkub:
        # Non-unguarded roles (frontend, backend, etc.) are already unconditionally
        # denied for ANY image kill by _HOST_DESTRUCTIVE_PATTERNS (rule: host_destructive:*).
        # For unguarded roles (lead, shell) who bypass host_destructive, instance_guard
        # denies image kill if foreign cockpits exist.
        if role in _UNGUARDED_ROLES or not role:
            for pat in _IMAGE_KILL_PATTERNS:
                m = pat.search(seg)
                if m:
                    image = m.group(1).lower()
                    if re.search(r"^(?:python|pythonw|agent-takkub)", image):
                        if foreign_pids_set:
                            return Verdict(
                                False,
                                rule="instance_guard:kill_foreign_instance",
                                reason=f"ห้าม kill process ด้วย image name '{image}' เพราะมี cockpit instance อื่นกำลังทำงานอยู่บนเครื่อง (นโยบาย cockpit #633)",
                            )

        # 3. Check Redirections to Protected DATA_HOME
        for m in _REDIRECTION_TARGET_RE.finditer(seg):
            target = m.group(1)
            in_prot, prot_home = is_in_protected_data_home(
                target, cwd=cwd, own_home=own_home, protected_homes=protected_homes
            )
            if in_prot:
                return Verdict(
                    False,
                    rule="instance_guard:protected_data_home",
                    reason=f"ห้ามเขียนหรือ redirect ข้อมูลลงใน Protected DATA_HOME ({prot_home}): {target} (#633)",
                )
        for m in _POWERSHELL_OUT_FILE_RE.finditer(seg):
            target = m.group(1)
            in_prot, prot_home = is_in_protected_data_home(
                target, cwd=cwd, own_home=own_home, protected_homes=protected_homes
            )
            if in_prot:
                return Verdict(
                    False,
                    rule="instance_guard:protected_data_home",
                    reason=f"ห้ามเขียนไฟล์ลงใน Protected DATA_HOME ({prot_home}): {target} (#633)",
                )

        # 4. Check File Deletions
        for m in _FILE_DELETE_CMDS.finditer(seg):
            args = m.group("args")
            tokens = _split_args_tokens(args)
            for t in tokens:
                if t.startswith(("-", "/")):
                    continue
                in_prot, prot_home = is_in_protected_data_home(
                    t, cwd=cwd, own_home=own_home, protected_homes=protected_homes
                )
                if in_prot:
                    return Verdict(
                        False,
                        rule="instance_guard:protected_data_home",
                        reason=f"ห้ามลบไฟล์ใน Protected DATA_HOME ({prot_home}): {t} (#633)",
                    )

        # 5. Check File Moves
        for m in _FILE_MOVE_CMDS.finditer(seg):
            args = m.group("args")
            tokens = _split_args_tokens(args)
            for t in tokens:
                if t.startswith(("-", "/")):
                    continue
                in_prot, prot_home = is_in_protected_data_home(
                    t, cwd=cwd, own_home=own_home, protected_homes=protected_homes
                )
                if in_prot:
                    return Verdict(
                        False,
                        rule="instance_guard:protected_data_home",
                        reason=f"ห้ามย้ายไฟล์ในหรือไปยัง Protected DATA_HOME ({prot_home}): {t} (#633)",
                    )

        # 6. Check File Copies (Destination only)
        for m in _FILE_COPY_CMDS.finditer(seg):
            args = m.group("args")
            dest_m = re.search(r"""-Destination\s+["']?([^\s"';&|]+)["']?""", args, re.I)
            if dest_m:
                dest = dest_m.group(1)
                in_prot, prot_home = is_in_protected_data_home(
                    dest, cwd=cwd, own_home=own_home, protected_homes=protected_homes
                )
                if in_prot:
                    return Verdict(
                        False,
                        rule="instance_guard:protected_data_home",
                        reason=f"ห้ามคัดลอกไฟล์ไปยังปลายทางใน Protected DATA_HOME ({prot_home}): {dest} (#633)",
                    )
            else:
                tokens = [t for t in _split_args_tokens(args) if not t.startswith(("-", "/"))]
                if len(tokens) >= 2:
                    dest = tokens[-1]
                    in_prot, prot_home = is_in_protected_data_home(
                        dest, cwd=cwd, own_home=own_home, protected_homes=protected_homes
                    )
                    if in_prot:
                        return Verdict(
                            False,
                            rule="instance_guard:protected_data_home",
                            reason=f"ห้ามคัดลอกไฟล์ไปยังปลายทางใน Protected DATA_HOME ({prot_home}): {dest} (#633)",
                        )

        # 7. Check File Writes / Creates
        for m in _FILE_WRITE_CMDS.finditer(seg):
            args = m.group("args")
            tokens = _split_args_tokens(args)
            for t in tokens:
                if t.startswith(("-", "/")):
                    continue
                in_prot, prot_home = is_in_protected_data_home(
                    t, cwd=cwd, own_home=own_home, protected_homes=protected_homes
                )
                if in_prot:
                    return Verdict(
                        False,
                        rule="instance_guard:protected_data_home",
                        reason=f"ห้ามเขียนหรือสร้างไฟล์ใน Protected DATA_HOME ({prot_home}): {t} (#633)",
                    )

        # 8. Check Git Mutate
        for m in _GIT_MUTATE_CMD.finditer(seg):
            path, subcmd = m.group(1), m.group(2).lower()
            in_prot, prot_home = is_in_protected_data_home(
                path, cwd=cwd, own_home=own_home, protected_homes=protected_homes
            )
            if in_prot:
                if subcmd not in (
                    "status",
                    "log",
                    "show",
                    "diff",
                    "blame",
                    "rev-parse",
                    "ls-files",
                    "cat-file",
                    "describe",
                ):
                    return Verdict(
                        False,
                        rule="instance_guard:protected_data_home",
                        reason=f"ห้ามรัน git mutating command (`{subcmd}`) ใน Protected DATA_HOME ({prot_home}) (#633)",
                    )

        # 9. Check Python Inline Writes
        for m in _PYTHON_INLINE_WRITE.finditer(seg):
            code = m.group("code")
            literals = re.findall(r"""['"]([^'"]+)['"]""", code)
            for lit in literals:
                in_prot, prot_home = is_in_protected_data_home(
                    lit, cwd=cwd, own_home=own_home, protected_homes=protected_homes
                )
                if in_prot:
                    if re.search(
                        r"""open\s*\([^)]*['"](?:w|a|r\+|w\+|x)|(?:\.write_text|\.write_bytes|\.unlink|\.rmdir)\s*\(|os\.(?:remove|unlink|rmdir|rename|replace)\s*\(|shutil\.(?:rmtree|move|copy)\s*\(""",
                        code,
                        re.I,
                    ):
                        return Verdict(
                            False,
                            rule="instance_guard:protected_data_home",
                            reason=f"ห้ามรัน Python code เขียนหรือลบไฟล์ใน Protected DATA_HOME ({prot_home}) (#633)",
                        )

    return None


# ── #609/#611 round 5: `-c <key>=<value>`/`--config-env` config injection ──
# `git -c diff.external=<script> diff` runs *<script>* as a subprocess —
# proven live (reviewer, round 5) to execute arbitrary commands regardless
# of which subcommand follows, and regardless of worktree ownership (the
# process spawns under the caller's own account either way). The pre-round-5
# `_GIT_CONFIG_KV`/`_resolve_git_command_aliases` machinery below only ever
# looked at `alias.*`/`core.*` keys (for alias-shadowing/carve-out-disabling
# purposes) — a `diff.external`/`core.sshCommand`/`core.pager`/`merge.*.
# driver`/`filter.*`/etc key sailed straight through unexamined. This is a
# SEPARATE, unconditional (no cwd/worktree carve-out — RCE doesn't care who
# owns the checkout) key-safe-list gate, checked before that machinery runs.
_GIT_SAFE_CONFIG_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^color\.", re.I),
    re.compile(r"^core\.quotepath$", re.I),
    re.compile(r"^log\.", re.I),
    re.compile(r"^diff\.renames$", re.I),
    re.compile(r"^diff\.algorithm$", re.I),
    re.compile(r"^status\.", re.I),
    re.compile(r"^advice\.", re.I),
    re.compile(r"^i18n\.", re.I),
    re.compile(r"^safe\.directory$", re.I),
)
# `core.pager`/`GIT_PAGER` are safe ONLY as one of these three bare viewer
# names — no shell metacharacters, no arguments, so there is nothing for git
# to pass through to a shell.
_SAFE_PAGER_VALUES = frozenset({"cat", "less", "more"})


def _core_pager_value_safe(value: str | None) -> bool:
    return value is not None and _strip_matching_quotes(value) in _SAFE_PAGER_VALUES


def _config_key_value_safe(key: str, value: str | None) -> bool:
    """Whether setting *key* to *value* via `-c`/`--config-env`/`git config`
    is safe regardless of who runs it — read/display-only settings that
    can't redirect where git operates or spawn a subprocess. `value=None`
    means "unknown/not literally on the command line" (e.g. `--config-env`'s
    value is an env var NAME, not the actual value) — `core.pager`/
    `credential.helper` need a known-safe literal value, so an unknown value
    for those two is NOT safe; every other safe-listed key doesn't care what
    the value is (`log.date=relative`, `status.short=true`, … can't do harm)."""
    k = key.strip().lower()
    if k == "core.pager":
        return _core_pager_value_safe(value)
    if k == "credential.helper":
        return value is not None and _strip_matching_quotes(value) == ""
    return any(p.match(k) for p in _GIT_SAFE_CONFIG_KEY_PATTERNS)


# Case-SENSITIVE on purpose: `-c` (lowercase, config override) and `-C`
# (uppercase, "run as if started in <path>" — an entirely different flag,
# already handled by `_explicit_git_target`/`_in_worktree`) are distinct
# git flags. An earlier `re.I` here matched `-C <path>` as if it were a
# `-c` config pair too, false-positive-denying every `-C`-carrying command
# on this codebase's own test suite.
_DASH_C_KV_RE = re.compile(r"(?<![A-Za-z-])-c\s+(?:\"([^\"]*)\"|'([^']*)'|(\S+))")
_CONFIG_ENV_KV_RE = re.compile(r"--config-env(?:=|\s+)(?:\"([^\"]*)\"|'([^']*)'|(\S+))")


def _extract_dash_c_pairs(flags_text: str) -> list[tuple[str, str | None]]:
    """Every `-c <key>=<value>`/`--config-env[=]<key>=<envvar>` pair found in
    *flags_text* (the raw text `_GIT_INVOCATION_RE` captured between the git
    binary and its subcommand). No `=` in the token (a malformed/truncated
    `-c`) is treated as `(token, None)` — unknown value, fails closed via
    `_config_key_value_safe`."""
    pairs: list[tuple[str, str | None]] = []
    for rx in (_DASH_C_KV_RE, _CONFIG_ENV_KV_RE):
        for m in rx.finditer(flags_text):
            raw = next(g for g in m.groups() if g is not None)
            if "=" in raw:
                key, val = raw.split("=", 1)
            else:
                key, val = raw, None
            pairs.append((key, val))
    return pairs


def _git_dash_c_deny_key(cmd: str) -> str | None:
    """The first unsafe `-c`/`--config-env` key found in any git invocation
    in *cmd*, or `None` when every one (if any) is safe-listed. Checked
    unconditionally in `classify()` — before role/worktree/subcommand
    checks — since this is a code-execution/behavior-injection vector, not a
    shared-tree ownership one."""
    for m in _GIT_INVOCATION_RE.finditer(cmd):
        for key, val in _extract_dash_c_pairs(m.group("flags")):
            if not _config_key_value_safe(key, val):
                return key
    return None


# Same class of injection, via environment variable instead of a `-c` flag —
# `GIT_SSH_COMMAND=<script> git fetch …`, `GIT_EDITOR=<script> git commit`,
# `GIT_EXTERNAL_DIFF=<script> git diff`, `GIT_CONFIG_PARAMETERS`/
# `GIT_CONFIG_COUNT`+`GIT_CONFIG_KEY_n`+`GIT_CONFIG_VALUE_n` (the env-var
# equivalent of a whole `-c` list), `GIT_TEMPLATE_DIR`/`GIT_EXEC_PATH`
# (redirect what git treats as trusted hook/helper code). Matched the same
# unanchored way as `_GIT_ENV_TARGET_RE` (GIT_DIR-family) above — `set
# VAR=…`/`$env:VAR = …` sit in their own chain segment ahead of `git`, not
# immediately in front of it. No "provably safe path" concept applies here
# (unlike GIT_DIR) — these vars inject BEHAVIOR, not a redirect target — so
# any value is unsafe except `GIT_PAGER` naming one of the three safe
# viewers, matching `core.pager` above.
_DANGEROUS_GIT_ENV_VAR_RE = re.compile(
    r"(?:\bset\s+|\$env:)?"
    r"\b(?P<var>GIT_CONFIG_PARAMETERS|GIT_CONFIG_COUNT|GIT_CONFIG_KEY_\d+|"
    r"GIT_CONFIG_VALUE_\d+|GIT_EXTERNAL_DIFF|GIT_SSH_COMMAND|GIT_EDITOR|"
    r"GIT_SEQUENCE_EDITOR|GIT_TEMPLATE_DIR|GIT_EXEC_PATH|GIT_PAGER)\b"
    r"\s*=\s*(?P<val>\"[^\"]*\"|'[^']*'|[^\s&;|\r\n]*)",
    re.I | re.M,
)


def _dangerous_git_env_var(raw_cmd: str) -> str | None:
    """The first dangerous GIT_*-family env-var assignment in *raw_cmd*
    (PRE-unwrap — see `_git_dir_env_override`'s note on why), or `None`.
    Unconditional, same as `_git_dash_c_deny_key` — an exported var changes
    behavior for every git invocation in the rest of the command line, not
    just one scoped to a role's own worktree."""
    for m in _DANGEROUS_GIT_ENV_VAR_RE.finditer(raw_cmd):
        var = m.group("var").upper()
        if var == "GIT_PAGER" and _core_pager_value_safe(m.group("val")):
            continue
        return var
    return None


# #609 H2: `-c alias.<name>=<value>` defines a git alias inline, on the same
# command line — `git -c alias.discard=restore discard .` runs `restore`
# even though the literal subcommand token is `discard`, which matches none
# of the deny patterns above (they only know real git verb names, not
# arbitrary user-chosen alias names). `-c core.*` doesn't rename anything
# but CAN redirect where git actually operates (`core.worktree`, …), which
# this module has no way to verify from text alone — treated conservatively
# below (disables the worktree carve-out, never widens it).
_GIT_CONFIG_KV = re.compile(
    r"-c\s+(?P<key>alias\.[\w-]+|core\.[\w.-]+)=(?P<val>\"[^\"]*\"|'[^']*'|\S+)",
    re.I,
)
_GIT_SUBCMD_TOKEN = re.compile(rf"{_CMD_START}{_GIT_BIN}(?![\w-]){_GIT_SUBCMD_GAP}(?P<sub>[\w-]+)")
# Real git verbs a locally-defined alias must never be allowed to shadow —
# if the subcommand-position token is already a known verb, it IS that verb
# regardless of any same-named `-c alias.<verb>=…` on the same line.
_KNOWN_GIT_SUBCOMMANDS = frozenset(
    {
        "commit",
        "push",
        "reset",
        "branch",
        "tag",
        "rebase",
        "checkout",
        "switch",
        "restore",
        "clean",
        "stash",
        "merge",
        "status",
        "diff",
        "log",
        "show",
        "fetch",
        "pull",
        "add",
        "config",
        "init",
        "clone",
        "remote",
        "merge-base",
        "merge-tree",
        "commit-tree",
        "checkout-index",
        "rev-parse",
        "worktree",
        "cherry-pick",
        "revert",
        "blame",
        "describe",
        "shortlog",
    }
)


def _resolve_git_command_aliases(cmd: str) -> tuple[str, bool]:
    """Best-effort inline alias resolution (#609 H2): returns
    `(normalized_cmd, config_override)` where *normalized_cmd* has every
    subcommand-position token that matches a `-c alias.<name>=<value>`
    defined earlier in the SAME command replaced with *value*'s first word
    — so every existing literal-subcommand deny pattern sees the real verb
    git would actually run. *config_override* is True when a `-c core.*`
    override, or an alias this function could not resolve (a `!shell`
    alias, or one with no value), is present anywhere in *cmd* — the caller
    uses it to refuse the worktree carve-out for that invocation, since an
    unresolvable config override could be redirecting where git operates."""
    alias_map: dict[str, str] = {}
    unresolved = False
    core_override = False
    for m in _GIT_CONFIG_KV.finditer(cmd):
        key = m.group("key")
        val = _strip_matching_quotes(m.group("val"))
        if key.lower().startswith("alias."):
            name = key.split(".", 1)[1].lower()
            words = val.lstrip("!").split()
            if val.strip().startswith("!") or not words:
                unresolved = True
            else:
                alias_map[name] = words[0].lower()
        else:
            core_override = True

    if not alias_map:
        return cmd, (core_override or unresolved)

    pieces: list[str] = []
    pos = 0
    changed = False
    for m in _GIT_SUBCMD_TOKEN.finditer(cmd):
        sub = m.group("sub").lower()
        if sub in _KNOWN_GIT_SUBCOMMANDS:
            continue  # a real subcommand shadows any same-named alias
        resolved = alias_map.get(sub)
        if resolved is None:
            continue
        start, end = m.span("sub")
        pieces.append(cmd[pos:start])
        pieces.append(resolved)
        pos = end
        changed = True
    pieces.append(cmd[pos:])
    return ("".join(pieces) if changed else cmd), (core_override or unresolved)


# ── #609 round 4: default-deny for git on the shared tree ──────────────────
# See the module docstring's "tenth rule" for why this replaced growing
# `_GIT_LEAD_ONLY_PATTERNS` one subcommand at a time. Generic invocation
# finder — same shape as `_GIT_PUSH_TAIL`/`_GIT_STASH_PATTERN` above, just not
# anchored to one literal subcommand name. `flags` (round 5) captures the raw
# text between the git binary and the subcommand — everything
# `_GIT_SUBCMD_GAP_FLAGS` swallowed (`-c ...`, `--config-env ...`, `-C ...`,
# …) — so `_git_dash_c_deny_key` can re-scan it for unsafe `-c`/
# `--config-env` keys without a second traversal of *cmd*.
_GIT_INVOCATION_RE = re.compile(
    rf"{_CMD_START}{_GIT_BIN}(?![\w-])(?P<flags>{_GIT_SUBCMD_GAP_FLAGS})\s+"
    rf"(?P<sub>[\w-]+){_SUBCMD_END}(?P<tail>[^\n|;&]*)",
    re.M,
)

_GIT_SHARED_TREE_DENY_TEXT = (
    "บน shared tree pane ใช้ได้เฉพาะคำสั่ง git อ่าน/เพิ่ม — ใช้ `--isolation worktree` หรือให้ Lead ทำ"
)

# Subcommands already given their own carve-out-aware verdict earlier in
# `classify()` (commit/merge/push/stash/config each check `in_worktree`
# themselves; checkout/restore/switch/rebase are unconditional-subcommand
# denies with no flag shape to re-check) — skipped here so this default-deny
# pass never re-litigates a verdict `classify()` already returned.
_GIT_SHARED_TREE_HANDLED_ELSEWHERE = frozenset(
    {"commit", "merge", "push", "stash", "config", "checkout", "restore", "switch", "rebase"}
)

# Fully permissive regardless of flags — either provably read-only, or
# additive-only (creates a loose object/ref-free commit without moving any
# ref or touching the working tree: `add`, `commit-tree`, `merge-tree`).
# `clean` stays here — its own `-f*` is already denied unconditionally by
# `_GIT_LEAD_ONLY_PATTERNS` ("clean-force") before this check ever runs.
# `branch`/`tag`/`fetch`/`mv`/`config` moved OUT to
# `_GIT_SHARED_TREE_RESTRICTED_CHECKS`/their own gate in round 5 — see
# `_branch_tail_allowed`/`_tag_tail_allowed`/`_fetch_tail_allowed`/
# `_mv_tail_allowed`/`_git_config_verdict`: each can still overwrite/mutate
# a ref or file another pane relies on (`branch -f`/`-M`, `tag -a`/`tag
# <name>`, `fetch <refspec>`, `mv -f`), which the pre-round-5 "none of them
# writes another pane's working tree" reasoning missed.
_GIT_SHARED_TREE_ALLOW_PLAIN = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "blame",
        "rev-parse",
        "rev-list",
        "ls-files",
        "ls-tree",
        "cat-file",
        "grep",
        "describe",
        "name-rev",
        "merge-base",
        "merge-tree",
        "shortlog",
        "help",
        "version",
        "add",
        "check-ignore",
        "count-objects",
        "commit-tree",
        "clean",
    }
)


def _remote_tail_allowed(tail_tokens: list[str]) -> bool:
    """Bare `git remote` (lists remotes), `-v`/`--verbose`, `show`, and
    `get-url` are read-only; `add`/`remove`/`set-url`/`rename`/... mutate
    shared repo config."""
    if not tail_tokens:
        return True
    return tail_tokens[0] in ("-v", "--verbose", "show", "get-url")


def _worktree_tail_allowed(tail_tokens: list[str]) -> bool:
    """Only `list` — `add`/the admin sub-verbs (already unconditionally
    Lead-only via `_GIT_LEAD_ONLY_PATTERNS`) never reach this check."""
    return bool(tail_tokens) and tail_tokens[0] == "list"


def _rm_tail_allowed(tail_tokens: list[str]) -> bool:
    """Deny `--cached` (untracks without deleting — silently drops a file
    from everyone's next `status`/`diff`), any `-r`/recursive short flag, and
    (round 5) `-f`/`--force` (bypasses the up-to-date-in-index safety check,
    silently deleting a file another pane hasn't committed its edit to)."""
    for tok in tail_tokens:
        if tok in ("--cached", "--force"):
            return False
        if tok.startswith("-") and not tok.startswith("--") and "r" in tok[1:].lower():
            return False
        if tok.startswith("-") and not tok.startswith("--") and "f" in tok[1:].lower():
            return False
    return True


def _mv_tail_allowed(tail_tokens: list[str]) -> bool:
    """Round 5: deny `-f`/`--force` (silently overwrites an existing
    destination path — another pane's file — instead of erroring)."""
    for tok in tail_tokens:
        if tok == "--force":
            return False
        if tok.startswith("-") and not tok.startswith("--") and "f" in tok[1:].lower():
            return False
    return True


# Round 5: `-f`/`--force`/`-M` are handled unconditionally (no worktree
# carve-out either — see `_GIT_LEAD_ONLY_PATTERNS` "branch-force"/
# "branch-force-rename") since they overwrite a ref regardless of who owns
# the checkout running them. What's left to gate HERE (shared-tree only) is
# every other ref-mutating form: safe delete (`-d`), safe rename (`-m`), and
# upstream-tracking changes (`-u`/`--set-upstream-to`) — a `git branch
# <name>` with no such flag just creates a new, non-colliding branch and
# stays allowed (matches the module's "creating new refs in a worktree is
# fine, overwriting/deleting a shared one is not" stance).
_BRANCH_DENY_BARE_FLAGS = frozenset({"-d", "-m", "-u"})


def _branch_tail_allowed(tail_tokens: list[str]) -> bool:
    for tok in tail_tokens:
        base = tok.split("=", 1)[0]
        if base in _BRANCH_DENY_BARE_FLAGS or base == "--set-upstream-to":
            return False
    return True


# Round 5: bare `git tag`/`tag -l ...`/`tag --list ...`/`tag --merged ...`
# etc list or query existing tags — read-only. Anything else on the tail
# either creates/overwrites a tag (`tag <name>`, `-f`/`--force`, `-a`/
# `--annotate`, `-s`/`--sign` — a NEW tag another pane's `git fetch --tags`
# would then see) or is an unrecognised shape, denied the same fail-closed
# direction as everywhere else in this module. `-d` (delete) is already
# unconditionally Lead-only via `_GIT_LEAD_ONLY_PATTERNS` ("tag-delete") and
# never reaches this check.
_TAG_LIST_ONLY_FLAGS = frozenset(
    {
        "-l",
        "--list",
        "-n",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
    }
)


def _tag_tail_allowed(tail_tokens: list[str]) -> bool:
    if not tail_tokens:
        return True
    first = tail_tokens[0].split("=", 1)[0]
    return first in _TAG_LIST_ONLY_FLAGS or first == "--sort"


# Round 5: only a plain `fetch [--prune] [<remote>] [<branch>]` — no refspec
# (`:`), no forced-update refspec (`+`), no `--refmap`/`--update-head-ok`
# (both exist specifically to let a fetch move/create a ref another pane's
# fetch wouldn't expect), no `-f`/`--force`. Everything else denied.
_FETCH_SAFE_FLAGS = frozenset({"--prune", "-p"})
_FETCH_DENY_FLAG_PREFIXES = ("--refmap", "--update-head-ok", "--force")


def _fetch_tail_allowed(tail_tokens: list[str]) -> bool:
    positionals: list[str] = []
    for tok in tail_tokens:
        if tok == "-f":
            return False
        if tok in _FETCH_SAFE_FLAGS:
            continue
        if tok.startswith(_FETCH_DENY_FLAG_PREFIXES):
            return False
        if tok.startswith("-"):
            return False  # unrecognised flag — fail closed
        positionals.append(tok)
    if len(positionals) > 2:
        return False
    return not any(":" in p or p.startswith("+") for p in positionals)


def _update_index_tail_allowed(tail_tokens: list[str]) -> bool:
    """Only `--refresh` (re-stats tracked files, no content change)."""
    return "--refresh" in tail_tokens


def _notes_tail_allowed(tail_tokens: list[str]) -> bool:
    return bool(tail_tokens) and tail_tokens[0] == "show"


def _bisect_tail_allowed(tail_tokens: list[str]) -> bool:
    return bool(tail_tokens) and tail_tokens[0] in ("log", "view")


def _reflog_tail_allowed(tail_tokens: list[str]) -> bool:
    """Bare `git reflog` defaults to `show`; `expire`/`delete` rewrite a ref's
    reflog other worktrees sharing it can still see."""
    if not tail_tokens:
        return True
    return tail_tokens[0] in ("show", "list")


# Subcommands allowed only when their tail matches a narrower safe shape —
# checked with the invocation's already-split tail tokens.
_GIT_SHARED_TREE_RESTRICTED_CHECKS: dict[str, Callable[[list[str]], bool]] = {
    "remote": _remote_tail_allowed,
    "worktree": _worktree_tail_allowed,
    "rm": _rm_tail_allowed,
    "mv": _mv_tail_allowed,
    "branch": _branch_tail_allowed,
    "tag": _tag_tail_allowed,
    "fetch": _fetch_tail_allowed,
    "update-index": _update_index_tail_allowed,
    "notes": _notes_tail_allowed,
    "bisect": _bisect_tail_allowed,
    "reflog": _reflog_tail_allowed,
}


def _git_shared_tree_deny_rule(git_cmd: str) -> str | None:
    """The offending subcommand name to deny with, or `None` when every git
    invocation in *git_cmd* is on the shared-tree allow-list. Only meaningful
    when the caller has already established `in_worktree` is False — see the
    module docstring's "tenth rule"."""
    for m in _GIT_INVOCATION_RE.finditer(git_cmd):
        sub = m.group("sub").lower()
        if sub in _GIT_SHARED_TREE_HANDLED_ELSEWHERE:
            continue
        if sub in _GIT_SHARED_TREE_ALLOW_PLAIN:
            continue
        checker = _GIT_SHARED_TREE_RESTRICTED_CHECKS.get(sub)
        if checker is not None and checker(m.group("tail").split()):
            continue
        return sub
    return None


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
    scope: str | None = None,
    snapshot_path: pathlib.Path | None = None,
    machine_state_path: pathlib.Path | None = None,
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

    *scope* (#585): current task scope budget ("tiny" | "normal" | "deep").
    When "tiny", blocks `takkub qa-gate` and full-suite test runs.

    *snapshot_path* (#585 round 2): optional path to last-session.json for
    busy-machine checks. None uses RUNTIME_DIR / "last-session.json".

    *machine_state_path* (#587 B1): optional path to machine-state.json,
    checked BEFORE `snapshot_path`/last-session.json (see
    `_is_machine_busy_from_snapshot`'s docstring for the exact precedence).
    None uses RUNTIME_DIR / "machine-state.json" unless `snapshot_path` was
    given explicitly, which keeps the pre-#587 single-file behavior.
    """
    cmd = (command or "").strip()
    if not cmd:
        return Verdict(True)
    # #609 round 3: captured BEFORE unwrapping — `_unwrap_segment`'s existing
    # `env VAR=value` stripping deletes a `GIT_DIR=`/etc prefix outright, and
    # `_git_dir_env_override` below needs to see it. Unwrapping/stripping
    # never ADD a GIT_DIR-family assignment that wasn't already literally
    # present, so scanning the original text is never less complete than
    # scanning the unwrapped one.
    raw_cmd = cmd
    # #609 H2: unwrap `cmd /c ...` / `pwsh -c "..."` / etc so every rule
    # below sees the command that actually runs, not the wrapper spawning it.
    cmd = _unwrap_shell_wrappers(cmd)

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

    # #585 round 2/4: Deny heavy build or full suite when machine is busy
    # (other pane working or RAM/CPU overloaded).
    # Gated BEFORE _UNGUARDED_ROLES so Lead is caught; `shell` (user terminal)
    # and unknown/empty role (human outside cockpit) are exempt.
    if name and name != "shell":
        is_heavy, heavy_type = _is_heavy_build_or_suite(cmd)
        if is_heavy:
            busy, busy_reason = _is_machine_busy_from_snapshot(
                name, snapshot_path=snapshot_path, machine_state_path=machine_state_path
            )
            if busy:
                return Verdict(
                    False,
                    rule=f"busy_machine:{heavy_type}",
                    reason=(
                        f"เครื่องกำลังไม่ว่าง ({busy_reason}) — ห้ามรัน heavy build หรือ full test suite. "
                        "ใช้ targeted test/typecheck ไฟล์เดียว หรือรอให้เครื่องว่างก่อน"
                    ),
                )

    # #633: Cross-cockpit protection & app boot guard.
    # Default-deny for EVERY role including Lead and shell.
    # Protects other cockpit instances on the machine (e.g. prod ~/.agent-takkub).
    if name:
        inst_verdict = evaluate_instance_guard(raw_cmd, name, cwd=cwd)
        if inst_verdict and not inst_verdict.allowed:
            return inst_verdict

    if not name or name in _UNGUARDED_ROLES:
        return Verdict(True)

    # #585: When task scope is tiny, deny takkub qa-gate and full-suite test runs.
    # Targeted tests of the modified file remain allowed.
    norm_scope = (scope or "").strip().lower()
    if norm_scope == "tiny":
        if _TAKKUB_QA_GATE.search(cmd):
            return Verdict(
                False,
                rule="scope_tiny:qa_gate",
                reason=SCOPE_TINY_DENY_TEXT,
            )
        fs_rule = _full_suite_rule(cmd)
        if fs_rule is not None:
            return Verdict(
                False,
                rule=f"scope_tiny:{fs_rule}",
                reason=SCOPE_TINY_DENY_TEXT,
            )
        pm_m = _PM_TEST.search(cmd)
        if pm_m and not _tail_is_narrow(pm_m.group("tail")):
            return Verdict(
                False,
                rule="scope_tiny:pm_test",
                reason=SCOPE_TINY_DENY_TEXT,
            )

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

    if _PYTHON_M_AGENT_TAKKUB.search(cmd):
        return Verdict(
            False,
            rule="cli_invocation:python_m_agent_takkub",
            reason=(
                f"role `{name}` รัน `python -m agent_takkub` ไม่ได้ (นโยบาย cockpit). "
                "specialist เรียก CLI ด้วยคำสั่ง `takkub <cmd>` ตรงๆ เท่านั้น (`takkub` อยู่ใน PATH ของ pane แล้ว)"
            ),
        )

    # `tester` (optional role, on-demand — see .claude/agents/tester.md) exists
    # specifically to run the raw, un-narrowed test runner on a pane of its
    # own instead of every other role reaching for one in parallel — so it is
    # exempt from this one rule while every other guarded role stays denied.
    if name != "tester":
        full_suite = _full_suite_rule(cmd)
        if full_suite is not None:
            return Verdict(
                False,
                rule=f"full_suite:{full_suite}",
                reason=(
                    f"role `{name}` รัน raw full-suite ไม่ได้ (นโยบาย cockpit). {FULL_SUITE_RULE_TEXT}"
                ),
            )

    # #609/#611 round 5: config-injection RCE (`git -c diff.external=<script>
    # diff`, `GIT_SSH_COMMAND=<script> git fetch`, …) doesn't care whether
    # the invocation happens to be inside this role's own worktree — the
    # subprocess still spawns under the caller's own account either way.
    # Checked unconditionally, before alias resolution/worktree carve-outs.
    dash_c_key = _git_dash_c_deny_key(cmd)
    if dash_c_key is not None:
        return Verdict(
            False,
            rule="git_config_injection:dash_c",
            reason=(
                f"role `{name}` ใช้ `git -c {dash_c_key}=...`/`--config-env` แบบนี้ไม่ได้ "
                "(#609/#611 round 5 — key นี้ redirect พฤติกรรม git ไปรัน subprocess ได้ เช่น "
                "diff.external/core.sshCommand/core.hooksPath/alias.*, ไม่เกี่ยวกับ worktree "
                "ของใครเป็นเจ้าของ). ใช้ได้เฉพาะ key อ่าน/แสดงผลอย่างเดียว เช่น `color.*`, "
                "`core.pager=cat|less|more`, `log.*`, `status.*`, `advice.*`, `i18n.*`, "
                "`diff.renames`, `diff.algorithm`, `safe.directory`."
            ),
        )

    dangerous_env_var = _dangerous_git_env_var(raw_cmd)
    if dangerous_env_var is not None:
        return Verdict(
            False,
            rule="git_config_injection:env_var",
            reason=(
                f"role `{name}` ตั้งค่า `{dangerous_env_var}` ก่อนรัน git แบบนี้ไม่ได้ "
                "(#609/#611 round 5 — ตัวแปรนี้ redirect พฤติกรรม git ไปรัน subprocess/hook "
                "ได้เหมือน `-c`, ไม่เกี่ยวกับ worktree ของใครเป็นเจ้าของ)."
            ),
        )

    # #609 H2: resolve inline `-c alias.X=Y` before matching any deny
    # pattern by literal subcommand name (`git_cmd`), and refuse the
    # worktree carve-out outright whenever an unresolvable/`core.*` config
    # override is present (`effective_in_worktree`) — see
    # `_resolve_git_command_aliases`'s docstring.
    git_cmd, git_config_override = _resolve_git_command_aliases(cmd)
    # #609 H2/H1: ownership of the target worktree, not just cwd's raw
    # "/worktrees/" substring — see `_in_worktree`/`_worktree_role_owns`.
    # #609 round 3: a GIT_DIR-family env override (the flag form is already
    # folded into `_in_worktree` via `_explicit_git_target`) disables the
    # carve-out the same way an unresolvable `-c core.*` override does — see
    # `_git_dir_env_override`.
    in_worktree = (
        _in_worktree(cmd, cwd, role)
        and not git_config_override
        and not _git_dir_env_override(raw_cmd, role)
    )

    if not in_worktree and _GIT_COMMIT_PATTERN.search(git_cmd):
        return Verdict(
            False,
            rule="git_lead_only:commit",
            reason=(f"role `{name}` commit เองไม่ได้ (นโยบาย cockpit). {GIT_LEAD_ONLY_RULE_TEXT}"),
        )

    if not in_worktree and _GIT_MERGE_PATTERN.search(git_cmd):
        return Verdict(
            False,
            rule="git_lead_only:merge",
            reason=(f"role `{name}` ใช้คำสั่งนี้ไม่ได้ (นโยบาย cockpit). {GIT_LEAD_ONLY_RULE_TEXT}"),
        )

    # #609/#611 H1: `push`/`save`/`apply` are safe inside the pane's own
    # (correctly-owned) worktree; `pop`/`drop`/`clear`/`branch` mutate the
    # shared `refs/stash` stack and stay Lead-only EVEN there — see
    # `_git_stash_verdict`'s docstring.
    if _git_stash_verdict(git_cmd, in_worktree):
        return Verdict(
            False,
            rule="git_lead_only:stash",
            reason=(
                f"role `{name}` ใช้ `git stash` แบบนี้ไม่ได้ (#609/#611 — `refs/stash` ใช้ร่วมกันทุก "
                "worktree ของ repo เดียวกัน `pop`/`drop`/`clear`/`branch` ลบ entry ที่ pane อื่นเห็นอยู่ "
                "ได้แม้รันจาก worktree ของตัวเอง). "
                f"{GIT_LEAD_ONLY_RULE_TEXT} ใน worktree ของตัวเอง ใช้ได้: `git stash push`/`save`/`apply`. "
                "อ่านอย่างเดียวใช้ได้ทุกที่: `git stash list` / `git stash show`."
            ),
        )

    # #609/#611 round 5: `git config` writes the SHARED main `.git/config`
    # from any linked worktree by default — see `_git_config_verdict`'s
    # docstring.
    if _git_config_verdict(git_cmd, in_worktree):
        return Verdict(
            False,
            rule="git_lead_only:config",
            reason=(
                f"role `{name}` เขียน `git config` แบบนี้ไม่ได้ (#609/#611 round 5 — worktree ที่ "
                "link กันใช้ `.git/config` ไฟล์เดียวกันโดย default). อ่านได้เสมอ (`--get`/`--get-all`/"
                "`--list`/`-l`/`config <key>` เฉยๆ). เขียนได้เฉพาะ key ปลอดภัย (`color.*`, "
                "`core.quotepath`, `log.*`, `status.*`, `advice.*`, `i18n.*`, `diff.renames`, "
                "`diff.algorithm`, `safe.directory`) หรือ `user.name`/`user.email` local ใน "
                f"worktree ของตัวเอง. {GIT_LEAD_ONLY_RULE_TEXT}"
            ),
        )

    for rule, pattern in _GIT_LEAD_ONLY_PATTERNS:
        if rule == "push" and in_worktree and _push_is_own_worktree_branch(git_cmd, role):
            continue  # #438: own wt/<role>-<ts> branch, named explicitly, no force
        if rule in _WORKTREE_SAFE_RULES and in_worktree:
            continue  # #545: pane's own disposable worktree checkout
        if pattern.search(git_cmd):
            if rule == "push" and in_worktree:
                # #466 point 3: a push that's already inside the pane's own
                # worktree is the ONE `git_lead_only` case with a real carve-out
                # — the generic "Lead only" text alone doesn't say what WOULD
                # have passed. Spell out the exact expected shape with this
                # pane's own role slug substituted in, so the reply is
                # copy-pasteable rather than something the pane has to reverse-
                # engineer from GIT_LEAD_ONLY_RULE_TEXT's prose.
                slug = _role_slug(role)
                return Verdict(
                    False,
                    rule="git_lead_only:push",
                    reason=(
                        f"role `{name}` push แบบนี้ไม่ได้ (#438). ใน worktree ของตัวเอง push ได้"
                        f"เฉพาะรูปแบบนี้เท่านั้น: `git push -u origin wt/{slug}-<ts>` — "
                        "remote ต้องเป็น `origin` เดี่ยวๆ (ไม่ใช่ URL), refspec ต้องเป็นชื่อ branch "
                        f"`wt/{slug}-<ts>` ของตัวเองตรงๆ (ห้ามรูปแบบ `HEAD:wt/...` หรือ branch อื่น), "
                        "ห้าม `--force`/`-f`/`--force-with-lease`/`--delete`/`-d`/`--mirror`/"
                        "`--all`/`--tags`/`--prune`. "
                        f"{GIT_LEAD_ONLY_RULE_TEXT}"
                    ),
                )
            return Verdict(
                False,
                rule=f"git_lead_only:{rule}",
                reason=(f"role `{name}` ใช้คำสั่งนี้ไม่ได้ (นโยบาย cockpit). {GIT_LEAD_ONLY_RULE_TEXT}"),
            )

    # #609 round 4: default-deny for every OTHER git subcommand once the
    # target resolves outside this role's own worktree — see the module
    # docstring's "tenth rule" and `_git_shared_tree_deny_rule`.
    if not in_worktree:
        deny_sub = _git_shared_tree_deny_rule(git_cmd)
        if deny_sub is not None:
            return Verdict(
                False,
                rule=f"git_shared_default_deny:{deny_sub}",
                reason=(
                    f"role `{name}` ใช้ `git {deny_sub}` แบบนี้บน shared tree ไม่ได้. "
                    f"{_GIT_SHARED_TREE_DENY_TEXT}"
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
