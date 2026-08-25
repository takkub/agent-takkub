"""Durable per-role message log for `takkub send` (issue #277).

`takkub assign` has always been auditable and respawn-proof: the task text is
written to the ledger, replayed after a crash-respawn, and readable back with
`takkub task show --role <role>`. `takkub send` had none of that. It wrote the
bytes into the pane's PTY, answered ``ok: sent to <role>`` unconditionally, and
kept no record — so when the pane was respawned (crash, stuck-recover, rate-
limit resume) the message vanished with the process and there was no way, even
in principle, to find out afterwards whether it had ever been read.

The reported cost of that (issue #277): a spec correction — the owner had
changed their mind about how passwords were to be stored — was sent, answered
``ok: sent``, and lost to a respawn. The agent carried on building the
cancelled design. `backend` respawned 4 times in one session and 3 messages
disappeared; the only reason it was caught at all was Lead happening to run
`takkub task show`.

This module owns the record, not the delivery:

  * `append` records every send with the pane generation it was written into.
  * `mark_delivered` is called once the submit-verify chain says the pane
    actually took it (the same signal task delivery uses), so "sent" and
    "confirmed received" stop being the same word.
  * `undelivered_for_generation` answers the one question the reaper needs:
    which messages were written into a session that no longer exists?
  * `read` backs `takkub messages --role <role>` so Lead can verify instead of
    trusting a return string.

Storage is one JSONL file per project under ``RUNTIME_DIR/messages/`` — same
shape and durability story as the pending-notice store, and readable by hand
when something goes wrong. Records are capped per role so a long session can't
grow it without bound.

Layer rule: pure store + policy. It MUST NOT import orchestrator / main_window
/ app / cli — the orchestrator calls in, never the reverse.
"""

from __future__ import annotations

import copy
import json
import pathlib
import time
import uuid

from .cached_read import read_cached

# Per (project, role) cap. Generous — a message is a few hundred bytes and the
# whole point is being able to look back at a long session — but bounded so an
# unattended cockpit can't fill the disk.
MAX_RECORDS_PER_ROLE = 200

# How many times one message may be replayed into a fresh session before the
# store gives up on it. A message that survives this many respawns is landing
# in a pane that keeps dying; re-pasting it forever would turn a lost message
# into an infinite loop of duplicated instructions.
MAX_REPLAYS = 3


def _store_path(runtime_dir: pathlib.Path, project_ns: str) -> pathlib.Path:
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in project_ns) or "default"
    return runtime_dir / "messages" / f"{safe}.jsonl"


def read(runtime_dir: pathlib.Path, project_ns: str, role: str | None = None) -> list[dict]:
    """Every recorded message for *project_ns*, oldest first.

    *role* narrows to one recipient. Unreadable or half-written lines are
    skipped rather than raised — an audit log that refuses to open because one
    line is corrupt is worse than one that shows the rest.
    """
    path = _store_path(runtime_dir, project_ns)
    try:
        # #386: stat-validated cache — `_reap_role_messages` reads this on
        # every idle-check tick from the main thread.
        records = read_cached(path, _parse_lines, missing=())
    except OSError:
        return []
    # Deep-copied: callers (`mark_delivered` etc.) mutate then `_write_all`,
    # and the cached parse must never be edited in place.
    return [copy.deepcopy(rec) for rec in records if role is None or rec.get("to") == role]


def _parse_lines(raw: str) -> tuple[dict, ...]:
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        out.append(rec)
    return tuple(out)


def _write_all(runtime_dir: pathlib.Path, project_ns: str, records: list[dict]) -> None:
    path = _store_path(runtime_dir, project_ns)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
        )
    except OSError:
        pass  # an audit record must never break the send it describes


def _append_record(
    runtime_dir: pathlib.Path,
    project_ns: str,
    *,
    to_role: str,
    from_role: str | None,
    body: str,
    generation: int,
    state: str,
) -> str:
    msg_id = uuid.uuid4().hex[:12]
    records = read(runtime_dir, project_ns)
    records.append(
        {
            "id": msg_id,
            "ts": time.time(),
            "to": to_role,
            "from": from_role or "lead",
            "body": body,
            "generation": int(generation),
            "state": state,
            "replays": 0,
        }
    )
    per_role: dict[str, int] = {}
    kept_reversed: list[dict] = []
    for rec in reversed(records):
        role = str(rec.get("to", ""))
        per_role[role] = per_role.get(role, 0) + 1
        if per_role[role] <= MAX_RECORDS_PER_ROLE:
            kept_reversed.append(rec)
    _write_all(runtime_dir, project_ns, list(reversed(kept_reversed)))
    return msg_id


def append(
    runtime_dir: pathlib.Path,
    project_ns: str,
    *,
    to_role: str,
    from_role: str | None,
    body: str,
    generation: int,
) -> str:
    """Record one outgoing message; returns its id.

    *generation* is the target pane's session generation at write time — the
    single fact that later distinguishes "still sitting in the session it was
    written to" from "written into a session that has since been replaced".
    """
    return _append_record(
        runtime_dir,
        project_ns,
        to_role=to_role,
        from_role=from_role,
        body=body,
        generation=generation,
        state="sent",
    )


