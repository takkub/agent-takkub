"""The only writer: validate every intent in memory, then land it atomically.

An intent that fails any rule is rejected with a reason (kept in the run
record so a rejection is visible, never silent). A merge whose umbrella is
invalid fails as a whole rather than losing the absorbed skill's content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import store
from .episode import Episode
from .reflector import BODY_MAX_LINES, DESC_INDEX_CUT, DESC_MAX

OPS = ("create", "patch", "merge", "archive")
_EVIDENCE_MIN = 20


@dataclass(slots=True)
class Outcome:
    op: str
    name: str
    ok: bool
    reason: str
    layer: str = ""
    into: str = ""


@dataclass(slots=True)
class PromoteReport:
    landed: list[Outcome] = field(default_factory=list)
    rejected: list[Outcome] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.landed:
            parts.append(", ".join(f"{o.op} {o.name}" for o in self.landed))
        if self.rejected:
            parts.append("rejected: " + ", ".join(o.name or o.op for o in self.rejected))
        return " · ".join(parts) or "nothing to change"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _has_secret(*texts: str) -> bool:
    from ..secret_redact import redact_secrets

    return any(redact_secrets(t or "")[1] for t in texts)


def _trigger_early(description: str) -> bool:
    """The index shows the first DESC_INDEX_CUT chars — the description must
    already say WHEN to use the skill inside that window."""
    head = description[:DESC_INDEX_CUT].lower()
    cues = (
        "use when",
        "when ",
        "before ",
        "after ",
        "if ",
        "ใช้เมื่อ",
        "เมื่อ",
        "ก่อน",
        "หลัง",
        "ถ้า",
        "ตอน",
    )
    return any(c in head for c in cues)


def _body_ok(body: str) -> str | None:
    lines = [ln for ln in (body or "").splitlines() if ln.strip()]
    if not lines:
        return "empty body"
    if len(lines) > BODY_MAX_LINES:
        return f"body has {len(lines)} non-blank lines (max {BODY_MAX_LINES}) — a rule, not a transcript"
    return None


def _desc_ok(desc: str) -> str | None:
    if not desc:
        return "missing description"
    if len(desc) > DESC_MAX:
        return f"description {len(desc)} chars (max {DESC_MAX})"
    if not _trigger_early(desc):
        return f"description must state its trigger within the first {DESC_INDEX_CUT} chars"
    return None


def validate(intent: dict, project_ns: str, corpus: str, *, curator: bool = False) -> str | None:
    """None when the intent may land, otherwise the rejection reason."""
    op = str(intent.get("op") or "")
    name = str(intent.get("name") or "").strip()
    if op not in OPS:
        return f"unknown op {op!r}"
    if not store.NAME_RE.fullmatch(name):
        return f"invalid name {name!r}"
    evidence = str(intent.get("evidence") or "")
    if len(_norm(evidence)) < _EVIDENCE_MIN:
        return "evidence missing or shorter than 20 chars"
    if _norm(evidence) not in _norm(corpus):
        return "evidence is not a verbatim quote of the episode"
    if not str(intent.get("reason") or "").strip():
        return "missing reason"
    desc = str(intent.get("description") or "").strip()
    body = str(intent.get("body") or "")
    if _has_secret(desc, body, evidence):
        return "contains a secret-looking value"
    managed = store.find_managed(project_ns, name)
    if op == "create":
        if curator:
            return "curator may not create skills"
        if str(intent.get("layer") or "project") not in store.LAYERS:
            return "layer must be project or global"
        if store.name_taken(project_ns, name):
            return f"name {name!r} already exists"
        return _desc_ok(desc) or _body_ok(body)
    if managed is None:
        return f"{name!r} is not a skill Skill Learning manages"
    if op == "patch":
        return (_desc_ok(desc) if desc else None) or _body_ok(body)
    if op == "merge":
        into = str(intent.get("into") or "").strip()
        if into == name:
            return "merge into itself"
        if store.find_managed(project_ns, into) is None:
            return f"umbrella {into!r} is not a live managed skill"
        return (_desc_ok(desc) if desc else None) or _body_ok(body)
    return None  # archive


def _requests_for(layer: str, project_ns: str) -> int:
    return store.requests(project_ns if layer == "project" else None)


def apply(
    intents: list[dict],
    episode: Episode | None,
    project_ns: str,
    *,
    max_intents: int,
    corpus: str | None = None,
    run_id: str = "",
    curator: bool = False,
) -> PromoteReport:
    report = PromoteReport()
    corpus = corpus if corpus is not None else (episode.corpus() if episode else "")
    for intent in intents:
        op = str(intent.get("op") or "")
        name = str(intent.get("name") or "").strip()
        if op == "none":
            continue
        if len(report.landed) >= max_intents:
            report.rejected.append(Outcome(op, name, False, f"over the {max_intents}-change cap"))
            continue
        err = validate(intent, project_ns, corpus, curator=curator)
        if err:
            report.rejected.append(Outcome(op, name, False, err))
            continue
        try:
            report.landed.append(_land(intent, episode, project_ns, run_id=run_id))
        except Exception as exc:  # disk full, race with a CLI archive, …
            report.rejected.append(Outcome(op, name, False, f"write failed: {exc}"))
    return report


def _ledger_entry(intent: dict, op: str, run_id: str, evidence_rel: str) -> dict:
    return {
        "action": op,
        "reason": str(intent.get("reason") or "")[:500],
        "evidence": evidence_rel,
        "run": run_id,
    }


def _evidence_text(intent: dict, episode: Episode | None) -> str:
    head = ""
    if episode is not None:
        ev = episode.event
        head = f"project `{ev.project_ns}` · role `{ev.role}` · provider `{ev.provider}`\n\n"
    return (
        f"# evidence\n\n{head}> " + str(intent.get("evidence") or "").replace("\n", "\n> ") + "\n"
    )


def _land(intent: dict, episode: Episode | None, project_ns: str, *, run_id: str) -> Outcome:
    op = str(intent["op"])
    name = str(intent["name"]).strip()
    desc = str(intent.get("description") or "").strip()
    body = str(intent.get("body") or "")
    category = re.sub(r"[^a-z0-9-]", "", str(intent.get("category") or "").lower())[:32]
    if op == "create":
        layer = str(intent.get("layer") or "project")
        rec = store.create_skill(
            layer,
            project_ns,
            name,
            desc,
            body,
            category=category or "general",
            requests_now=_requests_for(layer, project_ns),
        )
        rel = store.write_evidence(rec.dir, _evidence_text(intent, episode))
        store.append_ledger(rec.dir, _ledger_entry(intent, op, run_id, rel))
        return Outcome(op, name, True, "landed", layer=layer)
    rec = store.find_managed(project_ns, name)
    assert rec is not None  # validate() checked
    if op == "patch":
        store.rewrite_skill(rec, description=desc or rec.description, body=body, category=category)
        rel = store.write_evidence(rec.dir, _evidence_text(intent, episode))
        store.append_ledger(rec.dir, _ledger_entry(intent, op, run_id, rel))
        return Outcome(op, name, True, "landed", layer=rec.layer)
    if op == "merge":
        into = str(intent["into"]).strip()
        umbrella = store.find_managed(project_ns, into)
        assert umbrella is not None
        store.rewrite_skill(
            umbrella, description=desc or umbrella.description, body=body, category=category
        )
        # Carry the absorbed skill's usage over — a merge is not a death.
        store.update_sidecar(
            umbrella,
            uses=int(umbrella.sidecar.get("uses") or 0) + int(rec.sidecar.get("uses") or 0),
            views=int(umbrella.sidecar.get("views") or 0) + int(rec.sidecar.get("views") or 0),
        )
        rel = store.write_evidence(umbrella.dir, _evidence_text(intent, episode))
        store.append_ledger(
            umbrella.dir, {**_ledger_entry(intent, "absorb", run_id, rel), "absorbed": name}
        )
        store.archive_skill(rec, reason=str(intent.get("reason") or "merged"), absorbed_by=into)
        return Outcome(op, name, True, "landed", layer=rec.layer, into=into)
    store.archive_skill(rec, reason=str(intent.get("reason") or "archived by reflector"))
    return Outcome(op, name, True, "landed", layer=rec.layer)
