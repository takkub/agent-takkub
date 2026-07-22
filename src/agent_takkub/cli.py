"""`takkub` CLI — agent-side client that talks to the orchestrator over TCP.

Usage from inside an agent pane (Claude running with TAKKUB_ROLE env set):

  takkub assign --role backend --cwd C:/x/api "task..."
  takkub send --to backend "msg"
  takkub spawn --role frontend
  takkub close --role frontend
  takkub list
  takkub done [note]

Output is human readable on stdout. Exit 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
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
        "restart",
        # machine-level npm installs — a teammate pane must never mutate the
        # host toolchain mid-task; Lead/terminal decides when to add a CLI.
        "provider",
    }
)

# Commands intended only for teammate panes. Lead summarises inline and never
# needs to call done on itself — blocking this prevents Lead from accidentally
# scheduling its own close via the done→QTimer→close chain.
TEAMMATE_ONLY_COMMANDS = frozenset({"done"})


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


def _request(payload: dict) -> dict:
    # Stamp the capability token so the server can verify the caller's identity.
    # Lead panes carry TAKKUB_LEAD_TOKEN (authorises Lead-only commands).
    # Teammate panes carry TAKKUB_PANE_TOKEN (authorises send/done).
    # Whichever is present in the env is stamped; if both are set (shouldn't
    # happen in normal operation) the Lead token takes precedence.
    token = os.environ.get("TAKKUB_LEAD_TOKEN") or os.environ.get("TAKKUB_PANE_TOKEN")
    if token:
        payload["auth"] = token
    s = _connect()
    try:
        s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        s.settimeout(15)
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return {"ok": False, "msg": "no response from orchestrator"}
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
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


def cmd_assign(args: argparse.Namespace) -> dict:
    # #1: validate --shards BEFORE the `or 1` fallback so explicit 0 / negative /
    # >8 values are rejected with a clear message rather than silently clamped.
    _SHARDS_MAX = 8
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
        return _request(
            _with_project(
                {
                    "cmd": "assign",
                    "role": args.role,
                    "cwd": args.cwd,
                    "task": args.task,
                    "from": _from_role(),
                    "plan": True,
                    "shard_total": shards,
                    "feature": getattr(args, "feature", "") or "",
                }
            )
        )
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
                        "feature": getattr(args, "feature", "") or "",
                    }
                )
            )
            results.append(resp)
        ok_count = sum(1 for r in results if r.get("ok"))
        return {"ok": ok_count == shards, "msg": f"queued {ok_count}/{shards} shards"}
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
                "feature": getattr(args, "feature", "") or "",
            }
        )
    )


def cmd_worktree(args: argparse.Namespace) -> dict:
    """`takkub worktree list|merge|clean` — Lead merge assist for #81 worktrees.

    Pure-local git operations (no orchestrator socket): the git state is the
    source of truth, so this works after a cockpit crash or with the cockpit
    closed — exactly when cleanup is most needed. Mutations are lead-gated at
    the CLI layer like assign/close.
    """
    from .worktree_manager import WorktreeManager

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
        ok, msg = mgr.merge_isolated(root, branch, keep=bool(args.keep))
        return {"ok": ok, "msg": msg}

    if sub == "clean":
        lines = mgr.clean_isolated(root, force=bool(args.force))
        if not lines:
            print("(nothing to clean)")
            return {"ok": True, "msg": "0 cleaned"}
        for line in lines:
            _utf8_print(f"  {line}")
        failed = sum(1 for line in lines if line.startswith("FAILED"))
        return {"ok": failed == 0, "msg": f"{len(lines)} processed, {failed} failed"}

    return {"ok": False, "msg": f"unknown worktree subcommand: {sub}"}


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
                "failed": bool(getattr(args, "fail", False)),
            }
        )
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


def cmd_task(args: argparse.Namespace) -> dict:
    """`takkub task show --role <r>` — print the full text of the last task
    assigned to `role` (issue #1 file-based task handoff).

    Works whether the assign pasted the task inline (short task, no handoff
    file) or a pointer (long task, read back from the on-disk handoff file)
    — the CLI always resolves to the full text either way.
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
        state = info.get("state", "?")
        stall = info.get("stall_minutes")
        human_ts = info.get("last_progress_human", "?")
        abs_ts = info.get("last_progress_abs", "?")
        stall_str = f" ⚠ stalled {stall}m" if stall is not None else ""
        print(f"\n  [{role}] {state}{stall_str}")
        print(f"    last progress: {human_ts} ({abs_ts})")
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


def cmd_status(args: argparse.Namespace) -> dict:
    payload = _with_project({"cmd": "status"})
    if getattr(args, "since", None):
        payload["since"] = args.since
    return _request(payload)


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

    if args.json:
        import json as _json

        _utf8_print(
            _json.dumps(
                [
                    {
                        "category": f.category,
                        "name": f.name,
                        "status": f.status.value,
                        "detail": f.detail,
                        "fix_hint": f.fix_hint,
                    }
                    for f in findings
                ],
                indent=2,
            )
        )
    else:
        _utf8_print(format_report(findings))

    n_fail = sum(1 for f in findings if f.status == Status.FAIL)
    ok = n_fail == 0
    return {"ok": ok, "msg": f"{n_fail} fail(s)" if not ok else "all checks passed"}


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
    res = release(
        REPO_ROOT,
        part=args.part,
        explicit_version=args.version,
        do_commit=not args.no_commit,
        do_tag=not args.no_tag,
        dry_run=args.dry_run,
        allow_empty=args.allow_empty,
        do_github_release=do_github_release,
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
    """Pure read-only grep across `~/.claude/projects/<*>/<uuid>.jsonl`.
    Does NOT go through the orchestrator's TCP socket — search is a
    passive query and works whether the cockpit is running or not."""
    from datetime import datetime, timedelta

    from .chatlog_scanner import search_sessions

    since: datetime | None = None
    if getattr(args, "days", None):
        since = datetime.now() - timedelta(days=args.days)
    # Default: today only. Keeps a "what did I touch this morning"
    # search fast on a vault with months of jsonls.
    if since is None and not getattr(args, "all", False):
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    hits = search_sessions(
        args.query,
        project_filter=args.project,
        since=since,
        limit=args.limit,
    )
    if not hits:
        return {"ok": True, "msg": f"no matches for {args.query!r}"}
    for h in hits:
        ts = h.get("timestamp") or ""
        # Trim "T" + microseconds for terminal display
        ts_short = ts.replace("T", " ")[:19] if ts else "(no ts)"
        proj = h.get("project") or "?"
        role = h.get("role") or "?"
        snippet = h.get("snippet") or ""
        # Project folder names are encoded — show the recognisable
        # tail so the line stays readable.
        proj_tail = proj.split("-")[-1] if "-" in proj else proj
        print(f"  {proj_tail:18s} {ts_short}  {role:9s}  {snippet}")
    return {
        "ok": True,
        "msg": f"{len(hits)} match(es)" + (" (limit reached)" if len(hits) == args.limit else ""),
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
        help="remove leftover wt/* worktrees (safe ones only; --force drops dirty/unmerged too)",
    )
    swc.add_argument(
        "--force",
        action="store_true",
        help="also remove dirty / unmerged worktrees (their work is LOST)",
    )
    swc.add_argument("--cwd", default=None, help="project dir (default: current dir)")
    swt.set_defaults(func=cmd_worktree)

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
    sd.set_defaults(func=cmd_done)

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

    st = sub.add_parser(
        "task",
        help="read back a role's last assigned task (issue #1 file-based task handoff)",
    )
    st_sub = st.add_subparsers(dest="t_cmd", required=True)
    sts = st_sub.add_parser("show", help="print the full text of a role's last assigned task")
    sts.add_argument("--role", required=True, help="role name to look up")
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
        help="grep past Claude Code conversations across all projects",
    )
    sse.add_argument("query", help="substring to grep for (case-insensitive)")
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

    # issue list
    sil = si_sub.add_parser("list", help="list issues with optional filters")
    sil.add_argument("--open", action="store_true", dest="open", help="show only open issues")
    sil.add_argument("--closed", action="store_true", dest="closed", help="show only closed issues")
    sil.add_argument("--noticed-in", dest="noticed_in", default=None, metavar="PROJECT")
    sil.add_argument("--role", default=None, metavar="ROLE")
    sil.add_argument("--severity", choices=["low", "med", "high"], default=None)

    # issue close
    sic = si_sub.add_parser("close", help="close an issue by GitHub number")
    sic.add_argument("id", help="GitHub issue number (e.g. 123, #123)")
    sic.add_argument(
        "--note", default="", metavar="MSG", help="cause / fix summary (posted as comment)"
    )

    # issue show
    sis = si_sub.add_parser("show", help="print issue from GitHub to stdout")
    sis.add_argument("id", help="GitHub issue number (e.g. 123, #123)")

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
    sdoc.set_defaults(func=cmd_doctor)

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
    if args.command in {"list", "status"}:
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
    elif "status" in resp:
        for role, state in resp["status"].items():
            print(f"  {role:12s} {state}")
    msg = resp.get("msg", "")
    if msg:
        print(("ok: " if ok else "err: ") + msg)
    return resp.get("exit_code", 0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
