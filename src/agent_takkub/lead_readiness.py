"""Session-bound evidence that the complete active Lead policy was delivered.

All providers share the CLI/orchestrator gate. Claude additionally has native
tool hooks; other providers' unobserved native tools remain unsupported.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

REQUIRED_DOCS = ("role-and-workflow.md", "team-presets.md", "task-brief.md", "impact-plan.md")
ENV_FILE = "TAKKUB_LEAD_READINESS_FILE"


def _write(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _sources(project: str, cwd: str) -> tuple[str, dict, dict]:
    from . import config, lead_context, team_preset

    paths = []
    claude_md = lead_context.ASSETS_ROOT / "CLAUDE.md"
    if not claude_md.exists():
        repo_claude = Path(__file__).resolve().parents[2] / "CLAUDE.md"
        if repo_claude.exists():
            claude_md = repo_claude
    if claude_md.exists():
        paths.append(claude_md)

    for doc in REQUIRED_DOCS:
        candidate = lead_context.ASSETS_ROOT / "docs" / "lead" / doc
        if not candidate.exists():
            pkg_fallback = Path(__file__).resolve().parent / "_assets" / "docs" / "lead" / doc
            repo_fallback = Path(__file__).resolve().parents[2] / "docs" / "lead" / doc
            if pkg_fallback.exists():
                candidate = pkg_fallback
            elif repo_fallback.exists():
                candidate = repo_fallback
        if candidate.exists():
            paths.append(candidate)
        else:
            raise ValueError(f"required Lead context is missing: {candidate}")

    documents = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"required Lead context is empty: {path}")
        documents[str(path)] = text
    # Project-owned policy also belongs to the current revision. A generated
    # AGENTS.md is an output of this policy, so it must not hash itself.
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = Path(cwd) / name
        if path.exists() and str(path) not in documents:
            text = path.read_text(encoding="utf-8")
            if not text.startswith("<!-- takkub"):
                documents[str(path)] = text
    settings_path = team_preset.path(project)
    if settings_path.exists():
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("preset") not in team_preset.PRESET_IDS:
            raise ValueError("cannot establish the saved team preset")
        if raw.get("override") is not None and raw["override"] not in team_preset.PRESET_IDS:
            raise ValueError("cannot establish the active team override")
    cfg = team_preset.current(project)
    if cfg.get("preset") not in team_preset.PRESET_IDS or not isinstance(cfg.get("roles"), dict):
        raise ValueError("cannot establish the effective team policy")
    facts = {
        "project": project,
        "cwd": cwd,
        "project_config": config._project_dict(project),
        "team": cfg,
    }
    revision = hashlib.sha256(
        json.dumps(
            {"documents": documents, "facts": facts}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    return revision, documents, facts


def _policy_block(facts: dict) -> str:
    policy = facts["team"]
    if policy["preset"] == "auto":
        rule = "Run takkub team suggest for this task; explain and honor the routing/size decision."
    elif policy["lead_may_implement"]:
        rule = "Lead may implement and self-verify. Honor the configured checker and disabled development positions."
    else:
        rule = "Delegate implementation/deployment to enabled positions through takkub assign --mode pane."
    return (
        "## Effective session policy (authoritative over generic delegation defaults)\n"
        + json.dumps(facts, ensure_ascii=False, indent=2)
        + f"\n{rule}\nDo not change settings or invent a team override to bypass policy.\n"
        "Native subagents require explicit permitted routing; they do not substitute for requested visible positions.\n"
        "Before substantive work, run `takkub context read`. Repeat after resume, compaction, failover or policy changes.\n"
        "Docker reports distinguish host checks, image build, container recreation and observed health.\n"
    )


def policy_docs_text(documents: dict) -> str:
    """The `## Required policy` sections. The rendered Lead context already
    opens with the cockpit CLAUDE.md (#854): it stays in the revision hash,
    but is not repeated here."""
    from .lead_context import ASSETS_ROOT

    cockpit_md = str(ASSETS_ROOT / "CLAUDE.md")
    return "".join(
        f"\n## Required policy: {path}\n\n{text}\n"
        for path, text in documents.items()
        if path != cockpit_md
    )


def _context(project: str, cwd: str, documents: dict, facts: dict) -> str:
    from .lead_context import _build_lead_context_text

    rendered = _build_lead_context_text(project, claude_cwd=cwd)
    if not rendered:
        raise ValueError("active Lead context could not be rendered")
    # #903: the effective policy (~1 KB) goes FIRST — it is the part Lead acts
    # on, and at the tail of a 60 KB read it sat behind a persisted-output cut.
    return "\n".join([_policy_block(facts), rendered, policy_docs_text(documents)])


def _compact_context(facts: dict, revision: str) -> str:
    """#903 / backlog a1ceeaae: what a read prints when this session's system
    prompt already carries the whole policy at the current revision."""
    return (
        _policy_block(facts) + "\n## Lead context + Required policy docs\n"
        f"อยู่ใน system prompt ของ session นี้แล้วตั้งแต่ spawn (revision {revision[:12]}) "
        "และยังไม่เปลี่ยน — คงอยู่หลัง compaction จึงไม่พิมพ์ซ้ำ. "
        "ต้องการเนื้อหาเต็ม: `takkub context read --full`. "
        "policy เปลี่ยนเมื่อไร read ครั้งถัดไปจะพิมพ์เต็มเอง.\n"
    )


def docs_for_system_prompt(path: str | Path) -> tuple[str, str]:
    """``(policy_docs_text, revision)`` to append to a system prompt that
    survives compaction (claude's `--append-system-prompt-file`). Call
    `mark_embedded` with that revision only once the prompt file is
    actually written (#903)."""
    state = read_state(path)
    revision, documents, _facts = _sources(state["project"], state["cwd"])
    return policy_docs_text(documents), revision


def mark_embedded(path: str | Path, revision: str) -> None:
    """Record that this session's system prompt carries the whole policy at
    *revision*; a read at that same revision prints `_compact_context`."""
    state = read_state(path)
    state["embedded_revision"] = revision
    _write(Path(path), state)


def prepare(project: str, provider: str, cwd: str) -> Path:
    from .config import RUNTIME_DIR

    revision, documents, facts = _sources(project, cwd)
    text = _context(project, cwd, documents, facts)
    path = RUNTIME_DIR / "lead-readiness" / f"{uuid.uuid4().hex}.json"
    _write(
        path,
        {
            "session": path.stem,
            "project": project,
            "provider": provider,
            "cwd": cwd,
            "revision": revision,
            "text": text,
            "epoch": 0,
            "read_epoch": None,
            "embedded_revision": None,
        },
    )
    return path


def read_state(path: str | Path) -> dict:
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not state.get("session") or not state.get("text"):
        raise ValueError("missing or partial Lead readiness record")
    return state


def _enforcement_path(session: str) -> Path:
    # Deliberately NOT under `lead-readiness/`: a sandboxed Lead must write its
    # readiness acknowledgement there, so it must not be able to forge the
    # record of which boundary it runs under (#842).
    from .config import RUNTIME_DIR

    return RUNTIME_DIR / "lead-enforcement" / f"{session}.json"


def record_enforcement(path: str | Path, mode: str) -> None:
    """#842: which OS-level boundary this Lead session actually runs under."""
    state = read_state(path)
    _write(_enforcement_path(state["session"]), {"mode": mode, "ts": time.time()})


def enforcement_status(state: dict, facts: dict) -> tuple[str, str]:
    """``(gate_label, gap_reason)`` for this session under the CURRENT policy."""
    from .lead_enforcement import enforcement_gap, gate_label

    restricted = not bool((facts.get("team") or {}).get("lead_may_implement"))
    provider = str(state.get("provider") or "")
    try:
        mode = json.loads(_enforcement_path(state["session"]).read_text(encoding="utf-8"))["mode"]
    except (OSError, ValueError, KeyError, TypeError):
        mode = None
    return gate_label(provider, mode, restricted), enforcement_gap(provider, mode, restricted)


def reason(path: str | Path, *, project: str | None = None) -> str:
    try:
        state = read_state(path)
        if project is not None and state["project"] != project:
            return "Lead context belongs to a different project"
        revision, _documents, facts = _sources(state["project"], state["cwd"])
        # #842: a restricted policy the session cannot honour is never "ready".
        _gate, gap = enforcement_status(state, facts)
        if gap:
            return gap
        if revision != state["revision"]:
            return "Lead project/policy/team settings changed; run takkub context read"
        if state.get("read_epoch") != state["epoch"]:
            return "Lead context has not been read for this session; run takkub context read"
        return ""
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"Lead context unavailable: {exc}; bootstrap/diagnostics remain available"


def current_reason() -> str:
    path = os.environ.get(ENV_FILE)
    return reason(path, project=os.environ.get("TAKKUB_PROJECT")) if path else ""


def refresh(path: str | Path, *, full: bool = False) -> tuple[dict, str]:
    state = read_state(path)
    revision, documents, facts = _sources(state["project"], state["cwd"])
    if revision != state["revision"]:
        state.update(
            revision=revision,
            text=_context(state["project"], state["cwd"], documents, facts),
        )
        state["epoch"] += 1
        state["read_epoch"] = None
        _write(Path(path), state)
    if not full and state.get("embedded_revision") == revision:
        return state, _compact_context(facts, revision)
    return state, state["text"]


def acknowledge(path: str | Path, state: dict) -> None:
    latest = read_state(path)
    revision, _documents, _facts = _sources(latest["project"], latest["cwd"])
    if (
        any(latest.get(key) != state.get(key) for key in ("session", "epoch", "revision"))
        or revision != state["revision"]
    ):
        raise ValueError("Lead context changed while being read; read again")
    latest.update(read_epoch=state["epoch"], read_ts=time.time())
    _write(Path(path), latest)


def invalidate(path: str | Path, event: str) -> None:
    state = read_state(path)
    state["epoch"] += 1
    state.update(read_epoch=None, invalidated_by=event)
    _write(Path(path), state)


def bootstrap_allowed(tool_name: str, tool_input: dict) -> bool:
    if tool_name in {"Read", "Glob", "Grep"}:
        return True
    if tool_name in {"Bash", "PowerShell"}:
        command = str(tool_input.get("command", "")).strip()
        # No shell separators/substitution: bootstrap cannot disguise a second
        # substantive action after an allowed read/status command. A trailing
        # stderr merge is harmless (#854); pipes stay out because a `| tail`
        # would acknowledge a read the Lead never actually saw.
        return bool(
            re.fullmatch(
                r"takkub (?:context (?:read(?: --full)?|status)|team status|doctor(?: --[\w-]+)*)"
                r"(?: 2>&1)?",
                command,
            )
        )
    return False
