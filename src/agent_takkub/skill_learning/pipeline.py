"""Glue: one done → count, record usage, lifecycle, maybe reflect, promote.

`on_done` is called on a `bg_pool` worker by the orchestrator and never
raises. A module lock serialises runs inside the cockpit process so two
panes finishing at once can't race the same counters or skill folder.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import episode as episode_mod
from . import lifecycle, promoter, reflector, store
from .settings import LearningSettings, load

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_KEEP_RUNS = 200
# Dedicated single worker, NOT bg_pool: a reflection can take minutes and
# bg_pool's 4 shared workers serve UI probes (#658). One worker = runs are
# naturally serialised and a burst of dones just queues.
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def submit(
    event: episode_mod.DoneEvent, *, notify: Notify | None = None, force: bool = False
) -> Future:
    """Queue `on_done` on the learning worker — cheap enough for the Qt thread."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skill-learning")
    return _executor.submit(on_done, event, notify=notify, force=force)


@dataclass(slots=True)
class RunRecord:
    run_id: str
    project_ns: str
    role: str
    kind: str  # "reflect" | "curate"
    mode: str
    started_at: float
    status: str = "skipped"  # skipped | landed | proposed | empty | failed
    skip_reason: str = ""
    provider: str = ""
    worth: int = 0
    reasons: list[str] = field(default_factory=list)
    landed: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    retired: list[dict] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    error: str = ""
    summary: str = ""


Notify = Callable[[str, str], None]  # (project_ns, one-line summary)


