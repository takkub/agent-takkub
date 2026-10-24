"""`takkub` argparse tree — moved out of `cli.build_parser` (a 1,900-line
function, 2026-10-24 review D). One `_add_<command>` per subcommand group,
in the original order. `c` is the `agent_takkub.cli` module: every
`set_defaults(func=c.cmd_…)` handler and shared constant still lives there,
so this module imports nothing from cli at import time (no cycle).
"""

from __future__ import annotations

import argparse
from types import ModuleType
from typing import Any


def _add_spawn(sub: Any, c: ModuleType) -> None:

    sp = sub.add_parser("spawn", help="open a pane for a role")
    sp.add_argument("--role", required=True)
    sp.add_argument("--cwd", default=None)
    sp.set_defaults(func=c.cmd_spawn)


def _add_assign(sub: Any, c: ModuleType) -> None:

    sa = sub.add_parser("assign", help="spawn (if needed) and send a task")
    sa.add_argument("--role", required=True)
    sa.add_argument("--cwd", default=None)
    sa.add_argument(
        "--scope",
        choices=("tiny", "normal", "deep", "auto"),
        default="auto",
        help="task scope budget (#585): tiny|normal|deep|auto (default: auto). "
        "tiny: small fixes/styling (forbids new test files, full suite, or qa-gate); "
        "normal: standard task (verify the change, no new test files); "
        "deep: high risk (schema/auth/payment/infra) — test real logic + allow gate.",
    )
    sa.add_argument(
        "--impact-plan-file",
        default=None,
        help="JSON impact plan for cross-flow changes (#833); persisted with the task",
    )
    sa.add_argument(
        "--spec-confirmation",
        default=None,
        metavar="HASH",
        help="legacy field; cannot confirm a task (the cockpit requires a user click)",
    )
    sa.add_argument(
        "--budget-confirmation",
        default=None,
        metavar="HASH",
        help="legacy field; cannot resume a capped task (the cockpit requires a user click)",
    )
    sa.add_argument(
        "--mode",
        choices=("pane", "subagent", "code", "e2e", "ui"),
        default=None,
        help="execution mode: pane (existing visible cockpit pane) or subagent "
        "(native same-provider child in the Lead process; no pane/model diversity). "
        "For --role reviewer: sub-mode code (default), e2e (browser/qa), or ui (critic/gemini). "
        "Omit to let the server auto-pick subagent for a short task with no "
        "isolation/model-diversity/plan need (#364 lever 2) — pass this flag "
        "explicitly to pin one mode and skip that auto-selection",
    )
    sa.add_argument(
        "--model",
        default=None,
        metavar="ID",
        help="override the model id for this assign's fresh spawn — must be a "
        "model id valid for the role's EFFECTIVE provider (its configured "
        "provider, or the one set by --provider on the same assign); it does "
        "NOT itself change which CLI runs (use --provider for that). Only "
        "takes effect when spawning a new pane; an already-running pane "
        "keeps its current model",
    )
    sa.add_argument(
        "--provider",
        default=None,
        metavar="NAME",
        help="force this ONE assign's fresh spawn onto a different CLI than "
        "the role's configured provider (issue #270 — e.g. reroute a "
        "codex-mapped role to claude when codex is boot-stalled/broken). A "
        "one-assign escape hatch, not a persistent remap (edit "
        "role-providers.json / Settings → Providers & Roles for that, which "
        "needs a cockpit restart to take effect). Only takes effect when "
        "spawning a new pane; an already-running pane keeps its current "
        "provider",
    )
    sa.add_argument(
        "--effort",
        choices=("low", "medium", "high"),
        default=None,
        help="override the reasoning-effort knob for this assign's fresh spawn "
        "(issue #323) — one-assign escape hatch, same shape as --model/--provider. "
        "Default: don't send anything, keep the role/provider's existing effort. "
        "Route mechanical work (rename, doc sync, running an existing test suite) "
        "at low for speed/cost; save high for design-sensitive or correctness-"
        "critical work. 4 providers accept an effort knob today (claude --effort, "
        "codex -c model_reasoning_effort=, gemini/agy --effort, all low/medium/"
        "high; claude alone also takes xhigh/max) — opencode/cursor have "
        "no CLI knob yet (GAP tracked in #103) and silently ignore this flag "
        "rather than erroring. Only takes effect when spawning a new pane; an "
        "already-running pane keeps its current effort",
    )
    sa.add_argument(
        "--backlog",
        default=None,
        metavar="ID",
        help="(#714) run this task under backlog card ID. Every assign runs "
        "under a card: without --backlog one is created from the task text "
        "automatically. Starting new work prints the other pending cards — "
        "tell the user about them.",
    )
    sa.add_argument(
        "--ack-ceiling",
        default=None,
        metavar="REASON",
        dest="ack_ceiling",
        help="(#762) the fix-loop ceiling refuses another fix assign of work that "
        "already failed 3x; pass this ONLY after the user approved trying again. "
        "REASON is logged to events.log.",
    )
    sa.add_argument(
        "--fresh-worktree",
        action="store_true",
        dest="fresh_worktree",
        help="with --isolation worktree: force a brand-new worktree even if this role's "
        "own unmerged worktree for the same --backlog card exists (default = reuse it, "
        "so a fix-loop re-assign lands where the work is; --base also forces new)",
    )
    sa.add_argument(
        "--distinct-from",
        default=None,
        metavar="ROLE",
        dest="distinct_from",
        help="cross-check pairing (#514): this task must never end up running "
        "the SAME provider as ROLE's pane. Consulted only if this pane later "
        "hits a quota limit and auto-resume looks for a fallback provider to "
        "reroute to — ROLE's current provider is excluded from the candidates "
        "so a correlated model error can't slip past both sides of the check. "
        "Does not affect this assign's own initial spawn/provider choice.",
    )
    sa.add_argument(
        "--team",
        default=None,
        metavar="PRESET",
        help="one-task team-preset override (#512), --role lead only: "
        "solo-lead|pair|full|auto. Sets the project's active override "
        "(wins over the project's standing preset until cleared/reassigned) "
        "and prepends a [system] notice to this task's text. Not 'custom' — "
        "that needs a roles/checker payload this one-line flag can't carry.",
    )
    sa.add_argument(
        "task",
        nargs="?",
        default=None,
        help="task content (positional) — omit and use --task-file instead when the "
        "text has backticks/$()/parens the sending shell would eat (#491); "
        '"-" reads the task from stdin',
    )
    sa.add_argument(
        "--task-file",
        default=None,
        metavar="PATH",
        help="read task content from this file (utf-8) instead of the positional arg — "
        "bypasses shell quoting entirely (#491); mutually exclusive with the positional "
        'task; PATH "-" reads from stdin',
    )
    sa.add_argument(
        "--requires-commit",
        action="store_true",
        dest="requires_commit",
        default=False,
        help="flag uncommitted changes to Lead on done (Lead reviews + commits; teammate ไม่ต้อง commit เอง)",
    )
    sa.add_argument(
        "--auto-chain",
        action="store_true",
        dest="auto_chain",
        default=False,
        help="after impl done, auto-trigger Lead to fire qa+reviewer "
        "without proposing (one-hop only — verify is terminal)",
    )
    sa.add_argument(
        "--shards",
        type=int,
        default=1,
        metavar="N",
        help="fan-out N ways. Default (#641): ONE pane that dispatches N native "
        "subagents of its own CLI (claude Agent / codex spawn_agent / agy "
        "run_subagent / opencode task) — no extra pane boots. Browser-QA "
        "shards (reviewer --mode e2e|ui), --plan, and providers without a "
        "native subagent fall back to N parallel shard panes (<role>#1 … "
        "<role>#N, each with TAKKUB_SHARD / TAKKUB_SHARD_TOTAL). See --fanout.",
    )
    sa.add_argument(
        "--fanout",
        choices=("auto", "pane", "subagent"),
        default="auto",
        help="(#641) how --shards runs: auto (default — subagent fan-out when the "
        "role's CLI has a native subagent tool and the role is not a browser-QA "
        "shard, else panes) · pane (force the legacy N shard panes) · subagent "
        "(force ONE pane + N native subagents; errors instead of falling back)",
    )
    sa.add_argument(
        "--plan",
        action="store_true",
        dest="plan",
        default=False,
        help="plan-first fan-out: spawn ONE planner pane that analyses the app "
        "and writes a balanced bucket plan, then auto-fan-out --shards N "
        "testers (each gets its bucket). Requires --shards >= 2",
    )
    sa.add_argument(
        "--isolation",
        choices=("shared", "worktree"),
        default="shared",
        help="pane workspace isolation (issue #81). 'shared' (default) = all "
        "panes share the project's git worktree. 'worktree' = spawn the pane "
        "in its OWN git worktree + branch (wt/<role>-<ts>) so parallel feature "
        "builds don't race; on done the Lead gets a merge PROPOSAL (never "
        "auto-merged). Falls back to shared + warns if the cwd isn't a git repo.",
    )
    sa.add_argument(
        "--base",
        default=None,
        metavar="REF",
        help="(#544, --isolation worktree only) fork the new worktree's branch "
        "off this ref (e.g. 'origin/release') instead of --cwd's checked-out "
        "HEAD — no need to `git checkout` a different base in the shared repo "
        "first just to seed one isolated worktree. Falls back to shared cwd + "
        "warns if the ref doesn't resolve.",
    )
    sa.add_argument(
        "--branch",
        dest="wt_branch",
        default=None,
        metavar="NAME",
        help="(#813, --isolation worktree only) name the new worktree branch "
        "(e.g. 'feature/recon-export') instead of wt/<role>-<ts>, so the work "
        "lands on the project's real branch — no cherry-pick afterwards. Must "
        "not exist yet. Cleanup removes the worktree but never this branch.",
    )
    sa.add_argument(
        "--feature",
        default="",
        help="feature/work-item label for the Task Ledger (A7) — groups this "
        "assign's row under '### N. <feature>' in runtime/tasks/<project>/"
        "INDEX.md. Omit for 'งานทั่วไป' (general work).",
    )
    sa.set_defaults(func=c.cmd_assign)


def _add_restart(sub: Any, c: ModuleType) -> None:

    srs = sub.add_parser(
        "restart",
        help="restart the whole cockpit (persist state → relaunch; panes respawn) — lead/terminal only",
    )
    srs.set_defaults(func=c.cmd_restart)


