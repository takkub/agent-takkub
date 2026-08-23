"""`takkub` CLI — agent-side client that talks to the orchestrator over TCP.

Usage from inside an agent pane (Claude running with TAKKUB_ROLE env set):

  takkub assign --role backend --cwd C:/x/api "task..."
  takkub send --to backend "msg"
  takkub spawn --role frontend
  takkub close --role frontend
  takkub list
  takkub done [note]
  takkub subagent-done --role reviewer [note]

Output is human readable on stdout. Exit 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from . import config
from .config import read_port

# Commands that orchestrate the cockpit (spawn/route/close panes). Only the
# Lead pane is allowed to invoke these; teammates must work on their assigned
# task and coordinate via `send` / `done`. The gate is enforced in `main()`
# based on the TAKKUB_ROLE env var that the orchestrator injects per pane.
LEAD_ONLY_COMMANDS = frozenset(
    {
        "spawn",
        "assign",
        "subagent-done",
        "close",
        "close-all",
        "end-session",
        "harvest",
        "release",
        "pipeline",
        "provision",
        "migrate-skills",
        "goal",
        "worktree",
        "prune",
        "restart",
        "inbox",  # reads other panes' report bodies — see cli_server._LEAD_ONLY_CMDS (#231)
        "wait",  # blocks on other panes' delivery pipeline — same rationale as inbox (#242)
        # machine-level npm installs — a teammate pane must never mutate the
        # host toolchain mid-task; Lead/terminal decides when to add a CLI.
        "provider",
    }
)

# #242: `takkub wait` always carries a bounded timeout — never an unbounded
# block. No explicit --timeout uses the default; either way the value is
# clamped into this range before being sent to the server.
#
# #253 lowered the ceiling from 7200s (2h) to 1800s (30 min). The original
# incident (`wait --role qa --timeout 3000` sitting blind for 9 minutes while
# devops's done report queued behind it) is now mostly closed by
# `poll_wait`'s "interrupt" wake — a FAILED/blocking report from a role
# OUTSIDE the watched set ends the wait immediately instead of waiting out
# the timeout. But a 2h ceiling was still a foot-gun on top of that fix: a
# PLAIN done notice from an unwatched role deliberately does NOT interrupt
# (routine parallel-fan-out noise, see `_pending_notice_outside`), so a Lead
# that watches a slow role while several fast ones report clean could still
# go silent for up to the full ceiling with nothing forcing it to look
# around. 30 min matches the pre-existing default (a `wait` that would have
# run longer than that already needed to be re-issued under the old rules
# too) and forces Lead back into the loop periodically on a genuinely long
# task instead of treating `wait` as a multi-hour park.
_WAIT_DEFAULT_TIMEOUT_S = 1800.0  # 30 min
_WAIT_MIN_TIMEOUT_S = 5.0
_WAIT_MAX_TIMEOUT_S = 1800.0  # 30 min hard ceiling (#253 — was 7200s/2h)
_WAIT_POLL_INTERVAL_S = 4.0
_WAIT_POLL_MAX_INTERVAL_S = 15.0
# #249 item 4: print a heartbeat line at least this often while roles are
# still pending, so an external observer (or the Lead session that spawned
# `takkub wait` as a background shell) can tell "still waiting" apart from
# "hung" without needing to cross-reference `takkub list`. A role resolving
# also prints immediately, independent of this interval.
_WAIT_HEARTBEAT_INTERVAL_S = 30.0

# Commands intended only for teammate panes. Lead summarises inline and never
# needs to call done on itself — blocking this prevents Lead from accidentally
# scheduling its own close via the done→QTimer→close chain.
TEAMMATE_ONLY_COMMANDS = frozenset({"done", "progress"})

# #341: write-path commands whose whole point is a side effect elsewhere
# (deliver a message, record a report, log an issue). An `ok: True` response
# with no confirmation message from these is indistinguishable, to whoever
# reads the CLI output, from "nothing happened" — the caller sees exit 0 and
# assumes success while the daemon never actually confirmed anything. Kept
# separate from LEAD_ONLY/TEAMMATE_ONLY (an authorization concern) — this is
# purely about never letting a write path read as a silent success.
_WRITE_COMMANDS_REQUIRE_CONFIRMATION = frozenset(
    {
        "send",
        "done",
        "progress",
        "issue",
        "assign",
        "goal",
        "close",
        "close-all",
        "restart",
        "subagent-done",
        "harvest",
        "end-session",
    }
)


def _connect() -> socket.socket:
    port = read_port()
    if port is None:
        raise RuntimeError(
            "agent-takkub cockpit is not running (no port file). Launch the app first."
        )
    # 15 s: long enough that codex/gemini pane spawns (which wait on
    # workspace-write sandbox + AGENTS.md/GEMINI.md plant + ready-
    # prompt detection, ~7-10 s) don't return "timed out" while the
    # orchestrator is still doing the right thing in the background.
    s = socket.create_connection(("127.0.0.1", port), timeout=15)
    return s


def _instance_banner() -> str:
    """Return a best-effort identity banner for the active cockpit instance."""
    try:
        label = config.instance_identity_label()
        port = config.read_port()
        port_file = Path(config._get_port_file())
        lines = [f"▸ {label}   (port {port} · {port_file.parent})"]
    except Exception:
        return ""

    try:
        is_dev = config.DATA_HOME == config.REPO_ROOT
        if is_dev:
            other_port_file = Path.home() / ".agent-takkub" / "runtime" / "port"
        else:
            other_port_file = Path(config.REPO_ROOT) / "runtime" / "port"

        # A port-file override can point at the conventional path for the
        # other instance. Do not probe (or warn about) ourselves in that case.
        if other_port_file == port_file:
            return "\n".join(lines)

        other_port = int(other_port_file.read_text(encoding="utf-8").strip())
        probe = socket.create_connection(("127.0.0.1", other_port), timeout=0.3)
        close = getattr(probe, "close", None)
        if callable(close):
            close()

        if is_dev:
            other_label = f"v{config.instance_display_version()}"
        else:
            other_label = f"dev · {Path(config.REPO_ROOT).name}"
        lines.append(f"  ⚠ {other_label} ก็รันอยู่ด้วย (port {other_port}) — คำสั่งนี้คุม {label} เท่านั้น")
    except Exception:
        pass

    return "\n".join(lines)


# #233: `socket.settimeout()` bounds each individual blocking call, not the
# total time spent in the recv loop below — a server that keeps dribbling
# non-empty, non-newline-terminated chunks (or anything else that resets the
# per-call clock before it expires) can hold this loop open indefinitely even
# though every single recv() "succeeded" within its timeout. _RESPONSE_TIMEOUT_S
# is enforced as a true wall-clock deadline covering the whole read, so
# `takkub assign`/every other command always returns within a fixed ceiling
# instead of hanging silently.
_RESPONSE_TIMEOUT_S = 15.0


def _timeout_response(total_timeout: float) -> dict:
    return {
        "ok": False,
        "msg": (
            f"timed out waiting for orchestrator response after {total_timeout:.0f}s "
            "(cockpit may be restarting or wedged) — check `takkub doctor` / `takkub list`"
        ),
    }


def _request(payload: dict, *, response_timeout: float = _RESPONSE_TIMEOUT_S) -> dict:
    # Stamp the capability token so the server can verify the caller's identity.
    # Lead panes carry TAKKUB_LEAD_TOKEN (authorises Lead-only commands).
    # Teammate panes carry TAKKUB_PANE_TOKEN (authorises send/done).
    # Whichever is present in the env is stamped; if both are set (shouldn't
    # happen in normal operation) the Lead token takes precedence.
    token = os.environ.get("TAKKUB_LEAD_TOKEN") or os.environ.get("TAKKUB_PANE_TOKEN")
    if token:
        payload["auth"] = token
    s = _connect()
    deadline = time.monotonic() + response_timeout
    try:
        s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _timeout_response(response_timeout)
            s.settimeout(remaining)
            try:
                chunk = s.recv(4096)
            except TimeoutError:
                return _timeout_response(response_timeout)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return {"ok": False, "msg": "no response from orchestrator"}
        line = buf.split(b"\n", 1)[0]
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            # A malformed frame (never valid JSON from cli_server._reply, which
            # always writes `json.dumps(...) + "\n"`) means something other
            # than the orchestrator's own reply landed in this response —
            # surface it as a clear diagnostic instead of letting a bare
            # traceback or, worse, another silent hang stand in for it.
            return {
                "ok": False,
                "msg": (
                    f"malformed response from orchestrator ({e}); got {len(line)} byte(s) "
                    f"starting {line[:80]!r} — check `takkub doctor`"
                ),
            }
    finally:
        s.close()


def _from_role() -> str | None:
    """The role that's invoking the CLI. Set by orchestrator at spawn time."""
    return os.environ.get("TAKKUB_ROLE")


def _from_project() -> str | None:
    """The project namespace that owns the calling pane. Set by the
    orchestrator at spawn time so the cli_server can scope routing
    (a Lead in project-a never reaches into project-b's pane registry)."""
    return os.environ.get("TAKKUB_PROJECT")


def _enforce_role_gate(command: str) -> str | None:
    """Return an error message if the caller's role can't run `command`.

    Defense against teammate panes drifting into Lead behavior (e.g. devops
    near the context limit calling `takkub assign --role devops ...`). The
    `--append-system-prompt` specialist override is text-only and can be
    diluted by compaction or high-context degradation — this CLI-level gate
    blocks the action regardless of how confused the agent is.

    Rules:
      - If TAKKUB_ROLE is unset (user typing manually from a terminal),
        allow everything. This is the debugging path.
      - If TAKKUB_ROLE == "lead", block TEAMMATE_ONLY_COMMANDS (done).
      - Otherwise, block LEAD_ONLY_COMMANDS with a hint pointing at the
        commands teammates *are* allowed to use.
    """
    # defense at CLI layer; orchestrator has matching guard for direct TCP attackers
    role = _from_role()
    if role is None:
        return None
    role_lower = role.lower()
    if command in LEAD_ONLY_COMMANDS:
        if role_lower == "lead":
            return None
        return (
            f"only lead can run 'takkub {command}'. you are '{role}'.\n"
            f"       do your task directly with Read/Write/Edit/Bash.\n"
            f"       use 'takkub send --to <role>' for peer coordination, "
            f"'takkub done' to report back."
        )
    if command in TEAMMATE_ONLY_COMMANDS and role_lower == "lead":
        return (
            f"lead cannot run 'takkub {command}'. "
            f"summarise your work inline — teammates use done to report back to you."
        )
    return None


def _with_project(payload: dict) -> dict:
    """Stamp every outbound request with `from_project` so the server can
    scope routing. Cockpit-launched panes always have TAKKUB_PROJECT set;
    when the CLI is invoked manually from a terminal the field is None and
    the server falls back to the active project from projects.json."""
    payload["from_project"] = _from_project()
    return payload


def cmd_spawn(args: argparse.Namespace) -> dict:
    return _request(
        _with_project({"cmd": "spawn", "role": args.role, "cwd": args.cwd, "from": _from_role()})
    )


def _browser_shard_warning(role: str, shards: int) -> str:
    """#304 point 5: tell Lead up front, in the `assign` response itself,
    that a browser-role shard fan-out may not be able to open a browser at
    all — Playwright MCP has been observed failing to connect under
    concurrent shard spawn (#146/#304, root cause not yet proven) and the
    `mb` fallback is blocked for shards by design (#92 — no per-shard CDP
    port). Surfacing this at assign time, not after minutes of a stuck pane,
    is the point of #304's item 5."""
    if shards <= 1:
        return ""
    from . import pane_guard

    if not pane_guard.is_browser_role(role):
        return ""
    return (
        "\n⚠️ shard เปิดเบราว์เซอร์อาจไม่ได้: Playwright MCP บาง shard เคย connect ไม่ติดภายใต้ "
        "concurrent spawn (#146/#304, สาเหตุยังไม่พิสูจน์) แล้ว mb ก็โดน guard บล็อคสำหรับ shard เสมอ "
        "(#92 — ไม่มี per-shard CDP port) — ถ้า shard ไหนพัง ให้มันลอง `takkub mcp-fallback request` "
        "ก่อนรายงาน FAILED, หรือใช้ `takkub doctor --pane <role>` วินิจฉัย"
    )


