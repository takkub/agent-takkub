"""vault_graph — analyse decision-note logs from the Obsidian vault sessions.

Parses session .md files written by ``_save_decision_note`` (vault_mirror.py)
under ``<vault>/01-Projects/<project>/sessions/`` and produces a project-health
report: decision chains, blocker frequency, role trend, and an overall health
score.

Usage
-----
    python -m agent_takkub.vault_graph <project> [--date YYYY-MM-DD]
    → writes docs/vault-graph/<YYYY-MM-DD>-<project>.md, prints the path
    → prints a warning and exits 0 when no vault / no sessions found

Importable
----------
    from agent_takkub.vault_graph import analyse
    path = analyse("agent-takkub")          # returns Path or None
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

import yaml

from .config import REPO_ROOT
from .vault_mirror import _resolve_vault_dir

# ── blocker detection ────────────────────────────────────────────────────────
# English keywords use \b; Thai keywords don't use \b (Thai has no ASCII word boundaries)
_BLOCKER_EN = re.compile(
    r"\b(block|blocked|blocking|fail|failed|error|crash|broken|cannot|can't|can not|reject|rejected)\b",
    re.IGNORECASE,
)
_BLOCKER_TH = re.compile(r"ติด|ค้าง|ล้มเหลว|ไม่ผ่าน")


# ── data model ───────────────────────────────────────────────────────────────
@dataclass
class SessionEntry:
    role: str
    project: str
    timestamp: datetime
    note: str
    filename: str

    @property
    def is_blocker(self) -> bool:
        return bool(_BLOCKER_EN.search(self.note) or _BLOCKER_TH.search(self.note))

    @property
    def note_preview(self) -> str:
        """First 80 chars of note, stripped."""
        text = self.note.strip()
        if len(text) <= 80:
            return text
        return text[:77] + "..."


@dataclass
class GraphReport:
    project: str
    generated_at: datetime
    entries: list[SessionEntry] = field(default_factory=list)
    date_filter: date | None = None

    @property
    def filtered(self) -> list[SessionEntry]:
        if self.date_filter is None:
            return self.entries
        return [e for e in self.entries if e.timestamp.date() == self.date_filter]


# ── parsing helpers ───────────────────────────────────────────────────────────
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML front matter from body. Returns ({}, text) on parse failure."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                fm = {}
            return (fm if isinstance(fm, dict) else {}), parts[2].lstrip("\n")
    return {}, text


def _extract_note_text(body: str) -> str:
    """Pull the first non-empty line under the '## Note' section."""
    marker = "## Note"
    idx = body.find(marker)
    if idx < 0:
        return ""
    tail = body[idx + len(marker) :].strip()
    if not tail:
        return ""
    # Return all lines up to the next ## section
    lines: list[str] = []
    for line in tail.splitlines():
        if line.startswith("## "):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _parse_session_file(path: pathlib.Path) -> SessionEntry | None:
    """Parse one session .md file; returns None on any parse failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    fm, body = _parse_frontmatter(text)

    # Role: from frontmatter; fall back to filename stem pattern
    role = str(fm.get("role", "")).strip()
    project = str(fm.get("project", "")).strip()

    # Timestamp: prefer frontmatter `date`, fall back to filename
    ts: datetime | None = None
    raw_date = fm.get("date")
    if raw_date:
        if isinstance(raw_date, datetime):
            ts = raw_date
        else:
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
                try:
                    ts = datetime.strptime(str(raw_date).strip(), fmt)
                    break
                except ValueError:
                    continue

    # Fall back: parse filename like "2026-06-09T143022-backend.md"
    stem = path.stem  # "2026-06-09T143022-backend"
    if ts is None or not role:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{6})-(.+)$", stem)
        if m:
            date_str, time_str, role_from_name = m.groups()
            if ts is None:
                try:
                    ts = datetime.strptime(f"{date_str}T{time_str}", "%Y-%m-%dT%H%M%S")
                except ValueError:
                    pass
            if not role:
                role = role_from_name

    if not role or ts is None:
        return None

    note = _extract_note_text(body)
    return SessionEntry(
        role=role,
        project=project or path.parent.parent.name,
        timestamp=ts,
        note=note,
        filename=path.name,
    )


