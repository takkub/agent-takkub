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


def _connect() -> socket.socket:
    port = read_port()
    if port is None:
        raise RuntimeError(
            "agent-takkub cockpit is not running (no port file). Launch the app first."
        )
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    return s


def _request(payload: dict) -> dict:
    s = _connect()
    try:
        s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        s.settimeout(5)
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
      - If TAKKUB_ROLE == "lead", allow everything.
      - Otherwise, block LEAD_ONLY_COMMANDS with a hint pointing at the
        commands teammates *are* allowed to use.
    """
    if command not in LEAD_ONLY_COMMANDS:
        return None
    role = _from_role()
    if role is None or role.lower() == "lead":
        return None
    return (
        f"only lead can run 'takkub {command}'. you are '{role}'.\n"
        f"       do your task directly with Read/Write/Edit/Bash.\n"
        f"       use 'takkub send --to <role>' for peer coordination, "
        f"'takkub done' to report back."
    )


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
        _with_project(
            {"cmd": "assign", "role": args.role, "cwd": args.cwd, "task": args.task}
        )
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
    return _request(
        _with_project({"cmd": "done", "from": _from_role(), "note": args.note or ""})
    )


def cmd_list(_: argparse.Namespace) -> dict:
    return _request(_with_project({"cmd": "list"}))


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