def cmd_assign(args: argparse.Namespace) -> dict:
    # #1: validate --shards BEFORE the `or 1` fallback so explicit 0 / negative /
    # >8 values are rejected with a clear message rather than silently clamped.
    # #364 lever 2: `args.mode` is None when the caller left --mode unset —
    # `mode_requested` (possibly None) travels to the server as-is so
    # `resolve_auto_assign_mode` can auto-pick pane vs. subagent for a
    # short, no-frills task; `mode` (never None) is only for THIS
    # function's own local validation/display below, which must stay
    # conservative (pane's stricter rules) since a None request might still
    # resolve to either mode server-side.
    mode_requested = getattr(args, "mode", None)
    mode = mode_requested or "pane"
    _SHARDS_MAX = 20 if mode == "subagent" else 8
    _raw_shards = getattr(args, "shards", 1)
    if _raw_shards is not None:
        _shards_int = int(_raw_shards)
        if not (1 <= _shards_int <= _SHARDS_MAX):
            return {
                "ok": False,
                "msg": (
                    f"--shards must be between 1 and {_SHARDS_MAX} (got {_shards_int}); "
                    "use a smaller fan-out to avoid overwhelming the system"
                ),
            }
    shards = int(_raw_shards or 1)
    model = (getattr(args, "model", None) or "").strip() or None
    provider = (getattr(args, "provider", None) or "").strip().lower() or None
    effort = (getattr(args, "effort", None) or "").strip().lower() or None
    if mode == "subagent" and model:
        return {
            "ok": False,
            "msg": "--model cannot be used with --mode subagent: native subagents always use the parent provider/model context",
        }
    if mode == "subagent" and provider:
        return {
            "ok": False,
            "msg": "--provider cannot be used with --mode subagent: native subagents always use the parent provider/model context",
        }
    if mode == "subagent" and effort:
        return {
            "ok": False,
            "msg": "--effort cannot be used with --mode subagent: native subagents always use the parent provider/model context",
        }
    if mode == "subagent" and getattr(args, "plan", False):
        return {
            "ok": False,
            "msg": "--plan cannot be used with --mode subagent; fan out native subagents directly with --shards",
        }
    if provider:
        from .provider_config import assign_provider_override_error

        provider_error = assign_provider_override_error(provider)
        if provider_error:
            return {"ok": False, "msg": provider_error}
    if model:
        from .provider_config import assign_model_override_error, assign_model_override_warning

        model_error = assign_model_override_error(
            args.role, model, _from_project(), provider_override=provider
        )
        if model_error:
            return {"ok": False, "msg": model_error}
        model_warning = assign_model_override_warning(
            args.role, model, _from_project(), provider_override=provider
        )
        if model_warning:
            print(f"warn: {model_warning}", file=sys.stderr)
    if effort:
        from .provider_config import assign_effort_override_error

        effort_error = assign_effort_override_error(
            args.role, effort, _from_project(), provider_override=provider
        )
        if effort_error:
            return {"ok": False, "msg": effort_error}
    if shards > 1 and getattr(args, "auto_chain", False):
        return {
            "ok": False,
            "msg": (
                "--shards and --auto-chain cannot be used together: "
                "shard fan-out already uses a consolidated handoff; "
                "--auto-chain would double-fire a verify hop."
            ),
        }
    isolation = getattr(args, "isolation", "shared") or "shared"
    plan = bool(getattr(args, "plan", False))
    if isolation == "worktree" and plan:
        return {
            "ok": False,
            "msg": (
                "--isolation worktree cannot be combined with --plan: the planner "
                "pane only analyses the app and writes a bucket plan (no code "
                "changes to isolate). Use --isolation worktree on the impl assign."
            ),
        }
    if plan:
        # Plan-then-fan-out: one PLANNER pane analyses the app, writes a
        # bucket plan, and on done the orchestrator auto-fans-out N shards
        # (each carrying its bucket). A single request — the orchestrator
        # drives the two-phase flow; the CLI never spawns shards directly.
        if shards < 2:
            return {
                "ok": False,
                "msg": (
                    "--plan requires --shards >= 2: the planner splits work "
                    "across N parallel QA shards, so N must be at least 2 "
                    "(use a plain assign for a single tester)"
                ),
            }
        resp = _request(
            _with_project(
                {
                    "cmd": "assign",
                    "role": args.role,
                    "cwd": args.cwd,
                    "task": args.task,
                    "from": _from_role(),
                    "plan": True,
                    "shard_total": shards,
                    "model": model,
                    "provider": provider,
                    "effort": effort,
                    "feature": getattr(args, "feature", "") or "",
                    "mode": mode_requested,
                }
            )
        )
        if resp.get("ok"):
            resp["msg"] = str(resp.get("msg", "")) + _browser_shard_warning(args.role, shards)
        return resp
    if shards > 1:
        # Fan-out: spawn <role>#1 … <role>#N in parallel; each carries shard_total.
        results = []
        for n in range(1, shards + 1):
            shard_key = f"{args.role}#{n}"
            resp = _request(
                _with_project(
                    {
                        "cmd": "assign",
                        "role": shard_key,
                        "cwd": args.cwd,
                        "task": args.task,
                        "from": _from_role(),
                        "requires_commit": bool(getattr(args, "requires_commit", False)),
                        "auto_chain": bool(getattr(args, "auto_chain", False)),
                        "shard_total": shards,
                        "isolation": isolation,
                        "model": model,
                        "provider": provider,
                        "effort": effort,
                        "feature": getattr(args, "feature", "") or "",
                        "mode": mode_requested,
                    }
                )
            )
            results.append(resp)
        ok_count = sum(1 for r in results if r.get("ok"))
        warn = _browser_shard_warning(args.role, shards)
        if mode == "subagent":
            details = "\n".join(str(r.get("msg", "")) for r in results if r.get("msg"))
            return {
                "ok": ok_count == shards,
                "msg": f"registered {ok_count}/{shards} subagents\n{details}".rstrip() + warn,
            }
        return {"ok": ok_count == shards, "msg": f"queued {ok_count}/{shards} shards{warn}"}
    return _request(
        _with_project(
            {
                "cmd": "assign",
                "role": args.role,
                "cwd": args.cwd,
                "task": args.task,
                "from": _from_role(),
                "requires_commit": bool(getattr(args, "requires_commit", False)),
                "auto_chain": bool(getattr(args, "auto_chain", False)),
                "isolation": isolation,
                "model": model,
                "provider": provider,
                "effort": effort,
                "feature": getattr(args, "feature", "") or "",
                "mode": mode,
            }
        )
    )


def cmd_subagent_done(args: argparse.Namespace) -> dict:
    return _request(
        _with_project(
            {
                "cmd": "subagent-done",
                "from": _from_role(),
                "role": args.role,
                "note": args.note or "",
                "failed": bool(getattr(args, "fail", False)),
            }
        )
    )


def _live_worktree_paths_best_effort() -> set[str]:
    """Live-pane worktree paths from the running orchestrator, if reachable.

    `clean` is the only `worktree` subcommand that needs this (#187
    live-pane guard). Best-effort by design, mirroring `cmd_worktree`'s
    crash-recovery rationale: with the cockpit not running there is no live
    pane that could hold a worktree open, so any connect failure (no port
    file, refused, timeout, malformed reply) is treated as "nothing to
    protect" rather than an error — `clean` must keep working with the
    cockpit closed.
    """
    try:
        resp = _request(_with_project({"cmd": "worktree-live-paths"}))
    except (RuntimeError, OSError, ValueError):
        return set()
    if not isinstance(resp, dict) or not resp.get("ok"):
        return set()
    paths = resp.get("paths")
    return set(paths) if isinstance(paths, list) else set()


def cmd_worktree(args: argparse.Namespace) -> dict:
    """`takkub worktree list|merge|clean` — Lead merge assist for #81 worktrees.

    Pure-local git operations (no orchestrator socket): the git state is the
    source of truth, so this works after a cockpit crash or with the cockpit
    closed — exactly when cleanup is most needed. Mutations are lead-gated at
    the CLI layer like assign/close. `merge` and `clean` both make a
    best-effort socket call for the live-pane guard (#187, #227) — see
    `_live_worktree_paths_best_effort`.

    `clean` additionally reports (and, with `--orphans`/
    `--orphans-node-modules-only`, deletes) on-disk checkout dirs git has
    completely forgotten — see `WorktreeManager.list_orphans` (#355).
    """
    from .worktree_manager import WorktreeManager, remove_worktree_tree

    cwd = getattr(args, "cwd", None) or os.getcwd()
    mgr = WorktreeManager()
    root = mgr.git_root(cwd)
    if root is None:
        return {"ok": False, "msg": f"'{cwd}' ไม่อยู่ใน git repo (ใช้ --cwd ชี้โปรเจค)"}

    sub = args.wt_cmd
    if sub == "list":
        rows = mgr.list_isolated(root)
        if not rows:
            print("(no isolated wt/* worktrees)")
            return {"ok": True, "msg": "0 worktrees"}
        for r in rows:
            flags = []
            if r["ahead"]:
                flags.append(f"{r['ahead']} commit ahead")
            if r["dirty"]:
                flags.append("dirty")
            _utf8_print(
                f"  {r['branch']:<32} {(' · '.join(flags) or 'clean/empty'):<28} {r['path']}"
            )
        return {"ok": True, "msg": f"{len(rows)} worktree(s)"}

    if sub == "merge":
        branch = args.branch
        if not branch and not args.role:
            return {"ok": False, "msg": "ระบุ --role <r> (branch ล่าสุดของ role) หรือ --branch wt/..."}
        if not branch:
            # resolve the newest wt/<role>-* branch for --role
            from .worktree_manager import sanitize_ref_component

            prefix = f"wt/{sanitize_ref_component(args.role or '')}-"
            cands = sorted(
                (r["branch"] for r in mgr.list_isolated(root) if r["branch"].startswith(prefix)),
            )
            if not cands:
                return {"ok": False, "msg": f"ไม่พบ worktree branch ของ role '{args.role}'"}
            branch = cands[-1]  # highest ts = newest
        live_paths = _live_worktree_paths_best_effort()
        ok, msg = mgr.merge_isolated(root, branch, keep=bool(args.keep), live_paths=live_paths)
        return {"ok": ok, "msg": msg}

    if sub == "clean":
        do_orphans = bool(getattr(args, "orphans", False))
        do_nm_only = bool(getattr(args, "orphans_node_modules_only", False))
        if do_orphans and do_nm_only:
            return {"ok": False, "msg": "ใช้ --orphans หรือ --orphans-node-modules-only อย่างเดียว"}

        live_paths = _live_worktree_paths_best_effort()
        lines = mgr.clean_isolated(root, force=bool(args.force), live_paths=live_paths)
        if lines:
            for line in lines:
                _utf8_print(f"  {line}")
        else:
            print("(nothing to clean)")
        failed = sum(1 for line in lines if line.startswith("FAILED"))

        # #355 — dirs git has completely forgotten (registration already
        # pruned) that the git-driven sweep above can never see, because it
        # only ever iterates `git worktree list`. Always reported; only
        # deleted when the caller opts in via a flag (may hold uncommitted
        # work git no longer has any record of).
        orphans = mgr.list_orphans(root, live_paths=live_paths)
        orphan_note = ""
        if orphans:
            total_bytes = sum(o["size_bytes"] for o in orphans)
            _utf8_print(f"\norphans (git ไม่รู้จักแล้ว): {len(orphans)} dir, {_fmt_bytes(total_bytes)}")
            for o in orphans:
                tag = " [node_modules]" if o["has_node_modules"] else ""
                _utf8_print(f"  ORPHAN  {_fmt_bytes(o['size_bytes']):>9}  {o['path']}{tag}")
                if do_orphans:
                    removed, msg, _leftover = remove_worktree_tree(Path(o["path"]))
                    if removed and not msg:
                        _utf8_print("          REMOVED")
                    else:
                        _utf8_print(f"          FAILED: {msg or 'ลบไม่ครบ'}")
                        failed += 1
                elif do_nm_only:
                    from .disk_usage import find_node_modules

                    nm_dirs = find_node_modules(Path(o["path"]))
                    nm_failed = []
                    for nm in nm_dirs:
                        removed, msg, _leftover = remove_worktree_tree(nm)
                        if not (removed and not msg):
                            nm_failed.append(msg or str(nm))
                    if nm_failed:
                        _utf8_print(f"          node_modules ลบไม่ครบ: {'; '.join(nm_failed)}")
                        failed += 1
                    else:
                        _utf8_print(f"          node_modules ลบแล้ว ({len(nm_dirs)} dir)")
            if do_orphans:
                orphan_note = f"; ลบ orphan {len(orphans)} dir ({_fmt_bytes(total_bytes)})"
            elif do_nm_only:
                orphan_note = f"; ลบ node_modules ใน orphan {len(orphans)} dir"
            else:
                orphan_note = (
                    f"; พบ orphan {len(orphans)} dir ({_fmt_bytes(total_bytes)}) — ยังไม่ลบ "
                    "(ใส่ --orphans หรือ --orphans-node-modules-only)"
                )

        if not lines and not orphans:
            return {"ok": True, "msg": "0 cleaned"}
        return {"ok": failed == 0, "msg": f"{len(lines)} processed, {failed} failed{orphan_note}"}

    return {"ok": False, "msg": f"unknown worktree subcommand: {sub}"}


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def cmd_disk(args: argparse.Namespace) -> dict:
    """`takkub disk` — categorized DATA_HOME usage report (safe/review/never).

    Pure-local (no orchestrator socket, same rationale as `worktree`): reads
    straight off disk + git state so it works even with the cockpit closed.
    Available to every pane (read-only) — mutation lives in `prune`.
    """
    from .disk_usage import disk_report

    report = disk_report()
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return {"ok": True, "msg": f"{_fmt_bytes(report['total_bytes'])} total"}

    _utf8_print(f"DATA_HOME: {report['data_home']}")
    # L2 (2026-08-05 cross-OS audit): graft-graphs/ can live OUTSIDE
    # DATA_HOME (a dev checkout falls back to Path.home()/".agent-takkub" —
    # see graft_store.py's module docstring) while its bytes still count
    # toward `total_bytes` below. Say so explicitly instead of letting the
    # total look like it's all under DATA_HOME.
    if report.get("graft_store_root_outside_data_home"):
        _utf8_print(
            f"  (note: graft-graphs/ counted above lives outside DATA_HOME, at {report['graft_store_root']})"
        )
    _utf8_print(f"total: {_fmt_bytes(report['total_bytes'])}\n")
    for c in sorted(report["categories"], key=lambda c: -c["size_bytes"]):
        tag = {"safe": "safe  ", "review": "review", "never": "never "}[c["level"]]
        _utf8_print(
            f"  [{tag}] {_fmt_bytes(c['size_bytes']):>9}  {c['file_count']:>7} files  "
            f"{c['key']:<20} {c['label']}"
        )
        if c.get("detail"):
            _utf8_print(f"           {c['detail']}")
    wt = report["worktrees"]
    if wt["orphan"] or wt["registered"]:
        _utf8_print("\nworktrees:")
        for r in wt["orphan"]:
            if r.get("prune_bucket") == "review":
                _utf8_print(
                    f"  ORPHAN? {_fmt_bytes(r['size_bytes']):>8}  {r['path']}"
                    "  (มีไฟล์ source/uncommitted/commit ค้าง — ต้อง review ก่อนลบ)"
                )
            else:
                _utf8_print(f"  ORPHAN  {_fmt_bytes(r['size_bytes']):>8}  {r['path']}")
        for r in wt["registered"]:
            flags = []
            if r.get("dirty"):
                flags.append("dirty")
            if r.get("ahead"):
                flags.append(f"{r['ahead']} ahead")
            if r.get("pane_active"):
                flags.append("pane-active?")
            _utf8_print(
                f"  LIVE   {_fmt_bytes(r['size_bytes']):>9}  {r.get('branch', ''):<28} "
                f"{(' · '.join(flags) or 'clean'):<24} {r['path']}"
            )
    return {"ok": True, "msg": f"{_fmt_bytes(report['total_bytes'])} total"}


