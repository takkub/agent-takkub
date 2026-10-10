"""Structured impact plan and completion evidence for cross-flow changes (#833)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

FLOWS = (
    "ui",
    "api",
    "state",
    "worker",
    "qa",
    "export",
    "resume",
    "retry",
    "regenerate",
    "continuation",
    "cost",
    "cache",
    "other_modes",
)
_RISK = re.compile(
    r"audio_mode|seedance_only|shared[ -]state|identity|account[ -]switch|"
    r"switch[ -]account|change[ -]mode|mode[ -]change|"
    r"สลับบัญชี|เปลี่ยนบัญชี|เปลี่ยนโหมด|สลับโหมด|โหมดเสียง|ภาพตัวละคร",
    re.I,
)


def needs_plan(task: str, scope: str = "normal") -> bool:
    """Flag cross-flow tasks; a caller can also opt in with a plan block."""
    return bool(_RISK.search(task or "")) or "[impact-required]" in (task or "").lower()


def _block(text: str, kind: str) -> tuple[dict | None, str]:
    matches = re.findall(rf"```takkub-{kind}\s*\n(.*?)\n```", text or "", re.I | re.S)
    if not matches:
        return None, ""
    if len(matches) != 1:
        return None, f"expected one takkub-{kind} block"
    try:
        value = json.loads(matches[0])
    except ValueError as exc:
        return None, f"invalid takkub-{kind} JSON: {exc}"
    if not isinstance(value, dict):
        return None, f"takkub-{kind} must be a JSON object"
    return value, ""


def append_block(text: str, kind: str, value: dict) -> str:
    return f"{text.rstrip()}\n\n```takkub-{kind}\n{json.dumps(value, ensure_ascii=False)}\n```"


def plan_from_task(task: str, scope: str = "normal") -> tuple[dict | None, str]:
    plan, error = _block(task, "impact-plan")
    if error:
        return None, error
    if plan is None:
        if needs_plan(task, scope):
            return (
                None,
                "impact plan required: pass --impact-plan-file with trigger, before/after, source_of_truth, upstream/downstream and checks for every flow",
            )
        return None, ""
    for key in ("trigger", "before", "after", "source_of_truth"):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            return None, f"impact plan missing {key}"
    for key in ("upstream", "downstream"):
        values = plan.get(key)
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(item, str) and item.strip() for item in values)
        ):
            return None, f"impact plan {key} must list affected inputs/consumers"
    checks = plan.get("checks")
    excluded = plan.get("not_applicable", {})
    if not isinstance(checks, list) or not checks or not isinstance(excluded, dict):
        return None, "impact plan needs checks and not_applicable"
    covered: set[str] = set()
    ids: set[str] = set()
    for check in checks:
        if not isinstance(check, dict):
            return None, "each impact check must be an object"
        for key in ("id", "flow", "expected", "method", "owner"):
            if not isinstance(check.get(key), str) or not check[key].strip():
                return None, f"impact check missing {key}"
        if check["flow"] not in FLOWS or check["id"] in ids:
            return None, f"invalid or duplicate impact check {check['id']}"
        if check.get("required", True) not in (True, False):
            return None, f"impact check {check['id']} requires boolean required"
        ids.add(check["id"])
        covered.add(check["flow"])
    for flow in FLOWS:
        if flow in covered:
            continue
        reason = excluded.get(flow)
        if not isinstance(reason, str) or len(reason.strip()) < 12:
            return None, f"impact flow {flow} needs a check or a specific not_applicable reason"
    if set(excluded) - set(FLOWS):
        return None, "impact plan has unknown not_applicable flows"
    if covered & set(excluded):
        return None, "impact flow cannot be checked and not_applicable"
    return plan, ""


def plan_digest(plan: dict) -> str:
    raw = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def evidence_from_note(note: str) -> tuple[dict | None, str]:
    return _block(note, "impact-evidence")


def validate_evidence(
    plan: dict, evidence: dict | None, *, task_id: str, revision: str | None
) -> str:
    if evidence is None:
        return "impact evidence missing; pass --impact-evidence-file"
    if evidence.get("task_id") != task_id:
        return "impact evidence belongs to a different task"
    if evidence.get("plan_digest") != plan_digest(plan):
        return "impact evidence belongs to a different plan revision"
    if not revision or evidence.get("revision") != revision:
        return "impact evidence revision differs from the current working tree"
    results = evidence.get("checks")
    if not isinstance(results, list):
        return "impact evidence needs checks"
    by_id = {}
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            return "invalid impact evidence check"
        if result["id"] in by_id:
            return f"duplicate impact evidence for {result['id']}"
        by_id[result["id"]] = result
    expected_ids = {check["id"] for check in plan["checks"]}
    if set(by_id) != expected_ids:
        return "impact evidence check IDs do not match the plan"
    for check in plan["checks"]:
        result = by_id[check["id"]]
        status = result.get("status")
        if status != "pass":
            if check.get("required", True):
                return f"required impact check {check['id']} is not passed"
            if status != "pending" or not str(result.get("limitation", "")).strip():
                return f"optional impact check {check['id']} needs pass or pending with limitation"
        if status == "pass" and (
            not str(result.get("evidence", "")).strip()
            or result.get("kind") not in ("fake", "mock", "manual", "live")
        ):
            return f"impact check {check['id']} needs evidence and kind"
    return ""


def git_revision(cwd: str | Path | None) -> str | None:
    """Hash HEAD plus the content of changed and untracked files, including deletions."""
    if not cwd:
        return None
    try:
        root = (
            subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                capture_output=True,
                check=True,
                timeout=5,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
            .stdout.decode("utf-8", errors="replace")
            .strip()
        )
        head = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            timeout=5,
            creationflags=SUBPROCESS_NO_WINDOW,
        ).stdout.strip()
        changed = subprocess.run(
            ["git", "-C", root, "diff", "--name-only", "-z", "HEAD"],
            capture_output=True,
            check=True,
            timeout=5,
            creationflags=SUBPROCESS_NO_WINDOW,
        ).stdout
        untracked = subprocess.run(
            ["git", "-C", root, "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            check=True,
            timeout=5,
            creationflags=SUBPROCESS_NO_WINDOW,
        ).stdout
        digest = hashlib.sha256(head)
        for raw in sorted(set((changed + untracked).split(b"\0")) - {b""}):
            rel = raw.decode("utf-8", errors="surrogateescape")
            path = Path(root) / rel
            digest.update(raw + b"\0")
            if path.is_file():
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                digest.update(b"<deleted>")
        return "git:" + digest.hexdigest()[:20]
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