def _add_worktree(sub: Any, c: ModuleType) -> None:

    swt = sub.add_parser(
        "worktree",
        help="manage isolated per-pane worktrees (#81): list / merge / clean (lead only)",
    )
    swt_sub = swt.add_subparsers(dest="wt_cmd", required=True)
    swl = swt_sub.add_parser("list", help="show wt/* worktrees + commits-ahead + dirty flags")
    swl.add_argument("--cwd", default=None, help="project dir (default: current dir)")
    swm = swt_sub.add_parser(
        "merge",
        help="merge --no-ff an isolated branch into the main tree, then clean it up",
    )
    swm.add_argument(
        "--role",
        default=None,
        help="merge the wt/<role>-* branch of this role (refuses to guess when the role "
        "has several — pass --branch, or --latest to take the newest, #439)",
    )
    swm.add_argument("--branch", default=None, help="merge this exact wt/* branch")
    swm.add_argument(
        "--latest",
        action="store_true",
        help="with --role: when several worktrees exist, take the newest instead of refusing",
    )
    swm.add_argument(
        "--check",
        action="store_true",
        help="dry-run (#442): report dirty state + files that would conflict with the "
        "current base, merge nothing",
    )
    swm.add_argument("--keep", action="store_true", help="merge but keep the worktree")
    swm.add_argument("--cwd", default=None, help="project dir (default: current dir)")
    swc = swt_sub.add_parser(
        "clean",
        help="remove leftover wt/* worktrees (safe ones only; --force drops unmerged, "
        "--discard drops dirty too) and report on-disk dirs git no longer knows about (#355)",
    )
    swc.add_argument(
        "--force",
        action="store_true",
        help="also remove unmerged worktrees (commits not yet merged are LOST, "
        "recoverable from the branch's own reflog for a while) — never bypasses "
        "a dirty (uncommitted-changes) worktree on its own; that needs --discard",
    )
    swc.add_argument(
        "--discard",
        action="store_true",
        help="also remove DIRTY worktrees (uncommitted changes are LOST, unrecoverable) — "
        "#573: the reported line always shows the discarded diff stat first",
    )
    swc.add_argument(
        "--branch",
        default=None,
        help="clean only this wt/* branch (#439 — a role with several worktrees); "
        "safety rules unchanged",
    )
    swc.add_argument(
        "--orphans",
        action="store_true",
        help="also delete on-disk dirs git has completely forgotten (registration already "
        "pruned, e.g. a pre-#226/#227 partial Windows delete) — #355; default is report-only "
        "since these may hold uncommitted work git no longer has any record of",
    )
    swc.add_argument(
        "--orphans-node-modules-only",
        action="store_true",
        help="like --orphans but deletes only each orphan's node_modules/ subtrees (usually "
        "~99%% of its size) and keeps the rest — the safer default for anyone unsure whether "
        "an orphan still holds real work",
    )
    swc.add_argument("--cwd", default=None, help="project dir (default: current dir)")
    swt.set_defaults(func=c.cmd_worktree)


def _add_disk(sub: Any, c: ModuleType) -> None:

    sdisk = sub.add_parser(
        "disk",
        help="report DATA_HOME disk usage by category (safe/review/never to delete)",
    )
    sdisk.add_argument("--json", action="store_true", help="machine-readable output")
    sdisk.set_defaults(func=c.cmd_disk)


def _add_prune(sub: Any, c: ModuleType) -> None:

    sprune = sub.add_parser(
        "prune",
        help="delete reclaimable DATA_HOME categories — dry-run unless --yes (lead only)",
    )
    sprune.add_argument(
        "--category",
        default=None,
        help="comma-separated categories to prune (default: every safe category). "
        "one of: browser-profiles,transcripts,exports,orphan-worktrees,"
        "orphan-worktrees-review,shell-snapshots,partial,chat-history,node-modules,"
        "graft-graphs",
    )
    sprune.add_argument(
        "--level",
        choices=("safe", "review"),
        default="safe",
        help="max safety level in scope when --category is omitted, and the ceiling "
        "a named --category must be at or under (default: safe; review must be explicit)",
    )
    sprune.add_argument(
        "--older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help="age threshold (days) for time-based categories (default: per-category retention)",
    )
    sprune.add_argument(
        "--include-live",
        action="store_true",
        help="also delete node_modules under STILL-REGISTERED worktrees (node-modules "
        "category only) — dangerous if a dev server/build is running there; default "
        "skips them",
    )
    sprune.add_argument(
        "--yes",
        action="store_true",
        help="actually delete (default is dry-run: preview only, nothing removed)",
    )
    sprune.set_defaults(func=c.cmd_prune)


def _add_send(sub: Any, c: ModuleType) -> None:

    ss = sub.add_parser("send", help="send a message to a running pane")
    ss.add_argument("--to", required=True)
    ss.add_argument(
        "msg",
        nargs="?",
        default="",
        help='message (positional) — "-" reads the message from stdin',
    )
    ss.add_argument(
        "--from-file",
        default=None,
        metavar="PATH",
        help="read the message from this file (utf-8) instead of the positional arg — "
        "bypasses shell quoting for content with backticks/$()/parens (#491); mutually "
        'exclusive with the positional msg; PATH "-" reads from stdin',
    )
    ss.add_argument(
        "--file",
        default=None,
        help="with --to user: publish + push this file to the mobile PWA (#390) instead of "
        "messaging a pane — shorthand for `takkub report publish --send`; msg (if given) "
        "becomes the report's --label",
    )
    ss.add_argument(
        "--project", default=None, help="with --to user --file: project namespace for the report"
    )
    ss.add_argument(
        "--attachment",
        action="store_true",
        help="with --to user --file: force Content-Disposition: attachment (see "
        "`report publish --attachment`)",
    )
    ss.add_argument(
        "--kind",
        choices=["instruction", "info"],
        default="instruction",
        help="message kind: instruction (default, requires follow-up review if unverified) or info (informational, does not trigger follow-up warning on done)",
    )
    ss.set_defaults(func=c.cmd_send)


def _add_close(sub: Any, c: ModuleType) -> None:

    sc = sub.add_parser("close", help="close a running pane")
    sc.add_argument("role_pos", nargs="?", default=None, metavar="ROLE", help="role name to close")
    sc.add_argument("--role", default=None, help="role name to close")
    sc.set_defaults(func=c.cmd_close)


def _add_close_all(sub: Any, c: ModuleType) -> None:

    sca = sub.add_parser("close-all", help="close every teammate (keeps Lead)")
    sca.set_defaults(func=c.cmd_close_all)


def _add_done(sub: Any, c: ModuleType) -> None:

    sd = sub.add_parser("done", help="(agent) report done to Lead")
    sd.add_argument("note", nargs="?", default="")
    sd.add_argument(
        "--impact-evidence-file", default=None, help="JSON results for the assigned impact checks"
    )
    sd.add_argument(
        "--note-file",
        default=None,
        help="read the note from this file (e.g. the copy a timed-out done saved) — #911",
    )
    sd.add_argument(
        "--screenshot",
        action="append",
        default=None,
        metavar="PATH",
        help="evidence image; appended to the note as its absolute path (repeatable)",
    )
    sd.add_argument(
        "--fail",
        action="store_true",
        help="report a FAILED result (QA/verify failed) → Lead proposes a fix loop",
    )
    sd.add_argument(
        "--blocked",
        action="store_true",
        help=(
            "report BLOCKED — the work could not RUN because something outside the "
            "codebase is missing (credential, test account, permission, data, an "
            "external service). Nothing is broken, so Lead asks the owner for the "
            "missing thing instead of routing a fix loop to a teammate (#296)"
        ),
    )
    sd.add_argument(
        "--force",
        action="store_true",
        help=(
            "report even though the cockpit has no record of a task reaching this pane "
            "(manual work driven by hand outside an assign/send) — #278"
        ),
    )
    sd.set_defaults(func=c.cmd_done)


def _add_subagent_done(sub: Any, c: ModuleType) -> None:

    ssd = sub.add_parser(
        "subagent-done",
        help="complete a pending --mode subagent assignment through the done pipeline (Lead/native child)",
    )
    ssd.add_argument("--role", required=True)
    ssd.add_argument("note", nargs="?", default="")
    ssd.add_argument(
        "--impact-evidence-file", default=None, help="JSON results for the assigned impact checks"
    )
    ssd.add_argument("--fail", action="store_true")
    ssd.set_defaults(func=c.cmd_subagent_done)


def _add_ma(sub: Any, c: ModuleType) -> None:

    sma = sub.add_parser(
        "ma",
        help="(operator) maintenance sweep: issues → PRs → code scanning → runtime log → repo → แผนทำต่อ",
        description=(
            "เดิน checklist บำรุงรักษา cockpit ทีละข้อ: issue ที่ค้าง, PR + สถานะ CI, "
            "code scanning alert ที่เปิดค้าง (หน้า Security ของ GitHub), "
            "สิ่งที่ events.log ของ cockpit ที่รันอยู่บอกว่าพังจริงในช่วงที่ผ่านมา, และ "
            "สภาพ repo ว่าพร้อม ship ไหม — แล้วสรุปเป็นแผนทำต่อ (อ่านอย่างเดียว ไม่แก้ไฟล์)"
        ),
    )
    sma.add_argument(
        "--since-hours",
        type=float,
        default=24.0,
        help="ย้อนดู events.log กี่ชั่วโมง (default 24)",
    )
    sma.add_argument(
        "--no-net",
        action="store_true",
        help="ข้ามส่วนที่ต้องใช้เครือข่าย (gh issue/pr/run) — ดูเฉพาะ log ในเครื่อง",
    )
    sma.add_argument("--json", action="store_true", help="พิมพ์เป็น JSON แทนรายงานอ่านง่าย")
    sma.add_argument(
        "--noise-threshold",
        type=float,
        default=None,
        help=(
            "ครั้ง/ชม. ที่ถือว่า lead_notice kind หนึ่งๆ 'บ่อยเกินไป' ใน section "
            "Lead noise (default 4, หรือ env TAKKUB_NOISE_PER_HOUR)"
        ),
    )
    sma.set_defaults(func=c.cmd_ma)


