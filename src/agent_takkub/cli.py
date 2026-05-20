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
LEAD_ONLY_COMMANDS = frozenset({"spawn", "assign", "close", "close-all"})

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
    return _request(_with_project({"cmd": "spawn", "role": args.role, "cwd": args.cwd}))


def cmd_assign(args: argparse.Namespace) -> dict:
    return _request(
        _with_project({"cmd": "assign", "role": args.role, "cwd": args.cwd, "task": args.task})
    )


def cmd_send(args: argparse.Namespace) -> dict:
    return _request(
        _with_project({"cmd": "send", "to": args.to, "msg": args.msg, "from": _from_role()})
    )


def cmd_close(args: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "close", "role": args.role}))


def cmd_close_all(_: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "close-all"}))


def cmd_done(args: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "done", "from": _from_role(), "note": args.note or ""}))


def cmd_list(_: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "list"}))


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

    sl = sub.add_parser("list", help="show pane status")
    sl.set_defaults(func=cmd_list)

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
    if "status" in resp:
        for role, state in resp["status"].items():
            print(f"  {role:12s} {state}")
    msg = resp.get("msg", "")
    if msg:
        print(("ok: " if ok else "err: ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