def cmd_prune(args: argparse.Namespace) -> dict:
    """`takkub prune` — delete reclaimable DATA_HOME categories (lead only).

    Dry-run by default; requires --yes to actually delete. Never touches a
    `never`-level category regardless of --category. Pure-local, same as
    `disk`/`worktree` — no orchestrator socket involved.
    """
    from .disk_usage import VALID_CATEGORIES, prune

    categories = None
    if args.category:
        categories = [c.strip() for c in args.category.split(",") if c.strip()]
        unknown = [c for c in categories if c not in VALID_CATEGORIES]
        if unknown:
            return {
                "ok": False,
                "msg": f"unknown --category {unknown}; valid: {', '.join(VALID_CATEGORIES)}",
            }

    result = prune(
        categories=categories,
        level=args.level,
        older_than_days=args.older_than,
        dry_run=not args.yes,
        include_live=bool(args.include_live),
    )

    for cat in result["categories"]:
        verb = "would remove" if cat["dry_run"] else "removed"
        bytes_field = cat["would_free_bytes"] if cat["dry_run"] else cat["freed_bytes"]
        _utf8_print(
            f"  [{cat['level']}] {cat['category']:<18} {verb} {cat['target_count']} "
            f"({_fmt_bytes(bytes_field)})"
        )
        if cat["category"] == "orphan-worktrees-review":
            # #132: this bucket can hold real source/uncommitted/unmerged
            # content — always show exactly what would disappear, dry-run or
            # not, so --yes is never fired blind.
            for t in cat.get("targets", []):
                _utf8_print(f"           - {t}")
        for s in cat["skipped"]:
            _utf8_print(f"           skip: {s}")
        for e in cat["errors"]:
            _utf8_print(f"           error: {e}")
    for r in result["refusals"]:
        _utf8_print(f"  REFUSED: {r}")

    total_field = (
        result["total_would_free_bytes"] if result["dry_run"] else result["total_freed_bytes"]
    )
    verb = "would free" if result["dry_run"] else "freed"
    msg = f"{verb} {_fmt_bytes(total_field)}"
    if result["dry_run"] and total_field:
        msg += " (dry-run — pass --yes to actually delete)"
    return {"ok": result["ok"], "msg": msg}


def cmd_send(args: argparse.Namespace) -> dict:
    return _request(
        _with_project({"cmd": "send", "to": args.to, "msg": args.msg, "from": _from_role()})
    )


def cmd_close(args: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "close", "role": args.role, "from": _from_role()}))


def cmd_close_all(_: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "close-all", "from": _from_role()}))


def cmd_restart(_: argparse.Namespace) -> dict:
    """Full cockpit restart from the terminal — no button needed. State/tabs/
    session snapshot persist first, then the app relaunches and panes respawn."""
    return _request(_with_project({"cmd": "restart", "from": _from_role()}))


def cmd_done(args: argparse.Namespace) -> dict:
    return _request(
        _with_project(
            {
                "cmd": "done",
                "from": _from_role(),
                "note": args.note or "",
                # #296: --blocked implies a non-success outcome (the task did
                # NOT get done) but carries the reason that it could not RUN,
                # which routes to a human instead of back to a role.
                "failed": bool(getattr(args, "fail", False))
                or bool(getattr(args, "blocked", False)),
                "blocked": bool(getattr(args, "blocked", False)),
                "force": bool(getattr(args, "force", False)),
            }
        )
    )


def cmd_ma(args: argparse.Namespace) -> dict:
    """(operator) run the standing maintenance checklist over this cockpit.

    Read-only by design — it reports what the checks found and the ordered plan
    that follows from them; deciding which findings to act on is Lead's call,
    not a script's. See `maintenance.py`.
    """
    from pathlib import Path

    from .maintenance import render_report, run_maintenance

    report = run_maintenance(
        Path.cwd(),
        since_hours=float(getattr(args, "since_hours", 24.0)),
        include_network=not bool(getattr(args, "no_net", False)),
    )
    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_report(report))
    attention = len(report.needs_attention)
    return {
        "ok": True,
        "msg": ("ไม่มีอะไรต้องทำ" if not attention else f"{attention} หัวข้อต้องดูต่อ"),
        "quiet": True,
    }


def cmd_progress(args: argparse.Namespace) -> dict:
    """(agent) report a status update to Lead WITHOUT ending the task.

    Unlike `done`, this never schedules the pane's teardown — use it for a
    long-running task (docker build, migration, e2e suite) that isn't
    finished yet but has something worth telling Lead. Calling `done` mid-
    task kills whatever subprocess is still running underneath the pane
    (#234) — `done` means the task is over, full stop."""
    return _request(
        _with_project({"cmd": "progress", "from": _from_role(), "note": args.note or ""})
    )


def cmd_end_session(args: argparse.Namespace) -> dict:
    return _request(
        _with_project({"cmd": "end-session", "from": _from_role(), "note": args.note or ""})
    )


def cmd_goal(args: argparse.Namespace) -> dict:
    """Set / show / clear the session objective (issue #50).

    `takkub goal "<objective>"` sets it; `takkub goal` (no arg) shows the
    current one; `takkub goal --clear` unsets it. The objective is prepended
    to every subsequent `takkub assign` task so parallel teammates share the
    same big picture and don't drift on scope."""
    return _request(
        _with_project(
            {
                "cmd": "goal",
                "from": _from_role(),
                "text": getattr(args, "text", None) or "",
                "clear": bool(getattr(args, "clear", False)),
            }
        )
    )


def cmd_harvest(args: argparse.Namespace) -> dict:
    """Scan artifact paths for a role that forgot `takkub done`, then optionally
    synthesize a done event via harvest-done IPC.

    Exit codes (returned in the dict as 'exit_code'):
      0 = done event synthesized
      1 = user declined or server error
      2 = role not running
      3 = no artifacts found
    """
    from datetime import datetime

    payload: dict = _with_project({"cmd": "harvest", "role": args.role, "from": _from_role()})
    if getattr(args, "since", None):
        payload["since"] = args.since
    payload["limit"] = getattr(args, "limit", None) or 100

    resp = _request(payload)
    if not resp.get("ok"):
        msg = resp.get("msg", "harvest query failed")
        if "not running" in msg:
            return {"ok": False, "msg": msg, "exit_code": 2}
        return {"ok": False, "msg": msg, "exit_code": 1}

    artifacts = resp.get("artifacts") or []
    state = resp.get("state", "?")
    since_ts = resp.get("since_ts") or 0

    since_str = datetime.fromtimestamp(since_ts).strftime("%H:%M:%S") if since_ts else "?"

    if not artifacts:
        print(f"no artifacts found for '{args.role}' (state: {state}) since {since_str}")
        return {"ok": False, "msg": "no artifacts found", "exit_code": 3}

    print(f"\n[harvest] role: {args.role}  state: {state}  since: {since_str}")
    print(f"  {len(artifacts)} artifact(s) found:")
    for a in artifacts:
        rel = a.get("mtime_rel", "?")
        path = a.get("path", "?")
        print(f"  {rel:>10}  {path}")
    print()

    if getattr(args, "auto_confirm", False):
        answer = "y"
    else:
        try:
            answer = input(f"mark '{args.role}' as done? [Y/n] ").strip().lower() or "y"
        except EOFError:
            answer = "n"

    if answer not in ("y", "yes"):
        print("harvest cancelled")
        return {"ok": False, "msg": "user declined", "exit_code": 1}

    note = f"harvest: {len(artifacts)} artifact(s) modified since {since_str}"
    done_resp = _request(
        _with_project(
            {"cmd": "harvest-done", "role": args.role, "note": note, "from": _from_role()}
        )
    )
    if done_resp.get("ok"):
        print(f"ok: '{args.role}' marked as done ({len(artifacts)} artifact(s))")
        return {"ok": True, "msg": f"harvested {len(artifacts)} artifact(s)"}
    return {"ok": False, "msg": done_resp.get("msg", "harvest-done failed"), "exit_code": 1}


def _require_lead_for_task_admin(action: str) -> str | None:
    """Same rationale/shape as `_require_lead_for_pane_tools`: `task
    reconcile`/`task close` mutate the shared ledger for *any* role, so only
    Lead runs them — `task show` (read-only, self-scoped) stays open."""
    role = _from_role()
    if role is None:
        return None
    if role.lower() != "lead":
        return (
            f"only lead can run 'takkub {action}'. you are '{role}'.\n"
            f"       'takkub task show' stays open for everyone; ask lead to clean up the ledger."
        )
    return None


def cmd_messages(args: argparse.Namespace) -> dict:
    """`takkub messages --role <r>` — read back what `takkub send` actually
    did (issue #277).

    `send` reports that it queued a message, not that anyone read it, and a
    pane respawn can swallow one whole. This is the lookup that makes that
    checkable instead of a matter of trust: every message, its delivery state
    (queued / confirmed received / abandoned), and whether the cockpit had to
    re-send it after a respawn.
    """
    resp = _request(
        _with_project(
            {
                "cmd": "messages",
                "role": args.role,
                "limit": int(getattr(args, "limit", 20) or 20),
                "from": _from_role(),
            }
        )
    )
    if not resp.get("ok"):
        return {"ok": False, "msg": resp.get("msg", "messages failed"), "exit_code": 1}
    lines = resp.get("lines") or []
    if not lines:
        print(f"[messages] ยังไม่มีข้อความที่ส่งถึง {args.role} ในโปรเจกต์นี้")
        return {"ok": True, "msg": "no messages"}
    for line in lines:
        _utf8_print(line)
    return {"ok": True, "msg": resp.get("msg", "messages")}


def cmd_task(args: argparse.Namespace) -> dict:
    """`takkub task show --role <r>` — print the full text of the last task
    assigned to `role` (issue #1 file-based task handoff).

    Works whether the assign pasted the task inline (short task, no handoff
    file) or a pointer (long task, read back from the on-disk handoff file)
    — the CLI always resolves to the full text either way.

    `takkub task reconcile [--dry-run]` / `takkub task close --role <r>
    [--force] [--dry-run]` are the issue #166 ledger-cleanup commands — a
    row can stick at "working" forever once the cockpit process that owned
    it exits, since only a live pane's done/close handler ever flips it.
    """
    if args.t_cmd == "show":
        resp = _request(
            _with_project({"cmd": "task-show", "role": args.role, "from": _from_role()})
        )
        if not resp.get("ok"):
            return {"ok": False, "msg": resp.get("msg", "task-show failed"), "exit_code": 1}
        task_file = resp.get("task_file")
        if task_file:
            print(f"[task file] {task_file}\n")
        _utf8_print(resp.get("task", ""))
        return {"ok": True, "msg": "task"}
    if args.t_cmd == "reconcile":
        gate_err = _require_lead_for_task_admin("task reconcile")
        if gate_err:
            return {"ok": False, "msg": gate_err}
        return _request(
            _with_project(
                {"cmd": "task-reconcile", "dry_run": bool(args.dry_run), "from": _from_role()}
            )
        )
    if args.t_cmd == "close":
        gate_err = _require_lead_for_task_admin("task close")
        if gate_err:
            return {"ok": False, "msg": gate_err}
        return _request(
            _with_project(
                {
                    "cmd": "task-close",
                    "role": args.role,
                    "force": bool(args.force),
                    "dry_run": bool(args.dry_run),
                    "from": _from_role(),
                }
            )
        )
    if args.t_cmd == "cancel":
        gate_err = _require_lead_for_task_admin("task cancel")
        if gate_err:
            return {"ok": False, "msg": gate_err}
        return _request(
            _with_project({"cmd": "task-cancel", "role": args.role, "from": _from_role()})
        )
    return {"ok": False, "msg": f"unknown task subcommand: {args.t_cmd}"}