def _add_progress(sub: Any, c: ModuleType) -> None:

    spg = sub.add_parser(
        "progress",
        help="(agent) report a status update to Lead without ending the task (#234)",
    )
    spg.add_argument("note")
    spg.set_defaults(func=c.cmd_progress)


def _add_end_session(sub: Any, c: ModuleType) -> None:

    ses = sub.add_parser(
        "end-session",
        help="(lead) write session summary to runtime/sessions and vault mirror",
    )
    ses.add_argument("--note", default="", help="summary note (default: 'session ended')")
    ses.set_defaults(func=c.cmd_end_session)


def _add_goal(sub: Any, c: ModuleType) -> None:

    sgoal = sub.add_parser(
        "goal",
        help="(lead) set/show/clear the session objective prepended to every assign",
    )
    sgoal.add_argument(
        "text",
        nargs="?",
        default=None,
        help="objective text to set; omit to show the current goal",
    )
    sgoal.add_argument(
        "--clear",
        action="store_true",
        default=False,
        help="unset the current session goal",
    )
    sgoal.set_defaults(func=c.cmd_goal)


def _add_preview(sub: Any, c: ModuleType) -> None:

    # #365 phase 5. examples/PROPOSED_CLI.md proposes flag-shaped
    # `--url`/`--file`; adapted here to nested subcommands (open-url/
    # open-file/close/status) to match this parser's own established
    # convention for a command with several distinct actions (see task/mcp/
    # plugins/worktree/migrate/issue/pipeline/services below).
    spv = sub.add_parser("preview", help="control this project's Live Preview")
    spv_sub = spv.add_subparsers(dest="preview_action", required=True)
    spv_url = spv_sub.add_parser("open-url", help="show a loopback dev-server URL")
    spv_url.add_argument("url")
    spv_url.add_argument("--device", choices=("desktop", "tablet", "mobile"), default=None)
    spv_url.set_defaults(func=c.cmd_preview)
    spv_file = spv_sub.add_parser("open-file", help="show an approved local .html/.htm file")
    spv_file.add_argument("path")
    spv_file.add_argument("--device", choices=("desktop", "tablet", "mobile"), default=None)
    spv_file.set_defaults(func=c.cmd_preview)
    spv_close = spv_sub.add_parser("close", help="close this project's preview")
    spv_close.set_defaults(func=c.cmd_preview)
    spv_status = spv_sub.add_parser("status", help="show this project's preview state")
    spv_status.set_defaults(func=c.cmd_preview)


def _add_team(sub: Any, c: ModuleType) -> None:

    steam = sub.add_parser(
        "team", help="project team preset — #512 (solo-lead/pair/full/custom/auto)"
    )
    steam_sub = steam.add_subparsers(dest="team_action", required=True)
    steam_status = steam_sub.add_parser("status", help="show this project's effective team preset")
    steam_status.set_defaults(func=c.cmd_team)
    steam_set = steam_sub.add_parser("set", help="set this project's standing team preset")
    steam_set.add_argument("preset", choices=("solo-lead", "pair", "full", "auto"))
    steam_set.set_defaults(func=c.cmd_team)
    steam_clear = steam_sub.add_parser(
        "clear-override", help="clear this project's active per-task team-preset override"
    )
    steam_clear.set_defaults(func=c.cmd_team)
    steam_suggest = steam_sub.add_parser(
        "suggest",
        help="advisory preset suggestion for preset=='auto' (#512 item 5) — "
        "prints a preset + one-line reason, changes nothing",
    )
    steam_suggest.add_argument("task", help="the task text to classify")
    steam_suggest.set_defaults(func=c.cmd_team)


def _add_design(sub: Any, c: ModuleType) -> None:

    sdz = sub.add_parser(
        "design", help="design artifact registry — publish/approve/revise (#365 phase 5)"
    )
    sdz_sub = sdz.add_subparsers(dest="design_action", required=True)
    sdz_pub = sdz_sub.add_parser("publish", help="record + preview a new design artifact")
    sdz_pub.add_argument(
        "--path", required=True, help="approved file path, or a loopback URL when --mode url"
    )
    sdz_pub.add_argument("--title", required=True)
    sdz_pub.add_argument("--mode", choices=("html", "url", "review"), default="html")
    sdz_pub.set_defaults(func=c.cmd_design)
    sdz_appr = sdz_sub.add_parser("approve", help="mark an artifact approved + notify lead")
    sdz_appr.add_argument("--id", dest="artifact_id", required=True)
    sdz_appr.set_defaults(func=c.cmd_design)
    sdz_rev = sdz_sub.add_parser("revise", help="request a revision + notify lead")
    sdz_rev.add_argument("--id", dest="artifact_id", required=True)
    sdz_rev.add_argument("--feedback", default="")
    sdz_rev.set_defaults(func=c.cmd_design)

    sdz_int = sdz_sub.add_parser(
        "integrations",
        help="21st.dev/Penpot design-tool integrations — status/enable/disable/doctor (#373)",
    )
    sdz_int_sub = sdz_int.add_subparsers(dest="integrations_action", required=True)
    sdz_int_status = sdz_int_sub.add_parser(
        "status", help="show configured/enabled status (read-only)"
    )
    sdz_int_status.add_argument(
        "integration_id",
        nargs="?",
        default=None,
        metavar="ID",
        help="e.g. penpot, reference-21st",
    )
    sdz_int_status.add_argument(
        "--role", default=None, help="also show enabled/disabled for this role"
    )
    sdz_int_status.set_defaults(func=c.cmd_design)
    sdz_int_enable = sdz_int_sub.add_parser(
        "enable", help="enable an integration for a role (lead only)"
    )
    sdz_int_enable.add_argument("integration_id", metavar="ID")
    sdz_int_enable.add_argument("--role", required=True)
    sdz_int_enable.add_argument(
        "--token",
        default=None,
        help="credential — stored via SecretManager, never written to projects.json",
    )
    sdz_int_enable.add_argument(
        "--base-url",
        default=None,
        help="required with --token for penpot; optional for reference-21st",
    )
    sdz_int_enable.set_defaults(func=c.cmd_design)
    sdz_int_disable = sdz_int_sub.add_parser(
        "disable", help="disable an integration for a role (lead only)"
    )
    sdz_int_disable.add_argument("integration_id", metavar="ID")
    sdz_int_disable.add_argument("--role", required=True)
    sdz_int_disable.set_defaults(func=c.cmd_design)
    sdz_int_doctor = sdz_int_sub.add_parser(
        "doctor", help="print design-integrations doctor findings"
    )
    sdz_int_doctor.set_defaults(func=c.cmd_design)


def _add_report(sub: Any, c: ModuleType) -> None:

    # #367 Remote Reports — share a standalone file on the cockpit's own
    # tunnel domain via a per-file token, instead of a Claude Artifact URL.
    sr = sub.add_parser(
        "report", help="Remote Reports — share a file on your own tunnel domain (#367)"
    )
    sr_sub = sr.add_subparsers(dest="report_action", required=True)
    sr_pub = sr_sub.add_parser(
        "publish", help="copy a standalone file into the reports store + mint/reuse its token"
    )
    sr_pub.add_argument(
        "file",
        help="local .html/.png/.jpg/.svg/.pdf/.json/.md/.docx/.xlsx/.pptx/.zip/.csv/.txt file",
    )
    sr_pub.add_argument(
        "--name", default=None, help="stored filename (default: the source file's own name)"
    )
    sr_pub.add_argument(
        "--project", default=None, help="project namespace (default: active project)"
    )
    sr_pub.add_argument(
        "--expires",
        default=None,
        help="30d, 12h, or none — default: keep existing expiry on republish, else never",
    )
    sr_pub.add_argument("--label", default="", help="human label shown by `takkub report list`")
    sr_pub.add_argument(
        "--attachment",
        action="store_true",
        help="force Content-Disposition: attachment (forced download) even for an "
        "extension that would otherwise render inline (#389) — sticky: cannot be "
        "un-set by republishing without the flag",
    )
    sr_pub.add_argument(
        "--send",
        action="store_true",
        help="also push this report as a native attachment into the connected mobile "
        "PWA's live feed (#390) — falls back to link-only (with a reason) if remote "
        "control isn't reachable right now",
    )
    sr_pub.set_defaults(func=c.cmd_report)
    sr_list = sr_sub.add_parser("list", help="show published reports for a project")
    sr_list.add_argument("--project", default=None)
    sr_list.set_defaults(func=c.cmd_report)
    sr_revoke = sr_sub.add_parser(
        "revoke", help="kill a report's link (file stays unless --delete)"
    )
    sr_revoke.add_argument("name")
    sr_revoke.add_argument("--project", default=None)
    sr_revoke.add_argument("--delete", action="store_true", help="also delete the stored file")
    sr_revoke.set_defaults(func=c.cmd_report)
    sr_rotate = sr_sub.add_parser("rotate", help="issue a new token for a report (old link dies)")
    sr_rotate.add_argument("name")
    sr_rotate.add_argument("--project", default=None)
    sr_rotate.set_defaults(func=c.cmd_report)
    sr_relink = sr_sub.add_parser(
        "relink",
        help="print every active report's current link (#451 — old links can go stale "
        "when secret_path rotates without anyone rotating tokens)",
    )
    sr_relink.add_argument("--project", default=None)
    sr_relink.set_defaults(func=c.cmd_report)
    sr_build = sr_sub.add_parser(
        "build", help="build a report from content directory (customer/dev/boss types) (#626)"
    )
    sr_build.add_argument(
        "--type",
        required=True,
        choices=["customer", "dev", "boss"],
        help="report type: customer (user manual) / dev (technical) / boss (executive summary)",
    )
    sr_build.add_argument(
        "--content", required=True, help="directory with content.html, images.txt, etc."
    )
    sr_build.add_argument("--out", default=None, help="output HTML file (default: stdout)")
    sr_build.add_argument("--title", default=None, help="HTML page title (default: based on type)")
    sr_build.add_argument(
        "--lint", action="store_true", help="check content for issues before building"
    )
    sr_build.add_argument(
        "--max-width",
        dest="max_width",
        type=int,
        default=1440,
        metavar="PX",
        help="maximum embedded-image width in pixels (default 1440) (#676)",
    )
    sr_build.add_argument(
        "--max-bytes",
        dest="max_bytes",
        type=int,
        default=4 * 1024 * 1024,
        metavar="BYTES",
        help=(
            "warn (never fail) when the built HTML exceeds this size "
            "(default 4194304 = 4 MB) (#676)"
        ),
    )
    sr_build.set_defaults(func=c.cmd_report)


