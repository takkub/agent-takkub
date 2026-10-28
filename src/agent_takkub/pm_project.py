"""The PM project: a cockpit tab whose Lead pane is the owner's project
manager across every project (docs/pm/role.md).

It is an ordinary registered project on purpose — tab, pane, resume, and the
Lead provider choice (claude/codex/gemini…) all come for free — with three
differences keyed off `is_pm_project`:

- its Lead reads docs/pm/role.md instead of the Lead playbook
  (`lead_context._build_lead_context_text`, `lead_readiness._sources`);
- it has no team: every non-Lead spawn is refused (`team_preset`);
- `pm_report` leaves it out of the cross-project summary.

Its folder lives under the runtime dir, never inside a user repo.
"""

from __future__ import annotations

from pathlib import Path

PM_PROJECT = "PM"
PM_KIND = "pm"


def pm_dir() -> Path:
    from .config import RUNTIME_DIR

    return RUNTIME_DIR / "pm"


def is_pm_project(project: str | None) -> bool:
    if not project:
        return False
    if project == PM_PROJECT:
        return True
    try:
        from .config import _project_dict

        return _project_dict(project).get("kind") == PM_KIND
    except Exception:
        return False


def role_doc_path() -> Path:
    from .config import ASSETS_ROOT

    candidates = (
        ASSETS_ROOT / "docs" / "pm" / "role.md",
        Path(__file__).resolve().parent / "_assets" / "docs" / "pm" / "role.md",
        Path(__file__).resolve().parents[2] / "docs" / "pm" / "role.md",
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def role_text() -> str:
    """The PM pane's whole role prompt, with the runtime path filled in."""
    from .config import RUNTIME_DIR

    path = role_doc_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"PM role doc is missing: {path}") from exc
    return text.replace("<runtime>", RUNTIME_DIR.as_posix())


def ensure_pm_project() -> str:
    """Register the PM project (idempotent) and create its folder. Returns
    its name. Raises RuntimeError when the project store can't be written."""
    from . import config

    folder = pm_dir()
    folder.mkdir(parents=True, exist_ok=True)
    data = config.load_projects()
    projects = data.setdefault("projects", {})
    entry = projects.get(PM_PROJECT)
    want_paths = {"main": folder.as_posix()}
    if (
        isinstance(entry, dict)
        and entry.get("kind") == PM_KIND
        and entry.get("paths") == want_paths
    ):
        return PM_PROJECT
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry.update(
        {
            "description": "PM — ภาพรวมทุกโปรเจค (อ่านอย่างเดียว)",
            "paths": want_paths,
            "kind": PM_KIND,
        }
    )
    projects[PM_PROJECT] = entry
    if not config.save_projects_json(data):
        raise RuntimeError("บันทึกโปรเจค PM ไม่ได้ (project store อ่าน/เขียนไม่ได้)")
    return PM_PROJECT
