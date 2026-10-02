"""On-disk layout of learned skills — the ONLY module that touches skill dirs.

A learned skill is a plain native skill folder inside the cockpit's central
store (so claude/codex discovery and the AGENTS.md appendix keep working
unchanged), plus three hidden sidecars autoharness taught us to keep out of
the skill body:

    <root>/<name>/SKILL.md                  native format, frontmatter name+description
    <root>/<name>/.sidecar.json             managed marker + lifecycle counters
    <root>/<name>/.ledger.jsonl             append-only why-it-changed log
    <root>/<name>/references/evidence-*.md  redacted transcript slices
    <root>/.archive/<name>-<ts>/            archived (moved, never deleted)

`root` is `config.project_skills_dir(ns)` (layer "project") or
`config.global_skills_dir()` (layer "global"). Anything without a sidecar
whose `managed_by` is ours is invisible here — a user's own or an installed
skill is never read for mutation, renamed, archived or merged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from . import MANAGED_BY

LAYERS = ("project", "global")
SKILL_FILE = "SKILL.md"
SIDECAR = ".sidecar.json"
LEDGER = ".ledger.jsonl"
ARCHIVE_DIR = ".archive"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
GLOBAL_STATE_NS = "_global"


@dataclass(slots=True)
class SkillRecord:
    name: str
    layer: str
    dir: Path
    description: str = ""
    body: str = ""
    sidecar: dict = field(default_factory=dict)

    @property
    def category(self) -> str:
        return str(self.sidecar.get("category") or "general")

    @property
    def skill_file(self) -> Path:
        return self.dir / SKILL_FILE


# ── paths ────────────────────────────────────────────────────────────────


def skills_root(layer: str, project_ns: str) -> Path:
    if layer == "global":
        return config.global_skills_dir()
    return config.project_skills_dir(project_ns)


def state_dir(project_ns: str | None) -> Path:
    """Runtime state (counters, runs, pending proposals, snapshots) for one
    project, or the global layer when *project_ns* is falsy."""
    ns = project_ns or GLOBAL_STATE_NS
    if ns != GLOBAL_STATE_NS:
        ns = config.validate_name(ns, "project")
    d = Path(config.RUNTIME_DIR) / "skill-learning" / ns
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── atomic io ────────────────────────────────────────────────────────────


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


# ── SKILL.md ─────────────────────────────────────────────────────────────


def _yaml_scalar(text: str) -> str:
    # JSON strings are valid YAML double-quoted scalars — no escaping bugs.
    return json.dumps(" ".join(text.split()), ensure_ascii=False)


def render_skill_md(name: str, description: str, body: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {_yaml_scalar(description)}\n"
        "---\n\n" + body.strip() + "\n"
    )


def parse_skill_md(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}, text
    import yaml

    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), parts[2].strip()


# ── records ──────────────────────────────────────────────────────────────


def is_managed(skill_dir: Path) -> bool:
    side = read_json(skill_dir / SIDECAR, {})
    return side.get("managed_by") == MANAGED_BY


def load_record(skill_dir: Path, layer: str) -> SkillRecord | None:
    f = skill_dir / SKILL_FILE
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fm, body = parse_skill_md(text)
    desc = fm.get("description")
    return SkillRecord(
        name=skill_dir.name,
        layer=layer,
        dir=skill_dir,
        description=desc.strip() if isinstance(desc, str) else "",
        body=body,
        sidecar=read_json(skill_dir / SIDECAR, {}),
    )


def _iter_skill_dirs(root: Path):
    if not root.is_dir():
        return
    for d in sorted(root.iterdir()):
        if d.name.startswith(".") or not d.is_dir() or not (d / SKILL_FILE).is_file():
            continue
        yield d


def list_skills(project_ns: str, *, managed_only: bool = True) -> list[SkillRecord]:
    """Live skills of both layers (project first). `managed_only=False`
    also returns foreign skill NAMES (sidecar empty) so callers can avoid
    collisions — never use those records for mutation."""
    out: list[SkillRecord] = []
    for layer in LAYERS:
        try:
            root = skills_root(layer, project_ns)
        except ValueError:
            continue
        for d in _iter_skill_dirs(root):
            managed = is_managed(d)
            if managed_only and not managed:
                continue
            rec = load_record(d, layer)
            if rec is not None:
                if not managed:
                    rec.sidecar = {}
                out.append(rec)
    return out


def find_managed(project_ns: str, name: str) -> SkillRecord | None:
    for rec in list_skills(project_ns):
        if rec.name == name:
            return rec
    return None


def name_taken(project_ns: str, name: str) -> bool:
    """True when ANY skill (ours or not) already uses *name* in either layer."""
    for layer in LAYERS:
        try:
            if (skills_root(layer, project_ns) / name).exists():
                return True
        except ValueError:
            continue
    return False


# ── mutations ────────────────────────────────────────────────────────────


def append_ledger(skill_dir: Path, entry: dict) -> None:
    entry = {"ts": time.time(), **entry}
    with open(skill_dir / LEDGER, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_ledger(skill_dir: Path) -> list[dict]:
    try:
        lines = (skill_dir / LEDGER).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def write_evidence(skill_dir: Path, text: str) -> str:
    """Content-addressed evidence file; returns its path relative to the skill."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    rel = f"references/evidence-{digest}.md"
    target = skill_dir / rel
    if not target.exists():
        atomic_write_text(target, text)
    return rel