def cmd_list(_: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "list"}))


def _print_status_report(report: object) -> None:
    """Pretty-print the per-pane report returned by `takkub status`."""
    if not isinstance(report, dict):
        print("  project: ?")
        return
    project = report.get("project") or "?"
    panes = report.get("panes") or {}
    if not isinstance(panes, dict):
        panes = {}
    print(f"  project: {project}")
    for role, info in panes.items():
        if not isinstance(info, dict):
            info = {}
        # #263: display_state is the unified login-required/booting/
        # waiting-delivery/busy/unknown verdict — falls back to the raw
        # state for a report predating this key.
        state = info.get("display_state", info.get("state", "?"))
        stall = info.get("stall_minutes")
        human_ts = info.get("last_progress_human", "?")
        abs_ts = info.get("last_progress_abs", "?")
        stall_str = f" ⚠ stalled {stall}m" if stall is not None else ""
        blocked = info.get("blocked_reason")
        blocked_str = f" ⛔ blocked:{blocked}-prompt" if blocked else ""
        unconfirmed_str = " ❓ delivery unconfirmed" if info.get("delivery_unconfirmed") else ""
        print(f"\n  [{role}] {state}{stall_str}{blocked_str}{unconfirmed_str}")
        print(f"    last progress: {human_ts} ({abs_ts})")
        # #301: quota-stalled panes carry their own reset-window/marker
        # instead of a bare "working" — surface both so Lead doesn't have to
        # go read the pane itself to know why it's frozen.
        if state == "stalled:quota":
            quota_marker = info.get("quota_marker") or ""
            quota_human = info.get("quota_resets_human") or "?"
            marker_str = f' — "{quota_marker}"' if quota_marker else ""
            print(f"    ⏳ quota resets in {quota_human}{marker_str}")
        model = info.get("model")
        if model:
            print(f"    model: {model}")
        tail = (info.get("transcript_tail") or "").strip()
        if tail:
            for line in tail.splitlines()[-3:]:
                print(f"    │ {line[:120]}")
        shot = info.get("last_screenshot") or ""
        if shot:
            print(f"    screenshot: {shot}")
        done_evts = info.get("done_events") or []
        if done_evts:
            print(f"    done events: {', '.join(done_evts)}")


_INBOX_QUEUE_LABEL = {
    "digest": "digest (debounce window, ~60s)",
    "live": "live (ready-prompt delivery queue)",
    "durable": "durable (survives a restart)",
}


def _print_inbox_items(items: object) -> None:
    """Pretty-print the pending-report list returned by `takkub inbox`."""
    if not isinstance(items, list) or not items:
        print("  (nothing pending — every report has been delivered)")
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "?")
        queue = _INBOX_QUEUE_LABEL.get(item.get("queue", ""), item.get("queue", "?"))
        confirmed = item.get("origin_confirmed")
        flag = " ⚠ unconfirmed origin — role respawned since queued" if confirmed is False else ""
        age = ""
        queued_ts = item.get("queued_ts")
        if isinstance(queued_ts, (int, float)):
            age_sec = max(0.0, time.time() - queued_ts)
            if age_sec < 60:
                age = f" · queued {int(age_sec)}s ago"
            elif age_sec < 3600:
                age = f" · queued {int(age_sec // 60)}m ago"
            else:
                age = f" · queued {int(age_sec // 3600)}h ago"
        print(f"\n  [{role}] · {queue}{age}{flag}")
        body = str(item.get("body", "")).strip()
        for line in body.splitlines():
            print(f"    │ {line}")


def cmd_status(args: argparse.Namespace) -> dict:
    payload = _with_project({"cmd": "status"})
    if getattr(args, "since", None):
        payload["since"] = args.since
    return _request(payload)


def cmd_inbox(args: argparse.Namespace) -> dict:
    """(Lead) read the actual content of every done/FAILED report still
    sitting in the outbound-to-Lead pipeline instead of already written into
    Lead's pane — `takkub status` could only ever say "queued", not show
    what's in the queue (#231)."""
    payload = _with_project({"cmd": "inbox", "from": _from_role()})
    if getattr(args, "role", None):
        payload["role"] = args.role
    return _request(payload)


def cmd_wait(args: argparse.Namespace) -> dict:
    """(lead) block until every requested role's done/FAILED report has
    actually reached the Lead pane, --timeout elapses, or a blocking report
    from a role OUTSIDE --role interrupts the wait (#242, #253).

    This function IS the canned polling loop — see `lead_wait.py`'s module
    docstring for the rationale. It replaces the hand-rolled `takkub status`
    loops every Lead pane used to write for itself; it isn't one more
    example of writing one. No --role at all defaults to every role
    currently tracked by this project (same set `takkub list` shows, minus
    Lead itself).

    #253: a `wait --role X` used to be deaf for the entire --timeout to a
    FAILED/spawn-failed/etc. report from role Y it wasn't watching — the
    report reached Lead's pane eventually (nothing was lost, see
    lead_inbox.py), but only after this call finally returned, up to 30
    minutes late. `poll_wait`'s "interrupt" field ends this call the moment
    that happens; roles still in --role stay genuinely pending in their own
    panes, so re-run `takkub wait` (same or no --role) to resume watching
    them once the interrupting report has been dealt with.

    #265: the same interrupt field also fires — with `reason: "user_input"`
    — the moment the pane's OWNER types anything into it (submitted or
    still drafting) after this call started watching. Without this, owner
    keystrokes typed while `wait` is blocking just sit as queued CLI input,
    unprocessed until `wait` returns, up to the full --timeout — the owner
    outranks every teammate role and must never be the one left waiting.

    --cancel (#249 item 5) skips begin/poll entirely and just releases
    whatever wait registration is active for this project — the cleanup
    path for a wait that's stuck watching a role that will never resolve
    (or one the caller simply no longer wants to keep blocking on).

    #357: a `reason: "user_input"` interrupt cannot always be trusted as
    "the owner actually typed something" — the same `_on_pane_input` choke
    point that stamps `_lead_last_user_input_ts` also carries terminal
    auto-replies (e.g. a cursor-position/device-attributes response the
    embedded xterm.js emits through its one public `onData` event when the
    Lead pane's own TUI redraws after a cockpit-injected digest/banner/
    notice — that echo is indistinguishable from a real keystroke at the
    widget layer, which has no API to tag it otherwise). `--no-interrupt`
    is the escape hatch: instead of stopping on that specific reason, this
    loop quietly re-attaches to whatever roles are still pending and keeps
    polling — a genuine blocking report from an outside role (plain
    `interrupt`, no `reason`) still stops the wait immediately, unaffected.
    """
    if getattr(args, "cancel", False):
        result = _request(_with_project({"cmd": "wait-cancel", "from": _from_role()}))
        print(f"[wait] {result.get('msg', 'cancel requested')}")
        return result

    timeout = getattr(args, "timeout", None) or _WAIT_DEFAULT_TIMEOUT_S
    timeout = max(_WAIT_MIN_TIMEOUT_S, min(float(timeout), _WAIT_MAX_TIMEOUT_S))
    no_interrupt = getattr(args, "no_interrupt", False)

    begin = _request(
        _with_project(
            {
                "cmd": "wait-begin",
                "roles": getattr(args, "role", None) or [],
                "timeout": timeout,
                "from": _from_role(),
            }
        )
    )
    if not begin.get("ok"):
        return begin

    wait_id = begin.get("wait_id")
    roles = begin.get("roles") or []
    if begin.get("attached"):
        print(f"[wait] attached to an existing wait already covering: {', '.join(roles)}")
    else:
        print(f"[wait] watching: {', '.join(roles)} (timeout {int(timeout)}s)")

    start = time.time()
    interval = _WAIT_POLL_INTERVAL_S
    last: dict = {}
    last_pending_keys: set[str] = set(roles)
    last_heartbeat = start
    interrupted_by: dict | None = None
    try:
        while True:
            poll = _request(
                _with_project({"cmd": "wait-poll", "wait_id": wait_id, "from": _from_role()})
            )
            if not poll.get("ok"):
                return poll
            last = poll
            pending = poll.get("pending") or {}
            pending_keys = set(pending)
            now_t = time.time()
            # #249 item 4: prove "still waiting" vs "hung" from the outside
            # — print the instant a role resolves, otherwise no more often
            # than the heartbeat interval while roles remain pending.
            resolved_now = last_pending_keys - pending_keys
            if resolved_now:
                print(
                    f"[wait] resolved: {', '.join(sorted(resolved_now))} "
                    f"— {len(pending_keys)} still pending ({int(now_t - start)}s elapsed)"
                )
                last_heartbeat = now_t
            elif pending and now_t - last_heartbeat >= _WAIT_HEARTBEAT_INTERVAL_S:
                print(
                    f"[wait] still waiting on {len(pending_keys)}: "
                    f"{', '.join(sorted(pending_keys))} ({int(now_t - start)}s elapsed)"
                )
                last_heartbeat = now_t
            last_pending_keys = pending_keys
            interrupt = poll.get("interrupt")
            if interrupt and interrupt.get("reason") == "user_input" and no_interrupt:
                # #357: caller asked to ride out this specific interrupt
                # reason — the server already tore down the registration
                # (poll_wait ends it on ANY interrupt), so transparently
                # re-attach to whatever roles are still pending instead of
                # surfacing it as a stop. Outer `start`/`timeout` bookkeeping
                # is unaffected — only this sub-registration is new.
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    break
                rebegin = _request(
                    _with_project(
                        {
                            "cmd": "wait-begin",
                            "roles": sorted(pending_keys),
                            "timeout": remaining,
                            "from": _from_role(),
                        }
                    )
                )
                if not rebegin.get("ok"):
                    return rebegin
                wait_id = rebegin.get("wait_id")
                continue
            if interrupt:
                interrupted_by = interrupt
                if interrupt.get("reason") == "user_input":
                    # #265: the owner typed something while this wait was
                    # blocking — they outrank every role. Stop immediately
                    # so Lead reads it now instead of leaving it queued as
                    # unprocessed CLI input for up to the full --timeout.
                    print(
                        "[wait] interrupted — คุณพิมพ์ข้อความใหม่เข้ามาระหว่างรอ: "
                        f"{interrupt.get('detail')} "
                        f"({len(pending_keys)} watched role(s) still pending: "
                        f"{', '.join(sorted(pending_keys))}) "
                        "— อ่าน/จัดการข้อความใหม่ก่อน แล้วค่อย `takkub wait` อีกครั้งเพื่อ resume watching"
                    )
                else:
                    # #253: a blocking report (FAILED/spawn-failed/etc.) from a
                    # role outside --role landed while we were still watching —
                    # stop blocking now instead of riding out the full timeout
                    # deaf to it. The watched roles above are still genuinely
                    # pending in their own panes; only this wait ends.
                    print(
                        f"[wait] interrupted — [{interrupt.get('role')}] needs attention: "
                        f"{interrupt.get('detail')} "
                        f"({len(pending_keys)} watched role(s) still pending: "
                        f"{', '.join(sorted(pending_keys))}) "
                        "— see `takkub inbox`, then `takkub wait` again to resume watching"
                    )
                break
            if not pending or poll.get("expired"):
                break
            remaining = timeout - (time.time() - start)
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
            interval = min(interval * 1.3, _WAIT_POLL_MAX_INTERVAL_S)
    finally:
        # Server auto-releases the registration once every role resolves or
        # its own timeout fires — only clean up here on an early client-side
        # exit (Ctrl-C, exception) that left it dangling.
        if last.get("pending"):
            _request(_with_project({"cmd": "wait-end", "wait_id": wait_id, "from": _from_role()}))

    done = last.get("done") or {}
    failed = last.get("failed") or {}
    gone = last.get("gone") or {}
    pending = last.get("pending") or {}
    elapsed = int(last.get("elapsed") or (time.time() - start))

    print(f"\n[wait] resolved after {elapsed}s")
    if done:
        print(f"  done: {', '.join(sorted(done))}")
    if failed:
        print(f"  FAILED: {', '.join(sorted(failed))}")
    if gone:
        print("  gone (will never report — never spawned or pane already closed):")
        for role, reason in sorted(gone.items()):
            print(f"    - {role}: {reason}")
    if pending:
        reason_label = "wait interrupted, see above" if interrupted_by else "timeout reached"
        print(f"  still pending ({reason_label}):")
        for role, reason in sorted(pending.items()):
            print(f"    - {role}: {reason}")

    ok = not pending
    if ok:
        msg = "all roles resolved"
    elif interrupted_by:
        if interrupted_by.get("reason") == "user_input":
            msg = f"interrupted by user input; {len(pending)} role(s) still pending"
        else:
            msg = (
                f"interrupted by [{interrupted_by.get('role')}]; "
                f"{len(pending)} role(s) still pending"
            )
    else:
        msg = f"timeout with {len(pending)} role(s) still pending"
    return {
        "ok": ok,
        "msg": msg,
        "done": done,
        "failed": failed,
        "gone": gone,
        "pending": pending,
        "interrupt": interrupted_by,
    }