def _add_cleanup(sub: Any, c: ModuleType) -> None:

    # Removal cleanup (docs/plans/remove-openviking-2026-08-24/) — reclaim a
    # leftover managed-runtime install an older Takkub version left behind.
    scl = sub.add_parser("cleanup", help="remove a leftover resource an older Takkub left behind")
    scl_sub = scl.add_subparsers(dest="cleanup_target", required=True)
    scl_ov = scl_sub.add_parser(
        "openviking",
        help="stop + remove the OpenViking managed runtime "
        "(~/.agent-takkub/services/openviking/); config/data kept unless --purge-data",
    )
    scl_ov.add_argument(
        "--purge-data",
        action="store_true",
        help="also delete config and indexed data",
    )
    scl_ov.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    scl_ov.set_defaults(func=c.cmd_cleanup)
    scl_am = scl_sub.add_parser(
        "agents-md",
        help="remove takkub-managed AGENTS.md/GEMINI.md that spawns planted into project "
        "roots (an IDE-launched codex/gemini otherwise reads itself as a cockpit pane)",
    )
    scl_am.add_argument(
        "--path",
        action="append",
        help="extra directory to sweep (repeatable) on top of every registered project path",
    )
    scl_am.add_argument("--dry-run", action="store_true", help="list without deleting")
    scl_am.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    scl_am.set_defaults(func=c.cmd_cleanup)


def _add_harvest(sub: Any, c: ModuleType) -> None:

    sh = sub.add_parser(
        "harvest",
        help="scan artifact paths for a role that never sent takkub done",
    )
    sh.add_argument("--role", required=True, help="role name to harvest")
    sh.add_argument(
        "--since",
        default=None,
        metavar="HH:MM",
        help="scan window start (default: pane spawn timestamp, fallback 1h ago)",
    )
    sh.add_argument(
        "--auto-confirm",
        action="store_true",
        dest="auto_confirm",
        default=False,
        help="skip interactive prompt — mark as done immediately",
    )
    sh.add_argument(
        "--limit",
        type=int,
        default=100,
        help="max artifacts to list (default: 100)",
    )
    sh.add_argument(
        "--note",
        default=None,
        help="the role's own evidence to put in the synthesized done note (screenshot "
        "paths, files it produced) — required with --auto-confirm when another role "
        "is active on the same project root (#697)",
    )
    sh.set_defaults(func=c.cmd_harvest)


def _add_list(sub: Any, c: ModuleType) -> None:

    sl = sub.add_parser("list", help="show pane status")
    sl.set_defaults(func=c.cmd_list)


def _add_messages(sub: Any, c: ModuleType) -> None:

    smsg = sub.add_parser(
        "messages",
        help="read the `takkub send` audit log for a role — was it actually received? (#277) · "
        "Lead: any role; a pane: its own role only (#852)",
    )
    smsg.add_argument("--role", required=True, help="recipient role to look up")
    smsg.add_argument("--limit", type=int, default=20, help="how many recent messages (default 20)")
    smsg.add_argument(
        "--drop",
        action="store_true",
        help="(lead) retire every message to this role that has not reached a pane yet "
        "(sent-unconfirmed / waiting for a pane) so it is never replayed into a new pane (#705)",
    )
    smsg.set_defaults(func=c.cmd_messages)


def _add_task(sub: Any, c: ModuleType) -> None:

    st = sub.add_parser(
        "task",
        help="read back a role's last assigned task (issue #1 file-based task handoff)",
    )
    st_sub = st.add_subparsers(dest="t_cmd", required=True)
    sts = st_sub.add_parser("show", help="print the full text of a role's last assigned task")
    sts.add_argument("--role", required=True, help="role name to look up")
    sti = st_sub.add_parser(
        "impact",
        help="print one JSON object with active impact plan, task id and current revision; errors go to stderr",
    )
    sti.add_argument("--role", required=True)
    sti.add_argument("--project", default=None)
    str_ = st_sub.add_parser(
        "reconcile",
        help="close ledger rows orphaned by a cockpit session that exited without `takkub done`",
    )
    str_.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="preview what would be closed without writing",
    )
    stc = st_sub.add_parser("close", help="manually close a role's open ledger row")
    stc.add_argument("--role", required=True, help="role name to close")
    stc.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="close even if the role currently has a live pane",
    )
    stc.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="preview without writing",
    )
    stx = st_sub.add_parser(
        "cancel",
        help=(
            "cancel a role's pending task delivery that is still retrying "
            "toward its current pane session (issue #255)"
        ),
    )
    stx.add_argument("--role", required=True, help="role name whose pending delivery to cancel")
    st.set_defaults(func=c.cmd_task)


def _add_backlog(sub: Any, c: ModuleType) -> None:

    # #684 — project backlog: cross-session store of noticed-but-not-done work.
    bl = sub.add_parser("backlog", help="project backlog: work noticed but not yet done (#684)")
    bl_sub = bl.add_subparsers(dest="b_cmd")
    bla = bl_sub.add_parser("add", help="add a backlog item")
    bla.add_argument("title", help="short title of the item")
    bla.add_argument("--detail", default="", help="longer description")
    bla.add_argument("--source", default="", help="where it came from (report/owner/reviewer/UAT)")
    bla.add_argument("--file", action="append", default=[], help="related file[:line] (repeatable)")
    bla.add_argument("--impact", choices=c.backlog_impacts(), default="internal")
    bla.add_argument("--severity", choices=c.backlog_severities(), default="med")
    bll = bl_sub.add_parser("list", help="list backlog items (newest first)")
    bll.add_argument(
        "--status",
        default="",
        help="filter: todo/doing/review/waiting/blocked/deferred/done/wont, "
        "'open' (active) or 'pending' (everything not done/wont)",
    )
    bls = bl_sub.add_parser("show", help="show one item's full detail")
    bls.add_argument("id", help="item id")
    bld = bl_sub.add_parser("done", help="mark an item done")
    bld.add_argument("id", help="item id")
    blk = bl_sub.add_parser("block", help="mark an item blocked (reason required)")
    blk.add_argument("id", help="item id")
    blk.add_argument("reason", help="what it is blocked on")
    blf = bl_sub.add_parser("defer", help="park an item (พักไว้)")
    blf.add_argument("id", help="item id")
    blf.add_argument("--reason", default="", help="why it was parked (#844)")
    blst = bl_sub.add_parser("status", help="set an item's status explicitly")
    blst.add_argument("id", help="item id")
    blst.add_argument("status", choices=c.backlog_statuses())
    blst.add_argument("--reason", default="", help="reason (for blocked/wont)")
    blas = bl_sub.add_parser("assign", help="fire `takkub assign` for an item and link it")
    blas.add_argument("id", help="item id")
    blas.add_argument("--role", required=True, help="role to assign the item to")
    blsr = bl_sub.add_parser(
        "start",
        help="(#714) Lead is about to do work itself: mark card ID doing, or create "
        "one with --title — required before Lead edits files",
    )
    blsr.add_argument("id", nargs="?", default="", help="existing item id")
    blsr.add_argument("--title", default="", help="create a new card with this title")
    bl_sub.add_parser("pending", help="(#714) list every pending card (not done/wont)")
    bli = bl_sub.add_parser("import", help="import a markdown table of items")
    bli.add_argument("file", help="path to a .md file containing a pipe table")
    bli.add_argument("--source", default="", help="source label (defaults to the file path)")
    bl.set_defaults(func=c.cmd_backlog)


def _add_hook(sub: Any, c: ModuleType) -> None:

    # Internal — wired as the Stop/Notification hook `command` for every
    # cockpit-spawned claude pane (see hook_wiring.py). Not a user-facing
    # command, so it's hidden from --help.
    shk = sub.add_parser("_hook", help=argparse.SUPPRESS)
    shk.set_defaults(func=c.cmd_hook)


def _add_session_report(sub: Any, c: ModuleType) -> None:

    # Internal — wired as the SessionStart hook `command` for every
    # cockpit-spawned claude pane (see hook_wiring.py). Not a user-facing
    # command, so it's hidden from --help. Not lead-only / teammate-only:
    # every pane (Lead + every teammate role) fires SessionStart and must
    # be able to report it.
    ssr = sub.add_parser("session-report", help=argparse.SUPPRESS)
    ssr.set_defaults(func=c.cmd_session_report)


def _add_context(sub: Any, c: ModuleType) -> None:

    sctx = sub.add_parser("context", help="read or inspect the complete active Lead policy")
    sctx.add_argument("context_action", choices=("read", "status"))
    sctx.add_argument(
        "--full",
        action="store_true",
        help="print the whole policy even when this session's system prompt already carries it",
    )
    sctx.set_defaults(func=c.cmd_context)


def _add_context_guard(sub: Any, c: ModuleType) -> None:
    scg = sub.add_parser("_context-guard", help=argparse.SUPPRESS)
    scg.set_defaults(func=c.cmd_context_guard)


def _add_guard(sub: Any, c: ModuleType) -> None:

    # Internal — wired as the PreToolUse/Bash hook `command` for every
    # cockpit-spawned claude pane (see hook_wiring.py). Blocks a teammate from
    # shelling around the MCP tool policy (`npx playwright`) or sweeping the
    # whole disk. Hidden from --help; fires on every Bash call.
    sgd = sub.add_parser("_guard", help=argparse.SUPPRESS)
    sgd.set_defaults(func=c.cmd_guard)


def _add_activity(sub: Any, c: ModuleType) -> None:

    sa = sub.add_parser("_activity", help=argparse.SUPPRESS)
    sa.set_defaults(func=c.cmd_activity)


