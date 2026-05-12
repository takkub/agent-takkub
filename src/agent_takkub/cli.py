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


def cmd_spawn(args: argparse.Namespace) -> dict:
    return _request({"cmd": "spawn", "role": args.role, "cwd": args.cwd})


def cmd_assign(args: argparse.Namespace) -> dict:
    return _request({"cmd": "assign", "role": args.role, "cwd": args.cwd, "task": args.task})


def cmd_send(args: argparse.Namespace) -> dict:
    return _request({"cmd": "send", "to": args.to, "msg": args.msg, "from": _from_role()})


def cmd_close(args: argparse.Namespace) -> dict:
    return _request({"cmd": "close", "role": args.role})


def cmd_close_all(_: argparse.Namespace) -> dict:
    return _request({"cmd": "close-all"})


def cmd_done(args: argparse.Namespace) -> dict:
    return _request({"cmd": "done", "from": _from_role(), "note": args.note or ""})


def cmd_list(_: argparse.Namespace) -> dict:
    return _request({"cmd": "list"})


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
