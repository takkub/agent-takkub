"""`takkub skills learned …` — inspect and steer Skill Learning from any shell.

Everything except `reflect` is local file work (same stores the cockpit
uses), so it works with the cockpit closed. `reflect` needs the running
cockpit's pane state and goes over IPC (Lead-only `skill-learn`).
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable

from . import lifecycle, pipeline, store
from .settings import MODES, REFLECTOR_PROVIDERS, load, update


def add_parser(ssk_sub) -> None:
    p = ssk_sub.add_parser(
        "learned", help="Skill Learning: skills the cockpit learned from real work"
    )
    sub = p.add_subparsers(dest="learned_cmd", required=True)

    def _proj(sp):
        sp.add_argument("--project", default=None, help="project name (default: active project)")
        return sp

    _proj(sub.add_parser("list", help="live learned skills with usage"))
    _proj(sub.add_parser("status", help="mode, reflector, counters, last run"))
    s = _proj(sub.add_parser("show", help="one learned skill: body + ledger"))
    s.add_argument("name")
    r = _proj(sub.add_parser("runs", help="recent learning runs (incl. skips and rejections)"))
    r.add_argument("--limit", type=int, default=15)
    sr = _proj(sub.add_parser("show-run", help="one run: proposed / landed / rejected + reasons"))
    sr.add_argument("run_id")
    for name, helptext in (
        ("approve", "land a pending proposal (propose mode)"),
        ("reject", "drop a pending proposal"),
    ):
        sp = _proj(sub.add_parser(name, help=helptext))
        sp.add_argument("run_id")
    _proj(sub.add_parser("pending", help="proposals waiting for approve/reject"))
    a = _proj(sub.add_parser("archive", help="archive a learned skill (moved, never deleted)"))
    a.add_argument("name")
    rv = _proj(sub.add_parser("revive", help="bring an archived learned skill back"))
    rv.add_argument("name")
    _proj(sub.add_parser("archived", help="archived learned skills"))
    m = sub.add_parser("mode", help="auto (land after validation) · propose (approve first) · off")
    m.add_argument("value", nargs="?", choices=MODES)
    pv = sub.add_parser("provider", help="which provider reflects (auto = first ready)")
    pv.add_argument("value", nargs="?", choices=("auto", *REFLECTOR_PROVIDERS))
    rf = _proj(sub.add_parser("reflect", help="reflect on a role's latest work now (/learn)"))
    rf.add_argument("--role", required=True)
    rf.add_argument("--note", default="", help="what to remember (optional hint for the reflector)")
    _proj(sub.add_parser("curate", help="run the whole-library consolidation pass now"))


def _project(args) -> str:
    if getattr(args, "project", None):
        return args.project
    from .. import config

    return config.active_project()[0]


def run(args: argparse.Namespace, request: Callable[[dict], dict]) -> dict:
    cmd = args.learned_cmd
    if cmd == "mode":
        if args.value:
            update(mode=args.value)
        return {"ok": True, "msg": f"skill learning mode = {load().mode}"}
    if cmd == "provider":
        if args.value:
            update(provider=args.value)
        return {"ok": True, "msg": f"reflector provider = {load().provider}"}
    ns = _project(args)
    if cmd == "reflect":
        return request({"cmd": "skill-learn", "role": args.role, "note": args.note})
    if cmd == "list":
        return _list(ns)
    if cmd == "status":
        return _status(ns)
    if cmd == "show":
        return _show(ns, args.name)
    if cmd == "runs":
        runs = pipeline.recent_runs(ns, args.limit)
        for r in runs:
            print(
                f"  {r['run_id']}  {r['status']:<8}  {r.get('role', ''):<10}  {r.get('summary', '')}"
            )
        return {"ok": True, "msg": f"{len(runs)} run(s) · {ns}"}
    if cmd == "show-run":
        return _show_run(ns, args.run_id)
    if cmd == "pending":
        items = pipeline.pending(ns)
        for it in items:
            ops = ", ".join(f"{i.get('op')} {i.get('name')}" for i in it.get("intents", []))
            print(f"  {it.get('run_id')}  ({it.get('role', '')})  {ops}")
        return {"ok": True, "msg": f"{len(items)} pending proposal(s)"}
    if cmd == "approve":
        rec = pipeline.approve(ns, args.run_id)
        return {"ok": True, "msg": rec.summary}
    if cmd == "reject":
        pipeline.reject(ns, args.run_id)
        return {"ok": True, "msg": f"rejected {args.run_id}"}
    if cmd == "archive":
        rec = store.find_managed(ns, args.name)
        if rec is None:
            return {"ok": False, "msg": f"no live learned skill named {args.name!r}"}
        dest = store.archive_skill(rec, reason="manual archive (CLI)")
        return {"ok": True, "msg": f"archived → {dest}"}
    if cmd == "archived":
        rows = store.list_archived(ns)
        for name, layer, d in rows:
            print(f"  {name:<32} {layer:<8} {d}")
        return {"ok": True, "msg": f"{len(rows)} archived"}
    if cmd == "revive":
        layer_req = store.requests(ns)
        rec = store.revive_skill(ns, args.name, requests_now=layer_req)
        if rec.layer == "global":
            store.update_sidecar(rec, requests_at_create=store.requests(None))
        return {"ok": True, "msg": f"revived {rec.name} ({rec.layer})"}
    if cmd == "curate":
        rec = pipeline.curate(ns)
        return {"ok": rec.status != "failed", "msg": rec.summary}
    return {"ok": False, "msg": f"unknown command {cmd}"}


def _list(ns: str) -> dict:
    recs = store.list_skills(ns)
    if not recs:
        return {"ok": True, "msg": f"ยังไม่มี skill ที่เรียนรู้ · {ns} · mode={load().mode}"}
    reqs = {"project": store.requests(ns), "global": store.requests(None)}
    width = max(len(r.name) for r in recs)
    for r in recs:
        side = r.sidecar
        rate = lifecycle.usage_rate(r, reqs)
        print(
            f"  {r.name:<{width}}  {r.layer:<7} {r.category:<12} "
            f"use={side.get('uses', 0)} view={side.get('views', 0)} patch={side.get('patches', 0)} "
            f"rate={rate:.2f}"
        )
        print(f"  {'':<{width}}  {r.description[:100]}")
    return {"ok": True, "msg": f"{len(recs)} learned skill(s) · {ns}"}


def _status(ns: str) -> dict:
    from .reflector import choose_provider

    s = load()
    last = store.read_json(store.state_dir(ns) / "last_run.json", {})
    lines = [
        f"mode            : {s.mode}",
        f"reflector       : {s.provider} → {choose_provider(s) or 'ไม่มีตัวที่พร้อม'}"
        + (f" (model {s.model})" if s.model else ""),
        f"learned skills  : {len(store.list_skills(ns))} live · {len(store.list_archived(ns))} archived",
        f"requests        : project {store.requests(ns)} · global {store.requests(None)}",
        f"pending         : {len(pipeline.pending(ns))}",
        f"last run        : {last.get('run_id', '-')} {last.get('status', '')} — {last.get('summary', '')}",
        f"cadence         : ≥{s.min_interval_s}s apart · ≤{s.daily_cap}/day · worth ≥{s.min_worth}",
    ]
    if last.get("ts"):
        lines[-2] += f" ({time.strftime('%Y-%m-%d %H:%M', time.localtime(last['ts']))})"
    print("\n".join("  " + ln for ln in lines))
    return {"ok": True, "msg": f"skill learning · {ns}"}


def _show(ns: str, name: str) -> dict:
    rec = store.find_managed(ns, name)
    if rec is None:
        return {"ok": False, "msg": f"no live learned skill named {name!r}"}
    print(rec.skill_file.read_text(encoding="utf-8"))
    print("--- ledger ---")
    for e in store.read_ledger(rec.dir):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("ts", 0)))
        print(f"  {ts}  {e.get('action'):<8} {e.get('reason', '')[:120]}")
    return {"ok": True, "msg": f"{rec.layer} · {rec.dir}"}


def _show_run(ns: str, run_id: str) -> dict:
    d = store.state_dir(ns) / "runs" / run_id
    r = store.read_json(d / "result.json", {})
    if not r:
        staged = store.read_json(store.state_dir(ns) / "pending" / f"{run_id}.json", {})
        if not staged:
            return {"ok": False, "msg": f"no run {run_id}"}
        for i in staged.get("intents", []):
            print(f"  [pending] {i.get('op')} {i.get('name')}: {i.get('reason', '')}")
        return {"ok": True, "msg": f"pending · approve/reject {run_id}"}
    print(f"  status   : {r.get('status')} {r.get('skip_reason', '')}")
    print(
        f"  provider : {r.get('provider') or '-'} · worth {r.get('worth')} ({', '.join(r.get('reasons', []))})"
    )
    for o in r.get("landed", []):
        print(f"  ✓ {o['op']} {o['name']}" + (f" → {o['into']}" if o.get("into") else ""))
    for o in r.get("rejected", []):
        print(f"  ✗ {o['op']} {o['name']}: {o['reason']}")
    for o in r.get("retired", []):
        print(f"  ⌫ archived {o['name']}: {o['reason']}")
    if r.get("error"):
        print(f"  error    : {r['error'][:300]}")
    return {"ok": True, "msg": r.get("summary", "")}