def _add_mcp_fallback(sub: Any, c: ModuleType) -> None:

    smf = sub.add_parser(
        "mcp-fallback",
        help="(browser-role shard) request the single, time-boxed mb fallback "
        "when Playwright MCP genuinely never connects (#146/#304)",
    )
    smf_sub = smf.add_subparsers(dest="mcp_fallback_cmd", required=True)
    smf_req = smf_sub.add_parser(
        "request",
        help="request the fallback grant — only after ToolSearch confirms no browser MCP tools",
    )
    smf_req.add_argument(
        "--reason", default="", help="why the browser MCP is unavailable (goes into the audit log)"
    )
    smf_req.set_defaults(func=c.cmd_mcp_fallback)
    smf_status = smf_sub.add_parser(
        "status", help="show who currently holds the fallback grant, if anyone"
    )
    smf_status.set_defaults(func=c.cmd_mcp_fallback)


def _add_status(sub: Any, c: ModuleType) -> None:

    sst = sub.add_parser(
        "status",
        help="per-pane progress summary with stall detection (post-compact awareness)",
    )
    sst.add_argument(
        "--since",
        default=None,
        metavar="HH:MM",
        help="window start for done-event scan (default: 1h ago)",
    )
    sst.add_argument(
        "--role",
        default=None,
        metavar="ROLE",
        help="filter status to this role (e.g. backend, reviewer)",
    )
    sst.set_defaults(func=c.cmd_status)


def _add_tail(sub: Any, c: ModuleType) -> None:

    stail = sub.add_parser(
        "tail",
        help="(lead) read recent PTY output/transcript of a role (including exited panes) — #541",
    )
    stail.add_argument(
        "role_pos", nargs="?", default=None, metavar="ROLE", help="role to inspect (e.g. codex, qa)"
    )
    stail.add_argument("--role", default=None, help="role to inspect (e.g. codex, qa)")
    stail.add_argument(
        "-n", "--lines", type=int, default=20, help="number of lines to show (default 20)"
    )
    stail.set_defaults(func=c.cmd_tail)


def _add_inbox(sub: Any, c: ModuleType) -> None:

    sib = sub.add_parser(
        "inbox",
        help="(lead) read pending done/FAILED report content still queued for delivery (#231)",
    )
    sib.add_argument(
        "--role",
        default=None,
        metavar="ROLE",
        help="only show reports from this role (e.g. backend#1)",
    )
    sib.set_defaults(func=c.cmd_inbox)


def _add_wait(sub: Any, c: ModuleType) -> None:

    swt = sub.add_parser(
        "wait",
        help="(lead) block until role(s)' done/FAILED report actually reaches Lead (#242)",
    )
    swt.add_argument(
        "--role",
        action="append",
        default=None,
        metavar="ROLE",
        help="role to wait on (repeatable; omit to wait on every active role)",
    )
    swt.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"max seconds to block (default {int(c._WAIT_DEFAULT_TIMEOUT_S)}, "
        f"capped at {int(c._WAIT_MAX_TIMEOUT_S)} — a value above the cap is clamped "
        "with a warning, not rejected)",
    )
    swt.add_argument(
        "--cancel",
        action="store_true",
        help="release the active wait registration for this project (#249) instead of blocking",
    )
    swt.add_argument(
        "--hold",
        action="store_true",
        help="deliberately block this turn (no other work, must act the moment the report "
        "lands). Required for a Lead pane — the default after assign is to end the turn",
    )
    swt.add_argument(
        "--no-interrupt",
        action="store_true",
        dest="no_interrupt",
        help="(#357) keep waiting through a 'reason: user_input' interrupt instead of "
        "stopping — transparently re-attaches to the still-pending roles and keeps "
        "polling until they resolve or --timeout elapses. Does NOT suppress a genuine "
        "blocking report from an outside role (#253) — that still stops the wait.",
    )
    swt.add_argument(
        "--all",
        action="store_true",
        dest="wait_all",
        help="(#856) return only when EVERY listed role resolved: a report from an "
        "outside role or a delivery notice is printed and the wait keeps going. "
        "Only text you type into the Lead pane still stops it.",
    )
    swt.set_defaults(func=c.cmd_wait)


def _add_lock(sub: Any, c: ModuleType) -> None:

    # #430 advisory locks
    slk = sub.add_parser(
        "lock",
        help="take an advisory per-project lock around a shared resource (build dir, db) — #430",
    )
    slk.add_argument("name", nargs="?", default=None, help="lock name, e.g. web-build")
    slk.add_argument(
        "--wait", type=float, default=0.0, metavar="SEC", help="block up to SEC for it"
    )
    slk.add_argument(
        "--ttl", type=float, default=None, metavar="SEC", help="auto-expire (default 30m)"
    )
    slk.add_argument("--note", default=None, help="what you're doing with it (shown to others)")
    slk.add_argument("--list", action="store_true", help="show locks held in this project")
    slk.set_defaults(func=c.cmd_lock)


def _add_unlock(sub: Any, c: ModuleType) -> None:

    sul = sub.add_parser("unlock", help="release a lock you hold (#430)")
    sul.add_argument("name")
    sul.add_argument("--force", action="store_true", help="(lead) release someone else's lock")
    sul.set_defaults(func=c.cmd_unlock)


def _add_kill(sub: Any, c: ModuleType) -> None:

    # #430 lead-side kill under a pane
    skl = sub.add_parser(
        "kill",
        help="(lead) kill the processes running under a pane — no PID hunting (#430)",
    )
    skl.add_argument(
        "role_pos",
        nargs="?",
        default=None,
        metavar="ROLE",
        help="pane whose child processes to kill",
    )
    skl.add_argument("--role", default=None, help="pane whose child processes to kill")
    skl.add_argument("--pid", type=int, default=None, help="only this PID (must be under the pane)")
    skl.set_defaults(func=c.cmd_kill)


def _add_submit(sub: Any, c: ModuleType) -> None:

    # #819 lead-side submit of a stuck composer draft
    ssb = sub.add_parser(
        "submit",
        help="(lead) press Enter on a pane whose task/message sits unsent in the composer (#819)",
    )
    ssb.add_argument("role_pos", nargs="?", default=None, metavar="ROLE", help="pane to submit")
    ssb.add_argument("--role", default=None, help="pane to submit")
    ssb.set_defaults(func=c.cmd_submit)


def _add_spawn_service(sub: Any, c: ModuleType) -> None:

    # #429 detached services
    ssv = sub.add_parser(
        "spawn-service",
        help="start a service that outlives this pane (cockpit-owned, outside the pane tree) — #429",
    )
    ssv.add_argument("--name", default=None, help="service name (default: command basename)")
    ssv.add_argument("--cwd", default=None, help="working directory (default: current)")
    ssv.add_argument("--list", action="store_true", help="show this project's live services")
    # dest must NOT be "command" — that collides with the top-level
    # `add_subparsers(dest="command")` (#483): argparse fills the Namespace's
    # `command` attribute with this REMAINDER list *after* the subparsers
    # action already set it to "spawn-service", leaving `args.command` a
    # list by the time `_enforce_role_gate(args.command)` runs — an
    # unhashable `in LEAD_ONLY_COMMANDS` check crashes every invocation.
    ssv.add_argument(
        "--cmd-file",
        dest="cmd_file",
        default=None,
        metavar="FILE",
        help="read the command from FILE (one argv element per line) instead of `-- ...` — "
        "avoids git-bash/MSYS rewriting /flags into Windows paths (#645)",
    )
    ssv.add_argument("service_argv", nargs=argparse.REMAINDER, help="-- <cmd> [args...]")
    ssv.set_defaults(func=c.cmd_spawn_service)


def _add_service_stop(sub: Any, c: ModuleType) -> None:

    sst = sub.add_parser("service-stop", help="(lead) stop a spawn-service by name (#429)")
    sst.add_argument("--name", required=True)
    sst.set_defaults(func=c.cmd_service_stop)


def _add_verify(sub: Any, c: ModuleType) -> None:

    sv = sub.add_parser("verify", help="auto-detect stack and run lint/test gate")
    sv.add_argument("--cwd", default=None, help="working directory (default: current dir)")
    sv.add_argument("--json", action="store_true", help="emit machine-readable result")
    sv.add_argument(
        "--skip", action="append", metavar="NAME", help="skip check by name (repeatable)"
    )
    sv.set_defaults(func=c.cmd_verify)


def _add_docs_verify(sub: Any, c: ModuleType) -> None:

    sdv = sub.add_parser("docs-verify", help="verify markdown file/symbol refs")
    sdv.add_argument("--report", default="runtime/docs_drift.md")
    sdv.add_argument("--exit-on-broken", action="store_true", dest="exit_on_broken")
    sdv.add_argument(
        "--exclude",
        action="append",
        metavar="GLOB",
        help="skip files matching this glob (repeatable, e.g. --exclude 'docs/reviews/*')",
    )
    sdv.add_argument(
        "--no-default-excludes",
        action="store_true",
        dest="no_default_excludes",
        help="disable auto-exclusion of docs/reviews/*.md",
    )
    sdv.set_defaults(func=c.cmd_docs_verify)


def _add_usage(sub: Any, c: ModuleType) -> None:

    su = sub.add_parser(
        "usage",
        help="usage ledger — real token/quota report per provider→account→model (#507)",
    )
    su.add_argument("--days", type=int, default=None, help="days back from today (default: 7)")
    su.add_argument("--month", default=None, help="YYYY-MM instead of --days")
    su.add_argument("--provider", default=None, help="limit to one provider")
    su.set_defaults(func=c.cmd_usage)
    # Deliberately `required=False` (unlike every other nested subparser in
    # this file) — #507 specifies the bare `takkub usage [--days N] [...]`
    # form as the primary UX; `import` is the one extra action bolted on.
    su_sub = su.add_subparsers(dest="usage_cmd", required=False)
    su_import = su_sub.add_parser(
        "import", help="idempotent historical backfill from provider transcripts"
    )
    su_import.add_argument("--provider", default=None, help="limit to one provider")
    su_import.add_argument(
        "--source",
        default=None,
        help=(
            "read transcripts from this dir instead of the profile registry "
            "(requires --provider; read-only, never writes there — combine with "
            "env TAKKUB_USAGE_LEDGER_DIR to also redirect the ledger itself)"
        ),
    )
    su_import.set_defaults(func=c.cmd_usage_import)


def _add_audit_skills(sub: Any, c: ModuleType) -> None:

    sas = sub.add_parser("audit-skills", help="TF-IDF role boundary audit")
    sas.add_argument("--threshold", type=float, default=0.6)
    sas.add_argument("--output", default="runtime/skill_audit.md")
    sas.add_argument("--json", action="store_true", help="emit JSON instead of writing markdown")
    sas.set_defaults(func=c.cmd_audit_skills)