def append_queued_no_pane(
    runtime_dir: pathlib.Path,
    project_ns: str,
    *,
    to_role: str,
    from_role: str | None,
    body: str,
) -> str:
    """Record a `takkub send` aimed at a role with no pane open yet (#303
    item 3) — `to_role` is a known role, but nothing to write the bytes
    into exists. `state="queued_no_pane"` keeps this out of
    `undelivered_in`'s respawn-reap filter (which only ever looks at
    generation-stale ``"sent"`` records) and is exactly what
    `queued_no_pane_for_role` looks for once that role's pane spawns."""
    return _append_record(
        runtime_dir,
        project_ns,
        to_role=to_role,
        from_role=from_role,
        body=body,
        generation=-1,
        state="queued_no_pane",
    )


def queued_no_pane_for_role(runtime_dir: pathlib.Path, project_ns: str, role: str) -> list[dict]:
    """Messages `takkub send` recorded while *role* had no pane open (#303
    item 3), still waiting to be delivered once it spawns."""
    return [
        rec
        for rec in read(runtime_dir, project_ns)
        if rec.get("to") == role and rec.get("state") == "queued_no_pane"
    ]


def _update(runtime_dir: pathlib.Path, project_ns: str, msg_id: str, **fields) -> bool:
    records = read(runtime_dir, project_ns)
    hit = False
    for rec in records:
        if rec.get("id") == msg_id:
            rec.update(fields)
            hit = True
            break
    if hit:
        _write_all(runtime_dir, project_ns, records)
    return hit


def mark_delivered(runtime_dir: pathlib.Path, project_ns: str, msg_id: str) -> bool:
    """Promote a record from "sent" (bytes written) to "delivered" (the pane
    demonstrably took it). Only this state means the message was received."""
    return _update(runtime_dir, project_ns, msg_id, state="delivered", delivered_ts=time.time())


def mark_replayed(runtime_dir: pathlib.Path, project_ns: str, msg_id: str, generation: int) -> bool:
    """Re-stamp a record that has just been re-sent into a NEW session."""
    records = read(runtime_dir, project_ns)
    for rec in records:
        if rec.get("id") == msg_id:
            rec["generation"] = int(generation)
            rec["replays"] = int(rec.get("replays", 0)) + 1
            rec["state"] = "sent"
            rec["last_replay_ts"] = time.time()
            _write_all(runtime_dir, project_ns, records)
            return True
    return False


def mark_abandoned(runtime_dir: pathlib.Path, project_ns: str, msg_id: str, reason: str) -> bool:
    """Stop trying to deliver a record, but keep it readable — an abandoned
    message is precisely the thing Lead needs to be able to find later."""
    return _update(runtime_dir, project_ns, msg_id, state="abandoned", abandoned_reason=reason)


def undelivered_in(records: list[dict], role: str, current_generation: int) -> list[dict]:
    """Pure filter behind `undelivered_for_generation` — messages written into
    a session older than *current_generation* that were never confirmed
    delivered, i.e. lost to a respawn (the #277 bug).

    A record still "sent" against the CURRENT generation is not returned: it
    may simply be sitting unread in a live pane, which is normal and must not
    be re-pasted. Records past `MAX_REPLAYS` are excluded too — the caller is
    expected to have abandoned them.

    Split out from the disk-reading wrapper so the orchestrator's watchdog
    sweep can read each project's log ONCE per tick and filter every role from
    that one read, instead of re-reading the same file per pane on a 5-second
    timer that runs on the Qt main thread.
    """
    out = []
    for rec in records:
        if rec.get("to") != role:
            continue
        if rec.get("state") != "sent":
            continue
        if int(rec.get("generation", 0)) >= int(current_generation):
            continue
        if int(rec.get("replays", 0)) >= MAX_REPLAYS:
            continue
        out.append(rec)
    return out


def undelivered_for_generation(
    runtime_dir: pathlib.Path, project_ns: str, role: str, current_generation: int
) -> list[dict]:
    """Disk-reading form of `undelivered_in` for single-role callers."""
    return undelivered_in(read(runtime_dir, project_ns), role, current_generation)


def format_for_cli(records: list[dict], *, limit: int = 20) -> list[str]:
    """Human-readable lines for `takkub messages`, newest last."""
    from datetime import datetime

    lines: list[str] = []
    for rec in records[-limit:]:
        clock = datetime.fromtimestamp(float(rec.get("ts", 0.0))).strftime("%H:%M:%S")
        state = str(rec.get("state", "?"))
        badge = {
            "delivered": "✅ ถึงแล้ว",
            "sent": "⏳ ส่งแล้วยังไม่ยืนยัน",
            "abandoned": "⛔ ส่งไม่สำเร็จ",
            "queued_no_pane": "🕓 รอ pane เปิด",
        }.get(state, state)
        replays = int(rec.get("replays", 0))
        replay_note = f" · ส่งซ้ำหลัง respawn {replays}x" if replays else ""
        body = " ".join(str(rec.get("body", "")).split())
        if len(body) > 300:
            body = body[:300] + "…"
        lines.append(
            f"[{clock}] {rec.get('from', '?')} → {rec.get('to', '?')} · {badge}{replay_note}\n"
            f"    {body}"
        )
    return lines