def cmd_verify(args: argparse.Namespace) -> dict:
    """Auto-detect stack and run lint/test gate in cwd."""
    import json as _json
    from pathlib import Path

    from .verify import detect_stack, format_summary, run_checks

    cwd = Path(args.cwd) if args.cwd else Path(".")
    checks = detect_stack(cwd)

    skip = set(getattr(args, "skip", None) or [])
    checks = [c for c in checks if c.name not in skip]

    result = run_checks(checks, cwd=cwd)
    summary = format_summary(result)
    print(summary)

    if args.json:
        data = {
            "all_passed": result.all_passed,
            "checks": [
                {
                    "name": cr.check.name,
                    "exit_code": cr.exit_code,
                    "duration_ms": round(cr.duration_ms, 1),
                    "stdout_tail": cr.stdout_tail,
                    "stderr_tail": cr.stderr_tail,
                }
                for cr in result.checks
            ],
        }
        print(_json.dumps(data, indent=2))

    ok = result.all_passed
    return {"ok": ok, "msg": "all checks passed" if ok else "some checks failed"}


def cmd_docs_verify(args: argparse.Namespace) -> dict:
    """Verify markdown references in docs/ and key root files."""
    from pathlib import Path

    from .docs_verify import format_drift_report, verify_docs

    exclude_globs: tuple[str, ...] = tuple(args.exclude) if args.exclude else ()
    results = verify_docs(
        docs_dirs=(Path("docs"),),
        extras=(Path("CLAUDE.md"), Path("README.md")),
        repo_root=Path("."),
        exclude_globs=exclude_globs,
        use_default_excludes=not args.no_default_excludes,
    )
    broken = [r for r in results if r.status != "ok"]
    report = format_drift_report(results)

    output_path = Path(args.report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nReport written to {output_path}")
    print(f"{len(broken)} broken ref(s) found")

    if args.exit_on_broken and broken:
        return {"ok": False, "msg": f"{len(broken)} broken ref(s)"}
    return {"ok": True, "msg": f"{len(broken)} broken ref(s)"}


def cmd_audit_skills(args: argparse.Namespace) -> dict:
    """Compute TF-IDF cosine similarity across role docs, produce a boundary report."""
    from pathlib import Path

    from .skill_audit import audit_skills, format_report

    skills_dir = Path(".claude/agents")
    pairs = audit_skills(skills_dir, threshold=args.threshold)
    report = format_report(pairs, threshold=args.threshold)

    if args.json:
        import json

        data = [{"role_a": a, "role_b": b, "similarity": s} for a, b, s in pairs]
        print(json.dumps(data, indent=2))
        print(f"\n{len(pairs)} pair(s) above threshold {args.threshold}")
    else:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(report)
        print(f"\nReport written to {output_path}")

    return {"ok": True, "msg": f"{len(pairs)} overlap pair(s) found"}


def cmd_migrate_skills(args: argparse.Namespace) -> dict:
    """Migrate legacy cockpit-created skills out of a project's repo into the
    central store (junction/symlink left behind). Pure local — no orchestrator
    IPC. Defaults to the active project; `--project NAME` targets another. Only
    git-untracked real skill dirs move (see `skill_scan` docstring) — a
    git-tracked (user-committed) skill is never touched. `--dry-run` reports the
    plan without changing anything."""
    from pathlib import Path

    from . import skill_scan
    from .config import active_project, lead_cwd
    from .lead_context import _allowed_project_roots

    project = args.project or active_project()[0]
    if not project:
        return {"ok": False, "msg": "no active project — pass --project NAME"}
    roots = _allowed_project_roots(project)
    if not roots:
        root = lead_cwd(project)
        roots = [Path(root)] if root else []
    if not roots:
        return {"ok": False, "msg": f"could not resolve a folder for project {project!r}"}

    records = skill_scan.migrate_legacy_project_skills(roots[0], project, dry_run=args.dry_run)
    if not records:
        return {"ok": True, "msg": f"no skills under {roots[0]}/.claude/skills (nothing to do)"}

    verb = "would migrate" if args.dry_run else "migrated"
    for r in records:
        print(f"  {r.action:18s} {r.name}" + (f"  — {r.detail}" if r.detail else ""))
    moved = [r for r in records if r.action in ("migrated", "would-migrate")]
    errored = [r for r in records if r.action == "error"]
    ok = not errored
    return {
        "ok": ok,
        "msg": f"{len(moved)} skill(s) {verb}, {len(errored)} error(s) (inspected {len(records)})",
    }


def cmd_codex(args: argparse.Namespace) -> dict:
    """Fire OpenAI Codex CLI non-interactively and print the result.

    Pure local invocation — no orchestrator IPC. Codex uses its own
    auth (ChatGPT login or `OPENAI_API_KEY`); cockpit doesn't touch
    those credentials. Works whether or not the cockpit is running.

    `cwd` defaults to the calling pane's working directory so a
    `takkub codex "review this"` inside a project pane naturally
    runs Codex against that project's files.
    """
    from .codex_helper import codex_exec

    ok, output = codex_exec(
        args.prompt,
        cwd=args.cwd,
        timeout=args.timeout,
        model=args.model,
    )
    if output:
        print(output)
    return {
        "ok": ok,
        "msg": "codex done" if ok else "codex failed",
    }


def cmd_gemini(args: argparse.Namespace) -> dict:
    """Fire Antigravity CLI (`agy`) non-interactively and print the result.

    Mirror of `cmd_codex`. Pure local invocation — no orchestrator IPC.
    Backs the `gemini` role: Google retired the standalone Gemini CLI on
    2026-06-18, so this runs `agy -p`. Antigravity uses its own auth
    (Google Sign-In on first run or `ANTIGRAVITY_API_KEY` env); cockpit
    doesn't touch those credentials. Works whether or not the cockpit is
    running.

    `cwd` defaults to the calling pane's working directory so a
    `takkub gemini "review this"` inside a project pane naturally
    runs Antigravity against that project's files.
    """
    from .gemini_helper import gemini_exec

    ok, output = gemini_exec(
        args.prompt,
        cwd=args.cwd,
        timeout=args.timeout,
        model=args.model,
    )
    if output:
        print(output)
    return {
        "ok": ok,
        "msg": "gemini done" if ok else "gemini failed",
    }


def _ensure_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 so Thai (and other non-ASCII) text
    prints correctly on Windows consoles instead of showing ???? (mojibake).
    Safe to call unconditionally — silently skips on streams that don't support
    reconfigure (e.g. already-closed or binary-mode streams)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def _utf8_print(text: str) -> None:
    """Print *text* to stdout, forcing UTF-8 on Windows to avoid charmap errors."""
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        print(text)


def cmd_doctor(args: argparse.Namespace) -> dict:
    from .doctor import Status, format_report, run_all_checks, run_auto_fixes

    findings = run_all_checks()

    if args.fix:
        run_auto_fixes(findings, install_providers=args.install_providers)
        findings = run_all_checks()

    if getattr(args, "live", False):
        from .doctor import (
            check_performance_live,
            check_port_identity_live,
            check_remote_mirror_live,
            check_spawn_queue_live,
        )

        live_resp: dict | None = None
        mirror_resp: dict | None = None
        performance_resp: dict | None = None
        identity_resp: dict | None = None
        if read_port() is not None:
            try:
                live_resp = _request({"cmd": "spawn-queue-status"})
            except Exception as e:
                live_resp = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
            try:
                mirror_resp = _request({"cmd": "remote-mirror-status"})
            except Exception as e:
                mirror_resp = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
            try:
                performance_resp = _request({"cmd": "performance-status"})
            except Exception as e:
                performance_resp = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
            try:
                identity_resp = _request({"cmd": "instance-identity"})
            except Exception as e:
                identity_resp = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
        findings += check_spawn_queue_live(live_resp)
        findings += check_remote_mirror_live(mirror_resp)
        findings += check_performance_live(performance_resp)
        findings += check_port_identity_live(identity_resp)

    ram_resp: dict | None = None
    if getattr(args, "ram", False):
        if read_port() is not None:
            try:
                ram_resp = _request({"cmd": "ram-status"})
            except Exception as e:
                ram_resp = {"ok": False, "msg": f"{type(e).__name__}: {e}"}

    if getattr(args, "core_version", False):
        from .doctor import check_core_version_compat

        findings += check_core_version_compat()

    if getattr(args, "storage_layout", False):
        from .doctor import check_storage_layout_state

        findings += check_storage_layout_state()

    pane_role = getattr(args, "pane", None)
    if pane_role:
        from .config import active_project
        from .doctor import check_pane_mcp_handshake

        pane_project = getattr(args, "project", None) or _from_project() or active_project()[0]
        findings += check_pane_mcp_handshake(pane_role, pane_project)

    if args.json:
        import json as _json

        findings_payload = [
            {
                "category": f.category,
                "name": f.name,
                "status": f.status.value,
                "detail": f.detail,
                "fix_hint": f.fix_hint,
            }
            for f in findings
        ]
        if getattr(args, "ram", False):
            # #364 lever 6: `--ram --json` is meant as a before/after baseline
            # for the other RAM-diet levers, so it keeps the raw byte-level
            # `ram_resp` dict (not flattened into Finding text) — this is the
            # one case where `doctor --json`'s shape grows from a bare list to
            # {findings, ram}; plain `--json` (no `--ram`) is unchanged.
            _utf8_print(_json.dumps({"findings": findings_payload, "ram": ram_resp}, indent=2))
        else:
            _utf8_print(_json.dumps(findings_payload, indent=2))
    else:
        _utf8_print(format_report(findings))
        if getattr(args, "ram", False):
            from .doctor import format_ram_report

            _utf8_print("")
            _utf8_print(format_ram_report(ram_resp))

    n_fail = sum(1 for f in findings if f.status == Status.FAIL)
    ok = n_fail == 0
    return {"ok": ok, "msg": f"{n_fail} fail(s)" if not ok else "all checks passed"}


def cmd_qa_gate(args: argparse.Namespace) -> dict:
    """`takkub qa-gate` (#325) — pure-local like `doctor`/`migrate`: no
    orchestrator socket needed, works with the cockpit closed. Heavy lifting
    lives in `.qa_gate` so it stays testable without going through argparse."""
    from .qa_gate import render_table, run_gate

    report = run_gate(targeted=args.targeted, v2_flags=args.v2_flags)
    _utf8_print(render_table(report))
    if report.ok:
        return {"ok": True, "msg": "qa-gate: all steps passed", "exit_code": 0}
    return {"ok": False, "msg": "qa-gate: failed — see table above", "exit_code": report.exit_code}


def cmd_migrate(args: argparse.Namespace) -> dict:
    """`takkub migrate {inspect,plan,dry-run,apply,validate,rollback}` — Core
    V2 storage migration (#309 Phase 4, docs/v2/V2_IMPLEMENTATION_PLAN.md
    §5). Pure-local, same rationale as `takkub worktree`/`takkub doctor`: no
    orchestrator socket needed, works with the cockpit closed."""
    from .core.migration.engine import MigrationEngine

    engine = MigrationEngine()
    dispatch = {
        "inspect": engine.inspect,
        "plan": engine.plan,
        "dry-run": engine.dry_run,
        "apply": engine.apply,
        "validate": engine.validate,
        "rollback": engine.rollback,
    }
    reports = dispatch[args.migrate_cmd]()

    if args.json:
        import json as _json

        _utf8_print(
            _json.dumps(
                [
                    {
                        "step_id": r.step_id,
                        "stage": r.stage,
                        "ok": r.ok,
                        "summary": r.summary,
                        "detail": r.detail,
                    }
                    for r in reports
                ],
                indent=2,
            )
        )
    else:
        for r in reports:
            icon = "✓" if r.ok else "✗"
            _utf8_print(f"  {icon} [{r.step_id}] {r.summary}")

    ok = all(r.ok for r in reports)
    return {"ok": ok, "msg": "ok" if ok else "one or more steps failed"}


def cmd_provider(args: argparse.Namespace) -> dict:
    """`takkub provider list|install|model` — provider management surface.

    Pure-local (no orchestrator socket): discovery, config, and installs run
    straight from this process, so it works with the cockpit closed — same
    rationale as `takkub worktree`. The cockpit picks changes up on the next
    spawn/chip refresh without a restart.
    """
    from .provider_install import _discover, install_provider, installable_providers
    from .provider_models import clear_model, model_for, set_model
    from .provider_spec import PROVIDER_REGISTRY

    if args.provider_cmd == "list":
        lines = []
        for name, spec in PROVIDER_REGISTRY.items():
            if name == "claude":
                continue
            path = _discover(spec)
            if path:
                state = f"installed  {path}"
            elif spec.install_command:
                state = f"not installed  (takkub provider install {name})"
            else:
                state = "not installed  (manual — see takkub doctor)"
            configured_model = model_for(name)
            if configured_model:
                state += f" · model: {configured_model}"
            lines.append(f"  {name:<10} {state}")
        _utf8_print("\n".join(lines) or "  (no providers registered)")
        return {"ok": True, "msg": f"{len(lines)} provider(s)"}

    if args.provider_cmd == "model":
        name = args.name
        if name not in PROVIDER_REGISTRY:
            msg = f"unknown provider: {name!r}"
            _utf8_print(f"✗ {msg}")
            return {"ok": False, "msg": msg}
        if args.clear:
            clear_model(name)
            msg = f"{name} model cleared (provider default)"
            _utf8_print(f"✓ {msg}")
            return {"ok": True, "msg": msg}
        if args.model is not None:
            set_model(name, args.model)
            configured_model = model_for(name)
            if configured_model:
                msg = f"{name} model: {configured_model}"
            else:
                msg = f"{name} model cleared (provider default)"
            _utf8_print(f"✓ {msg}")
            return {"ok": True, "msg": msg}
        configured_model = model_for(name)
        msg = f"{name} model: {configured_model or '(provider default)'}"
        _utf8_print(msg)
        return {"ok": True, "msg": msg}

    # install
    name = args.name
    ok, msg = install_provider(name)
    _utf8_print(("✓ " if ok else "✗ ") + msg)
    if not ok and name not in installable_providers() and name in PROVIDER_REGISTRY:
        # manual-only provider — the message already carries the instructions
        pass
    return {"ok": ok, "msg": msg}


def cmd_release(args: argparse.Namespace) -> dict:
    """Bump version + roll CHANGELOG's [vNEXT] + git commit & tag."""
    from .config import REPO_ROOT, is_installed_package
    from .release import release

    if is_installed_package():
        return {
            "ok": False,
            "msg": "takkub release is only available in dev checkouts (installed builds "
            "update via `npm update -g agent-takkub`, not this command)",
        }

    do_github_release = getattr(args, "github_release", True)
    do_build_wheel = getattr(args, "build_wheel", True)
    res = release(
        REPO_ROOT,
        part=args.part,
        explicit_version=args.version,
        do_commit=not args.no_commit,
        do_tag=not args.no_tag,
        dry_run=args.dry_run,
        allow_empty=args.allow_empty,
        do_github_release=do_github_release,
        do_build_wheel=do_build_wheel,
    )
    if res["dry_run"]:
        _utf8_print(
            f"[dry-run] {res['current']} → {res['new_version']} · tag {res['tag']} · {res['date']}"
        )
        step6 = (
            "push + GitHub Release"
            if do_github_release
            else "no GitHub Release (--no-github-release)"
        )
        _utf8_print(f"  (no files or git touched) · would: {step6}")
        return {"ok": True, "msg": "dry-run"}

    bits = [f"{res['current']} → {res['new_version']}"]
    if res["committed"]:
        bits.append("committed")
    if res["tagged"]:
        bits.append(f"tagged {res['tag']}")
    if res.get("wheel_built"):
        bits.append(f"built {os.path.basename(res['wheel_path'])}")
    _utf8_print("  " + " · ".join(bits))
    if res.get("github_released"):
        _utf8_print(f"  GitHub Release:  {res['github_url']}")
    elif do_github_release and res.get("github_error"):
        # Publish failed but the local release is intact — tell the user how to finish.
        _utf8_print(f"  ⚠ GitHub Release skipped: {res['github_error']}")
        _utf8_print(
            "  finish manually:  git push --follow-tags  &&  gh release create "
            f"{res['tag']} --verify-tag --title {res['tag']} --notes-file <section>"
        )
    else:
        _utf8_print("  push when ready:  git push --follow-tags")
    return {"ok": True, "msg": f"released {res['tag']}"}


def cmd_search(args: argparse.Namespace) -> dict:
    """Ranked (BM25) search across `~/.claude/projects/<*>/<uuid>.jsonl` +
    role-memory archives — `--grep` forces the old plain-substring path.
    Does NOT go through the orchestrator's TCP socket — search is a
    passive query and works whether the cockpit is running or not."""
    from datetime import datetime, timedelta

    since: datetime | None = None
    if getattr(args, "days", None):
        since = datetime.now() - timedelta(days=args.days)
    # Default: today only. Keeps a "what did I touch this morning"
    # search fast on a vault with months of jsonls.
    if since is None and not getattr(args, "all", False):
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    if getattr(args, "grep", False):
        from .chatlog_scanner import search_sessions

        hits = search_sessions(
            args.query, project_filter=args.project, since=since, limit=args.limit
        )
        used_bm25 = False
    else:
        from .bm25_search import search as bm25_search

        hits, used_bm25 = bm25_search(
            args.query, project_filter=args.project, since=since, limit=args.limit
        )
    if not hits:
        return {"ok": True, "msg": f"no matches for {args.query!r}"}
    for h in hits:
        ts = h.get("timestamp") or ""
        # Trim "T" + microseconds for terminal display
        ts_short = ts.replace("T", " ")[:19] if ts else "(no ts)"
        proj = h.get("project") or "?"
        role = h.get("role") or "?"
        line = h.get("line")
        snippet = (f"L{line}: " if line else "") + (h.get("snippet") or "")
        score_prefix = f"[{h['score']:.2f}] " if used_bm25 and "score" in h else ""
        # Project folder names are encoded — show the recognisable
        # tail so the line stays readable.
        proj_tail = proj.split("-")[-1] if "-" in proj else proj
        print(f"  {score_prefix}{proj_tail:18s} {ts_short}  {role:9s}  {snippet}")
    mode = "bm25" if used_bm25 else "grep"
    return {
        "ok": True,
        "msg": f"{len(hits)} match(es) ({mode})"
        + (" (limit reached)" if len(hits) == args.limit else ""),
    }


def _read_hook_stdin() -> dict:
    """Best-effort parse of the Claude Code hook JSON on stdin. Never raises —
    an empty/malformed payload just means every gate below treats it as an
    unrecognised event and allows the stop (fail open)."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _hook_request(payload: dict, timeout: float = 1.5) -> dict | None:
    """Short-timeout, fail-silent request for the hook path only. Every other
    CLI command uses `_request()` (15 s timeout, raises on no cockpit); a hook
    runs synchronously inside the pane's Stop/Notification event, so it must
    return fast and NEVER raise — any failure (cockpit not running, socket
    error, malformed reply) just returns None and the caller allows the stop."""
    try:
        token = os.environ.get("TAKKUB_LEAD_TOKEN") or os.environ.get("TAKKUB_PANE_TOKEN")
        if token:
            payload["auth"] = token
        port = read_port()
        if port is None:
            return None
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        try:
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            s.settimeout(timeout)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                return None
            return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        finally:
            s.close()
    except Exception:
        return None


def cmd_hook(_: argparse.Namespace) -> dict:
    """Internal command wired as the Stop/Notification `command` for every
    cockpit-spawned claude pane (see hook_wiring.py). Reports the event to the
    orchestrator as an authoritative turn-end/idle signal and, for a teammate
    pane with an outstanding assigned task, may emit a Stop-hook block decision
    nudging it to run `takkub done`.

    Never raises and always exits 0 — a hook failure must never break the
    pane's turn (guard required by the feature spec)."""
    try:
        payload = _read_hook_stdin()
        role = _from_role()
        if not role:
            return {"ok": True, "msg": ""}  # manual / non-cockpit invocation
        event = payload.get("hook_event_name", "")
        # stop_hook_active guards Claude Code recursively re-entering THIS
        # Stop event — skip entirely rather than risk a block loop.
        if payload.get("stop_hook_active"):
            return {"ok": True, "msg": ""}
        resp = _hook_request(
            _with_project(
                {
                    "cmd": "hook",
                    "event": event,
                    "notification_type": payload.get("notification_type", ""),
                    "from": role,
                }
            )
        )
        if resp and resp.get("block"):
            reason = resp.get("msg") or "รายงานผลด้วย takkub done ก่อนจบ"
            print(
                json.dumps(
                    {
                        "decision": "block",
                        "hookSpecificOutput": {
                            "hookEventName": "Stop",
                            "additionalContext": reason,
                        },
                    }
                )
            )
        return {"ok": True, "msg": ""}
    except Exception:
        return {"ok": True, "msg": ""}


def cmd_session_report(_: argparse.Namespace) -> dict:
    """Internal command wired as the `SessionStart` hook `command` for every
    cockpit-spawned claude pane (see hook_wiring.py). Fires on every session
    start — initial spawn, `/resume`, `/clear`, and post-compact — carrying
    the CURRENT `session_id` in the hook's stdin JSON. Reports it to the
    orchestrator so `PaneState.session_uuid` never drifts from the transcript
    claude is actually writing to (the bug: a manual `/resume` inside a pane
    switches claude to a different uuid that the orchestrator, which only
    stamped `session_uuid` once at spawn time, never learns about).

    Never raises and always exits 0 — same fail-open contract as `cmd_hook`;
    a hook failure (cockpit not running, missing env, malformed JSON) must
    never break the pane's session start."""
    try:
        payload = _read_hook_stdin()
        role = _from_role()
        if not role:
            return {"ok": True, "msg": ""}  # manual / non-cockpit invocation
        session_id = payload.get("session_id") or ""
        if not session_id:
            return {"ok": True, "msg": ""}  # malformed payload — nothing to report
        _hook_request(
            _with_project(
                {
                    "cmd": "session-report",
                    "session_id": session_id,
                    "source": payload.get("source", ""),
                    "cwd": payload.get("cwd", ""),
                    "from": role,
                }
            )
        )
        return {"ok": True, "msg": ""}
    except Exception:
        return {"ok": True, "msg": ""}


def cmd_guard(_: argparse.Namespace) -> dict:
    """Internal command wired as the `PreToolUse`/`Bash` hook for every
    cockpit-spawned claude pane (see hook_wiring.py). Blocks a teammate pane
    from routing around the MCP tool policy via the shell — installing or
    driving a browser it was never granted, or sweeping the whole disk.

    Denies by exiting 2 with the reason on stderr: that is the one PreToolUse
    blocking contract every Claude Code build honours, so a version that does
    not understand a JSON `permissionDecision` payload still blocks instead of
    silently failing open.

    Never raises. Any unexpected failure allows the command (exit 0) — the
    guard must never be able to wedge a pane's shell. Goes through
    `core.capabilities.permission_engine.PermissionEngine.evaluate_shell_command`
    (#309 Wave C) rather than calling `pane_guard.classify` directly, so a
    denial is also audited; the outer `except Exception` below is what
    keeps the fail-open contract, not anything inside that engine. Rules
    and rationale live in `pane_guard.py`."""
    try:
        from .core.capabilities.permission_engine import PermissionEngine

        payload = _read_hook_stdin()
        role = _from_role()
        if not role:
            return {"ok": True, "msg": ""}  # manual / non-cockpit invocation
        tool_input = payload.get("tool_input")
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

        def _mb_fallback_check() -> bool:
            # #304 point 3: lazy on purpose — pane_guard only calls this in
            # its one mb-shard-deny branch, so a plain Bash call from every
            # other pane never touches disk here.
            from . import mcp_fallback

            granted = mcp_fallback.is_granted(role)
            if granted:
                from .orchestrator_text import _log_event

                _log_event("mcp_fallback_used", role=role, project=_from_project())
            return granted

        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        verdict = PermissionEngine().evaluate_shell_command(
            command, role, mb_fallback_check=_mb_fallback_check, cwd=cwd
        )
        if verdict.allowed:
            return {"ok": True, "msg": ""}
        print(f"[takkub guard: {verdict.rule}] {verdict.reason}", file=sys.stderr)
        return {"ok": True, "msg": "", "exit_code": 2}
    except Exception:
        return {"ok": True, "msg": ""}


def cmd_mcp_fallback(args: argparse.Namespace) -> dict:
    """(agent, browser-role shard only) request or check the single,
    time-boxed `mb` escape hatch for when Playwright MCP genuinely never
    connected (#146/#304).

    Local-only, no orchestrator IPC — same as `takkub doctor`/`takkub
    worktree`. Only call `request` AFTER confirming via ToolSearch that the
    browser MCP tools are unavailable, never speculatively: this hands out
    ONE grant at a time (`mcp_fallback.py`'s module docstring explains why —
    `mb` shares a single CDP endpoint machine-wide, #92), so a request made
    "just in case" can block a shard that genuinely needs it.
    """
    from . import mcp_fallback

    role = _from_role()
    if not role:
        return {"ok": False, "msg": "TAKKUB_ROLE not set — run this from inside a cockpit pane"}

    if args.mcp_fallback_cmd == "status":
        info = mcp_fallback.status()
        if info is None:
            return {"ok": True, "msg": "no active mb fallback grant"}
        remaining = int(info.get("expires_at", 0) - time.time())
        return {"ok": True, "msg": f"held by {info.get('holder')} for {max(0, remaining)}s more"}

    project = _from_project() or ""
    reason = getattr(args, "reason", "") or ""
    grant = mcp_fallback.request(role, project, reason=reason)

    from .orchestrator_text import _log_event

    _log_event(
        "mcp_fallback_granted" if grant.granted else "mcp_fallback_denied",
        role=role,
        project=project,
        reason=reason,
        holder=grant.holder,
    )
    if grant.granted:
        remaining = int((grant.expires_at or 0) - time.time())
        return {
            "ok": True,
            "msg": (
                f"granted: mb ใช้ได้ชั่วคราวอีก {remaining}s — ใช้เท่าที่จำเป็นแล้วเลิกใช้ "
                "(ยังแชร์ CDP 9222 กับทุก pane บนเครื่อง #92, อย่าเปิดค้าง)"
            ),
        }
    return {"ok": False, "msg": grant.reason}


def cmd_services(args: argparse.Namespace) -> dict:
    """Local docker compose operations — no orchestrator IPC.

    Works whether the cockpit is running or not. Resolves the compose path
    from --cwd; if omitted, scans the active project's configured paths for
    a compose file.
    """
    from pathlib import Path

    from .config import active_project
    from .services import detect_compose, down, logs, ps, up

    sub = args.services_command

    # Resolve the working directory that contains the compose file.
    cwd: Path | None = Path(args.cwd) if getattr(args, "cwd", None) else None
    if cwd is None:
        _, proj = active_project()
        paths = proj.get("paths", {})
        for _p in paths.values():
            candidate = Path(_p)
            if detect_compose(candidate) is not None:
                cwd = candidate
                break
        if cwd is None and paths:
            cwd = Path(next(iter(paths.values())))
    if cwd is None:
        cwd = Path(".")

    project_name = getattr(args, "project", None) or str(cwd.resolve().name)

    if sub == "start":
        ok, msg = up(project_name, cwd)
        return {"ok": ok, "msg": msg}

    if sub == "stop":
        ok, msg = down(project_name, cwd)
        return {"ok": ok, "msg": msg}

    if sub == "ps":
        services = ps(project_name, cwd)
        if not services:
            print("  (no services running or compose file not found)")
            return {"ok": True, "msg": "0 services"}
        for svc in services:
            health_str = f"  [{svc.health}]" if svc.health else ""
            print(f"  {svc.name:<30} {svc.state}{health_str}")
        return {"ok": True, "msg": f"{len(services)} service(s)"}

    if sub == "logs":
        tail = getattr(args, "tail", 50) or 50
        ok, output = logs(project_name, cwd, tail=tail)
        if output:
            print(output)
        return {"ok": ok, "msg": "logs fetched" if ok else output}

    return {"ok": False, "msg": f"unknown services subcommand: {sub}"}


# Mutating pane-tools-policy subcommands — everything except `list` changes
# ~/.takkub/pane-tools.json (or the master shared-mcp.json for add/remove) and
# is therefore lead-only, same rationale as LEAD_ONLY_COMMANDS above. `list`
# stays open so teammates can see what tools they currently have.
_MUTATING_MCP_SUBCOMMANDS = frozenset({"allow", "deny", "reset", "add", "remove"})
_MUTATING_PLUGIN_SUBCOMMANDS = frozenset({"allow", "deny", "reset"})


def _require_lead_for_pane_tools(action: str) -> str | None:
    role = _from_role()
    if role is None:
        return None
    if role.lower() != "lead":
        return (
            f"only lead can run 'takkub {action}'. you are '{role}'.\n"
            f"       'takkub mcp list' / 'takkub plugins list' stay read-only for everyone; "
            f"ask lead to change the policy."
        )
    return None


def _pane_tools_table(kind: str, role_filter: str | None) -> dict:
    """Print a role → items table for `kind` ("mcps" or "plugins"), marking
    roles that have an explicit override in pane-tools.json with `*`."""
    from . import pane_tools_policy as ptp
    from . import shared_dev_tools as sdt

    known = ptp.known_roles()
    if role_filter is not None and role_filter not in known:
        return {
            "ok": False,
            "msg": f"unknown role {role_filter!r}. known roles: {', '.join(sorted(known))}",
        }

    policy = ptp.load_policy()
    roles = [role_filter] if role_filter else sorted(known)

    mcp_defaults: dict[str, frozenset[str]] = getattr(sdt, "_ROLE_MCP_POLICY", {})
    try:
        from .lead_context import _ROLE_PLUGIN_POLICY, _TEAMMATE_PLUGINS
    except Exception:  # pragma: no cover — CLI must degrade, not crash
        _ROLE_PLUGIN_POLICY, _TEAMMATE_PLUGINS = {}, frozenset()
    rows: list[tuple[str, list[str] | None, bool]] = []
    for role in roles:
        overridden = role in policy
        if kind == "mcps":
            # None = no policy anywhere → the role receives the full master
            # config (passthrough) — display that honestly, not as "(none)".
            items = ptp.effective_mcps(role, mcp_defaults.get(role))
        else:
            items = ptp.effective_plugins(role, _ROLE_PLUGIN_POLICY.get(role, _TEAMMATE_PLUGINS))
        rows.append((role, sorted(items) if items is not None else None, overridden))

    label = "mcps" if kind == "mcps" else "plugins"
    name_width = max([len("role")] + [len(r) + (1 if o else 0) for r, _, o in rows])
    _utf8_print(f"{'role':<{name_width}}  {label}")
    for role, items, overridden in rows:
        name = role + ("*" if overridden else "")
        if items is None:
            shown = "(master passthrough — ทุกตัว)"
        else:
            shown = ", ".join(items) if items else "(none)"
        _utf8_print(f"{name:<{name_width}}  {shown}")
    return {"ok": True, "msg": ""}


def cmd_mcp(args: argparse.Namespace) -> dict:
    import shlex

    from . import pane_tools_policy as ptp
    from . import shared_dev_tools as sdt

    sub = args.mcp_command
    if sub in _MUTATING_MCP_SUBCOMMANDS:
        gate_err = _require_lead_for_pane_tools(f"mcp {sub}")
        if gate_err:
            return {"ok": False, "msg": gate_err}

    if sub == "list":
        return _pane_tools_table("mcps", args.role)

    if sub in ("allow", "deny"):
        if args.role not in ptp.known_roles():
            return {"ok": False, "msg": f"unknown role {args.role!r}"}
        fn = ptp.allow_item if sub == "allow" else ptp.deny_item
        if not fn(args.role, "mcps", args.name):
            return {
                "ok": False,
                "msg": f"could not {sub} MCP {args.name!r} for {args.role} — invalid name?",
            }
        sdt.regen_role_variants()
        _pane_tools_table("mcps", None)
        return {"ok": True, "msg": f"{sub}ed {args.name!r} for {args.role}"}

    if sub == "reset":
        if args.role is not None and args.role not in ptp.known_roles():
            return {"ok": False, "msg": f"unknown role {args.role!r}"}
        roles = [args.role] if args.role else sorted(ptp.load_policy().keys())
        if not roles:
            _utf8_print("nothing to reset — no role overrides set")
            return {"ok": True, "msg": ""}
        for role in roles:
            ptp.reset_role(role)
        sdt.regen_role_variants()
        _pane_tools_table("mcps", None)
        return {"ok": True, "msg": f"reset {', '.join(roles)}"}

    if sub == "add":
        cfg = {"type": "stdio", "command": args.command, "args": shlex.split(args.args or "")}
        if not sdt.add_mcp_server(args.name, cfg, force=args.force):
            return {
                "ok": False,
                "msg": (
                    f"could not add MCP {args.name!r} — either the name is invalid/reserved "
                    f"(e.g. a browser MCP name), or the config looks like it carries a secret "
                    f"(token/key/password in command or args). If that's intentional, retry "
                    f"with --force."
                ),
            }
        sdt.regen_role_variants()
        _pane_tools_table("mcps", None)
        return {"ok": True, "msg": f"added MCP {args.name!r}"}

    if sub == "remove":
        if not sdt.remove_mcp_server(args.name):
            return {
                "ok": False,
                "msg": f"could not remove MCP {args.name!r} — not found, or it's a protected browser MCP",
            }
        sdt.regen_role_variants()
        _pane_tools_table("mcps", None)
        return {"ok": True, "msg": f"removed MCP {args.name!r}"}

    return {"ok": False, "msg": f"unknown mcp subcommand: {sub}"}


def cmd_plugins(args: argparse.Namespace) -> dict:
    from . import pane_tools_policy as ptp
    from . import shared_dev_tools as sdt

    sub = args.plugins_command
    if sub in _MUTATING_PLUGIN_SUBCOMMANDS:
        gate_err = _require_lead_for_pane_tools(f"plugins {sub}")
        if gate_err:
            return {"ok": False, "msg": gate_err}

    if sub == "list":
        return _pane_tools_table("plugins", args.role)

    if sub in ("allow", "deny"):
        if args.role not in ptp.known_roles():
            return {"ok": False, "msg": f"unknown role {args.role!r}"}
        fn = ptp.allow_item if sub == "allow" else ptp.deny_item
        if not fn(args.role, "plugins", args.name):
            return {
                "ok": False,
                "msg": f"could not {sub} plugin {args.name!r} for {args.role} — invalid name?",
            }
        sdt.regen_role_variants()
        _pane_tools_table("plugins", None)
        return {"ok": True, "msg": f"{sub}ed {args.name!r} for {args.role}"}

    if sub == "reset":
        if args.role is not None and args.role not in ptp.known_roles():
            return {"ok": False, "msg": f"unknown role {args.role!r}"}
        roles = [args.role] if args.role else sorted(ptp.load_policy().keys())
        if not roles:
            _utf8_print("nothing to reset — no role overrides set")
            return {"ok": True, "msg": ""}
        for role in roles:
            ptp.reset_role(role)
        sdt.regen_role_variants()
        _pane_tools_table("plugins", None)
        return {"ok": True, "msg": f"reset {', '.join(roles)}"}

    return {"ok": False, "msg": f"unknown plugins subcommand: {sub}"}


def cmd_provision(args: argparse.Namespace) -> dict:
    """Install the recommended plugin set + browser MCPs — detect-first, idempotent.

    Meant to run once after ``npm install -g agent-takkub`` (or any fresh setup):
    it detects what's already present and fills ONLY the gaps, so a machine that
    already has everything is a clean no-op. Uses the ``claude plugin`` CLI, so it
    touches ``~/.claude`` (shared) — an intentional, conscious step, never
    automatic. Requires the ``claude`` CLI to be installed + logged in.
    """
    from . import plugin_installer, shared_dev_tools

    _utf8_print("takkub provision — detect-first, non-clobbering\n")

    have = plugin_installer.installed_on_disk()
    missing = plugin_installer.missing_plugins(have)
    _utf8_print(
        f"plugins: {len(have)} present"
        + (f", installing {len(missing)} missing…" if missing else " — all recommended present ✓")
    )
    installed_now: list[dict] = []
    if missing:
        plugin_installer.ensure_marketplaces(missing)
        for p in missing:
            ok, msg = plugin_installer.install_plugin(p, ensure_marketplace=False)
            _utf8_print(f"   {'✓' if ok else '✗'} {p.key}@{p.marketplace} — {msg}")
            installed_now.append({"plugin": p.key, "ok": ok, "msg": msg})

    ok_mcp, msg_mcp = shared_dev_tools.ensure_browser_mcps()
    _utf8_print(f"browser MCPs: {msg_mcp}")

    failed = [x for x in installed_now if not x["ok"]]
    summary = "provisioned" if not failed else f"provisioned ({len(failed)} plugin failure(s))"
    _utf8_print(f"\n{summary}. next: `claude login` (ถ้ายัง) → `agent-takkub`")
    return {
        "ok": not failed,
        "msg": summary,
        "plugins_installed": installed_now,
        "mcps": {"ok": ok_mcp, "msg": msg_mcp},
    }


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    p = argparse.ArgumentParser(prog="takkub", description="agent-takkub cockpit CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("spawn", help="open a pane for a role")
    sp.add_argument("--role", required=True)
    sp.add_argument("--cwd", default=None)
    sp.set_defaults(func=cmd_spawn)

    sa = sub.add_parser("assign", help="spawn (if needed) and send a task")
    sa.add_argument("--role", required=True)
    sa.add_argument("--cwd", default=None)
    sa.add_argument(
        "--mode",
        choices=("pane", "subagent"),
        default=None,
        help="execution mode: pane (existing visible cockpit pane) or subagent "
        "(native same-provider child in the Lead process; no pane/model diversity). "
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
        "high; claude alone also takes xhigh/max) — opencode/kimi/cursor have "
        "no CLI knob yet (GAP tracked in #103) and silently ignore this flag "
        "rather than erroring. Only takes effect when spawning a new pane; an "
        "already-running pane keeps its current effort",
    )
    sa.add_argument("task", help="task content (positional)")
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
        help="fan-out to N parallel shard panes (<role>#1 … <role>#N); "
        "each pane gets TAKKUB_SHARD / TAKKUB_SHARD_TOTAL env vars",
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
        "--feature",
        default="",
        help="feature/work-item label for the Task Ledger (A7) — groups this "
        "assign's row under '### N. <feature>' in runtime/tasks/<project>/"
        "INDEX.md. Omit for 'งานทั่วไป' (general work).",
    )
    sa.set_defaults(func=cmd_assign)

    srs = sub.add_parser(
        "restart",
        help="restart the whole cockpit (persist state → relaunch; panes respawn) — lead/terminal only",
    )
    srs.set_defaults(func=cmd_restart)

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
    swm.add_argument("--role", default=None, help="merge the NEWEST wt/<role>-* branch")
    swm.add_argument("--branch", default=None, help="merge this exact wt/* branch")
    swm.add_argument("--keep", action="store_true", help="merge but keep the worktree")
    swm.add_argument("--cwd", default=None, help="project dir (default: current dir)")
    swc = swt_sub.add_parser(
        "clean",
        help="remove leftover wt/* worktrees (safe ones only; --force drops dirty/unmerged too) "
        "and report on-disk dirs git no longer knows about at all (#355)",
    )
    swc.add_argument(
        "--force",
        action="store_true",
        help="also remove dirty / unmerged worktrees (their work is LOST)",
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
    swt.set_defaults(func=cmd_worktree)

    sdisk = sub.add_parser(
        "disk",
        help="report DATA_HOME disk usage by category (safe/review/never to delete)",
    )
    sdisk.add_argument("--json", action="store_true", help="machine-readable output")
    sdisk.set_defaults(func=cmd_disk)

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
    sprune.set_defaults(func=cmd_prune)

    ss = sub.add_parser("send", help="send a message to a running pane")
    ss.add_argument("--to", required=True)
    ss.add_argument("msg", help="message (positional)")
    ss.set_defaults(func=cmd_send)

    sc = sub.add_parser("close", help="close a running pane")
    sc.add_argument("--role", required=True)
    sc.set_defaults(func=cmd_close)

    sca = sub.add_parser("close-all", help="close every teammate (keeps Lead)")
    sca.set_defaults(func=cmd_close_all)

    sd = sub.add_parser("done", help="(agent) report done to Lead")
    sd.add_argument("note", nargs="?", default="")
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
    sd.set_defaults(func=cmd_done)

    ssd = sub.add_parser(
        "subagent-done",
        help="complete a pending --mode subagent assignment through the done pipeline (Lead/native child)",
    )
    ssd.add_argument("--role", required=True)
    ssd.add_argument("note", nargs="?", default="")
    ssd.add_argument("--fail", action="store_true")
    ssd.set_defaults(func=cmd_subagent_done)

    sma = sub.add_parser(
        "ma",
        help="(operator) maintenance sweep: issues → PRs → runtime log → repo → แผนทำต่อ",
        description=(
            "เดิน checklist บำรุงรักษา cockpit ทีละข้อ: issue ที่ค้าง, PR + สถานะ CI, "
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
    sma.set_defaults(func=cmd_ma)

    spg = sub.add_parser(
        "progress",
        help="(agent) report a status update to Lead without ending the task (#234)",
    )
    spg.add_argument("note")
    spg.set_defaults(func=cmd_progress)

    ses = sub.add_parser(
        "end-session",
        help="(lead) write session summary to runtime/sessions and vault mirror",
    )
    ses.add_argument("--note", default="", help="summary note (default: 'session ended')")
    ses.set_defaults(func=cmd_end_session)

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
    sgoal.set_defaults(func=cmd_goal)

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
    sh.set_defaults(func=cmd_harvest)

    sl = sub.add_parser("list", help="show pane status")
    sl.set_defaults(func=cmd_list)

    smsg = sub.add_parser(
        "messages",
        help="(lead) read the `takkub send` audit log for a role — was it actually received? (#277)",
    )
    smsg.add_argument("--role", required=True, help="recipient role to look up")
    smsg.add_argument("--limit", type=int, default=20, help="how many recent messages (default 20)")
    smsg.set_defaults(func=cmd_messages)

    st = sub.add_parser(
        "task",
        help="read back a role's last assigned task (issue #1 file-based task handoff)",
    )
    st_sub = st.add_subparsers(dest="t_cmd", required=True)
    sts = st_sub.add_parser("show", help="print the full text of a role's last assigned task")
    sts.add_argument("--role", required=True, help="role name to look up")
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
    st.set_defaults(func=cmd_task)

    # Internal — wired as the Stop/Notification hook `command` for every
    # cockpit-spawned claude pane (see hook_wiring.py). Not a user-facing
    # command, so it's hidden from --help.
    shk = sub.add_parser("_hook", help=argparse.SUPPRESS)
    shk.set_defaults(func=cmd_hook)

    # Internal — wired as the SessionStart hook `command` for every
    # cockpit-spawned claude pane (see hook_wiring.py). Not a user-facing
    # command, so it's hidden from --help. Not lead-only / teammate-only:
    # every pane (Lead + every teammate role) fires SessionStart and must
    # be able to report it.
    ssr = sub.add_parser("session-report", help=argparse.SUPPRESS)
    ssr.set_defaults(func=cmd_session_report)

    # Internal — wired as the PreToolUse/Bash hook `command` for every
    # cockpit-spawned claude pane (see hook_wiring.py). Blocks a teammate from
    # shelling around the MCP tool policy (`npx playwright`) or sweeping the
    # whole disk. Hidden from --help; fires on every Bash call.
    sgd = sub.add_parser("_guard", help=argparse.SUPPRESS)
    sgd.set_defaults(func=cmd_guard)

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
    smf_req.set_defaults(func=cmd_mcp_fallback)
    smf_status = smf_sub.add_parser(
        "status", help="show who currently holds the fallback grant, if anyone"
    )
    smf_status.set_defaults(func=cmd_mcp_fallback)

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
    sst.set_defaults(func=cmd_status)

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
    sib.set_defaults(func=cmd_inbox)

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
        help=f"max seconds to block (default {int(_WAIT_DEFAULT_TIMEOUT_S)}, "
        f"capped at {int(_WAIT_MAX_TIMEOUT_S)})",
    )
    swt.add_argument(
        "--cancel",
        action="store_true",
        help="release the active wait registration for this project (#249) instead of blocking",
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
    swt.set_defaults(func=cmd_wait)

    sv = sub.add_parser("verify", help="auto-detect stack and run lint/test gate")
    sv.add_argument("--cwd", default=None, help="working directory (default: current dir)")
    sv.add_argument("--json", action="store_true", help="emit machine-readable result")
    sv.add_argument(
        "--skip", action="append", metavar="NAME", help="skip check by name (repeatable)"
    )
    sv.set_defaults(func=cmd_verify)

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
    sdv.set_defaults(func=cmd_docs_verify)

    sas = sub.add_parser("audit-skills", help="TF-IDF role boundary audit")
    sas.add_argument("--threshold", type=float, default=0.6)
    sas.add_argument("--output", default="runtime/skill_audit.md")
    sas.add_argument("--json", action="store_true", help="emit JSON instead of writing markdown")
    sas.set_defaults(func=cmd_audit_skills)

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
    sms.set_defaults(func=cmd_migrate_skills)

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
    sse.set_defaults(func=cmd_search)

    # ── issue tracker ────────────────────────────────────────────────────────
    si = sub.add_parser(
        "issue", help="manage issues via GitHub Issues (auto-detects repo from project)"
    )
    si_sub = si.add_subparsers(dest="issue_command", required=True)

    # issue new
    sin = si_sub.add_parser("new", help="create a new issue")
    sin.add_argument("title", help="issue title")
    sin.add_argument("--severity", choices=["low", "med", "high"], default="med")
    sin.add_argument("--noticed-in", dest="noticed_in", default=None, metavar="PROJECT")
    sin.add_argument("--role", default=None, metavar="ROLE")
    sin.add_argument("--tag", default=None, metavar="a,b,c", help="comma-separated tags")
    sin.add_argument(
        "--body", default=None, metavar="TEXT", help="body text (opens $EDITOR if omitted on TTY)"
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
    sil.add_argument("--severity", choices=["low", "med", "high"], default=None)
    sil.add_argument(
        "--cockpit-bug",
        dest="cockpit_bug",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=_COCKPIT_BUG_READ_HELP,
    )

    # issue close
    sic = si_sub.add_parser("close", help="close an issue by GitHub number")
    sic.add_argument("id", help="GitHub issue number (e.g. 123, #123)")
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

    sm.set_defaults(func=cmd_mcp)

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

    spl.set_defaults(func=cmd_plugins)

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
        "--storage-layout",
        action="store_true",
        help="also report V1/V2/mixed storage layout state (#309 Phase 8b); "
        "opt-in, off by default so plain `takkub doctor` is unchanged",
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
    sdoc.set_defaults(func=cmd_doctor)

    sqag = sub.add_parser(
        "qa-gate",
        help="canonical gate: venv-check -> pytest -> ruff check -> lint-imports on a "
        "Python project, or that project's own checks on a Node one (#329) — one "
        "entrypoint shared by qa pane / CI / a user's terminal (#325)",
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
        "--v2-flags",
        action="store_true",
        help="force every TAKKUB_V2_* flag on for this run, to check the Core V2 ladder "
        "before flipping any flag on by default (#309)",
    )
    sqag.set_defaults(func=cmd_qa_gate)

    smig = sub.add_parser(
        "migrate",
        help="Core V2 storage migration: inspect/plan/dry-run/apply/validate/rollback (#309 Phase 4)",
    )
    smig_sub = smig.add_subparsers(dest="migrate_cmd", required=True)
    smig_help = {
        "inspect": "V1 อะไรอยู่ตรงไหน, schema version เท่าไหร่ (read-only)",
        "plan": "จะย้ายอะไรไปไหน (ไม่แตะดิสก์)",
        "dry-run": "จำลอง apply แบบเต็ม ไม่เขียนดิสก์จริง",
        "apply": "ทำจริง + journal (copy-never-move)",
        "validate": "cross-check V2 กับ V1 ที่ยังอยู่",
        "rollback": "ย้อนจาก journal + backup",
    }
    for _name, _help in smig_help.items():
        _p = smig_sub.add_parser(_name, help=_help)
        _p.add_argument("--json", action="store_true", help="emit JSON instead of text report")
        _p.set_defaults(func=cmd_migrate)

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
    spm.add_argument("name", help="provider name (e.g. claude, kimi, cursor)")
    spm.add_argument("model", nargs="?", help="model id; omit to show the current value")
    spm.add_argument("--clear", action="store_true", help="clear the model and use CLI default")
    sprv.set_defaults(func=cmd_provider)

    sprov = sub.add_parser(
        "provision",
        help="install recommended plugins + browser MCPs (idempotent, detect-first; run after npm install)",
    )
    sprov.set_defaults(func=cmd_provision)

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
            return _request(
                _with_project(
                    {
                        "cmd": "pipeline-run",
                        "template_id": args.template_id,
                        "from": _from_role(),
                    }
                )
            )
        return {"ok": False, "msg": f"unknown pipeline subcommand: {args.pipeline_command}"}

    spipe.set_defaults(func=_cmd_pipeline)

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
    sx.set_defaults(func=cmd_codex)

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
    sg.set_defaults(func=cmd_gemini)

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
    srel.set_defaults(func=cmd_release)

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

    ssvcs.set_defaults(func=cmd_services)

    args = p.parse_args(argv)

    gate_err = _enforce_role_gate(args.command)
    if gate_err:
        print(f"error: {gate_err}", file=sys.stderr)
        return 1

    try:
        resp = args.func(args)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    ok = bool(resp.get("ok"))
    # #341: never let a write-path command exit 0 on an unconfirmed response
    # (ok=True but no message from the daemon) — that reads as success to
    # whoever's watching exit codes/output even though nothing was confirmed.
    if (
        ok
        and not resp.get("msg")
        and not resp.get("quiet")
        and args.command in _WRITE_COMMANDS_REQUIRE_CONFIRMATION
    ):
        ok = False
        resp = {
            **resp,
            "msg": (
                "no confirmation message from the orchestrator — treating this as "
                "failed rather than silently exiting 0 (this should never happen; "
                "please report it)"
            ),
        }
    if args.command in {"list", "status", "inbox"}:
        try:
            banner = _instance_banner()
        except Exception:
            banner = ""
        if banner:
            print(banner)
    if "report" in resp:
        _print_status_report(resp["report"])
        report = resp.get("report")
        if isinstance(report, dict) and report.get("any_stalled"):
            ok = False
    elif "items" in resp:
        _print_inbox_items(resp["items"])
    elif "status" in resp:
        for role, state in resp["status"].items():
            print(f"  {role:12s} {state}")
    msg = resp.get("msg", "")
    if msg:
        print(("ok: " if ok else "err: ") + msg)
    return resp.get("exit_code", 0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