def create_skill(
    layer: str,
    project_ns: str,
    name: str,
    description: str,
    body: str,
    *,
    category: str,
    requests_now: int,
) -> SkillRecord:
    """Build the folder off to the side, then one `os.replace` makes it live —
    a crash never leaves a half-written skill where discovery can see it."""
    root = skills_root(layer, project_ns)
    root.mkdir(parents=True, exist_ok=True)
    final = root / name
    if final.exists():
        raise FileExistsError(str(final))
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{name}-", dir=str(root)))
    try:
        atomic_write_text(tmp / SKILL_FILE, render_skill_md(name, description, body))
        write_json(
            tmp / SIDECAR,
            {
                "managed_by": MANAGED_BY,
                "layer": layer,
                "category": category or "general",
                "created_at": time.time(),
                "requests_at_create": int(requests_now),
                "uses": 0,
                "views": 0,
                "patches": 0,
                "last_used": None,
            },
        )
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    rec = load_record(final, layer)
    assert rec is not None
    return rec


def rewrite_skill(rec: SkillRecord, *, description: str, body: str, category: str = "") -> None:
    atomic_write_text(rec.skill_file, render_skill_md(rec.name, description, body))
    side = dict(rec.sidecar)
    side["patches"] = int(side.get("patches") or 0) + 1
    if category:
        side["category"] = category
    write_json(rec.dir / SIDECAR, side)
    rec.description, rec.body, rec.sidecar = description, body, side


def update_sidecar(rec: SkillRecord, **changes) -> None:
    side = read_json(rec.dir / SIDECAR, {})
    if side.get("managed_by") != MANAGED_BY:
        return
    side.update(changes)
    write_json(rec.dir / SIDECAR, side)
    rec.sidecar = side


def archive_skill(rec: SkillRecord, *, reason: str, absorbed_by: str = "") -> Path:
    """Move the whole folder (ledger + evidence) out of discovery. Dangling
    links left in project `.claude/skills/` / provider homes are pruned by
    `skill_scan.ensure_project_skill_links` and `native_skills` on next spawn."""
    entry = {"action": "archive", "reason": reason}
    if absorbed_by:
        entry["absorbed_by"] = absorbed_by
    append_ledger(rec.dir, entry)
    arch_root = rec.dir.parent / ARCHIVE_DIR
    arch_root.mkdir(parents=True, exist_ok=True)
    dest = arch_root / f"{rec.name}-{time.strftime('%Y%m%d%H%M%S')}"
    n = 1
    while dest.exists():
        n += 1
        dest = arch_root / f"{rec.name}-{time.strftime('%Y%m%d%H%M%S')}-{n}"
    shutil.move(str(rec.dir), str(dest))
    return dest


def list_archived(project_ns: str) -> list[tuple[str, str, Path]]:
    """(name, layer, archived_dir), newest first per name."""
    out: list[tuple[str, str, Path]] = []
    for layer in LAYERS:
        try:
            arch = skills_root(layer, project_ns) / ARCHIVE_DIR
        except ValueError:
            continue
        if not arch.is_dir():
            continue
        for d in sorted(arch.iterdir(), reverse=True):
            if d.is_dir() and is_managed(d):
                name = re.sub(r"-\d{14}(?:-\d+)?$", "", d.name)
                out.append((name, layer, d))
    return out


def revive_skill(project_ns: str, name: str, *, requests_now: int) -> SkillRecord:
    for arch_name, layer, d in list_archived(project_ns):
        if arch_name != name:
            continue
        if name_taken(project_ns, name):
            raise FileExistsError(f"skill '{name}' already exists — rename or archive it first")
        dest = skills_root(layer, project_ns) / name
        shutil.move(str(d), str(dest))
        rec = load_record(dest, layer)
        assert rec is not None
        # Fresh probation: the clock restarts so a revived skill isn't
        # instantly re-archived for the requests it missed while archived.
        update_sidecar(rec, requests_at_create=int(requests_now), uses=0, views=0)
        append_ledger(dest, {"action": "revive", "reason": "manual revive"})
        return rec
    raise FileNotFoundError(f"no archived learned skill named '{name}'")


# ── request counters (usage-rate denominator) ─────────────────────────────


def requests(project_ns: str | None) -> int:
    return int(read_json(state_dir(project_ns) / "counters.json", {}).get("requests") or 0)


def tick_requests(project_ns: str) -> tuple[int, int]:
    """One done = one request in the project layer AND in the global layer
    (a global skill was available to every project's request)."""
    out = []
    for ns in (project_ns, None):
        p = state_dir(ns) / "counters.json"
        data = read_json(p, {})
        data["requests"] = int(data.get("requests") or 0) + 1
        write_json(p, data)
        out.append(data["requests"])
    return out[0], out[1]