# ── session loader ────────────────────────────────────────────────────────────
def load_sessions(project: str, vault: pathlib.Path) -> list[SessionEntry]:
    """Load all session .md files for *project* from the vault mirror."""
    sessions_dir = vault / "01-Projects" / project / "sessions"
    if not sessions_dir.is_dir():
        return []

    entries: list[SessionEntry] = []
    for path in sorted(sessions_dir.glob("*.md")):
        entry = _parse_session_file(path)
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda e: e.timestamp)
    return entries


# ── analysis ──────────────────────────────────────────────────────────────────
def _analyse_role_trend(entries: list[SessionEntry]) -> list[dict]:
    """Count sessions, avg note length, and last-active per role."""
    by_role: dict[str, list[SessionEntry]] = defaultdict(list)
    for e in entries:
        by_role[e.role].append(e)

    rows = []
    for role, es in sorted(by_role.items()):
        note_lens = [len(e.note) for e in es if e.note]
        avg_len = int(sum(note_lens) / len(note_lens)) if note_lens else 0
        last = max(es, key=lambda x: x.timestamp)
        rows.append(
            {
                "role": role,
                "count": len(es),
                "avg_note_len": avg_len,
                "last_active": last.timestamp.strftime("%Y-%m-%d"),
            }
        )
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


def _analyse_blockers(entries: list[SessionEntry]) -> list[dict]:
    """Return blocker events grouped by role with example notes."""
    blockers: dict[str, list[SessionEntry]] = defaultdict(list)
    for e in entries:
        if e.is_blocker:
            blockers[e.role].append(e)

    rows = []
    for role, es in sorted(blockers.items(), key=lambda kv: -len(kv[1])):
        examples = [e.note_preview for e in es[:2]]
        rows.append({"role": role, "count": len(es), "examples": examples})
    return rows


def _build_decision_chains(entries: list[SessionEntry]) -> list[tuple[str, list[SessionEntry]]]:
    """Group entries by calendar date, most-recent first."""
    by_date: dict[str, list[SessionEntry]] = defaultdict(list)
    for e in entries:
        day = e.timestamp.strftime("%Y-%m-%d")
        by_date[day].append(e)

    return sorted(by_date.items(), key=lambda kv: kv[0], reverse=True)