def _add_skills(sub: Any, c: ModuleType) -> None:

    ssk = sub.add_parser(
        "skills",
        help="list skills (global/project/built-in) or show what a role effectively gets (#422)",
    )
    ssk_sub = ssk.add_subparsers(dest="skills_cmd", required=True)
    sskl = ssk_sub.add_parser("list", help="every skill visible to the project, with its real home")
    sskl.add_argument("--project", default=None, help="project name (default: active project)")
    sskl.add_argument(
        "--global", dest="only_global", action="store_true", help="only cockpit-level skills"
    )
    sske = ssk_sub.add_parser(
        "effective", help="skills a role actually receives (Skill Matrix × provider bridge)"
    )
    sske.add_argument("--role", required=True, help="role name (e.g. backend)")
    sske.add_argument("--project", default=None, help="project name (default: active project)")
    sske.add_argument(
        "--provider", default=None, help="override provider (default: resolved for the role)"
    )
    for _name, _help in (
        ("exclude", "stop linking these global skills into the project (every provider)"),
        ("include", "undo `exclude` — link these global skills into the project again"),
    ):
        _p = ssk_sub.add_parser(_name, help=_help)
        _p.add_argument("names", nargs="+", help="skill name(s)")
        _p.add_argument("--project", default=None, help="project name (default: active project)")
    from .skill_learning.cli_commands import add_parser as _add_learned_parser

    _add_learned_parser(ssk_sub)
    ssk.set_defaults(func=c.cmd_skills)


def _add_backups(sub: Any, c: ModuleType) -> None:

    sbk = sub.add_parser(
        "backups",
        help="list / prune old safety copies in DATA_HOME/backups (migration + cleanups)",
    )
    sbk_sub = sbk.add_subparsers(dest="backups_cmd", required=True)
    sbk_sub.add_parser("list", help="every backup entry with size, age and whether it can go")
    sbkp = sbk_sub.add_parser("prune", help="delete old prunable entries (dry run without --yes)")
    sbkp.add_argument("--yes", action="store_true", help="actually delete")
    sbk.set_defaults(func=c.cmd_backups)


def _add_ccc(sub: Any, c: ModuleType) -> None:

    sccc = sub.add_parser(
        "ccc",
        help="cocoindex-code semantic code search: build/inspect a project's index "
        "(the `ccc` MCP reaches panes only for projects indexed here)",
    )
    sccc_sub = sccc.add_subparsers(dest="ccc_cmd", required=True)
    for _name, _help in (
        ("index", "run `ccc init` (if needed) + `ccc index` for the project, DB under DATA_HOME"),
        ("status", "is the project indexed where the cockpit expects it, plus `ccc status`"),
        ("env", "print the env the cockpit gives `ccc` for the project (for manual runs)"),
        ("serve", "internal MCP runner; resolves the pane project and starts cocoindex-code"),
        ("provision", "configure agy, OpenCode, and Cursor to invoke the safe ccc runner"),
    ):
        _p = sccc_sub.add_parser(_name, help=_help)
        _p.add_argument("--project", default=None, help="project name (default: this pane's)")
        _p.add_argument(
            "--cwd", default=None, help="project root (default: the project's lead cwd)"
        )
    sccc.set_defaults(func=c.cmd_ccc)


def _add_migrate_skills(sub: Any, c: ModuleType) -> None:

    sms = sub.add_parser(
        "migrate-skills",
        help="move legacy cockpit-created skills out of a project's repo into the central store",
    )
    sms.add_argument(
        "--project",
        default=None,
        help="project name to migrate (default: active project)",
    )
    sms.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would move without changing anything",
    )
    sms.add_argument(
        "--to-global",
        metavar="NAME",
        default=None,
        help=(
            "promote one central project skill to the cockpit-level global store "
            "(visible to every project + every provider, #419)"
        ),
    )
    sms.set_defaults(func=c.cmd_migrate_skills)


def _add_search(sub: Any, c: ModuleType) -> None:

    sse = sub.add_parser(
        "search",
        help="BM25-ranked search across past Claude Code conversations (+ role-memory archives)",
    )
    sse.add_argument("query", help="text to search for (BM25-ranked; case-insensitive)")
    sse.add_argument(
        "--project",
        default=None,
        help="filter by project name substring (default: all projects)",
    )
    sse.add_argument(
        "--days",
        type=int,
        default=None,
        help="search last N days (default: today only)",
    )
    sse.add_argument(
        "--all",
        action="store_true",
        help="search all history (overrides default 'today only')",
    )
    sse.add_argument(
        "--limit",
        type=int,
        default=20,
        help="max hits to print (default: 20)",
    )
    sse.add_argument(
        "--grep",
        action="store_true",
        help="force the old plain-substring grep path instead of BM25 ranking",
    )
    sse.set_defaults(func=c.cmd_search)


def _add_issue(sub: Any, c: ModuleType) -> None:

    # ── issue tracker ────────────────────────────────────────────────────────
    si = sub.add_parser(
        "issue", help="manage issues via GitHub Issues (auto-detects repo from project)"
    )
    si_sub = si.add_subparsers(dest="issue_command", required=True)

    # issue new
    sin = si_sub.add_parser("new", help="create a new issue")
    sin.add_argument("title", help="issue title")
    sin.add_argument(
        "--severity", type=c._severity_alias, choices=["low", "med", "high"], default="med"
    )
    sin.add_argument("--noticed-in", dest="noticed_in", default=None, metavar="PROJECT")
    sin.add_argument("--role", default=None, metavar="ROLE")
    sin.add_argument("--tag", default=None, metavar="a,b,c", help="comma-separated tags")
    sin.add_argument(
        "--body",
        default=None,
        metavar="TEXT",
        help=(
            "body text (opens $EDITOR if omitted on TTY). Note: on Windows, "
            "multiline strings passed to --body are cut at the first newline by "
            "cmd.exe — use --body-file instead (#741)."
        ),
    )
    sin.add_argument(
        "--body-file",
        dest="body_file",
        default=None,
        metavar="PATH",
        help=(
            'read body text from a file, or "-" for stdin — bypasses shell '
            "interpolation and cmd.exe newline truncation entirely (#679/#741; "
            "recommended for multiline bodies, e.g. `$body | takkub issue new ... --body-file -`)"
        ),
    )
    sin.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="allow filing issue even if body appears truncated (e.g. single markdown header, #741)",
    )
    sin.add_argument(
        "--cockpit-bug",
        dest="cockpit_bug",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "route this issue to the agent-takkub install repo regardless of cwd "
            "(DEFAULT — the cockpit tracker is for cockpit/orchestrator/CLI/UI bugs). "
            "Use --no-cockpit-bug to file against the active project's repo instead."
        ),
    )

    _COCKPIT_BUG_READ_HELP = (
        "read from the agent-takkub install repo regardless of cwd (DEFAULT — "
        "matches `issue new`'s default, so an issue filed with no flag is found "
        "with no flag). Use --no-cockpit-bug to read the active project's repo "
        "instead — must match whatever `--no-cockpit-bug` (or not) was used to "
        "file the issue, or it queries the wrong store (#142)."
    )

    # issue list
    sil = si_sub.add_parser("list", help="list issues with optional filters")
    sil.add_argument("--open", action="store_true", dest="open", help="show only open issues")
    sil.add_argument("--closed", action="store_true", dest="closed", help="show only closed issues")
    sil.add_argument("--noticed-in", dest="noticed_in", default=None, metavar="PROJECT")
    sil.add_argument("--role", default=None, metavar="ROLE")
    sil.add_argument(
        "--severity", type=c._severity_alias, choices=["low", "med", "high"], default=None
    )
    sil.add_argument(
        "--cockpit-bug",
        dest="cockpit_bug",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=_COCKPIT_BUG_READ_HELP,
    )

    # issue close
    sic = si_sub.add_parser("close", help="close a GitHub issue or local fallback record")
    sic.add_argument("id", help="issue number (e.g. 123, #123)")
    sic.add_argument("--local", action="store_true", help="close the local fallback record")
    sic.add_argument(
        "--note", default="", metavar="MSG", help="cause / fix summary (posted as comment)"
    )
    sic.add_argument(
        "--cockpit-bug",
        dest="cockpit_bug",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=_COCKPIT_BUG_READ_HELP,
    )

    # issue show
    sis = si_sub.add_parser("show", help="print issue from GitHub to stdout")
    sis.add_argument("id", help="GitHub issue number (e.g. 123, #123)")
    sis.add_argument(
        "--cockpit-bug",
        dest="cockpit_bug",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=_COCKPIT_BUG_READ_HELP,
    )

    # --issues-dir kept for backward compat — deprecated, issues.py emits a warning and ignores it
    for sp in (sin, sil, sic, sis):
        sp.add_argument(
            "--issues-dir",
            dest="issues_dir",
            default=None,
            metavar="PATH",
            help="[DEPRECATED] ignored — issues are now stored in GitHub",
        )

    def _cmd_issue(args: argparse.Namespace) -> dict:
        from .issues import cmd_issue_close, cmd_issue_list, cmd_issue_new, cmd_issue_show

        dispatch = {
            "new": cmd_issue_new,
            "list": cmd_issue_list,
            "close": cmd_issue_close,
            "show": cmd_issue_show,
        }
        fn = dispatch.get(args.issue_command)
        if fn is None:
            return {"ok": False, "msg": f"unknown issue subcommand: {args.issue_command}"}
        return fn(args)

    si.set_defaults(func=_cmd_issue)


