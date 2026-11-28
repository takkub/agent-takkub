"""Eval cases: past failures kept as re-runnable deterministic checks.

A case is "this went wrong once; this command's exit code says whether it is
still fixed". `takkub eval run` executes every active case's check in the
CLI process (never the GUI thread, never automatically — a stored command
only runs when someone asks) and compares each result with the previous run,
so a pass→fail flip stands out.

Storage (own domain folder, nothing deleted — retire keeps the case):
``RUNTIME_DIR/eval/<project>/cases.json`` (the cases + each one's last result)
and ``RUNTIME_DIR/eval/<project>/runs.jsonl`` (one line per run, append-only).

Provider-neutral: plain CLI + files, no pane or provider involved.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from . import config
from ._win_console import SUBPROCESS_NO_WINDOW
from .backlog import _atomic_write
from .config import RUNTIME_DIR
from .job_object_manager import JobObjectManager
from .path_safe import safe_segment
from .usage_shared import _file_lock

SCHEMA_VERSION = 1
STATUSES = ("draft", "active", "retired")
DEFAULT_TIMEOUT_S = 300
_TAIL_CHARS = 1500
_LOCK = threading.Lock()


def _dir(project: str) -> Path:
    return RUNTIME_DIR / "eval" / safe_segment(project)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load(project: str) -> dict:
    try:
        raw = json.loads((_dir(project) / "cases.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("cases"), dict):
            return raw
    except (OSError, ValueError):
        pass
    return {"schema_version": SCHEMA_VERSION, "next_seq": 1, "cases": {}}


def _save(project: str, store: dict) -> None:
    _atomic_write(_dir(project) / "cases.json", json.dumps(store, ensure_ascii=False, indent=1))


@contextlib.contextmanager
def _store_lock(project: str) -> Iterator[None]:
    """Read-modify-write guard for cases.json across threads AND processes
    (GUI drafts + CLI add/set/run write the same file). Not taking the lock
    raises instead of writing anyway — a lost update silently drops cases."""
    with _LOCK, _file_lock(_dir(project) / "cases.json") as got:
        if not got:
            raise TimeoutError(f"eval store for {project} is busy (cases.lock) — try again")
        yield


def _project_roots(project: str) -> list[Path]:
    """The project's registered paths (projects.json `paths`) — a check may
    only run with its cwd under one of them."""
    try:
        paths = (config.load_projects().get("projects", {}).get(project) or {}).get("paths") or {}
    except Exception:
        return []
    roots = []
    for p in paths.values() if isinstance(paths, dict) else ():
        if isinstance(p, str) and p and Path(p).expanduser().is_absolute():
            roots.append(Path(p).expanduser().resolve())
    return roots


def cwd_refusal(project: str, cwd: str) -> str:
    """Why *cwd* may not run a check for *project* ("" = allowed). Resolved,
    so a symlink/junction inside the project that points out is refused."""
    if not cwd:
        return "no cwd (draft — set one with `takkub eval set <id> --cwd ..`)"
    roots = _project_roots(project)
    if not roots:
        return f"project {project} has no registered paths in projects.json"
    real = os.path.normcase(str(Path(cwd).resolve()))
    for root in roots:
        r = os.path.normcase(str(root))
        try:
            if os.path.commonpath([r, real]) == r:
                return ""
        except ValueError:  # different drives
            continue
    return f"cwd {cwd} is outside the project's registered paths"


def _new_case(store: dict, **fields) -> dict:
    seq = int(store.get("next_seq") or 1)
    store["next_seq"] = seq + 1
    case = {
        "id": f"ev-{seq:03d}",
        "title": "",
        "source": {},
        "expected": "",
        "check": "",
        "cwd": "",
        "timeout_s": DEFAULT_TIMEOUT_S,
        "created": _now(),
        "status": "active",
        "last_result": None,
        **fields,
    }
    store["cases"][case["id"]] = case
    return case


def add_case(
    project: str,
    *,
    title: str,
    expected: str,
    check: str,
    cwd: str,
    source: dict | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict:
    if not title.strip() or not check.strip():
        raise ValueError("title and check are required")
    if not Path(cwd).is_dir():
        raise ValueError(f"cwd is not a directory: {cwd}")
    why = cwd_refusal(project, cwd)
    if why:
        raise ValueError(why)
    with _store_lock(project):
        store = _load(project)
        case = _new_case(
            store,
            title=title.strip(),
            expected=expected.strip(),
            check=check.strip(),
            cwd=str(Path(cwd).resolve()),
            source={k: v for k, v in (source or {}).items() if v},
            timeout_s=max(1, int(timeout_s)),
        )
        _save(project, store)
    return case


def list_cases(project: str, *, include_retired: bool = False) -> list[dict]:
    cases = list(_load(project)["cases"].values())
    if not include_retired:
        cases = [c for c in cases if c.get("status") != "retired"]
    return sorted(cases, key=lambda c: c["id"])


def get_case(project: str, case_id: str) -> dict | None:
    return _load(project)["cases"].get(case_id)


def update_case(project: str, case_id: str, **fields) -> dict:
    """Fill or change a case (Lead completes a draft with a check here). A
    draft that now has a check becomes active; retired stays retired."""
    if fields.get("check") is not None and not str(fields["check"]).strip():
        raise ValueError("check cannot be empty (an empty command exits 0 = a false pass)")
    if fields.get("cwd"):
        if not Path(fields["cwd"]).is_dir():
            raise ValueError(f"cwd is not a directory: {fields['cwd']}")
        why = cwd_refusal(project, fields["cwd"])
        if why:
            raise ValueError(why)
    with _store_lock(project):
        store = _load(project)
        case = store["cases"].get(case_id)
        if case is None:
            raise KeyError(case_id)
        for k in ("title", "expected", "check"):
            if fields.get(k) is not None:
                case[k] = str(fields[k]).strip()
        if fields.get("cwd"):
            case["cwd"] = str(Path(fields["cwd"]).resolve())
        if fields.get("timeout_s"):
            case["timeout_s"] = max(1, int(fields["timeout_s"]))
        if case["status"] == "draft" and case.get("check") and case.get("cwd"):
            case["status"] = "active"
        _save(project, store)
    return case


def retire_case(project: str, case_id: str) -> dict:
    with _store_lock(project):
        store = _load(project)
        case = store["cases"].get(case_id)
        if case is None:
            raise KeyError(case_id)
        case["status"] = "retired"
        case["retired_at"] = _now()
        _save(project, store)
    return case


def draft_from_fix_loop(
    project: str, identity: str, sequences: list[dict], verifier: str
) -> dict | None:
    """A fix-loop sequence closed clean after ≥1 failed attempt → a draft case
    (no check yet; Lead adds one with `takkub eval set`). One draft per work
    identity — a second close of the same work does not pile up drafts."""
    seqs = [s for s in sequences if s.get("attempts")]
    if not seqs:
        return None
    with _store_lock(project):
        store = _load(project)
        for c in store["cases"].values():
            if c.get("source", {}).get("fix_loop") == identity:
                return None
        attempts = [a for s in seqs for a in s["attempts"]]
        ref = next((s.get("ref") for s in seqs if s.get("ref")), "")
        source = {"fix_loop": identity}
        if identity.startswith("backlog:"):
            source["backlog"] = identity.split(":", 1)[1]
        if ref:
            source["issue"] = ref
        first = attempts[0].get("summary", "") if attempts else ""
        case = _new_case(
            store,
            title=(ref + " " if ref else "") + (first[:100] or identity),
            expected=f"ผ่านการตรวจโดย {verifier} หลังล้ม {len(attempts)} รอบ — เติม expected/check",
            status="draft",
            source=source,
            failures=[a.get("summary", "") for a in attempts][:10],
        )
        _save(project, store)
    return case


def git_revision(cwd: str) -> str:
    try:
        out = subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            stdin=subprocess.DEVNULL,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _kill_tree(proc: subprocess.Popen) -> None:
    """Timed-out check: kill the shell AND what it started, by PID only (#169)."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _decode_output(raw: bytes) -> str:
    """Check output is whatever the shell's programs wrote: UTF-8 on macOS;
    on Windows cmd.exe builtins and most console tools write the OEM code
    page (cp874 on a Thai machine, cp437/850 elsewhere) — asked from the OS,
    never hardcoded. Strict UTF-8 first so UTF-8 tools still read right."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            oem = ctypes.windll.kernel32.GetOEMCP()  # type: ignore[attr-defined]
            return raw.decode(f"cp{oem}", "replace")
        except (OSError, LookupError, AttributeError):
            pass
    return raw.decode("utf-8", "replace")


def run_check(case: dict) -> dict:
    """Run one case's check through the platform's own shell (cmd.exe on
    Windows, /bin/sh on POSIX) with a hard timeout. Output goes to a temp file,
    not a pipe, so a grandchild holding the handle can't hang the read.

    The check's tree never outlives this call: timeout or any exception here
    (Ctrl+C = KeyboardInterrupt) kills it by PID, and on Windows the shell is
    in a kill-on-close Job Object so even a hard-killed `takkub eval run`
    (TerminateProcess from a tool timeout) takes the tree with it."""
    started = time.monotonic()
    timeout = int(case.get("timeout_s") or DEFAULT_TIMEOUT_S)
    timed_out, code, tail = False, None, ""
    if not str(case.get("check") or "").strip():
        return {
            "passed": False,
            "exit": None,
            "timed_out": False,
            "duration_s": 0.0,
            "tail": "empty check — not run (an empty command exits 0)",
        }
    job = JobObjectManager()  # no-op off Windows
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(
                case["check"],
                shell=True,
                cwd=case["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=SUBPROCESS_NO_WINDOW,
                start_new_session=sys.platform != "win32",
            )
            # ponytail: assigned right after start, so a grandchild the shell
            # spawns in its first instant could escape the job; the Ctrl+C /
            # timeout paths still kill the whole tree via taskkill /T.
            job.assign(proc.pid)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
            except BaseException:
                _kill_tree(proc)
                raise
        except OSError as e:
            tail = f"could not start check: {e}"
        finally:
            job.close()
        out.seek(0)
        raw = out.read()
    if not tail:
        tail = _decode_output(raw)[-_TAIL_CHARS:]
    return {
        "passed": code == 0 and not timed_out,
        "exit": code,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - started, 2),
        "tail": tail,
    }


def change_of(prev: dict | None, passed: bool) -> str:
    if not prev:
        return "new"
    if prev.get("passed") == passed:
        return "same"
    return "fixed" if passed else "REGRESSED"


def plan_run(project: str, ids: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """(cases that will run, [{id, reason}] refused). Explicit *ids* that are
    draft/retired are refused with the reason; so is any case whose cwd is
    outside the project's registered paths (cases.json is a plain file a pane
    could write — see docs/architecture/eval-loop.md)."""
    cases = _load(project)["cases"]
    if ids:
        missing = [i for i in ids if i not in cases]
        if missing:
            raise KeyError(", ".join(missing))
        picked = [cases[i] for i in dict.fromkeys(ids)]
    else:
        picked = [c for c in cases.values() if c.get("status") == "active"]
    targets, refused = [], []
    for case in sorted(picked, key=lambda c: c["id"]):
        status = case.get("status")
        if status != "active":
            why = f"status {status}"
            if status == "draft":
                why += " — needs a check and a cwd (takkub eval set <id> --check .. --cwd ..)"
        elif not str(case.get("check") or "").strip():
            why = "empty check"
        else:
            why = cwd_refusal(project, str(case.get("cwd") or ""))
        if why:
            refused.append({"id": case["id"], "reason": why})
        else:
            targets.append(case)
    return targets, refused


def run_cases(
    project: str,
    ids: list[str] | None = None,
    runner=run_check,
    announce: Callable[[list[dict], list[dict]], None] | None = None,
) -> dict:
    """Run active cases (or exactly *ids*), record each result + git revision
    on the case and append the run to runs.jsonl. *announce(targets, refused)*
    is called before any check starts, so the caller can show what will run."""
    targets, refused = plan_run(project, ids)
    if announce is not None:
        announce(targets, refused)
    run_at = _now()
    results = []
    for case in targets:
        res = runner(case)
        res.update(
            id=case["id"],
            rev=git_revision(case["cwd"]),
            change=change_of(case.get("last_result"), res["passed"]),
        )
        results.append(res)
    with _store_lock(project):
        store = _load(project)
        for res in results:
            case = store["cases"].get(res["id"])
            if case is not None:
                case["last_result"] = {
                    "passed": res["passed"],
                    "exit": res["exit"],
                    "timed_out": res["timed_out"],
                    "rev": res["rev"],
                    "at": run_at,
                }
        if results:
            _save(project, store)
            line = json.dumps({"at": run_at, "results": results}, ensure_ascii=False)
            # ponytail: append-only, never trimmed (storage V2 keeps data); a
            # long-lived project may want rotation once this grows past MBs.
            with open(_dir(project) / "runs.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
    return {
        "at": run_at,
        "results": results,
        "refused": refused,
        "titles": {c["id"]: c["title"] for c in targets},
    }