# ── report renderer ───────────────────────────────────────────────────────────
def _render_report(report: GraphReport) -> str:
    entries = report.filtered
    total = len(entries)
    project = report.project
    gen = report.generated_at.strftime("%Y-%m-%dT%H:%M:%S")

    if not entries:
        return (
            f"---\nproject: {project}\ngenerated: {gen}\nsessions_analysed: 0\n---\n\n"
            f"# Vault Decision Graph · {project}\n\n"
            f"_ไม่พบ session logs สำหรับ project นี้_\n"
        )

    date_range_start = min(e.timestamp for e in entries).strftime("%Y-%m-%d")
    date_range_end = max(e.timestamp for e in entries).strftime("%Y-%m-%d")
    blocker_count = sum(1 for e in entries if e.is_blocker)
    blocker_pct = f"{blocker_count / total * 100:.1f}%" if total else "0%"

    role_rows = _analyse_role_trend(entries)
    most_active = role_rows[0]["role"] if role_rows else "—"
    most_active_count = role_rows[0]["count"] if role_rows else 0

    blocker_rows = _analyse_blockers(entries)
    chains = _build_decision_chains(entries)

    lines: list[str] = [
        "---",
        f"project: {project}",
        f"generated: {gen}",
        f"sessions_analysed: {total}",
        f"date_range: {date_range_start} → {date_range_end}",
        "---",
        "",
        f"# Vault Decision Graph · {project}",
        "",
        f"Generated: {gen} · {total} sessions analysed",
        "",
        "## Project Health",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total sessions | {total} |",
        f"| Blocker events | {blocker_count} ({blocker_pct}) |",
        f"| Most active role | {most_active} ({most_active_count} sessions) |",
        f"| Date range | {date_range_start} → {date_range_end} |",
        "",
    ]

    # ── Role Trend ──
    lines += [
        "## Role Trend",
        "",
        "| Role | Sessions | Avg Note Length | Last Active |",
        "|---|---|---|---|",
    ]
    for row in role_rows:
        lines.append(
            f"| {row['role']} | {row['count']} | {row['avg_note_len']} chars"
            f" | {row['last_active']} |"
        )
    lines.append("")

    # ── Blocker Frequency ──
    lines += ["## Blocker Frequency", ""]
    if blocker_rows:
        lines += [
            "| Role | Blockers | Examples |",
            "|---|---|---|",
        ]
        for row in blocker_rows:
            examples_str = "; ".join(f'"{ex}"' for ex in row["examples"])
            lines.append(f"| {row['role']} | {row['count']} | {examples_str} |")
    else:
        lines.append("_ไม่พบ blocker events_")
    lines.append("")

    # ── Decision Chain ──
    lines += ["## Decision Chain", ""]
    for day, day_entries in chains:
        lines.append(f"### {day}")
        lines.append("")
        for i, e in enumerate(day_entries, 1):
            time_str = e.timestamp.strftime("%H:%M")
            note_preview = e.note_preview or "_(no note)_"
            blocker_tag = " ⚠ blocker" if e.is_blocker else ""
            lines.append(f"{i}. **{e.role}** ({time_str}){blocker_tag} — {note_preview}")
        lines.append("")

    return "\n".join(lines)


# ── public API ────────────────────────────────────────────────────────────────
def analyse(
    project: str,
    date_str: str | None = None,
    vault: pathlib.Path | None = None,
) -> pathlib.Path | None:
    """Parse vault sessions for *project*, write a markdown report, and return
    its path. Returns None when no vault is configured or sessions dir is empty.

    Parameters
    ----------
    project:
        Project name as used in ``01-Projects/<project>/sessions/``.
    date_str:
        Optional ``YYYY-MM-DD`` to filter entries to a single day.
    vault:
        Override vault root (used in tests). Uses ``_resolve_vault_dir`` when None.
    """
    if vault is None:
        vault = _resolve_vault_dir()
    if vault is None:
        print(
            "vault_graph: ไม่พบ Obsidian vault (set $TAKKUB_VAULT_DIR หรือ ~/WebstormProjects/second-brain)"
        )
        return None

    entries = load_sessions(project, vault)

    date_filter: date | None = None
    if date_str:
        try:
            date_filter = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            print(f"vault_graph: รูปแบบ date ไม่ถูกต้อง: {date_str!r} (ต้องเป็น YYYY-MM-DD)")
            return None

    now = datetime.now()
    report = GraphReport(
        project=project,
        generated_at=now,
        entries=entries,
        date_filter=date_filter,
    )

    if not report.filtered:
        label = f"date={date_str}" if date_str else "ทุก date"
        print(f"vault_graph: ไม่พบ session logs สำหรับ project={project!r} ({label})")

    md = _render_report(report)

    day_str = (date_filter or now.date()).strftime("%Y-%m-%d")
    out_dir = REPO_ROOT / "docs" / "vault-graph"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{day_str}-{project}.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"OK {out_path}")
    return out_path


# ── CLI entry point ───────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_takkub.vault_graph",
        description="Analyse Obsidian vault session logs for a takkub project.",
    )
    parser.add_argument("project", help="Project name (e.g. agent-takkub)")
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        default=None,
        help="Filter to a specific date (default: all dates)",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    result = analyse(args.project, date_str=args.date)
    return 0 if result is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
