"""Terminal renderer for `boot_flow.py` (#574) — the same 5-screen flow the
Qt boot splash shows (mockup: provider-update choice, pre-migrate backup
plan, progress, success, failure), rendered as plain text for `takkub
migrate run` and headless boot. Text/formatting only; every decision is
still `boot_flow.py`'s (and, underneath it, `auto_migrate_boot.py`'s).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace

from . import boot_flow

_BAR_WIDTH = 20


def _bar(percent: float) -> str:
    filled = round(_BAR_WIDTH * max(0.0, min(100.0, percent)) / 100.0)
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _fmt_eta(eta_s: float | None) -> str:
    if eta_s is None:
        return ""
    minutes = int(eta_s) // 60
    if minutes <= 0:
        return "เหลือไม่ถึงนาที"
    return f"เหลือ ~{minutes} นาที"


def format_progress_line(event: boot_flow.ProgressEvent) -> str:
    """One `\\r`-refreshable line (screen C) — the mockup's own format:
    ``[████░░░░░░] 34% · ขั้น 2/5 คัดลอกขึ้นโครงใหม่ 3/9 (120/5000 ไฟล์) · เหลือ ~2 นาที``.

    #574 round11 item 3: `files_done`/`files_total` (progress WITHIN one
    large directory entry, from `on_file_progress`) read defensively via
    `getattr` — they were added to `ProgressEvent` after the dataclass
    first shipped, and a caller building its own `ProgressEvent` by hand
    (a test, an older serialized event) may not carry them at all."""
    step_part = f"ขั้น {event.phase}/{event.phases_total} {event.phase_label}"
    if event.done is not None and event.total:
        # #574 round12 (R3-M4): `unit` reads defensively (`getattr`) —
        # a hand-built/older event may predate the field, same convention
        # as `files_done`/`files_total` below.
        unit = getattr(event, "unit", "รายการ")
        step_part += f" {event.done}/{event.total} {unit}"
    files_done = getattr(event, "files_done", None)
    files_total = getattr(event, "files_total", None)
    if files_done is not None and files_total:
        step_part += f" ({files_done}/{files_total} ไฟล์)"
    parts = [f"[{_bar(event.percent_overall)}] {event.percent_overall:.0f}%", step_part]
    eta = _fmt_eta(event.eta_s)
    if eta:
        parts.append(eta)
    return " · ".join(parts)


def format_provider_menu(items: list[boot_flow.ProviderUpdateItem]) -> str:
    """Screen A as a numbered menu."""
    lines = ["พบอัพเดตของ provider — เลือกได้ว่าจะอัพเดตตอนนี้หรือใช้เวอร์ชันเดิมต่อ", ""]
    status_labels = {
        boot_flow.PROVIDER_STATUS_UP_TO_DATE: "ล่าสุดแล้ว",
        boot_flow.PROVIDER_STATUS_UPDATE_AVAILABLE: "มีอัพเดต",
        boot_flow.PROVIDER_STATUS_NOT_INSTALLED: "ไม่ได้ติดตั้ง",
        boot_flow.PROVIDER_STATUS_DISABLED: "ปิดใช้งาน",
        boot_flow.PROVIDER_STATUS_NO_MECHANISM: "ตรวจอัตโนมัติไม่ได้",
        boot_flow.PROVIDER_STATUS_FAILED: "ตรวจสอบไม่สำเร็จ",
    }
    for i, item in enumerate(items, start=1):
        mark = "[x]" if item.selected else "[ ]"
        version = f"{item.current or '?'} → {item.latest}" if item.latest else (item.current or "")
        label = status_labels.get(item.status, item.status)
        lines.append(f"  {i}. {mark} {item.label:<16} {version:<20} {label}")
    lines.append("")
    lines.append("เลือกได้เสมอ — ข้ามได้, provider เวอร์ชันเดิมยังใช้งานได้ตามปกติ")
    return "\n".join(lines)


def format_plan_summary(plan: boot_flow.MigrationPlanSummary) -> str:
    """Screen B."""
    lines = ["เวอร์ชันใหม่ใช้โครงสร้างข้อมูลใหม่ — ต้องย้ายข้อมูลครั้งเดียวก่อนเปิดใช้งาน", ""]
    lines.append("จะสำรองข้อมูลก่อนย้าย:")
    for label, count, size, unit in plan.backup_items:
        lines.append(f"  - {label}: {count:,} {unit} ({_fmt_bytes(size)})")
    lines.append("")
    lines.append(f"ที่เก็บสำรอง: {plan.backup_dir}")
    lines.append(
        f"ขนาดโดยประมาณ: {_fmt_bytes(plan.estimated_bytes)} · ว่างในดิสก์ {_fmt_bytes(plan.free_bytes)}"
    )
    lines.append("ย้อนกลับได้ทุกเมื่อ — takkub migrate restore-v1")
    lines.append("")
    lines.append("ระหว่างย้าย จะคัดลอกก่อนเสมอ และลบของเก่าเฉพาะหลังตรวจสอบครบทุกรายการ")
    return "\n".join(lines)


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_outcome(outcome: boot_flow.MigrationOutcome) -> str:
    """Screen D (success) or E (failure)."""
    if outcome.ok:
        lines = [
            f"ย้ายข้อมูลเสร็จแล้ว — ใช้เวลา {outcome.duration_s:.0f} วินาที",
            f"ย้ายขึ้นโครงใหม่: {len(outcome.promoted)} รายการ",
            f"เก็บของเก่าเข้า archive: {len(outcome.archived)} รายการ"
            + (f" · ลบไฟล์ขยะ {outcome.junk_deleted} รายการ" if outcome.junk_deleted else ""),
            f"โปรเจค: {outcome.projects_count} โปรเจค",
        ]
        if outcome.backup_dir is not None:
            lines.append(f"สำรองก่อนย้าย: {outcome.backup_dir}")
        if outcome.archive_dir is not None:
            lines.append(f"archive ของเก่า: {outcome.archive_dir}")
        return "\n".join(lines)

    lines = [f"ย้ายข้อมูลไม่สำเร็จ{' — ย้อนกลับอัตโนมัติแล้ว' if outcome.rolled_back else ''}"]
    if outcome.failed_step:
        step_pos = (
            f" ({outcome.failed_step_index}/{outcome.failed_step_total})"
            if outcome.failed_step_index and outcome.failed_step_total
            else ""
        )
        lines.append(
            f"ขั้นที่ {outcome.failed_phase or '?'}{step_pos} ({outcome.failed_step}) "
            f"ไม่ผ่าน: {outcome.error or ''}"
        )
    elif outcome.error:
        lines.append(outcome.error)
    lines.append(
        "สถานะข้อมูล: เหมือนก่อนเริ่มทุกไบต์" if outcome.data_intact else "สถานะข้อมูล: ต้องตรวจด้วยมือ"
    )
    if outcome.backup_dir is not None:
        lines.append(f"สำรองก่อนย้าย: {outcome.backup_dir}")
    if outcome.log_paths:
        lines.append("log: " + " · ".join(str(p) for p in outcome.log_paths))
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# CLI entry point — `takkub migrate run`
# ─────────────────────────────────────────────────────────────────────


def _parse_selected(
    csv: str, items: list[boot_flow.ProviderUpdateItem]
) -> list[boot_flow.ProviderUpdateItem]:
    wanted = {n.strip() for n in csv.split(",") if n.strip()}
    return [replace(it, selected=(it.name in wanted)) for it in items]


def _apply_remembered_choice(
    items: list[boot_flow.ProviderUpdateItem], remembered: dict
) -> list[boot_flow.ProviderUpdateItem]:
    mode = remembered.get("mode")
    if mode == "skip":
        return [replace(it, selected=False) for it in items]
    if mode == "update_all":
        return [
            replace(it, selected=(it.status == boot_flow.PROVIDER_STATUS_UPDATE_AVAILABLE))
            for it in items
        ]
    if mode == "selected":
        wanted = set(remembered.get("selected", []))
        return [replace(it, selected=(it.name in wanted)) for it in items]
    return items  # mode == "ask" (or malformed) — never applied, caller still prompts


def _prompt_provider_choices(
    items: list[boot_flow.ProviderUpdateItem], out
) -> list[boot_flow.ProviderUpdateItem]:
    """#574 round11 R7-M3: `--providers ask` (the default) must actually
    ask — one provider at a time, `y`/`n`/`all`/`none` — never silently
    run whatever `check_provider_updates()` happened to pre-select."""
    result: list[boot_flow.ProviderUpdateItem] = []
    for index, it in enumerate(items):
        if it.status != boot_flow.PROVIDER_STATUS_UPDATE_AVAILABLE:
            result.append(replace(it, selected=False))
            continue
        while True:
            answer = (
                input(f"  {it.label}: {it.current} → {it.latest} — อัพเดตไหม? [y/n/all/none] ")
                .strip()
                .lower()
            )
            if answer in ("y", "yes"):
                result.append(replace(it, selected=True))
                break
            if answer in ("n", "no", ""):
                result.append(replace(it, selected=False))
                break
            if answer == "all":
                result.append(replace(it, selected=True))
                result.extend(
                    replace(
                        later, selected=(later.status == boot_flow.PROVIDER_STATUS_UPDATE_AVAILABLE)
                    )
                    for later in items[index + 1 :]
                )
                return result
            if answer == "none":
                result.append(replace(it, selected=False))
                result.extend(replace(later, selected=False) for later in items[index + 1 :])
                return result
            print("  กรุณาตอบ y, n, all, หรือ none", file=out)
    return result


def run_cli(argv: list[str] | None = None, *, out=None) -> int:
    """`takkub migrate run` — interactive by default; `--json` emits one
    JSON object per line (provider rows, then progress events, then the
    final outcome) for automation instead of the human-readable screens."""
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="takkub migrate run", add_help=True)
    parser.add_argument("--providers", default="ask", help="ask|all|none|<csv of names>")
    parser.add_argument("--remember", action="store_true")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompts")
    parser.add_argument(
        "--json", action="store_true", help="emit JSON lines instead of text screens"
    )
    args = parser.parse_args(argv)

    def emit(obj: dict) -> None:
        if args.json:
            print(json.dumps(obj, default=str), file=out)

    # #574 round11 R7-L1: `--json` implies `--yes` (a JSON consumer has no
    # human to answer the confirm prompt) — record that this run's
    # confirmation was auto-granted rather than silently treating it the
    # same as an explicit `--yes`.
    auto_confirmed = args.json and not args.yes
    if auto_confirmed:
        emit({"type": "auto_confirmed", "reason": "--json implies --yes (no prompt possible)"})

    # --- Screen A: provider updates ---
    items = boot_flow.check_provider_updates()
    remembered = boot_flow.remembered_provider_choice()
    selection_mode: str | None = None
    if args.providers == "none":
        items = [replace(it, selected=False) for it in items]
        selection_mode = "skip"
    elif args.providers == "all":
        items = [
            replace(it, selected=(it.status == boot_flow.PROVIDER_STATUS_UPDATE_AVAILABLE))
            for it in items
        ]
        selection_mode = "update_all"
    elif args.providers != "ask":
        items = _parse_selected(args.providers, items)
        selection_mode = "selected"
    elif remembered is not None and remembered.get("mode") != "ask":
        # #574 round11 R7-M4: a remembered non-"ask" choice from a prior
        # run is honored on the default `ask` path — never re-prompt for
        # something the user already asked to remember.
        items = _apply_remembered_choice(items, remembered)
        selection_mode = remembered.get("mode")
        if not args.json:
            print(f"ใช้ตัวเลือก provider ที่จำไว้ก่อนหน้า: {selection_mode}", file=out)
    else:
        # Genuinely "ask", nothing remembered (or the remembered choice was
        # itself "always ask") — a non-interactive caller (`--json`, or no
        # real TTY) has no one to answer a per-provider prompt: interpret
        # that as "none" rather than either hanging on `input()` or
        # silently running whatever `check_provider_updates()` pre-selected
        # (the R7-M3 bug this whole branch exists to fix).
        if args.json or not sys.stdin.isatty():
            items = [replace(it, selected=False) for it in items]
            selection_mode = "skip"
            emit(
                {
                    "type": "provider_prompt_skipped",
                    "reason": "non-interactive (--json or no tty)",
                    "resolved_as": "none",
                }
            )
        else:
            items = _prompt_provider_choices(items, out)
            selection_mode = "selected"

    if args.json:
        for it in items:
            emit({"type": "provider", **asdict(it)})
    else:
        print(format_provider_menu(items), file=out)

    if any(it.selected for it in items):
        items = boot_flow.run_provider_updates(
            items,
            progress_cb=(lambda it: emit({"type": "provider_update", **asdict(it)}))
            if args.json
            else None,
        )

    # #574 round11 R7-M4: persist the choice for EVERY resolved mode
    # (skip/update_all/selected), not only when something ended up
    # selected — `--providers none --remember` must actually stick.
    if args.remember and selection_mode is not None:
        boot_flow.remember_provider_choice(
            {
                "mode": selection_mode,
                "selected": [it.name for it in items if it.selected],
            }
        )

    # --- Screens B/C/D/E: migration ---
    plan = boot_flow.plan_migration()
    if plan is not None:
        if args.json:
            emit(
                {
                    "type": "plan",
                    "backup_dir": str(plan.backup_dir),
                    "promote_items": plan.promote_items,
                    "archive_items": plan.archive_items,
                    "estimated_bytes": plan.estimated_bytes,
                    "free_bytes": plan.free_bytes,
                }
            )
        else:
            print(file=out)
            print(format_plan_summary(plan), file=out)
        if not args.yes and not args.json:
            answer = input("สำรองข้อมูลแล้วเริ่มย้าย? [Y/n] ").strip().lower()
            if answer not in ("", "y", "yes"):
                print("ยกเลิก — ไม่มีการเปลี่ยนแปลงใดๆ", file=out)
                return 1

    last_line_len = 0

    def on_progress(event: boot_flow.ProgressEvent) -> None:
        nonlocal last_line_len
        if args.json:
            emit({"type": "progress", **asdict(event)})
            return
        line = format_progress_line(event)
        pad = max(0, last_line_len - len(line))
        print("\r" + line + (" " * pad), end="", file=out, flush=True)
        last_line_len = len(line)

    outcome = boot_flow.run_migration(progress_cb=on_progress if plan is not None else None)
    if not args.json and plan is not None:
        print(file=out)  # move past the \r progress line

    if args.json:
        emit({"type": "outcome", **asdict(outcome)})
    else:
        print(format_outcome(outcome), file=out)

    return 0 if outcome.ok else 1


def main(argv: list[str] | None = None) -> int:
    return run_cli(argv)


__all__ = [
    "format_outcome",
    "format_plan_summary",
    "format_progress_line",
    "format_provider_menu",
    "main",
    "run_cli",
]