def _add_mcp(sub: Any, c: ModuleType) -> None:

    # ── pane-tools policy: MCP servers ──────────────────────────────────────
    sm = sub.add_parser("mcp", help="per-role MCP server policy (~/.takkub/pane-tools.json)")
    sm_sub = sm.add_subparsers(dest="mcp_command", required=True)

    sm_list = sm_sub.add_parser("list", help="show effective MCP allowlist per role")
    sm_list.add_argument("--role", default=None, metavar="ROLE", help="show only this role")

    sm_allow = sm_sub.add_parser("allow", help="allow an MCP for a role (lead only)")
    sm_allow.add_argument("name")
    sm_allow.add_argument("--role", required=True, metavar="ROLE")

    sm_deny = sm_sub.add_parser("deny", help="deny an MCP for a role (lead only)")
    sm_deny.add_argument("name")
    sm_deny.add_argument("--role", required=True, metavar="ROLE")

    sm_reset = sm_sub.add_parser(
        "reset",
        help="clear role override(s) back to defaults (lead only; clears both mcps+plugins for the role)",
    )
    sm_reset.add_argument(
        "--role", default=None, metavar="ROLE", help="reset only this role (omit = reset all)"
    )

    sm_add = sm_sub.add_parser(
        "add", help="register a new MCP server in the master config (lead only)"
    )
    sm_add.add_argument("name")
    sm_add.add_argument("--command", required=True, metavar="CMD")
    sm_add.add_argument("--args", default="", metavar='"..."', help="shell-quoted args string")
    sm_add.add_argument(
        "--force", action="store_true", help="bypass the credential-looking-value block"
    )

    sm_remove = sm_sub.add_parser(
        "remove", help="remove an MCP server from the master config (lead only)"
    )
    sm_remove.add_argument("name")

    sm.set_defaults(func=c.cmd_mcp)


def _add_plugins(sub: Any, c: ModuleType) -> None:

    # ── pane-tools policy: plugins ──────────────────────────────────────────
    spl = sub.add_parser("plugins", help="per-role plugin policy (~/.takkub/pane-tools.json)")
    spl_sub = spl.add_subparsers(dest="plugins_command", required=True)

    spl_list = spl_sub.add_parser("list", help="show effective plugin allowlist per role")
    spl_list.add_argument("--role", default=None, metavar="ROLE", help="show only this role")

    spl_allow = spl_sub.add_parser("allow", help="allow a plugin for a role (lead only)")
    spl_allow.add_argument("name")
    spl_allow.add_argument("--role", required=True, metavar="ROLE")

    spl_deny = spl_sub.add_parser("deny", help="deny a plugin for a role (lead only)")
    spl_deny.add_argument("name")
    spl_deny.add_argument("--role", required=True, metavar="ROLE")

    spl_reset = spl_sub.add_parser(
        "reset",
        help="clear role override(s) back to defaults (lead only; clears both mcps+plugins for the role)",
    )
    spl_reset.add_argument(
        "--role", default=None, metavar="ROLE", help="reset only this role (omit = reset all)"
    )

    spl.set_defaults(func=c.cmd_plugins)


def _add_doctor(sub: Any, c: ModuleType) -> None:

    sdoc = sub.add_parser(
        "doctor",
        help="diagnose cockpit env (claude, node, plugins, mcps, projects)",
    )
    sdoc.add_argument(
        "--fix",
        action="store_true",
        help="apply safe auto-fixes (provider installs are skipped by default)",
    )
    sdoc.add_argument(
        "--install-providers",
        action="store_true",
        help="with --fix, also install all missing provider CLIs (default: skipped)",
    )
    sdoc.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of text report",
    )
    sdoc.add_argument(
        "--live",
        action="store_true",
        help="also query the running cockpit for spawn-queue wedge state (#141); "
        "skipped (not failed) when the cockpit isn't running",
    )
    sdoc.add_argument(
        "--core-version",
        action="store_true",
        help="also run Core V2 Schema/Adapter/Compat checks per provider (#309 Phase 4); "
        "opt-in, off by default so plain `takkub doctor` is unchanged",
    )
    sdoc.add_argument(
        "--ram",
        action="store_true",
        help="also query the running cockpit for a live per-pane RAM breakdown "
        "(#364 lever 6): provider CLI / node-MCP children / QtWebEngine per pane, "
        "plus main process and machine-wide numbers; opt-in, off by default so "
        "plain `takkub doctor` is unchanged. Skipped (not failed) when the "
        "cockpit isn't running.",
    )
    sdoc.add_argument(
        "--ram-profile",
        action="store_true",
        help="with --ram, also do a one-off main-process tracemalloc/gc snapshot "
        "(#364 lever 5): top Python-heap allocators + object census, "
        "including a leak check on watched pane object types. Briefly "
        "enables tracemalloc if it wasn't already on, never leaves it "
        "running; no effect without --ram. A lower-bound diagnostic, not a "
        "full accounting of the process's RSS — see "
        "ram_report.collect_main_process_profile's docstring.",
    )
    sdoc.add_argument(
        "--workspace",
        action="store_true",
        help="also report Workspace/IDE-lite diagnostics (#365 phase 10): "
        "Monaco bundle presence on disk, live editor host/preview/design "
        "artifact/file-watcher/git-changes/tree-scan state where the "
        "cockpit is running; opt-in, off by default so plain `takkub "
        "doctor` is unchanged. The live half is skipped (not failed) when "
        "the cockpit isn't running.",
    )
    sdoc.add_argument(
        "--storage-layout",
        action="store_true",
        help="also report V1/V2/mixed storage layout state (#309 Phase 8b); "
        "opt-in, off by default so plain `takkub doctor` is unchanged",
    )
    sdoc.add_argument(
        "--obsidian",
        action="store_true",
        help="also report Obsidian vault hardening state (#365 phase 8): vault "
        "presence, canonical-metadata coverage, persistent dedup index size, "
        "default-deny indexing boundary; opt-in, off by default so plain "
        "`takkub doctor` is unchanged",
    )
    sdoc.add_argument(
        "--pane",
        metavar="ROLE",
        default=None,
        help="cross-check a role's (e.g. 'qa#2') last recorded spawn-time MCP "
        "handshake against its shared-MCP config on disk (#304); does not "
        "prove a live MCP connection — the cockpit can't observe that",
    )
    sdoc.add_argument(
        "--project",
        default=None,
        help="project namespace for --pane (default: current pane's project, or the active project)",
    )
    sdoc.add_argument(
        "--boot-context",
        action="store_true",
        help="issue #516: measure per-role/per-category boot-context token cost "
        "without spawning a real pane — reconstructs the exact "
        "--append-system-prompt-file content + repo CLAUDE.md + MCP config a "
        "real spawn would use. Combine with --role to scope to one role "
        "(default: every claude-capable role known to the active project)",
    )
    sdoc.add_argument(
        "--role",
        default=None,
        help="scope --boot-context to one role (base role, no #shard)",
    )
    sdoc.set_defaults(func=c.cmd_doctor)


def _add_qa_gate(sub: Any, c: ModuleType) -> None:

    sqag = sub.add_parser(
        "qa-gate",
        help="canonical gate: venv-check -> pytest -> ruff check -> lint-imports on a "
        "Python project, or typecheck -> test -> prisma drift/migration-integrity "
        "(#469) -> smoke (#475, opt-in, running stack only) on a Node one (#329) — "
        "one entrypoint shared by qa pane / CI / a user's terminal (#325)",
    )
    sqag.add_argument(
        "--targeted",
        nargs="+",
        metavar="PATH",
        default=None,
        help="mid-flight tier: run pytest on only these paths (skips ruff/lint-imports, "
        "no report file written) — team policy: targeted mid-flight, full gate once at the "
        "batch gate. Python only: on a Node project the gate says so and runs unnarrowed",
    )
    sqag.add_argument(
        "--auto",
        action="store_true",
        help="#436: pick the tier from `git diff --name-only` and say why — style/asset/"
        "wording-only diff runs NO test suite (Node: typecheck only), module logic runs "
        "the mapped targeted tests, anything touching api/auth/schema/shared/tooling runs the "
        "full gate. The specialist's default before `takkub done`; every provider",
    )
    sqag.add_argument(
        "--v2-flags",
        action="store_true",
        help="force every TAKKUB_V2_* flag on for this run, to check the Core V2 ladder "
        "before flipping any flag on by default (#309)",
    )
    sqag.add_argument(
        "--exec",
        dest="exec_cmd",
        default=None,
        metavar="CMD",
        help="run pytest/ruff/lint-imports through this prefix instead of resolving a local "
        '.venv (#401) — e.g. --exec "docker compose exec -T gateway" delegates to a '
        "container that owns its own toolchain. Disables local venv-check and ENV_GAP "
        "detection entirely; a tool missing on the exec target surfaces as a normal FAIL",
    )
    sqag.set_defaults(func=c.cmd_qa_gate)


def _add_migrate(sub: Any, c: ModuleType) -> None:

    smig = sub.add_parser(
        "migrate",
        help="Core V2 storage migration: inspect/plan/dry-run/apply/validate/rollback (#309 Phase 4)",
    )
    smig_sub = smig.add_subparsers(dest="migrate_cmd", required=True)
    smig_help = {
        "inspect": "V1 อะไรอยู่ตรงไหน, schema version เท่าไหร่ (read-only)",
        "plan": "จะย้ายอะไรไปไหน (ไม่แตะดิสก์)",
        "dry-run": "จำลอง apply แบบเต็ม ไม่เขียนดิสก์จริง",
        # L1 (#504 round 2 acceptance review): "copy-never-move" only
        # describes the 8 V1->V2 domain steps (steps_v1.py) — promote-v2-root
        # / archive-v1-legacy verify-then-remove their source (never a raw
        # shutil.move, still recoverable via restore-v1/rollback, but the
        # source IS removed on success, unlike the domain steps).
        "apply": "ทำจริง + journal (domain step = copy-never-move; promote/archive = copy-verify แล้วลบต้นทาง, กู้คืนได้ผ่าน restore-v1/rollback)",
        "validate": "cross-check V2 กับ V1 ที่ยังอยู่",
        "rollback": "ย้อนจาก journal + backup (ทั้ง ladder)",
        "restore-v1": "#504: ย้าย V1 จาก backups/v1-archive-<ts>/ กลับที่เดิม — ลง 2.0.x ใช้ต่อได้",
    }
    for _name, _help in smig_help.items():
        _p = smig_sub.add_parser(_name, help=_help)
        _p.add_argument("--json", action="store_true", help="emit JSON instead of text report")
        if _name == "restore-v1":
            # #504 H1: pick one specific v1-archive-<ts> generation instead
            # of only ever reaching the latest one, and list what exists.
            _p.add_argument(
                "--archive",
                dest="archive_ts",
                default=None,
                metavar="TS",
                help="restore only this v1-archive-<ts> generation (default: every "
                "generation, oldest first — see --list)",
            )
            _p.add_argument(
                "--list",
                dest="list_archives",
                action="store_true",
                help="list available v1-archive-<ts> generations and exit, without "
                "restoring anything",
            )
            _p.add_argument(
                "--from-backup",
                dest="from_backup",
                default=None,
                metavar="DIR",
                help="#574: restore from a pre-migrate-backup/ directory instead of the "
                "normal v1-archive-<ts> generation walk — an alternate recovery path",
            )
        _p.set_defaults(func=c.cmd_migrate)

    srun = smig_sub.add_parser(
        "run",
        help="#574: interactive boot flow (provider updates + migrate) with progress, "
        "outside the Qt cockpit",
    )
    srun.add_argument("--providers", default="ask", help="ask|all|none|<csv of provider names>")
    srun.add_argument("--remember", action="store_true", help="remember this provider choice")
    srun.add_argument("--yes", action="store_true", help="skip the pre-migrate confirm prompt")
    srun.add_argument("--json", action="store_true", help="emit JSON lines instead of text screens")
    srun.set_defaults(func=c.cmd_migrate_run)