def _run_id(kind: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{int(time.time() * 1000) % 1000:03d}"


def _runs_dir(project_ns: str) -> Path:
    return store.state_dir(project_ns) / "runs"


def _reflections_today(project_ns: str) -> tuple[int, float]:
    st = store.read_json(store.state_dir(project_ns) / "cadence.json", {})
    today = time.strftime("%Y-%m-%d")
    count = int(st.get("count") or 0) if st.get("day") == today else 0
    return count, float(st.get("last") or 0)


def _stamp_reflection(project_ns: str) -> None:
    count, _ = _reflections_today(project_ns)
    store.write_json(
        store.state_dir(project_ns) / "cadence.json",
        {"day": time.strftime("%Y-%m-%d"), "count": count + 1, "last": time.time()},
    )


def _gate(ep: episode_mod.Episode, s: LearningSettings, *, force: bool) -> str:
    """'' = reflect, otherwise why not."""
    if s.mode == "off":
        return "mode off"
    if force:
        return ""
    if ep.worth < s.min_worth:
        return f"worth {ep.worth} < {s.min_worth}"
    if ep.source == "none":
        return "no transcript"
    count, last = _reflections_today(ep.event.project_ns)
    if count >= s.daily_cap:
        return f"daily cap {s.daily_cap} reached"
    if last and time.time() - last < s.min_interval_s:
        return f"cooldown ({int(s.min_interval_s - (time.time() - last))}s left)"
    return ""


def _save(rec: RunRecord) -> None:
    d = _runs_dir(rec.project_ns) / rec.run_id
    store.write_json(d / "result.json", asdict(rec))
    store.write_json(
        store.state_dir(rec.project_ns) / "last_run.json",
        {"run_id": rec.run_id, "status": rec.status, "summary": rec.summary, "ts": time.time()},
    )
    runs = sorted(p for p in _runs_dir(rec.project_ns).iterdir() if p.is_dir())
    for old in runs[: max(0, len(runs) - _KEEP_RUNS)]:
        shutil.rmtree(old, ignore_errors=True)  # run scratch only — skills live elsewhere


def _stage(rec: RunRecord, intents: list[dict], ep: episode_mod.Episode) -> None:
    store.write_json(
        store.state_dir(rec.project_ns) / "pending" / f"{rec.run_id}.json",
        {"run_id": rec.run_id, "intents": intents, "corpus": ep.corpus(), "role": rec.role},
    )


def _bump_changes(project_ns: str, n: int) -> int:
    p = store.state_dir(project_ns) / "counters.json"
    data = store.read_json(p, {})
    data["changes_since_curate"] = int(data.get("changes_since_curate") or 0) + n
    store.write_json(p, data)
    return data["changes_since_curate"]


def on_done(
    event: episode_mod.DoneEvent,
    *,
    notify: Notify | None = None,
    runner: reflector.Runner | None = None,
    force: bool = False,
    settings: LearningSettings | None = None,
) -> RunRecord | None:
    try:
        with _lock:
            rec = _on_done(event, notify, runner, force, settings or load())
        # An explicit `reflect` request always answers — even "nothing to learn".
        if force and notify and rec.status not in ("landed", "proposed"):
            notify(event.project_ns, f"🧠 Skill Learning ({event.role}): {rec.summary}")
        return rec
    except Exception:
        _log.exception("skill learning failed for %s/%s", event.project_ns, event.role)
        return None


def _on_done(event, notify, runner, force, s: LearningSettings) -> RunRecord:
    ns = event.project_ns
    if not force:
        store.tick_requests(ns)
    ep = episode_mod.build(event)
    rec = RunRecord(
        run_id=_run_id("reflect"),
        project_ns=ns,
        role=event.role,
        kind="reflect",
        mode=s.mode,
        started_at=time.time(),
        worth=ep.worth,
        reasons=ep.reasons,
    )
    names = {r.name for r in store.list_skills(ns)}
    used, viewed = lifecycle.detect_usage(names, event.note, ep.transcript)
    lifecycle.record_usage(ns, used, viewed)
    rec.used = sorted(used)
    rec.retired = [asdict(r) for r in lifecycle.review(ns, s)]

    why_not = _gate(ep, s, force=force)
    if why_not:
        rec.skip_reason = why_not
        rec.summary = f"skip: {why_not}"
        _save(rec)  # skips stay on disk for `takkub skills learned runs`
        return rec

    _stamp_reflection(ns)
    provider, intents, raw = reflector.run(
        reflector.build_bundle(ep),
        _runs_dir(ns) / rec.run_id,
        s,
        prefer=event.provider,
        runner=runner,
        project_ns=ns,
    )
    rec.provider = provider or ""
    if intents is None:
        rec.status, rec.error = "failed", (raw or "")[-500:]
        rec.summary = f"reflector failed ({provider or 'no provider'})"
        _save(rec)
        return rec
    intents = [i for i in intents if str(i.get("op") or "") != "none"]
    if not intents:
        rec.status, rec.summary = "empty", "ไม่มีบทเรียนใหม่"
        _save(rec)
        return rec
    if s.mode == "propose":
        _stage(rec, intents, ep)
        rec.status = "proposed"
        rec.summary = f"เสนอ {len(intents)} รายการ — `takkub skills learned approve {rec.run_id}`"
        _save(rec)
        if notify:
            notify(
                ns,
                f"🧠 Skill Learning เสนอ {len(intents)} รายการจากงาน {event.role} — "
                f"`takkub skills learned show-run {rec.run_id}`",
            )
        return rec
    report = promoter.apply(intents, ep, ns, max_intents=s.max_intents, run_id=rec.run_id)
    _finish(rec, report, notify, event.role)
    if report.landed and _bump_changes(ns, len(report.landed)) >= s.curate_every:
        curate(ns, notify=notify, runner=runner, settings=s)
    return rec


def _finish(
    rec: RunRecord, report: promoter.PromoteReport, notify: Notify | None, role: str
) -> None:
    rec.landed = [asdict(o) for o in report.landed]
    rec.rejected = [asdict(o) for o in report.rejected]
    rec.status = "landed" if report.landed else "empty"
    rec.summary = report.summary()
    _save(rec)
    if notify and report.landed:
        notify(
            rec.project_ns,
            f"🧠 Skill Learning ({role}): {report.summary()} — `takkub skills learned list`",
        )


def curate(
    project_ns: str,
    *,
    notify: Notify | None = None,
    runner: reflector.Runner | None = None,
    settings: LearningSettings | None = None,
) -> RunRecord:
    """Whole-library consolidation. Snapshots both skill trees first — a merge
    is the one change a single atomic rename can't undo."""
    s = settings or load()
    rec = RunRecord(_run_id("curate"), project_ns, "curator", "curate", s.mode, time.time())
    if len(store.list_skills(project_ns)) < 2:
        rec.skip_reason = rec.summary = "skip: fewer than 2 learned skills"
        _save(rec)
        return rec
    _snapshot(project_ns, rec.run_id)
    corpus = "\n".join(f"{r.description}\n{r.body}" for r in store.list_skills(project_ns))
    provider, intents, raw = reflector.run(
        reflector.build_curator_bundle(project_ns),
        _runs_dir(project_ns) / rec.run_id,
        s,
        runner=runner,
        project_ns=project_ns,
    )
    rec.provider = provider or ""
    if intents is None:
        rec.status, rec.error, rec.summary = "failed", (raw or "")[-500:], "curator failed"
        _save(rec)
        return rec
    report = promoter.apply(
        intents,
        None,
        project_ns,
        max_intents=max(s.max_intents, 6),
        corpus=corpus,
        run_id=rec.run_id,
        curator=True,
    )
    p = store.state_dir(project_ns) / "counters.json"
    data = store.read_json(p, {})
    data["changes_since_curate"] = 0
    store.write_json(p, data)
    _finish(rec, report, notify, "curator")
    return rec


def _snapshot(project_ns: str, run_id: str, keep: int = 5) -> None:
    import tarfile

    snap = store.state_dir(project_ns) / "snapshots"
    snap.mkdir(parents=True, exist_ok=True)
    for layer in store.LAYERS:
        root = store.skills_root(layer, project_ns)
        if not root.is_dir():
            continue
        with tarfile.open(snap / f"{run_id}-{layer}.tar.gz", "w:gz") as tar:
            for d in root.iterdir():
                if store.is_managed(d):
                    tar.add(d, arcname=d.name)
        olds = sorted(snap.glob(f"*-{layer}.tar.gz"), key=lambda p: p.stat().st_mtime)
        for old in olds[: max(0, len(olds) - keep)]:
            old.unlink(missing_ok=True)


def approve(project_ns: str, run_id: str, *, settings: LearningSettings | None = None) -> RunRecord:
    """Land a staged proposal (propose mode) through the same validation."""
    s = settings or load()
    p = store.state_dir(project_ns) / "pending" / f"{run_id}.json"
    staged = store.read_json(p, {})
    if not staged:
        raise FileNotFoundError(f"no pending proposal {run_id}")
    rec = RunRecord(
        run_id, project_ns, str(staged.get("role") or ""), "reflect", s.mode, time.time()
    )
    with _lock:
        report = promoter.apply(
            list(staged.get("intents") or []),
            None,
            project_ns,
            max_intents=s.max_intents,
            corpus=str(staged.get("corpus") or ""),
            run_id=run_id,
        )
        _finish(rec, report, None, rec.role)
    p.unlink(missing_ok=True)
    return rec


def reject(project_ns: str, run_id: str) -> None:
    p = store.state_dir(project_ns) / "pending" / f"{run_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"no pending proposal {run_id}")
    p.unlink()


def pending(project_ns: str) -> list[dict]:
    d = store.state_dir(project_ns) / "pending"
    if not d.is_dir():
        return []
    return [store.read_json(f, {}) for f in sorted(d.glob("*.json"))]


def recent_runs(project_ns: str, limit: int = 20) -> list[dict]:
    d = _runs_dir(project_ns)
    if not d.is_dir():
        return []
    out = []
    for sub in sorted(d.iterdir(), reverse=True)[:limit]:
        r = store.read_json(sub / "result.json", {})
        if r:
            out.append(r)
    return out
