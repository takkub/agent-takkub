"""`takkub spawn-service` — start a long-lived process that OUTLIVES its pane (#429).

Every pane's process tree is owned by the cockpit: `takkub done` / `close`
tear the whole tree down (Windows Job Object kill-on-close, `taskkill /T`,
psutil `children(recursive=True)`). That is right for a stray `vite build`
and wrong for a service the pane started on purpose — a devops pane's
`node scripts/autostart.js` spawned 8 `cloudflared` tunnels with Node's
`detached: true` + `unref()`, and every one of them died the moment the pane
reported done (production tunnel down, restored by hand). Node's detach is
only a POSIX session trick; it never leaves the Windows job.

The fix is to not spawn the service from the pane at all: the CLI asks the
cockpit process (via cli_server) to start it. The child is then a child of
the cockpit — outside every pane's job/tree — started with the platform's
strongest detach flags so it also survives a cockpit restart:

* Windows: ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
  CREATE_BREAKAWAY_FROM_JOB`` (breakaway falls back silently when the
  cockpit's own job forbids it).
* POSIX: ``start_new_session=True`` (own session + process group, no
  controlling terminal).

Every service is recorded in ``RUNTIME_DIR/services/<project>/registry.json``
(pid + create_time, name, cmd, log path) so `app.py`'s single-instance
old-process kill and the pane teardown paths can skip these PIDs, and so a
human can find the log. stdout/stderr go to
``RUNTIME_DIR/services/<project>/<name>.log``.

A row names its process by ``(pid, create_time)``, never by the bare PID:
the registry outlives cockpit restarts and reboots by design, and the OS
recycles a dead service's PID (every reboot for sure, within minutes on
Windows under pane churn). `stop()` kills a whole tree, so it must never act
on a stranger wearing that PID — same guard as `remote/tunnel.py`'s
``owner_create_time``.

Pure leaf (stdlib + lazily imported psutil) — imported by `cli_server`
(spawn) and `app` (pid exclusion).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_ARGV = 64


class ServiceSpawnError(ValueError):
    pass


@dataclass(frozen=True)
class ServiceRecord:
    name: str
    pid: int
    cmd: list[str]
    cwd: str
    log_path: str
    started_ts: float
    # psutil create_time of the spawned process — the half of its identity a
    # bare PID lacks; 0.0 when it exited before spawn() could read it.
    create_time: float
    by_role: str
    project: str


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ServiceSpawnError(
            f"invalid service name {name!r} — letters/digits/._- only (e.g. cloudflared, api-dev)"
        )
    return name


def _project_dir(runtime_dir: Path, project: str | None) -> Path:
    ns = re.sub(r"[^A-Za-z0-9._-]", "_", (project or "default").strip() or "default")
    return runtime_dir / "services" / ns


def registry_path(runtime_dir: Path, project: str | None) -> Path:
    return _project_dir(runtime_dir, project) / "registry.json"


def _load_registry(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _save_registry(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# Same process ⇒ identical create_time from the same kernel source on every
# call; the slack only absorbs float/JSON rounding, never a real successor.
_CREATE_TIME_TOLERANCE = 1.0


def _live_process(rec: dict):
    """The `psutil.Process` a registry row still names, or None when the PID
    is gone or now belongs to an unrelated process (reuse after the service
    died / a reboot)."""
    try:
        pid = int(rec.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    import psutil

    try:
        proc = psutil.Process(pid)
        born = float(proc.create_time())
    except psutil.Error:
        # NoSuchProcess/ZombieProcess = gone. AccessDenied = not something the
        # cockpit spawned (its own children are always inspectable) — never a
        # kill candidate either way.
        return None
    recorded = rec.get("create_time")
    if isinstance(recorded, (int, float)) and recorded > 0:
        return proc if abs(born - float(recorded)) < _CREATE_TIME_TOLERANCE else None
    # Row written before `create_time` was recorded (≤ 2.1.32), or the service
    # exited before spawn() could read it: it was alive at `started_ts`, so a
    # process born after that instant can only be wearing a recycled PID.
    started = rec.get("started_ts")
    if isinstance(started, (int, float)) and started > 0:
        return proc if born <= float(started) + _CREATE_TIME_TOLERANCE else None
    return None


def registered_pids(runtime_dir: Path) -> set[int]:
    """Every live service PID across all projects — for teardown paths to
    skip. Best-effort: unreadable registries contribute nothing, and a row
    whose PID was recycled is not a shield for the stranger holding it."""
    root = runtime_dir / "services"
    out: set[int] = set()
    if not root.is_dir():
        return out
    for reg in root.glob("*/registry.json"):
        for rec in _load_registry(reg):
            if _live_process(rec) is not None:
                out.add(int(rec["pid"]))
    return out


def list_services(runtime_dir: Path, project: str | None) -> list[dict]:
    """Registry rows with a live `alive` flag; dead rows are pruned on read."""
    path = registry_path(runtime_dir, project)
    records = _load_registry(path)
    live: list[dict] = []
    changed = False
    for rec in records:
        if _live_process(rec) is not None:
            live.append({**rec, "alive": True})
        else:
            changed = True
    if changed:
        _save_registry(path, [{k: v for k, v in r.items() if k != "alive"} for r in live])
    return live


def _creation_kwargs() -> list[dict]:
    """Strongest-first list of Popen kwargs to try for the detach."""
    if sys.platform == "win32":
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        )
        no_breakaway = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        return [{"creationflags": flags}, {"creationflags": no_breakaway}]
    return [{"start_new_session": True}]


def spawn(
    runtime_dir: Path,
    project: str | None,
    name: str,
    cmd: list[str],
    *,
    cwd: str | None,
    by_role: str,
    env: dict[str, str] | None = None,
) -> ServiceRecord:
    name = validate_name(name)
    if not cmd or not all(isinstance(c, str) for c in cmd):
        raise ServiceSpawnError(
            "missing command — usage: takkub spawn-service --name X -- <cmd...>"
        )
    if len(cmd) > _MAX_ARGV:
        raise ServiceSpawnError(f"too many argv items ({len(cmd)} > {_MAX_ARGV})")
    workdir = Path(cwd) if cwd else Path.cwd()
    if not workdir.is_dir():
        raise ServiceSpawnError(f"cwd does not exist: {workdir}")
    pdir = _project_dir(runtime_dir, project)
    pdir.mkdir(parents=True, exist_ok=True)
    log_path = pdir / f"{name}.log"
    child_env = dict(env if env is not None else os.environ)
    # The service is nobody's pane: strip the pane identity so a `takkub`
    # call from inside it never impersonates the role that started it.
    for key in ("TAKKUB_ROLE", "TAKKUB_PANE_TOKEN", "TAKKUB_LEAD_TOKEN"):
        child_env.pop(key, None)
    child_env["TAKKUB_SERVICE"] = name
    last_exc: Exception | None = None
    proc = None
    with log_path.open("ab") as log_fh:
        log_fh.write(
            f"\n=== takkub spawn-service {name} by {by_role} @ {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"cwd={workdir} cmd={cmd!r}\n".encode()
        )
        log_fh.flush()
        for kwargs in _creation_kwargs():
            try:
                # DETACHED_PROCESS on Windows ⇒ no console window at all.
                # subprocess-console-ok: creationflags come from `_creation_kwargs`
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(workdir),
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    **kwargs,
                )
                break
            except (OSError, ValueError) as exc:
                last_exc = exc
                continue
    if proc is None:
        raise ServiceSpawnError(f"could not start {cmd[0]!r}: {last_exc}")
    try:
        import psutil

        create_time = float(psutil.Process(proc.pid).create_time())
    except Exception:
        create_time = 0.0  # e.g. `cmd /c exit` already gone — `started_ts` still rules out reuse
    record = ServiceRecord(
        name=name,
        pid=int(proc.pid),
        cmd=list(cmd),
        cwd=str(workdir),
        log_path=str(log_path),
        started_ts=time.time(),
        create_time=create_time,
        by_role=by_role,
        project=(project or "default"),
    )
    path = registry_path(runtime_dir, project)
    records = [r for r in _load_registry(path) if _live_process(r) is not None]
    records.append(asdict(record))
    _save_registry(path, records)
    return record


def stop(runtime_dir: Path, project: str | None, name: str) -> tuple[bool, str]:
    """Terminate a registered service by name (the only sanctioned way to
    kill it — it lives outside every pane's tree on purpose)."""
    path = registry_path(runtime_dir, project)
    records = _load_registry(path)
    target = next((r for r in records if r.get("name") == name), None)
    if target is None:
        return False, f"no registered service named {name!r}"
    pid = int(target.get("pid") or 0)
    killed = False
    p = _live_process(target)
    if p is not None:
        import psutil

        try:
            for child in p.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            # psutil re-checks (pid, create_time) inside kill(): a reuse that
            # lands between our check and here raises NoSuchProcess, never kills.
            p.kill()
            killed = True
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:
            return False, f"could not kill pid {pid}: {exc}"
    _save_registry(path, [r for r in records if r.get("name") != name])
    return True, f"stopped service {name!r} (pid {pid})" if killed else (
        f"service {name!r} (pid {pid}) was already gone — registry entry removed"
    )
