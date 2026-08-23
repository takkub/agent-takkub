"""Auto-file a cockpit issue from RUNTIME SIGNALS, not just from crashes (#297).

`auto_issue_capture` only ever fires on an unhandled exception. Every real
cockpit defect found in the field so far was something else entirely — the UI
freezing for seconds, the watchdog killing a pane that was still working, a
task delivery cancelled before it reached anyone. None of those raise, so none
of them were ever auto-captured, and every one of them had to be noticed by a
human and typed up by hand.

The signals were in `events.log` the whole time. This module reads the same
event taxonomy `takkub ma` already uses (`maintenance._SEVERE_EVENTS` /
`_WARN_EVENTS`) and files ONE issue when a rule's threshold is crossed, reusing
`auto_issue_capture`'s dedup + rate cap so a bad hour can't turn into a flood.

Thresholds are calibrated against measured field data (2026-08-16 → 08-18 on
the reference box), not guessed — see each rule's `why`. The bar is "a human
would have opened an issue about this", so ordinary operation stays silent:
a handful of stalls on a busy machine is not a defect report.

Runs on its own daemon thread. It never touches Qt, and it must never read the
event log on the GUI thread — that is the exact class of bug it exists to
report (#291).
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import EVENTS_LOG

# How far back each scan looks, and how often it runs. A 6h window is long
# enough that a genuine defect clears the bar and short enough that yesterday's
# already-reported trouble doesn't keep re-triggering.
WINDOW_HOURS = 6.0
SCAN_INTERVAL_S = float(os.environ.get("TAKKUB_AUTO_ISSUE_SCAN_INTERVAL_S", 30 * 60))
# Wait before the first scan so a cockpit that is still booting (panes
# spawning, MCPs loading) isn't judged on its noisiest minutes.
FIRST_SCAN_DELAY_S = float(os.environ.get("TAKKUB_AUTO_ISSUE_FIRST_SCAN_S", 10 * 60))

_ENABLED_FLAG = "auto-issue.json"


def _flag_path() -> Path:
    """Resolved at call time so a test monkeypatching SETTINGS_HOME lands in
    its own tmp dir (same rule `rtk_helper._enabled_flag_path` follows)."""
    from . import config

    return config.SETTINGS_HOME / _ENABLED_FLAG


def auto_issue_enabled() -> bool:
    """Whether this install may file cockpit issues automatically.

    **Default on.** The cockpit's own defects are invisible to its maintainers
    otherwise — the whole point of #297. Reports are scrubbed of the caller's
    home directory and redacted for token-shaped substrings before they leave
    the machine (`auto_issue_capture._scrub_home` / `_redact`), and the switch
    below turns it off for good.
    """
    if os.environ.get("TAKKUB_AUTO_ISSUE", "").strip().lower() in {"0", "off", "false", "no"}:
        return False
    try:
        data = json.loads(_flag_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return bool(data.get("enabled", True))


def set_auto_issue_enabled(enabled: bool) -> None:
    """Persist the toggle. Best-effort — a failed write never raises."""
    path = _flag_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"enabled": bool(enabled)}, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


@dataclass(frozen=True)
class SignalRule:
    """One "this is worth a bug report" condition over a scan window."""

    key: str
    event: str
    min_count: int
    title: str
    why: str
    # Optional extra bar on a numeric field of the event (e.g. stall duration),
    # so a high count of trivial occurrences doesn't fire on its own.
    field: str | None = None
    field_min: float | None = None
    # Values of `ignore_field` that mean "this occurrence is routine" — the
    # event fired, but not for a reason worth a bug report. Needed because one
    # event name can cover outcomes that are nothing alike (#331:
    # `task_delivery_failed` is emitted both when the task never reached the
    # pane AND when a teammate simply reported the task failed).
    ignore_field: str | None = None
    ignore_values: frozenset[str] = frozenset()
    # Overrides the scan's default `WINDOW_HOURS` for just this rule — e.g.
    # a fleet-telemetry signal (#361's `auto_migrate_rolled_back` /
    # `model_pin_v2_drift`) that fires at most once per boot needs a wider
    # window than the 6h default to ever cross `min_count=1` meaningfully.
    # `None` means "use the scan's own `window_hours`".
    window_hours: float | None = None


# Each `min_count` is stated against what the reference box actually produced,
# so the bar is a measurement rather than a guess.
RULES: tuple[SignalRule, ...] = (
    SignalRule(
        key="stuck_pane_recover",
        event="stuck_pane_recover",
        min_count=3,
        title="watchdog respawn ซ้ำผิดปกติ",
        why="วัดจริง 6 ครั้งใน 2 วัน — 3 ครั้งใน 6 ชม. คือผิดปกติชัดเจน",
    ),
    SignalRule(
        key="task_delivery_failed",
        event="task_delivery_failed",
        min_count=3,
        title="ส่งใบงานไม่สำเร็จซ้ำ",
        why=(
            "วัดจริง 18 ครั้งใน 2 วัน กระจายทั้งช่วง — กระจุก 3 ครั้งใน 6 ชม. คือมีอะไรพัง · "
            "นับเฉพาะที่ใบงานไปไม่ถึง pane จริงๆ: #331 เปิดเป็น false positive จาก "
            "`takkub done --failed` ธรรมดา 4 ใบ ซึ่ง delivery สำเร็จทุกใบ"
        ),
        ignore_field="reason",
        ignore_values=frozenset({"agent_reported_failed", "pane_closed"}),
    ),
    SignalRule(
        key="delivery_boot_timeout_failed",
        event="delivery_boot_timeout_failed",
        min_count=2,
        title="ใบงานตายเพราะ pane boot ไม่ทันซ้ำ",
        why="วัดจริง 1 ครั้งใน 2 วัน — เกิด 2 ครั้งใน 6 ชม. คือ regression",
    ),
    SignalRule(
        key="no_content_pane_recover",
        event="no_content_pane_recover",
        min_count=3,
        title="pane ไม่พ่น output จน watchdog ต้องเข้าแทรกซ้ำ",
        why="วัดจริง 4 ครั้งใน 2 วัน",
    ),
    SignalRule(
        key="main_thread_stall_severe",
        event="main_thread_stall",
        min_count=40,
        field="duration_ms",
        field_min=5000,
        title="UI ค้างยาวและบ่อยผิดปกติ",
        why=(
            "ก่อนแก้ #291 วัดได้ 1,448 ครั้ง/6 ชม. สูงสุด 21s — หลังแก้เหลือ 13 ครั้ง/ชม. "
            "สูงสุด 3.3s เกณฑ์นี้จึงเงียบตอนปกติ และดังตอนที่ #291 กลับมา"
        ),
    ),
    SignalRule(
        key="auto_migrate_rolled_back",
        event="auto_migrate_rolled_back",
        min_count=1,
        window_hours=24.0,
        title="boot-time auto-migrate rollback",
        why=(
            "#361: fires at most once per boot, and the ladder is copy-never-move — a single "
            "rollback already means a machine (likely the first-ever macOS `apply`, per the "
            "task's own risk note) needs a human to look, not a repeat count to build up first"
        ),
    ),
    SignalRule(
        key="model_pin_v2_drift",
        event="model_pin_v2_drift",
        min_count=1,
        window_hours=24.0,
        title="model pin V1/V2 disagree (router shadow-read)",
        why=(
            "#361: 1.1.0's V2 router shadow-read is still new on every machine but this one — "
            "one disagreement is already worth a look, same one-shot bar as the rollback rule "
            "above, so a friend's machine reports its own drift back without anyone pressing "
            "anything"
        ),
    ),
)


@dataclass
class SignalHit:
    rule: SignalRule
    count: int
    worst: float
    samples: list[str]


def _parse_ts(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def scan_for_signals(
    log_path: Path | None = None,
    *,
    window_hours: float = WINDOW_HOURS,
    now: datetime | None = None,
    rules: tuple[SignalRule, ...] = RULES,
) -> list[SignalHit]:
    """Rules whose threshold was crossed inside the window. Pure read."""
    path = log_path or EVENTS_LOG
    if not path.is_file():
        return []
    now = now or datetime.now()
    # Widest window any rule needs, so a per-rule override longer than the
    # scan's own `window_hours` (e.g. #361's 24h rollback/drift rules vs the
    # 6h default) doesn't get pre-pruned before ever reaching that rule's own
    # check below.
    widest_hours = max([window_hours, *(r.window_hours for r in rules if r.window_hours)])
    cutoff = now - timedelta(hours=widest_hours)
    counts: Counter[str] = Counter()
    worst: dict[str, float] = {}
    samples: dict[str, list[str]] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = _parse_ts(rec.get("ts"))
                if stamp is None or stamp < cutoff:
                    continue
                name = str(rec.get("event") or "")
                for rule in rules:
                    if rule.event != name:
                        continue
                    if stamp < now - timedelta(hours=rule.window_hours or window_hours):
                        continue
                    if rule.ignore_field is not None and (
                        str(rec.get(rule.ignore_field) or "") in rule.ignore_values
                    ):
                        continue
                    if rule.field is not None:
                        try:
                            value = float(rec.get(rule.field) or 0)
                        except (TypeError, ValueError):
                            value = 0.0
                        if rule.field_min is not None and value < rule.field_min:
                            continue
                        worst[rule.key] = max(worst.get(rule.key, 0.0), value)
                    counts[rule.key] += 1
                    if len(samples.setdefault(rule.key, [])) < 5:
                        # Include the reason when the event carries one —
                        # an auto-filed issue that only says "×4" gives the
                        # reader nothing to act on (#331).
                        reason = str(rec.get("reason") or "")
                        samples[rule.key].append(
                            f"{stamp:%m-%d %H:%M} {name} {rec.get('role', '')}"
                            + (f" [{reason}]" if reason else "")
                        )
    except OSError:
        return []
    hits = [
        SignalHit(rule, counts[rule.key], worst.get(rule.key, 0.0), samples.get(rule.key, []))
        for rule in rules
        if counts[rule.key] >= rule.min_count
    ]
    return hits


def build_issue(hit: SignalHit, *, window_hours: float | None = None) -> tuple[str, str]:
    """`(title, body)` for one crossed rule. Counts and samples only — never
    the event payloads themselves, which can carry task text and paths.

    `window_hours=None` (the default) reports the rule's OWN window
    (`hit.rule.window_hours`) when it overrides the scan default — e.g.
    #361's 24h rules — rather than always stamping the scan's 6h default on
    every issue regardless of which window actually applied."""
    from . import __version__

    window_hours = (
        window_hours if window_hours is not None else (hit.rule.window_hours or WINDOW_HOURS)
    )
    title = f"[auto] {hit.rule.title} — {hit.rule.event} ×{hit.count}/{window_hours:.0f}h"
    lines = [
        "รายงานอัตโนมัติจากสัญญาณใน `events.log` ของ cockpit ที่รันอยู่ "
        "(ไม่ใช่ crash — ดู #297 ว่าทำไมถึงต้องมีทางนี้)",
        "",
        f"- event: `{hit.rule.event}`",
        f"- นับได้: **{hit.count}** ครั้งใน {window_hours:.0f} ชั่วโมง (เกณฑ์ ≥ {hit.rule.min_count})",
    ]
    if hit.rule.field and hit.worst:
        lines.append(f"- {hit.rule.field} สูงสุด: {hit.worst:.0f}")
    lines += [
        f"- เหตุผลของเกณฑ์: {hit.rule.why}",
        f"- version: {__version__}",
        "",
        "ตัวอย่างเวลาที่เกิด:",
        "```",
        *hit.samples,
        "```",
        "",
        "ดูรายละเอียดเต็มด้วย `takkub ma --since-hours 6` บนเครื่องที่เกิด",
    ]
    return title, "\n".join(lines)


def file_signal_issue(hit: SignalHit) -> None:
    """File one signal as an issue, through the same dedup/rate-cap as crashes."""
    from . import auto_issue_capture, issues

    if auto_issue_capture._auto_issue_suppressed() or not auto_issue_enabled():
        return
    if not auto_issue_capture.reserve_signature(f"signal:{hit.rule.key}"):
        return
    title, body = build_issue(hit)
    try:
        issues.new_issue(
            title,
            body,
            severity="med",
            tags=["auto-captured", "runtime-signal"],
            noticed_in="cockpit",
            cockpit_bug=True,
        )
    except Exception:
        pass


def run_scan_once(log_path: Path | None = None) -> list[SignalHit]:
    """One scan + file. Returns what fired (for tests and manual runs)."""
    if not auto_issue_enabled():
        return []
    hits = scan_for_signals(log_path)
    for hit in hits:
        file_signal_issue(hit)
    return hits


_watch_thread: threading.Thread | None = None
_stop = threading.Event()


def start_watch() -> None:
    """Begin periodic scanning on a daemon thread. Idempotent.

    Its own thread on purpose: reading the event log is file I/O, and doing
    that on the Qt main thread is the very defect this watcher reports (#291).
    """
    global _watch_thread
    if _watch_thread is not None and _watch_thread.is_alive():
        return
    if not auto_issue_enabled():
        return

    def _loop() -> None:
        if _stop.wait(FIRST_SCAN_DELAY_S):
            return
        while not _stop.is_set():
            try:
                run_scan_once()
            except Exception:
                pass
            if _stop.wait(SCAN_INTERVAL_S):
                return

    _stop.clear()
    _watch_thread = threading.Thread(target=_loop, name="auto-issue-signals", daemon=True)
    _watch_thread.start()


def stop_watch() -> None:
    _stop.set()


__all__ = [
    "RULES",
    "SignalHit",
    "SignalRule",
    "auto_issue_enabled",
    "build_issue",
    "file_signal_issue",
    "run_scan_once",
    "scan_for_signals",
    "set_auto_issue_enabled",
    "start_watch",
    "stop_watch",
]