def _add_provider(sub: Any, c: ModuleType) -> None:

    sprv = sub.add_parser(
        "provider",
        help="provider CLIs: list/install providers and configure spawn models",
    )
    sprv_sub = sprv.add_subparsers(dest="provider_cmd", required=True)
    sprv_sub.add_parser("list", help="show each registered provider + install state")
    spi = sprv_sub.add_parser(
        "install",
        help="install a provider CLI via its registered package command (e.g. npm)",
    )
    spi.add_argument("name", help="provider name (e.g. codex, opencode)")
    spm = sprv_sub.add_parser("model", help="show or set a provider's spawn model")
    spm.add_argument("name", help="provider name (e.g. claude, cursor)")
    spm.add_argument("model", nargs="?", help="model id; omit to show the current value")
    spm.add_argument("--clear", action="store_true", help="clear the model and use CLI default")
    spp = sprv_sub.add_parser(
        "probe",
        help="ask a provider whether its quota is usable now; clears a recorded quota-hit (#663)",
    )
    spp.add_argument("name", help="provider name (e.g. codex)")
    sprv.set_defaults(func=c.cmd_provider)


def _add_provision(sub: Any, c: ModuleType) -> None:

    sprov = sub.add_parser(
        "provision",
        help="install recommended plugins + browser MCPs (idempotent, detect-first; run after npm install)",
    )
    sprov.set_defaults(func=c.cmd_provision)


def _add_pipeline(sub: Any, c: ModuleType) -> None:

    # ── pipeline ────────────────────────────────────────────────────────────
    spipe = sub.add_parser("pipeline", help="pipeline template commands (lead only)")
    spipe_sub = spipe.add_subparsers(dest="pipeline_command", required=True)

    spipe_run = spipe_sub.add_parser("run", help="start a pipeline template")
    spipe_run.add_argument(
        "template_id", help="pipeline template id (e.g. feature, design, quickfix)"
    )
    spipe_run.add_argument(
        "--project",
        default=None,
        help="project namespace override (default: active project)",
    )

    def _cmd_pipeline(args: argparse.Namespace) -> dict:
        if args.pipeline_command == "run":
            return c._request(
                c._with_project(
                    {
                        "cmd": "pipeline-run",
                        "template_id": args.template_id,
                        "from": c._from_role(),
                    }
                )
            )
        return {"ok": False, "msg": f"unknown pipeline subcommand: {args.pipeline_command}"}

    spipe.set_defaults(func=_cmd_pipeline)


def _add_codex(sub: Any, c: ModuleType) -> None:

    sx = sub.add_parser(
        "codex",
        help="one-shot OpenAI Codex CLI query (non-interactive, pure local)",
    )
    sx.add_argument("prompt", help="prompt text to send to Codex (positional)")
    sx.add_argument(
        "--cwd",
        default=None,
        help="working directory for the Codex run (default: current dir)",
    )
    sx.add_argument(
        "--model",
        default=None,
        help="override Codex's default model (e.g. gpt-5-codex)",
    )
    sx.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait before killing the codex process (default: 120)",
    )
    sx.set_defaults(func=c.cmd_codex)


def _add_gemini(sub: Any, c: ModuleType) -> None:

    sg = sub.add_parser(
        "gemini",
        help="one-shot Antigravity CLI (agy) query (non-interactive, pure local)",
    )
    sg.add_argument("prompt", help="prompt text to send to Antigravity (positional)")
    sg.add_argument(
        "--cwd",
        default=None,
        help="working directory for the agy run (default: current dir)",
    )
    sg.add_argument(
        "--model",
        default=None,
        help="override agy's default model (e.g. gemini-3.1-pro)",
    )
    sg.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait before killing the agy process (default: 120)",
    )
    sg.set_defaults(func=c.cmd_gemini)


def _add_release(sub: Any, c: ModuleType) -> None:

    srel = sub.add_parser(
        "release",
        help="bump version + roll CHANGELOG [vNEXT] + git commit & tag",
    )
    srel.add_argument(
        "part",
        nargs="?",
        choices=["major", "minor", "patch"],
        default="patch",
        help="which SemVer part to bump (default: patch)",
    )
    srel.add_argument(
        "--version",
        default=None,
        help="set an explicit version (e.g. 0.4.0) instead of bumping a part",
    )
    srel.add_argument("--no-commit", action="store_true", help="edit files but don't git commit")
    srel.add_argument("--no-tag", action="store_true", help="commit but don't create the git tag")
    srel.add_argument(
        "--allow-empty",
        action="store_true",
        help="release even if ## [vNEXT] has no changelog entries",
    )
    srel.add_argument(
        "--github-release",
        dest="github_release",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "after commit+tag, push and create the GitHub Release with the "
            "changelog section as notes (DEFAULT). Use --no-github-release to "
            "only commit+tag locally (push left to you)."
        ),
    )
    srel.add_argument(
        "--build-wheel",
        dest="build_wheel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "after commit+tag, delete stale dist/*.whl and `python -m build "
            "--wheel` (DEFAULT) — without it `npm publish` can silently bundle "
            "a previous version's wheel (#340). Use --no-build-wheel to skip."
        ),
    )
    srel.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned version/tag without touching files or git",
    )
    srel.set_defaults(func=c.cmd_release)


def _add_services(sub: Any, c: ModuleType) -> None:

    # ── services (docker compose) ────────────────────────────────────────────
    ssvcs = sub.add_parser(
        "services",
        help="docker compose operations for the active project (start/stop/ps/logs)",
    )
    ssvcs.add_argument(
        "--cwd",
        default=None,
        help="directory containing the compose file (default: active project's compose path)",
    )
    ssvcs.add_argument(
        "--project",
        default=None,
        help="project name override (default: derived from cwd directory name)",
    )
    ssvcs_sub = ssvcs.add_subparsers(dest="services_command", required=True)

    ssvcs_sub.add_parser("start", help="docker compose up -d")
    ssvcs_sub.add_parser("stop", help="docker compose down")
    ssvcs_sub.add_parser("ps", help="show running services and health state")

    ssvcs_logs = ssvcs_sub.add_parser("logs", help="fetch recent log lines (non-blocking)")
    ssvcs_logs.add_argument(
        "--tail",
        type=int,
        default=50,
        help="number of log lines to fetch (default: 50)",
    )

    ssvcs.set_defaults(func=c.cmd_services)


def _add_lead_edits(sub: Any, c: ModuleType) -> None:

    sled = sub.add_parser(
        "lead-edits",
        help="inspect or reset lead direct edit counters (#585 round 4)",
    )
    sled.add_argument(
        "--reset",
        action="store_true",
        default=False,
        help="reset lead direct edit line/file counters",
    )
    sled.add_argument(
        "--project",
        default=None,
        help="project name (defaults to current project)",
    )
    sled.set_defaults(func=c.cmd_lead_edits)


def build_parser(c: ModuleType) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="takkub", description="agent-takkub cockpit CLI")
    sub = p.add_subparsers(dest="command", required=True)
    _add_spawn(sub, c)
    _add_assign(sub, c)
    _add_restart(sub, c)
    _add_worktree(sub, c)
    _add_disk(sub, c)
    _add_prune(sub, c)
    _add_send(sub, c)
    _add_close(sub, c)
    _add_close_all(sub, c)
    _add_done(sub, c)
    _add_subagent_done(sub, c)
    _add_ma(sub, c)
    _add_progress(sub, c)
    _add_end_session(sub, c)
    _add_goal(sub, c)
    _add_preview(sub, c)
    _add_team(sub, c)
    _add_design(sub, c)
    _add_report(sub, c)
    _add_cleanup(sub, c)
    _add_harvest(sub, c)
    _add_list(sub, c)
    _add_messages(sub, c)
    _add_task(sub, c)
    _add_backlog(sub, c)
    _add_hook(sub, c)
    _add_session_report(sub, c)
    _add_context(sub, c)
    _add_context_guard(sub, c)
    _add_guard(sub, c)
    _add_activity(sub, c)
    _add_mcp_fallback(sub, c)
    _add_status(sub, c)
    _add_tail(sub, c)
    _add_inbox(sub, c)
    _add_wait(sub, c)
    _add_lock(sub, c)
    _add_unlock(sub, c)
    _add_kill(sub, c)
    _add_submit(sub, c)
    _add_spawn_service(sub, c)
    _add_service_stop(sub, c)
    _add_verify(sub, c)
    _add_docs_verify(sub, c)
    _add_usage(sub, c)
    _add_audit_skills(sub, c)
    _add_skills(sub, c)
    _add_backups(sub, c)
    _add_ccc(sub, c)
    _add_migrate_skills(sub, c)
    _add_search(sub, c)
    _add_issue(sub, c)
    _add_mcp(sub, c)
    _add_plugins(sub, c)
    _add_doctor(sub, c)
    _add_qa_gate(sub, c)
    _add_migrate(sub, c)
    _add_provider(sub, c)
    _add_provision(sub, c)
    _add_pipeline(sub, c)
    _add_codex(sub, c)
    _add_gemini(sub, c)
    _add_release(sub, c)
    _add_services(sub, c)
    _add_lead_edits(sub, c)
    return p
