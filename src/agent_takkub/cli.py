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

from .config import read_port

# Commands that orchestrate the cockpit (spawn/route/close panes). Only the
# Lead pane is allowed to invoke these; teammates must work on their assigned
# task and coordinate via `send` / `done`. The gate is enforced in `main()`
# based on the TAKKUB_ROLE env var that the orchestrator injects per pane.
LEAD_ONLY_COMMANDS = frozenset(
    {"spawn", "assign", "close", "close-all", "end-session", "harvest", "release"}
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


def _request(payload: dict) -> dict:
    # Stamp the Lead capability token when running inside a Lead pane.
    # Teammates don't have TAKKUB_LEAD_TOKEN in their env, so their payloads
    # won't carry the auth field and the server will reject Lead-only commands.
    token = os.environ.get("TAKKUB_LEAD_TOKEN")
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
    (a Lead in unirecon never reaches into pms's pane registry)."""
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
            }
        )
    )


def cmd_send(args: argparse.Namespace) -> dict:
    return _request(
        _with_project({"cmd": "send", "to": args.to, "msg": args.msg, "from": _from_role()})
    )


def cmd_close(args: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "close", "role": args.role, "from": _from_role()}))


def cmd_close_all(_: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "close-all", "from": _from_role()}))


def cmd_done(args: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "done", "from": _from_role(), "note": args.note or ""}))


def cmd_end_session(args: argparse.Namespace) -> dict:
    return _request(
        _with_project({"cmd": "end-session", "from": _from_role(), "note": args.note or ""})
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

    payload: dict = _with_project({"cmd": "harvest", "role": args.role})
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
    done_resp = _request(_with_project({"cmd": "harvest-done", "role": args.role, "note": note}))
    if done_resp.get("ok"):
        print(f"ok: '{args.role}' marked as done ({len(artifacts)} artifact(s))")
        return {"ok": True, "msg": f"harvested {len(artifacts)} artifact(s)"}
    return {"ok": False, "msg": done_resp.get("msg", "harvest-done failed"), "exit_code": 1}


def cmd_list(_: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "list"}))


def _print_status_report(report: dict) -> None:
    """Pretty-print the per-pane report returned by `takkub status`."""
    project = report.get("project") or "?"
    panes = report.get("panes") or {}
    print(f"  project: {project}")
    for role, info in panes.items():
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
    """Fire Google Gemini CLI non-interactively and print the result.

    Mirror of `cmd_codex`. Pure local invocation — no orchestrator IPC.
    Gemini uses its own auth (Google login on first run or
    `GEMINI_API_KEY` env); cockpit doesn't touch those credentials.
    Works whether or not the cockpit is running.

    `cwd` defaults to the calling pane's working directory so a
    `takkub gemini "review this"` inside a project pane naturally
    runs Gemini against that project's files.
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
        run_auto_fixes(findings)
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


def cmd_release(args: argparse.Namespace) -> dict:
    """Bump version + roll CHANGELOG's [vNEXT] + git commit & tag."""
    from .config import REPO_ROOT
    from .release import release

    res = release(
        REPO_ROOT,
        part=args.part,
        explicit_version=args.version,
        do_commit=not args.no_commit,
        do_tag=not args.no_tag,
        dry_run=args.dry_run,
        allow_empty=args.allow_empty,
    )
    if res["dry_run"]:
        _utf8_print(
            f"[dry-run] {res['current']} → {res['new_version']} · tag {res['tag']} · {res['date']}"
        )
        _utf8_print("  (no files or git touched)")
        return {"ok": True, "msg": "dry-run"}

    bits = [f"{res['current']} → {res['new_version']}"]
    if res["committed"]:
        bits.append("committed")
    if res["tagged"]:
        bits.append(f"tagged {res['tag']}")
    _utf8_print("  " + " · ".join(bits))
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


def main(argv: list[str] | None = None) -> int:
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
    sa.set_defaults(func=cmd_assign)

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
    sd.set_defaults(func=cmd_done)

    ses = sub.add_parser(
        "end-session",
        help="(lead) write session summary to runtime/sessions and vault mirror",
    )
    ses.add_argument("--note", default="", help="summary note (default: 'session ended')")
    ses.set_defaults(func=cmd_end_session)

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
        action="store_true",
        help=(
            "route this issue to the agent-takkub install repo regardless of cwd — "
            "used by the 🐛 Bug Check broadcast for cockpit/orchestrator/CLI/UI bugs "
            "noticed inside another project's pane"
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

    sdoc = sub.add_parser(
        "doctor",
        help="diagnose cockpit env (claude, node, plugins, mcps, projects)",
    )
    sdoc.add_argument(
        "--fix",
        action="store_true",
        help="apply safe auto-fixes for missing/broken state",
    )
    sdoc.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of text report",
    )
    sdoc.set_defaults(func=cmd_doctor)

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
        help="one-shot Google Gemini CLI query (non-interactive, pure local)",
    )
    sg.add_argument("prompt", help="prompt text to send to Gemini (positional)")
    sg.add_argument(
        "--cwd",
        default=None,
        help="working directory for the Gemini run (default: current dir)",
    )
    sg.add_argument(
        "--model",
        default=None,
        help="override Gemini's default model (e.g. gemini-2.5-pro)",
    )
    sg.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait before killing the gemini process (default: 120)",
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
        "--dry-run",
        action="store_true",
        help="print the planned version/tag without touching files or git",
    )
    srel.set_defaults(func=cmd_release)

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
    if "report" in resp:
        _print_status_report(resp["report"])
        if resp.get("report", {}).get("any_stalled"):
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
